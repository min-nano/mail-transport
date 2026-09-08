"""SQLite 状態ストアのテスト."""

from __future__ import annotations

import os
import threading

import pytest

from mailtransport.state import MailboxState, SqliteStateStore


@pytest.fixture
def store(tmp_path):
    # 途中のディレクトリごと作られること
    return SqliteStateStore(str(tmp_path / "nested" / "state.db"))


def test_state_survives_a_restart(tmp_path):
    path = str(tmp_path / "state.db")
    SqliteStateStore(path).put_mailbox_state("k", MailboxState(uidvalidity=7, last_uid=42))

    reopened = SqliteStateStore(path)
    assert reopened.get_mailbox_state("k") == MailboxState(uidvalidity=7, last_uid=42)


def test_missing_state_is_none(store):
    assert store.get_mailbox_state("nope") is None


def test_state_is_overwritten_not_duplicated(store):
    store.put_mailbox_state("k", MailboxState(1, 1))
    store.put_mailbox_state("k", MailboxState(1, 2))

    assert store.get_mailbox_state("k") == MailboxState(1, 2)


def test_lock_is_exclusive_and_expires(store):
    assert store.acquire_lock("sync", 60, "a") is True
    assert store.acquire_lock("sync", 60, "b") is False

    store.release_lock("sync", "b")  # 持ち主でないので解放されない
    assert store.acquire_lock("sync", 60, "b") is False

    store.release_lock("sync", "a")
    assert store.acquire_lock("sync", 60, "b") is True

    # 期限切れのロックは奪える (プロセスが落ちた場合の復旧)
    assert store.acquire_lock("other", 0, "dead") is True
    assert store.acquire_lock("other", 60, "alive") is True


def test_only_one_thread_wins_the_lock(store):
    winners = []
    barrier = threading.Barrier(8)

    def contend(i):
        barrier.wait()
        if store.acquire_lock("sync", 60, f"h{i}"):
            winners.append(i)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


def test_seen_records_expire(store):
    store.mark_seen("keep", 30)
    store.mark_seen("gone", -1)

    assert store.is_seen("keep") is True
    assert store.is_seen("gone") is False  # 期限切れは未取り込み扱い
    assert store.purge_expired() == 1
    assert store.is_seen("keep") is True


def test_database_file_is_created_on_disk(tmp_path):
    path = str(tmp_path / "state.db")
    SqliteStateStore(path).mark_seen("x", 1)

    assert os.path.exists(path)


def test_force_release_breaks_a_lock_left_by_a_killed_process(store):
    """デプロイ時にプロセスが強制終了されても、次の起動が止まらないこと."""
    store.acquire_lock("sync", 600, "killed-process")
    assert store.acquire_lock("sync", 60, "new-process") is False

    assert store.force_release_lock("sync") is True
    assert store.acquire_lock("sync", 60, "new-process") is True
    assert store.force_release_lock("nothing-here") is False
