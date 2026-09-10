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
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# SEARCH の応答は UID が 1 行に並ぶため既定の 10000 バイト上限では溢れうる
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 10000), 10 * 1024 * 1024)

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)", re.IGNORECASE)
_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.IGNORECASE)
_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
# IDLE 中に「新着があった」と判断する未応答レスポンス。
# サーバーが送ってくる "* OK Still here" のような keepalive では起こさない。
_IDLE_ACTIVITY_RE = re.compile(rb"^\*\s+\d+\s+(EXISTS|RECENT)\b", re.IGNORECASE)
_BYE_RE = re.compile(rb"^\*\s+BYE\b", re.IGNORECASE)


# IDLE 終了時に読み捨てる未応答レスポンスの上限 (無限ループ防止)
_MAX_IDLE_DRAIN_LINES = 1000

# 1 コマンドに並べる UID の上限。行が長くなりすぎるサーバーを避けるため分割する。
_MAX_UIDS_PER_COMMAND = 200


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
        self._selected_readonly = True

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
        self._refresh_capabilities()
        log.info("iCloud IMAP に接続しました", extra={"extra_fields": {"host": self._host}})

    def _refresh_capabilities(self) -> None:
        """ログイン後の CAPABILITY を取り直す.

        imaplib は接続直後 (認証前) の CAPABILITY しか覚えていない。サーバーは
        認証後にしか見せない機能があるため、MOVE / UIDPLUS の有無を正しく
        判定するには取り直す必要がある。
        """
        conn = self.conn
        try:
            typ, data = conn.capability()
            if typ == "OK" and data and data[-1]:
                conn.capabilities = tuple(_decode(data[-1]).upper().split())
        except Exception:  # pragma: no cover - 取れなければ接続時のものを使う
            log.debug("CAPABILITY を取り直せませんでした", exc_info=True)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if self._selected:
                # 読み書きで開いたときの CLOSE は、\Deleted が立ったメールを無条件に
                # 削除する。UIDPLUS が無くて削除を見送ったぶんや、他のクライアントが
                # 立てたフラグまで巻き込むので、何も消さない UNSELECT を使う。
                # それも無いサーバーでは、何も消さない LOGOUT だけで閉じる。
                if self._selected_readonly:
                    conn.close()
                elif _has_capability(conn, "UNSELECT"):
                    conn.unselect()
        except Exception:  # pragma: no cover - 切断時のエラーは無視してよい
            pass
        finally:
            self._selected = None
            self._selected_readonly = True
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

    def select(self, mailbox: str, readonly: bool = True) -> tuple[int, int]:
        """SELECT し ``(uidvalidity, uidnext)`` を返す.

        既定の読み取り専用では iCloud 側を一切変更しない。転送後にゴミ箱へ
        移す場合だけ ``readonly=False`` にする (MOVE には書き込み権限が要る)。
        本文の取得は常に ``BODY.PEEK[]`` なので、どちらでも既読にはならない。
        """
        typ, data = self.conn.select(quote_mailbox(mailbox), readonly=readonly)
        self._check(typ, data, f"SELECT {mailbox}")
        self._selected = mailbox
        self._selected_readonly = readonly
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
            if not isinstance(line, tuple) or len(line) < 2:
                continue
            if isinstance(line[1], bytes | bytearray):
                return bytes(line[1])
        # 取得中に別クライアントが削除した場合など
        log.warning("UID %s の本文を取得できませんでした (削除された可能性)", uid)
        return None

    # --- 移動 (転送後の後始末) ----------------------------------------------
    def move_uids(self, uids: list[int], destination: str, on_moved=None) -> int:
        """``uids`` を ``destination`` へ移し、移せた通数を返す.

        転送済みのメールを iCloud のゴミ箱へ送って容量を空けるために使う。
        RFC 6851 の ``UID MOVE`` があればそれを使い、無ければ COPY してから
        元を削除する。書き込み可能な状態で SELECT していること。

        UID が多いときは分割して送るため、途中で失敗すると戻り値が返らない。
        そこまでに移せた通数は ``on_moved`` (チャンクごとに通数で呼ばれる) で
        受け取れる。
        """
        if not uids:
            return 0
        if self._selected_readonly:
            raise ImapError("読み取り専用で SELECT しているためメールを移動できません")

        moved = 0
        target = quote_mailbox(destination)
        supports_move = self.has_capability("MOVE")
        for chunk in _chunk(uids, _MAX_UIDS_PER_COMMAND):
            uid_set = ",".join(str(uid) for uid in chunk)
            if supports_move:
                typ, data = self.conn.uid("MOVE", uid_set, target)
                self._check(typ, data, f"UID MOVE -> {destination}")
            else:
                typ, data = self.conn.uid("COPY", uid_set, target)
                self._check(typ, data, f"UID COPY -> {destination}")
                typ, data = self.conn.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)")
                self._check(typ, data, r"UID STORE (\Deleted)")
                if self.has_capability("UIDPLUS"):
                    typ, data = self.conn.uid("EXPUNGE", uid_set)
                    self._check(typ, data, "UID EXPUNGE")
                else:
                    # 素の EXPUNGE は他のクライアントが \Deleted を立てたメールまで
                    # 消してしまう。コピーは済んでいるので削除は行わない。
                    log.warning(
                        "UIDPLUS が無いため削除を見送りました "
                        "(ゴミ箱にコピー済み、元は \\Deleted のまま残ります)"
                    )
            moved += len(chunk)
            if on_moved is not None:
                on_moved(len(chunk))
        return moved

    # --- IDLE (push 受信) ---------------------------------------------------
    def has_capability(self, name: str) -> bool:
        return _has_capability(self.conn, name)

    def idle_wait(self, timeout: float, poll_step: float = 30.0) -> bool:
        """IDLE で新着を待ち、通知が来たら True、時間切れなら False を返す.

        RFC 2177 の IDLE は、サーバー側に変化があった時点で未応答レスポンスを
        送ってくる。ポーリングと違い到着から数秒で気付けるが、接続を張り続ける
        必要があるため常時起動のホストでのみ使える。

        ``timeout`` には 29 分未満を指定すること。それ以上 IDLE を続けると
        サーバーやその手前の NAT に切断されうる (RFC 2177 の推奨)。
        """
        conn = self.conn
        tag = conn._new_tag()
        conn.send(b"%s IDLE\r\n" % tag)
        line = conn.readline()
        if not line.startswith(b"+"):
            raise ImapError(f"IDLE を開始できませんでした: {line!r}")

        sock = conn.socket()
        previous_timeout = sock.gettimeout()
        activity = False
        broken = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # select ではなくソケットタイムアウトで待つ。imaplib の
                # バッファに溜まった通知を取りこぼさないため。
                sock.settimeout(min(poll_step, remaining))
                try:
                    line = conn.readline()
                except TimeoutError:
                    continue
                if not line:
                    broken = True
                    raise ImapError("IDLE 中に接続が切断されました")
                if _BYE_RE.match(line):
                    broken = True
                    raise ImapError(f"サーバーが接続を閉じました: {line!r}")
                if _IDLE_ACTIVITY_RE.match(line):
                    activity = True
                    break
        finally:
            try:
                sock.settimeout(previous_timeout)
            except OSError:  # pragma: no cover - 切断済みなら意味がない
                pass
            if not broken:
                self._end_idle(tag)
        return activity

    def _end_idle(self, tag: bytes) -> None:
        """DONE を送り、IDLE の完了応答を読み切って通常状態に戻す."""
        conn = self.conn
        conn.send(b"DONE\r\n")
        for _ in range(_MAX_IDLE_DRAIN_LINES):
            line = conn.readline()
            if not line or line.startswith(tag):
                return
        raise ImapError("IDLE の終了応答を受け取れませんでした")


def _has_capability(conn, name: str) -> bool:
    capabilities = getattr(conn, "capabilities", ()) or ()
    return name.upper() in {str(c).upper() for c in capabilities}


def _chunk(values: list[int], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
