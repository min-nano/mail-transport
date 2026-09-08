"""Gmail API クライアントのテスト.

google-api-python-client が入っていない環境ではスキップされる。
"""

from __future__ import annotations

import pytest

pytest.importorskip("googleapiclient", reason="google-api-python-client が必要です")

from googleapiclient.errors import HttpError  # noqa: E402

from mailtransport.config import GmailAuth  # noqa: E402
from mailtransport.gmail_sink import GMAIL_SCOPES, GmailError, GmailSink  # noqa: E402


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "err"


class FakeRequest:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = 0

    def execute(self, num_retries=0):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeMessages:
    def __init__(self, request) -> None:
        self.request = request
        self.kwargs: dict = {}

    def insert(self, **kwargs):
        self.kwargs = kwargs
        return self.request


class FakeUsers:
    def __init__(self, messages) -> None:
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, request) -> None:
        self.messages_resource = FakeMessages(request)

    def users(self):
        return FakeUsers(self.messages_resource)


AUTH = GmailAuth(client_id="cid", client_secret="cs", refresh_token="rt")


def make_sink(results):
    request = FakeRequest(results)
    service = FakeService(request)
    return GmailSink(AUTH, "me", service=service), service, request


def test_insert_uses_the_date_header_and_given_labels():
    sink, service, _ = make_sink([{"id": "m1", "threadId": "t1"}])

    result = sink.insert(b"Subject: hi\r\n\r\nbody", ["SPAM", "UNREAD"])

    assert (result.message_id, result.thread_id) == ("m1", "t1")
    kwargs = service.messages_resource.kwargs
    assert kwargs["body"] == {"labelIds": ["SPAM", "UNREAD"]}
    # 転送時刻ではなく元メールの日時で Gmail に並ぶようにする
    assert kwargs["internalDateSource"] == "dateHeader"
    assert kwargs["media_body"].mimetype() == "message/rfc822"
    # 小さいメールは multipart で 1 往復にする
    assert kwargs["media_body"].resumable() is False


def test_large_messages_use_a_resumable_upload():
    sink, service, _ = make_sink([{"id": "m1", "threadId": None}])

    sink.insert(b"x" * (6 * 1024 * 1024), ["INBOX"])

    assert service.messages_resource.kwargs["media_body"].resumable() is True


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sink, _, request = make_sink(
        [
            HttpError(FakeResponse(429), b"rate limited"),
            HttpError(FakeResponse(503), b"unavailable"),
            {"id": "m1", "threadId": None},
        ]
    )

    assert sink.insert(b"raw", ["INBOX"]).message_id == "m1"
    assert request.calls == 3


def test_permanent_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sink, _, request = make_sink([HttpError(FakeResponse(400), b"bad request")])

    with pytest.raises(GmailError, match="400"):
        sink.insert(b"raw", ["INBOX"])
    assert request.calls == 1


def test_retries_give_up_eventually(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    sink, _, request = make_sink([HttpError(FakeResponse(500), b"boom")] * 5)

    with pytest.raises(GmailError):
        sink.insert(b"raw", ["INBOX"])
    assert request.calls == 5


def test_scope_is_limited_to_insert():
    """閲覧・変更・送信の権限を持たない最小スコープであることを保証する."""
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.insert"]
