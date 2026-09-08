"""同期状態の永続化 (Firestore) と多重起動防止ロック.

Firestore を使う理由:
- Cloud Run のインスタンスは使い捨てなので、どこまで転送したかを外部に持つ必要がある
- 無料枠 (1GiB / 読み取り 5万・書き込み 2万 per day) に十分収まる規模
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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclasses.dataclass
class MailboxState:
    """1 メールボックス分の同期位置.

    IMAP の UID は (UIDVALIDITY, UID) の組で初めて一意になる。サーバ側で
    UIDVALIDITY が変わったら UID の連続性は保証されないため、状態を作り直す。
    """

    uidvalidity: int = 0
    last_uid: int = 0

    def to_dict(self) -> dict:
        return {"uidvalidity": self.uidvalidity, "last_uid": self.last_uid}

    @classmethod
    def from_dict(cls, data: dict | None) -> MailboxState | None:
        if not data:
            return None
        return cls(
            uidvalidity=int(data.get("uidvalidity") or 0),
            last_uid=int(data.get("last_uid") or 0),
        )


class StateStore(Protocol):
    def get_mailbox_state(self, key: str) -> MailboxState | None: ...

    def put_mailbox_state(self, key: str, state: MailboxState) -> None: ...

    def acquire_lock(self, name: str, ttl_seconds: int, holder: str) -> bool: ...

    def release_lock(self, name: str, holder: str) -> None: ...

    def is_seen(self, key: str) -> bool: ...

    def mark_seen(self, key: str, retention_days: int) -> None: ...


class MemoryStateStore:
    """テストおよび ``DRY_RUN`` 用のインメモリ実装."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, MailboxState] = {}
        self._locks: dict[str, tuple[str, dt.datetime]] = {}
        self._seen: set[str] = set()

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

    def is_seen(self, key: str) -> bool:
        return key in self._seen

    def mark_seen(self, key: str, retention_days: int) -> None:
        self._seen.add(key)


class FirestoreStateStore:
    """Firestore (Native モード) を使った状態ストア."""

    def __init__(
        self,
        project: str | None,
        database: str = "(default)",
        state_collection: str = "mail_transport_state",
        seen_collection: str = "mail_transport_seen",
        client=None,
    ) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project, database=database)
        self._client = client
        self._state_collection = state_collection
        self._seen_collection = seen_collection

    # --- メールボックス状態 -------------------------------------------------
    def get_mailbox_state(self, key: str) -> MailboxState | None:
        snapshot = self._client.collection(self._state_collection).document(key).get()
        if not snapshot.exists:
            return None
        return MailboxState.from_dict(snapshot.to_dict())

    def put_mailbox_state(self, key: str, state: MailboxState) -> None:
        payload = state.to_dict()
        payload["updated_at"] = _now()
        self._client.collection(self._state_collection).document(key).set(payload)

    # --- ロック -------------------------------------------------------------
    def acquire_lock(self, name: str, ttl_seconds: int, holder: str) -> bool:
        """トランザクションで排他的にロックを取る.

        Cloud Scheduler の再送や処理の長時間化で実行が重なると、同じメールを
        二重に取り込む恐れがあるため、期限付きロックで直列化する。
        """
        from google.cloud import firestore

        doc_ref = self._client.collection(self._state_collection).document(f"lock__{name}")
        expires_at = _now() + dt.timedelta(seconds=ttl_seconds)

        @firestore.transactional
        def _acquire(transaction) -> bool:
            snapshot = doc_ref.get(transaction=transaction)
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                current_expiry = data.get("expires_at")
                if current_expiry and _to_aware(current_expiry) > _now():
                    return False
            transaction.set(doc_ref, {"holder": holder, "expires_at": expires_at})
            return True

        return bool(_acquire(self._client.transaction()))

    def release_lock(self, name: str, holder: str) -> None:
        from google.cloud import firestore

        doc_ref = self._client.collection(self._state_collection).document(f"lock__{name}")

        @firestore.transactional
        def _release(transaction) -> None:
            snapshot = doc_ref.get(transaction=transaction)
            if snapshot.exists and (snapshot.to_dict() or {}).get("holder") == holder:
                transaction.delete(doc_ref)

        try:
            _release(self._client.transaction())
        except Exception:  # pragma: no cover - 解放失敗は TTL 切れで回復する
            log.warning("ロックの解放に失敗しました (TTL 経過で自動解放されます)", exc_info=True)

    # --- 重複排除 -----------------------------------------------------------
    def is_seen(self, key: str) -> bool:
        return self._client.collection(self._seen_collection).document(key).get().exists

    def mark_seen(self, key: str, retention_days: int) -> None:
        self._client.collection(self._seen_collection).document(key).set(
            {
                "created_at": _now(),
                # Firestore の TTL ポリシーをこのフィールドに設定すると自動削除される
                "expire_at": _now() + dt.timedelta(days=retention_days),
            }
        )


def _to_aware(value) -> dt.datetime:
    """Firestore から返るタイムスタンプを aware な datetime に正規化する."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    # google.api_core.datetime_helpers.DatetimeWithNanoseconds など
    return dt.datetime.fromisoformat(str(value))


def new_holder_id() -> str:
    return uuid.uuid4().hex


class SqliteStateStore:
    """ローカルファイル (SQLite) を使った状態ストア.

    常時起動の VM で動かす場合、状態を外部サービスに置く必要はない。
    Firestore を使わないぶん依存も無料枠の消費も減り、障害点も 1 つ減る。
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
                    key         TEXT PRIMARY KEY,
                    uidvalidity INTEGER NOT NULL,
                    last_uid    INTEGER NOT NULL,
                    updated_at  TEXT NOT NULL
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
                """
            )

    # --- メールボックス状態 -------------------------------------------------
    def get_mailbox_state(self, key: str) -> MailboxState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT uidvalidity, last_uid FROM mailbox_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return MailboxState(uidvalidity=int(row["uidvalidity"]), last_uid=int(row["last_uid"]))

    def put_mailbox_state(self, key: str, state: MailboxState) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_state (key, uidvalidity, last_uid, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_uid    = excluded.last_uid,
                    updated_at  = excluded.updated_at
                """,
                (key, state.uidvalidity, state.last_uid, _now().isoformat()),
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

    # --- 重複排除 -----------------------------------------------------------
    def is_seen(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen WHERE key = ? AND expire_at > ?",
                (key, _now().timestamp()),
            ).fetchone()
        return row is not None

    def mark_seen(self, key: str, retention_days: int) -> None:
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
        """期限切れの重複排除レコードを削除する (常駐プロセスが定期的に呼ぶ)."""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM seen WHERE expire_at <= ?", (_now().timestamp(),))
            return cursor.rowcount or 0


def build_state_store(config) -> StateStore:
    """設定に応じた状態ストアを組み立てる."""
    if config.dry_run or config.state_backend == "memory":
        return MemoryStateStore()
    if config.state_backend == "sqlite":
        return SqliteStateStore(config.state_db_path)
    return FirestoreStateStore(
        project=config.project_id,
        database=config.firestore_database,
        state_collection=config.state_collection,
        seen_collection=config.seen_collection,
    )
