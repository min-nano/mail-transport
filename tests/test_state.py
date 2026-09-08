from __future__ import annotations

from mailtransport.state import MailboxState, MemoryStateStore, new_holder_id


def test_lock_is_exclusive():
    store = MemoryStateStore()
    assert store.acquire_lock("sync", 60, "a") is True
    assert store.acquire_lock("sync", 60, "b") is False

    store.release_lock("sync", "b")  # 持ち主でないので解放できない
    assert store.acquire_lock("sync", 60, "b") is False

    store.release_lock("sync", "a")
    assert store.acquire_lock("sync", 60, "b") is True


def test_expired_lock_can_be_taken_over():
    store = MemoryStateStore()
    store.acquire_lock("sync", 0, "dead-instance")
    assert store.acquire_lock("sync", 60, new_holder_id()) is True


def test_mailbox_state_round_trip():
    store = MemoryStateStore()
    store.put_mailbox_state("k", MailboxState(uidvalidity=7, last_uid=42))

    loaded = store.get_mailbox_state("k")
    assert loaded == MailboxState(uidvalidity=7, last_uid=42)

    # 取り出した値を書き換えても保存済みの状態には影響しない
    loaded.last_uid = 999
    assert store.get_mailbox_state("k").last_uid == 42


def test_mailbox_state_from_dict_handles_missing_fields():
    # 空のドキュメントは「状態なし」扱い。last_uid=0 と解釈すると、過去メールを
    # まるごと取り込んでしまうため、初期化 (ブートストラップ) に倒す。
    assert MailboxState.from_dict(None) is None
    assert MailboxState.from_dict({}) is None
    assert MailboxState.from_dict({"uidvalidity": "5"}) == MailboxState(5, 0)


def test_seen_marks_are_remembered():
    store = MemoryStateStore()
    assert store.is_seen("x") is False
    store.mark_seen("x", 30)
    assert store.is_seen("x") is True
