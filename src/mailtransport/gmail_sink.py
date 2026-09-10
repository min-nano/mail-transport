"""Gmail API へのメッセージ挿入.

``users.messages.insert`` を使う点が肝心:

- SMTP を経由しないので DMARC / SPF / DKIM の再検証が発生しない
  (転送によって認証が壊れ、受信拒否や消失が起きる問題を根本的に回避する)
- ``import`` と違いスパム分類器を通らず、指定したラベルがそのまま適用される
  ので「受信トレイ → 受信トレイ」を確定できる (Gmail 側で迷惑メール扱いに
  されることがない)
- 送信ではないため送信数の制限やループの心配がない

挿入したメールは ``users.messages.get`` で読み戻して確認する。iCloud の原本を
ゴミ箱へ移す前に「Gmail 側に確かに入った」と言い切るために、``insert`` の応答
だけでなく実際に取得できることまで確かめる。この確認には ``gmail.metadata``
スコープが要るが、本文は読まない (``format=minimal``)。
"""

from __future__ import annotations

import io
import logging
import random
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 挿入に必要な最小権限。既読/未読の変更や削除、本文の閲覧は含まれない。
GMAIL_INSERT_SCOPE = "https://www.googleapis.com/auth/gmail.insert"
# 挿入したメールの存在とラベルだけを読み戻すための権限。本文と添付は読めない。
GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"

# トークンを取得するときに要求するスコープ (tools/get_gmail_refresh_token.py)。
GMAIL_SCOPES = [GMAIL_INSERT_SCOPE, GMAIL_METADATA_SCOPE]

# トークンの更新時に「最低限これは付いているはず」と検証するスコープ。
# google-auth は要求したスコープが 1 つでも欠けると更新を拒否するため、
# metadata を入れると insert だけで発行済みの既存トークンが使えなくなる。
# metadata の有無は API を呼んだときの 403 で判定する (GmailScopeError)。
GMAIL_REQUIRED_SCOPES = [GMAIL_INSERT_SCOPE]

_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
_MAX_ATTEMPTS = 5

# 403 は「一時的な流量制限」と「恒久的なスコープ不足」の両方で返る。
# 後者を再試行しても直らないので、応答本文の理由で見分ける。
_SCOPE_MARKERS = (
    "insufficientpermissions",
    "access_token_scope_insufficient",
    "insufficient authentication scopes",
    "request had insufficient authentication scopes",
)


class GmailError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GmailScopeError(GmailError):
    """トークンに必要な OAuth スコープが付いていない (再取得しない限り直らない)."""


@dataclass(frozen=True)
class InsertResult:
    message_id: str
    thread_id: str | None


@dataclass(frozen=True)
class VerifyResult:
    """Gmail から読み戻せたメッセージ."""

    message_id: str
    label_ids: tuple[str, ...]


def build_credentials(auth) -> object:
    """リフレッシュトークンから短命なアクセストークンを取得する認証情報を作る."""
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=auth.refresh_token,
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        token_uri=auth.token_uri,
        scopes=GMAIL_REQUIRED_SCOPES,
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

    def verify(self, message_id: str) -> VerifyResult | None:
        """挿入したメールが Gmail から実際に読み出せるかを確かめる.

        iCloud の原本をゴミ箱へ移す前の確認に使う。``insert`` の応答は
        「受け付けた」ことしか示さないので、ID を引き直して存在を確認する。

        見つからなければ ``None``。トークンに ``gmail.metadata`` が無ければ
        ``GmailScopeError`` を送出する (呼び出し側が 1 度だけ警告して降りる)。
        ``format=minimal`` なので本文も添付もダウンロードしない。
        """
        request = (
            self._service.users()
            .messages()
            .get(userId=self._user_id, id=message_id, format="minimal")
        )
        try:
            response = self._execute_with_retry(request)
        except GmailScopeError:
            raise
        except GmailError as exc:
            if exc.status == 404:
                return None
            raise
        returned = str(response.get("id") or "")
        if returned != message_id:
            # ここに来ることは無いはずだが、取り違えを見逃さない
            return None
        return VerifyResult(
            message_id=returned,
            label_ids=tuple(str(x) for x in response.get("labelIds") or ()),
        )

    def _execute_with_retry(self, request):
        from googleapiclient.errors import HttpError

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if _is_scope_error(status, exc):
                    raise GmailScopeError(
                        f"Gmail API に必要なスコープがトークンに付いていません: {exc}",
                        status=status,
                    ) from exc
                if status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                    raise GmailError(
                        f"Gmail API の呼び出しに失敗しました (status={status}): {exc}",
                        status=status,
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


def _is_scope_error(status: int | None, exc: Exception) -> bool:
    """403 のうち、スコープ不足によるものかどうか."""
    if status != 403:
        return False
    text = str(exc).lower()
    content = getattr(exc, "content", None)
    if isinstance(content, bytes | bytearray):
        text += bytes(content).decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _SCOPE_MARKERS)
