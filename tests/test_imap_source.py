from __future__ import annotations

import datetime as dt

from mailtransport.imap_source import (
    ImapSource,
    Mailbox,
    _parse_internaldate,
    date_header_of,
    message_id_of,
    quote_mailbox,
)


class FakeConn:
    """imaplib.IMAP4_SSL の応答を模したテスト用コネクション."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[tuple] = []

    def list(self):
        return self.responses["LIST"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        return self.responses[command.upper()]


def source_with(conn) -> ImapSource:
    source = ImapSource("h", 993, "u", "p")
    source._conn = conn
    return source


def test_quote_mailbox_escapes_special_characters():
    assert quote_mailbox("Junk") == '"Junk"'
    assert quote_mailbox('od"d\\name') == '"od\\"d\\\\name"'


def test_list_mailboxes_parses_flags_and_names():
    conn = FakeConn(
        {
            "LIST": (
                "OK",
                [
                    rb'(\HasNoChildren) "/" "INBOX"',
                    rb'(\HasNoChildren \Junk) "/" "Junk"',
                    rb'(\HasNoChildren \Trash) "/" "Deleted Messages"',
                ],
            )
        }
    )

    mailboxes = source_with(conn).list_mailboxes()

    assert [m.name for m in mailboxes] == ["INBOX", "Junk", "Deleted Messages"]
    assert mailboxes[1].has_flag(r"\Junk") is True
    assert mailboxes[0].has_flag(r"\Junk") is False


def test_resolve_mailbox_finds_junk_by_special_use_flag():
    """迷惑メールの名前はロケール依存なので、フラグで探せることを確認する."""
    conn = FakeConn(
        {"LIST": ("OK", [rb'(\HasNoChildren) "/" "INBOX"', '(\\Junk) "/" "迷惑メール"'.encode()])}
    )
    source = source_with(conn)

    assert source.resolve_mailbox(r"\Junk") == "迷惑メール"
    assert source.resolve_mailbox("INBOX") == "INBOX"
    assert source.resolve_mailbox(r"\Archive") is None


def test_search_uids_after_drops_the_sentinel_result():
    """IMAP の "n:*" は該当なしでも最大 UID を返すため、必ず絞り込む."""
    conn = FakeConn({"SEARCH": ("OK", [b"42"])})
    source = source_with(conn)

    assert source.search_uids_after(42) == []
    assert source.search_uids_after(41) == [42]


def test_search_uids_are_sorted_and_deduplicated():
    conn = FakeConn({"SEARCH": ("OK", [b"12 10 11", b" 11"])})

    assert source_with(conn).search_uids_after(9) == [10, 11, 12]


def test_fetch_metadata_parses_size_and_flags():
    conn = FakeConn(
        {
            "FETCH": (
                "OK",
                [
                    rb"1 (UID 10 RFC822.SIZE 2048 FLAGS (\Seen)"
                    rb' INTERNALDATE "01-May-2024 12:00:00 +0900")',
                    rb"2 (UID 11 RFC822.SIZE 99 FLAGS ())",
                ],
            )
        }
    )

    metas = source_with(conn).fetch_metadata([10, 11])

    assert [m.uid for m in metas] == [10, 11]
    assert metas[0].size == 2048 and metas[0].seen is True
    assert metas[0].internaldate == dt.datetime(
        2024, 5, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))
    )
    assert metas[1].seen is False


def test_fetch_raw_returns_the_literal_body():
    body = b"Subject: hi\r\n\r\nbody"
    conn = FakeConn({"FETCH": ("OK", [(b"1 (BODY[] {19}", body), b")"])})
    source = source_with(conn)

    assert source.fetch_raw(10) == body
    # 既読フラグを立てないよう BODY.PEEK を使っていること
    assert "BODY.PEEK[]" in conn.calls[0][1][1]


def test_fetch_raw_returns_none_when_message_vanished():
    conn = FakeConn({"FETCH": ("OK", [None])})

    assert source_with(conn).fetch_raw(10) is None


def test_message_id_and_date_headers_are_extracted():
    raw = b"Message-ID: <abc@example.com>\r\nDate: Wed, 01 May 2024 12:00:00 +0900\r\n\r\nhi"

    assert message_id_of(raw) == "<abc@example.com>"
    assert date_header_of(raw).utcoffset() == dt.timedelta(hours=9)
    assert message_id_of(b"Subject: none\r\n\r\nhi") is None


def test_internaldate_parser_rejects_garbage():
    assert _parse_internaldate(b"not a date") is None


def test_mailbox_flag_comparison_is_case_insensitive():
    assert Mailbox("Junk", (r"\junk",)).has_flag(r"\Junk") is True
