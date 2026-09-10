"""SQLite 状態ストアのテスト."""

from __future__ import annotations

import os
import sqlite3
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


def test_seen_records_expire(store, tmp_path):
    store.mark_seen("keep", 30)
    # 期限切れのレコードは時間を進めないと作れないので、直接書き込む
    with sqlite3.connect(store._path) as conn:
        conn.execute("INSERT INTO seen (key, expire_at) VALUES ('gone', 1.0)")

    assert store.is_seen("keep") is True
    assert store.is_seen("gone") is False  # 期限切れは未取り込み扱い
    assert store.purge_expired() == 1
    assert store.is_seen("keep") is True


def test_seen_records_can_be_kept_forever(store):
    """保持日数 0 は無期限.

    期限切れで「転送済み」の記録が消えると、利用者が受信トレイへ戻した
    メールが再転送され、再びゴミ箱へ移されてしまう (#24)。
    """
    store.mark_seen("forever", 0)

    assert store.is_seen("forever") is True
    assert store.purge_expired() == 0
    assert store.is_seen("forever") is True


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


def test_import_floor_is_persisted(store):
    store.put_mailbox_state("k", MailboxState(uidvalidity=1, last_uid=0, import_floor=120))

    assert store.get_mailbox_state("k") == MailboxState(1, 0, 120)


def test_old_database_gains_the_import_floor_column(tmp_path):
    """VM を作り直さずに更新できること. 既存の state.db に列を足す."""
    path = str(tmp_path / "state.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE mailbox_state (
                key         TEXT PRIMARY KEY,
                uidvalidity INTEGER NOT NULL,
                last_uid    INTEGER NOT NULL,
                updated_at  TEXT NOT NULL
            );
            INSERT INTO mailbox_state VALUES ('k', 7, 42, '2026-01-01T00:00:00+00:00');
            """
        )

    store = SqliteStateStore(path)

    assert store.get_mailbox_state("k") == MailboxState(uidvalidity=7, last_uid=42, import_floor=0)
    store.put_mailbox_state("k", MailboxState(7, 43, 42))
    assert store.get_mailbox_state("k") == MailboxState(7, 43, 42)


def test_leftovers_are_recorded_and_listed(store):
    store.record_leftover("k", 2, "INBOX", "too_large", "40MB")
    store.record_leftover("k", 1, "INBOX", "duplicate")
    store.record_leftover("other", 9, "Archive", "unverified")

    rows = store.list_leftovers("k")
    assert [(row.uid, row.reason, row.detail) for row in rows] == [
        (2, "too_large", "40MB"),
        (1, "duplicate", None),
    ]
    assert len(store.list_leftovers()) == 3


def test_leftovers_are_not_silently_truncated(store):
    """既定で全件返す.

    手で受信トレイを整理する前に見るものなので、黙って切り詰めると
    「一覧に無い＝転送済み」と読み違えられる。
    """
    for uid in range(600):
        store.record_leftover("k", uid, "INBOX", "duplicate")

    assert len(store.list_leftovers("k")) == 600
    assert len(store.list_leftovers("k", limit=10)) == 10


def test_leftover_is_updated_not_duplicated(store):
    store.record_leftover("k", 1, "INBOX", "unverified")
    store.record_leftover("k", 1, "INBOX", "trash_failed")

    rows = store.list_leftovers("k")
    assert [(row.uid, row.reason) for row in rows] == [(1, "trash_failed")]


def test_leftovers_can_be_cleared_per_mailbox(store):
    store.record_leftover("k", 1, "INBOX", "duplicate")
    store.record_leftover("other", 1, "Archive", "duplicate")

    assert store.clear_leftovers("k") == 1
    assert store.list_leftovers("k") == []
    assert len(store.list_leftovers("other")) == 1
