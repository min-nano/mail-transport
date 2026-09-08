"""Gmail API へのメッセージ挿入.

``users.messages.insert`` を使う点が肝心:

- SMTP を経由しないので DMARC / SPF / DKIM の再検証が発生しない
  (転送によって認証が壊れ、受信拒否や消失が起きる問題を根本的に回避する)
- ``import`` と違いスパム分類器を通らず、指定したラベルがそのまま適用される
  ので「受信トレイ → 受信トレイ」「迷惑メール → 迷惑メール」を確定できる
- 送信ではないため送信数の制限やループの心配がない
"""

from __future__ import annotations

import io
import logging
import random
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# messages.insert のみを許可する最小権限スコープ。
# 既読/未読の変更や削除、閲覧の権限は含まれない。
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.insert"]

_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
_MAX_ATTEMPTS = 5


class GmailError(RuntimeError):
    pass


@dataclass(frozen=True)
class InsertResult:
    message_id: str
    thread_id: str | None


def build_credentials(auth) -> object:
    """リフレッシュトークンから短命なアクセストークンを取得する認証情報を作る."""
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=auth.refresh_token,
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        token_uri=auth.token_uri,
        scopes=GMAIL_SCOPES,
    )


class GmailSink:
    def __init__(self, auth, user_id: str = "me", service=None) -> None:
        self._user_id = user_id
        if service is None:
            from googleapiclient.discovery import build

            service = build(
                "gmail",
                "v1",
                credentials=build_credentials(auth),
                cache_discovery=False,
            )
        self._service = service

    def insert(self, raw: bytes, label_ids: list[str]) -> InsertResult:
        """生の RFC822 メッセージを指定ラベル付きで Gmail に挿入する."""
        from googleapiclient.http import MediaIoBaseUpload

        body: dict = {"labelIds": list(label_ids)}
        media = MediaIoBaseUpload(
            io.BytesIO(raw),
            mimetype="message/rfc822",
            chunksize=-1,
            # 小さいメールは 1 往復で済む multipart、大きいメールは途中で切れても
            # 復帰できる resumable を使う (Gmail の上限は 50MB)。
            resumable=len(raw) > _RESUMABLE_THRESHOLD_BYTES,
        )
        request = (
            self._service.users()
            .messages()
            .insert(
                userId=self._user_id,
                body=body,
                # 元メールの Date ヘッダを Gmail 上の日時として採用し、
                # 転送時刻ではなく本来の受信順に並ぶようにする
                internalDateSource="dateHeader",
                media_body=media,
            )
        )
        response = self._execute_with_retry(request)
        return InsertResult(
            message_id=str(response.get("id")),
            thread_id=response.get("threadId"),
        )

    def _execute_with_retry(self, request):
        from googleapiclient.errors import HttpError

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                    raise GmailError(
                        f"Gmail API の呼び出しに失敗しました (status={status}): {exc}"
                    ) from exc
                last_exc = exc
            except (TimeoutError, ConnectionError, OSError) as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise GmailError(f"Gmail API への接続に失敗しました: {exc}") from exc
                last_exc = exc
            delay = min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
            log.warning(
                "Gmail API を再試行します (%s/%s, %.1fs 待機): %s",
                attempt,
                _MAX_ATTEMPTS,
                delay,
                last_exc,
            )
            time.sleep(delay)
        raise GmailError(f"Gmail API の再試行に失敗しました: {last_exc}")  # pragma: no cover
