"""iCloud (imap.mail.me.com) からのメール取得.

SMTP 転送を使うと、元の送信者を詐称した形になるため受信側の DMARC/SPF 検証で
拒否され、迷惑メールフォルダにすら残らないことがある。そこで本モジュールは
IMAP で生のメッセージ (RFC822) をそのまま取り出し、Gmail API 側で挿入する。
"""

from __future__ import annotations

import datetime as dt
import imaplib
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# SEARCH の応答は UID が 1 行に並ぶため既定の 10000 バイト上限では溢れうる
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 10000), 10 * 1024 * 1024)

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)", re.IGNORECASE)
_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.IGNORECASE)
_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)


class ImapError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mailbox:
    name: str
    flags: tuple[str, ...]

    def has_flag(self, flag: str) -> bool:
        target = flag.lower()
        return any(f.lower() == target for f in self.flags)


@dataclass(frozen=True)
class MessageMeta:
    uid: int
    size: int
    flags: tuple[str, ...]
    internaldate: dt.datetime | None = None

    @property
    def seen(self) -> bool:
        return any(f.lower() == r"\seen" for f in self.flags)

    @property
    def flagged(self) -> bool:
        return any(f.lower() == r"\flagged" for f in self.flags)


def _decode(value: bytes) -> str:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace")


def _unquote_mailbox(raw: bytes) -> str:
    text = raw.strip()
    if text.startswith(b'"') and text.endswith(b'"'):
        text = text[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return _decode(text)


def quote_mailbox(name: str) -> str:
    """SELECT/EXAMINE に渡せるようメールボックス名を引用符で囲む."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_flag_list(raw: bytes) -> tuple[str, ...]:
    return tuple(_decode(f) for f in raw.split())


class ImapSource:
    """iCloud IMAP への接続をラップする. ``with`` で使うこと."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 60,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None

    # --- 接続 ---------------------------------------------------------------
    def __enter__(self) -> ImapSource:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> None:
        # IMAP4_SSL は既定で証明書検証を行う ssl.create_default_context() を使う
        self._conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=self._timeout)
        try:
            self._conn.login(self._username, self._password)
        except imaplib.IMAP4.error as exc:
            raise ImapError(
                "iCloud への IMAP ログインに失敗しました。"
                "Apple ID とアプリ用パスワード (App-Specific Password) を確認してください"
            ) from exc
        log.info("iCloud IMAP に接続しました", extra={"extra_fields": {"host": self._host}})

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if self._selected:
                conn.close()
        except Exception:  # pragma: no cover - 切断時のエラーは無視してよい
            pass
        finally:
            self._selected = None
            try:
                conn.logout()
            except Exception:  # pragma: no cover
                pass

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise ImapError("IMAP に接続していません")
        return self._conn

    def _check(self, typ: str, data, command: str):
        if typ != "OK":
            raise ImapError(f"IMAP コマンド {command} が失敗しました: {typ} {data!r}")
        return data

    # --- メールボックス -----------------------------------------------------
    def list_mailboxes(self) -> list[Mailbox]:
        typ, data = self.conn.list()
        self._check(typ, data, "LIST")
        mailboxes: list[Mailbox] = []
        for line in data or []:
            if not isinstance(line, bytes):
                # 名前がリテラルで返る稀なケース。特殊用途フラグ判定には使えない。
                log.debug("解析できない LIST 応答を無視します: %r", line)
                continue
            match = _LIST_RE.match(line.strip())
            if not match:
                continue
            mailboxes.append(
                Mailbox(
                    name=_unquote_mailbox(match.group("name")),
                    flags=_parse_flag_list(match.group("flags")),
                )
            )
        return mailboxes

    def resolve_mailbox(self, source: str) -> str | None:
        """``INBOX`` のような実名、または ``\\Junk`` のような特殊用途フラグを解決する.

        迷惑メールフォルダの名前はロケールによって変わる (Junk / 迷惑メール など)
        ため、RFC 6154 の特殊用途フラグで探すほうが確実。
        """
        if not source.startswith("\\"):
            return source
        for mailbox in self.list_mailboxes():
            if mailbox.has_flag(source):
                return mailbox.name
        return None

    def select(self, mailbox: str) -> tuple[int, int]:
        """読み取り専用で SELECT し ``(uidvalidity, uidnext)`` を返す.

        readonly にすることで iCloud 側の既読フラグを変化させない。
        """
        typ, data = self.conn.select(quote_mailbox(mailbox), readonly=True)
        self._check(typ, data, f"SELECT {mailbox}")
        self._selected = mailbox
        uidvalidity = self._response_int("UIDVALIDITY")
        uidnext = self._response_int("UIDNEXT")
        return uidvalidity, uidnext

    def _response_int(self, key: str) -> int:
        _, values = self.conn.response(key)
        for value in values or []:
            if value:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    # --- メッセージ ---------------------------------------------------------
    def search_uids_after(self, last_uid: int) -> list[int]:
        """``last_uid`` より大きい UID を昇順で返す."""
        start = max(last_uid + 1, 1)
        typ, data = self.conn.uid("SEARCH", None, f"UID {start}:*")
        self._check(typ, data, "UID SEARCH")
        raw = b" ".join(chunk for chunk in (data or []) if isinstance(chunk, bytes))
        # IMAP の "n:*" は該当がなくても最大 UID を 1 件返すため必ず絞り込む
        uids = sorted({int(token) for token in raw.split() if token.isdigit()})
        return [uid for uid in uids if uid > last_uid]

    def fetch_metadata(self, uids: list[int]) -> list[MessageMeta]:
        """本文を落とさずに UID / サイズ / フラグだけをまとめて取得する."""
        if not uids:
            return []
        uid_set = ",".join(str(uid) for uid in uids)
        typ, data = self.conn.uid("FETCH", uid_set, "(UID RFC822.SIZE FLAGS INTERNALDATE)")
        self._check(typ, data, "UID FETCH (metadata)")
        metas: dict[int, MessageMeta] = {}
        for line in data or []:
            payload = line[0] if isinstance(line, tuple) else line
            if not isinstance(payload, bytes):
                continue
            uid_match = _UID_RE.search(payload)
            if not uid_match:
                continue
            size_match = _SIZE_RE.search(payload)
            flags_match = _FLAGS_RE.search(payload)
            date_match = _INTERNALDATE_RE.search(payload)
            uid = int(uid_match.group(1))
            metas[uid] = MessageMeta(
                uid=uid,
                size=int(size_match.group(1)) if size_match else 0,
                flags=_parse_flag_list(flags_match.group(1)) if flags_match else (),
                internaldate=_parse_internaldate(date_match.group(1)) if date_match else None,
            )
        return [metas[uid] for uid in uids if uid in metas]

    def fetch_raw(self, uid: int) -> bytes | None:
        """RFC822 の生バイト列を取得する.

        ``BODY.PEEK[]`` を使うので iCloud 側で既読になることはない。
        """
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        self._check(typ, data, f"UID FETCH {uid}")
        for line in data or []:
            if (
                isinstance(line, tuple)
                and len(line) >= 2
                and isinstance(line[1], (bytes, bytearray))
            ):
                return bytes(line[1])
        # 取得中に別クライアントが削除した場合など
        log.warning("UID %s の本文を取得できませんでした (削除された可能性)", uid)
        return None


def _parse_internaldate(raw: bytes) -> dt.datetime | None:
    """IMAP の INTERNALDATE (``01-Jan-2024 09:30:00 +0900``) を解析する."""
    try:
        return dt.datetime.strptime(raw.decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
    except (ValueError, UnicodeDecodeError):
        return None


def message_id_of(raw: bytes) -> str | None:
    """重複排除キーに使う Message-ID ヘッダを取り出す."""
    import email
    import email.policy

    try:
        parsed = email.message_from_bytes(raw[:65536], policy=email.policy.compat32)
    except Exception:  # pragma: no cover
        return None
    value = parsed.get("Message-ID") or parsed.get("Message-Id")
    if not value:
        return None
    value = value.strip()
    return value or None


def date_header_of(raw: bytes) -> dt.datetime | None:
    import email
    import email.policy

    try:
        parsed = email.message_from_bytes(raw[:65536], policy=email.policy.compat32)
        value = parsed.get("Date")
    except Exception:  # pragma: no cover
        return None
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
