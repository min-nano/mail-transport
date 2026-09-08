"""同期状態の永続化 (Firestore) と多重起動防止ロック.

Firestore を使う理由:
- Cloud Run のインスタンスは使い捨てなので、どこまで転送したかを外部に持つ必要がある
- 無料枠 (1GiB / 読み取り 5万・書き込み 2万 per day) に十分収まる規模
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
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
