"""同期状態の永続化と多重起動防止ロック.

常駐プロセスは 1 台に 1 つしか動かないので、状態はローカルの SQLite に持つ。
外部サービスに依存しないぶん障害点が減り、無料枠の消費もない。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import os
import sqlite3
import threading
import uuid
from typing import Protocol

log = logging.getLogger(__name__)


# ``mark_seen`` の保持日数が 0 以下のときに使う期限。重複排除の記録を
# 消さないことで、利用者が受信トレイへ戻したメールの再転送を防ぐ。
NEVER_EXPIRES = float("inf")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclasses.dataclass(frozen=True)
class Leftover:
    """転送されずに iCloud 側へ残ったメール 1 通の記録.

    「受信トレイに残っているのは未処理のものだけ」という前提が崩れると、
    利用者が手で整理した拍子にどこにも無いメールができてしまう。理由付きで
    残しておき、``--leftovers`` で見られるようにする。
    """

    key: str
    uid: int
    mailbox: str
    reason: str
    detail: str | None
    recorded_at: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MailboxState:
    """1 メールボックス分の同期位置.

    IMAP の UID は (UIDVALIDITY, UID) の組で初めて一意になる。サーバ側で
    UIDVALIDITY が変わったら UID の連続性は保証されないため、状態を作り直す。
    """

    uidvalidity: int = 0
    last_uid: int = 0
    # 稼働開始時点で既にメールボックスにあった UID の上限 (INITIAL_IMPORT=all の
    # ときだけ 0 以外になる)。ここ以下のメールは「利用者が自分で貯めてきたもの」
    # なので、転送しても既定ではゴミ箱へ移さない。
    import_floor: int = 0

    def to_dict(self) -> dict:
        return {
            "uidvalidity": self.uidvalidity,
            "last_uid": self.last_uid,
            "import_floor": self.import_floor,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> MailboxState | None:
        if not data:
            return None
        return cls(
            uidvalidity=int(data.get("uidvalidity") or 0),
            last_uid=int(data.get("last_uid") or 0),
            import_floor=int(data.get("import_floor") or 0),
        )


class StateStore(Protocol):
    def get_mailbox_state(self, key: str) -> MailboxState | None: ...

    def put_mailbox_state(self, key: str, state: MailboxState) -> None: ...

    def acquire_lock(self, name: str, ttl_seconds: int, holder: str) -> bool: ...

    def release_lock(self, name: str, holder: str) -> None: ...

    def force_release_lock(self, name: str) -> bool: ...

    def is_seen(self, key: str) -> bool: ...

    def mark_seen(self, key: str, retention_days: int) -> None: ...

    def record_leftover(
        self, key: str, uid: int, mailbox: str, reason: str, detail: str | None = None
    ) -> None: ...

    def list_leftovers(
        self, key: str | None = None, limit: int | None = None
    ) -> list[Leftover]: ...

    def clear_leftovers(self, key: str) -> int: ...


class MemoryStateStore:
    """テストおよび ``DRY_RUN`` 用のインメモリ実装."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, MailboxState] = {}
        self._locks: dict[str, tuple[str, dt.datetime]] = {}
        self._seen: set[str] = set()
        self._leftovers: dict[tuple[str, int], Leftover] = {}

    def get_mailbox_state(self, key: str) -> MailboxState | None:
        state = self._mailboxes.get(key)
        return dataclasses.replace(state) if state else None

    def put_mailbox_state(self, key: str, state: MailboxState) -> None:
        self._mailboxes[key] = dataclasses.replace(state)

    def acquire_lock(self, name: str, ttl_seconds: int, holder: str) -> bool:
        current = self._locks.get(name)
        if current and current[1] > _now():
            return False
        self._locks[name] = (holder, _now() + dt.timedelta(seconds=ttl_seconds))
        return True

    def release_lock(self, name: str, holder: str) -> None:
        current = self._locks.get(name)
        if current and current[0] == holder:
            del self._locks[name]

    def force_release_lock(self, name: str) -> bool:
        return self._locks.pop(name, None) is not None

    def is_seen(self, key: str) -> bool:
        return key in self._seen

    def mark_seen(self, key: str, retention_days: int) -> None:
        self._seen.add(key)

    def record_leftover(
        self, key: str, uid: int, mailbox: str, reason: str, detail: str | None = None
    ) -> None:
        self._leftovers[(key, uid)] = Leftover(
            key=key,
            uid=uid,
            mailbox=mailbox,
            reason=reason,
            detail=detail,
            recorded_at=_now().isoformat(),
        )

    def list_leftovers(self, key: str | None = None, limit: int | None = None) -> list[Leftover]:
        rows = [row for row in self._leftovers.values() if key is None or row.key == key]
        rows.sort(key=lambda row: (row.recorded_at, row.uid))
        return rows if limit is None else rows[:limit]

    def clear_leftovers(self, key: str) -> int:
        stale = [k for k in self._leftovers if k[0] == key]
        for k in stale:
            del self._leftovers[k]
        return len(stale)


def new_holder_id() -> str:
    return uuid.uuid4().hex


class SqliteStateStore:
    """ローカルファイル (SQLite) を使った状態ストア.

    常時起動の VM で動かすので、状態を外部サービスに置く必要はない。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._write_lock = threading.Lock()
        self._initialize()

    @contextlib.contextmanager
    def _connect(self):
        """接続を開いて必ず閉じる.

        sqlite3.Connection を ``with`` に渡してもトランザクションが閉じるだけで
        接続自体は閉じないため、明示的に閉じる必要がある。
        """
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # 読み書きの競合を避け、突然の停止でも壊れにくくする
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mailbox_state (
                    key          TEXT PRIMARY KEY,
                    uidvalidity  INTEGER NOT NULL,
                    last_uid     INTEGER NOT NULL,
                    updated_at   TEXT NOT NULL,
                    import_floor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS locks (
                    name       TEXT PRIMARY KEY,
                    holder     TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen (
                    key        TEXT PRIMARY KEY,
                    expire_at  REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS seen_expire_at ON seen (expire_at);
                CREATE TABLE IF NOT EXISTS leftovers (
                    key         TEXT NOT NULL,
                    uid         INTEGER NOT NULL,
                    mailbox     TEXT NOT NULL,
                    reason      TEXT NOT NULL,
                    detail      TEXT,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (key, uid)
                );
                CREATE INDEX IF NOT EXISTS leftovers_recorded_at
                    ON leftovers (recorded_at);
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """既存の state.db を新しい列に合わせる.

        VM を作り直さずに更新できるよう、足りない列だけを後から足す。
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(mailbox_state)")}
        if "import_floor" not in columns:
            conn.execute(
                "ALTER TABLE mailbox_state ADD COLUMN import_floor INTEGER NOT NULL DEFAULT 0"
            )

    # --- メールボックス状態 -------------------------------------------------
    def get_mailbox_state(self, key: str) -> MailboxState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uidvalidity, last_uid, import_floor FROM mailbox_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return MailboxState(
            uidvalidity=int(row["uidvalidity"]),
            last_uid=int(row["last_uid"]),
            import_floor=int(row["import_floor"] or 0),
        )

    def put_mailbox_state(self, key: str, state: MailboxState) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_state
                    (key, uidvalidity, last_uid, updated_at, import_floor)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    uidvalidity  = excluded.uidvalidity,
                    last_uid     = excluded.last_uid,
                    updated_at   = excluded.updated_at,
                    import_floor = excluded.import_floor
                """,
                (
                    key,
                    state.uidvalidity,
                    state.last_uid,
                    _now().isoformat(),
                    state.import_floor,
                ),
            )

    # --- ロック -------------------------------------------------------------
    def acquire_lock(self, name: str, ttl_seconds: int, holder: str) -> bool:
        now = _now().timestamp()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT expires_at FROM locks WHERE name = ?", (name,)).fetchone()
            if row is not None and float(row["expires_at"]) > now:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                INSERT INTO locks (name, holder, expires_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    holder = excluded.holder, expires_at = excluded.expires_at
                """,
                (name, holder, now + ttl_seconds),
            )
            conn.execute("COMMIT")
        return True

    def release_lock(self, name: str, holder: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))

    def force_release_lock(self, name: str) -> bool:
        """持ち主に関係なくロックを外す (常駐プロセスの起動時のみ使う)."""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM locks WHERE name = ?", (name,))
            return bool(cursor.rowcount)

    # --- 重複排除 -----------------------------------------------------------
    def is_seen(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen WHERE key = ? AND expire_at > ?",
                (key, _now().timestamp()),
            ).fetchone()
        return row is not None

    def mark_seen(self, key: str, retention_days: int) -> None:
        if retention_days <= 0:
            expire_at = NEVER_EXPIRES
        else:
            expire_at = (_now() + dt.timedelta(days=retention_days)).timestamp()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen (key, expire_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET expire_at = excluded.expire_at
                """,
                (key, expire_at),
            )

    def purge_expired(self) -> int:
        """期限切れの重複排除レコードを削除する (常駐プロセスが定期的に呼ぶ).

        ``expire_at`` が無期限 (inf) のレコードはここで消えない。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM seen WHERE expire_at <= ?", (_now().timestamp(),))
            return cursor.rowcount or 0

    # --- iCloud に残ったメール ---------------------------------------------
    def record_leftover(
        self, key: str, uid: int, mailbox: str, reason: str, detail: str | None = None
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO leftovers (key, uid, mailbox, reason, detail, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key, uid) DO UPDATE SET
                    reason      = excluded.reason,
                    detail      = excluded.detail,
                    recorded_at = excluded.recorded_at
                """,
                (key, uid, mailbox, reason, detail, _now().isoformat()),
            )

    def list_leftovers(self, key: str | None = None, limit: int | None = None) -> list[Leftover]:
        """記録を古い順に返す.

        既定で全件返す。手で受信トレイを整理する前に見るものなので、黙って
        切り詰めると「一覧に無い＝転送済み」と読み違えられてしまう。
        """
        sql = "SELECT key, uid, mailbox, reason, detail, recorded_at FROM leftovers"
        params: list = []
        if key is not None:
            sql += " WHERE key = ?"
            params.append(key)
        sql += " ORDER BY recorded_at, uid"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Leftover(
                key=str(row["key"]),
                uid=int(row["uid"]),
                mailbox=str(row["mailbox"]),
                reason=str(row["reason"]),
                detail=row["detail"],
                recorded_at=str(row["recorded_at"]),
            )
            for row in rows
        ]

    def clear_leftovers(self, key: str) -> int:
        """UIDVALIDITY が変わったときなど、UID の意味が失われたら記録も捨てる."""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM leftovers WHERE key = ?", (key,))
            return cursor.rowcount or 0


def build_state_store(config) -> StateStore:
    """設定に応じた状態ストアを組み立てる."""
    if config.dry_run or config.state_backend == "memory":
        return MemoryStateStore()
    return SqliteStateStore(config.state_db_path)
