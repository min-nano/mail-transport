"""常駐デーモン全体の結合テスト.

IMAP からの取得 → 重複排除 → Gmail への挿入 → SQLite への状態保存までを
ひとつながりで動かし、プロセスを再起動しても取りこぼしと二重取り込みが
起きないことを確かめる。
"""

from __future__ import annotations

import threading

from conftest import FakeGmail, FakeImapSource, FakeMailbox, build_raw, make_config
from mailtransport.daemon import run_daemon
from mailtransport.state import SqliteStateStore
from mailtransport.sync import state_key, sync_once


class NoopWatcher:
    """IDLE の代わり. 起動時の 1 回の同期だけを見たいので何もしない."""

    def __init__(self, route) -> None:
        self.route = route

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass


def run_one_cycle(config, store, source, gmail) -> None:
    """デーモンを 1 サイクルだけ回す (プロセスを 1 回起動したのと同じ)."""
    stop = threading.Event()

    def sync(cfg, st):
        report = sync_once(cfg, st, gmail_factory=lambda: gmail, source_factory=lambda: source)
        stop.set()
        return report

    run_daemon(config, store, stop=stop, sync_fn=sync, watcher_factory=NoopWatcher, max_cycles=1)


def test_daemon_forwards_new_mail_and_survives_a_restart(tmp_path):
    db_path = str(tmp_path / "state.db")
    config = make_config(
        state_backend="sqlite",
        state_db_path=db_path,
        safety_sync_seconds=0.05,
        debounce_seconds=0.0,
    )
    inbox, junk = FakeMailbox("INBOX"), FakeMailbox("Junk")
    source = FakeImapSource({"INBOX": inbox, "Junk": junk}, special_use={r"\Junk": "Junk"})
    gmail = FakeGmail()

    # 1 回目の起動: 既存メールは取り込まず同期位置だけ合わせる
    inbox.add(1, build_raw("<old@x>"))
    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)
    assert gmail.inserted == []

    # 稼働中に新着が届く
    inbox.add(2, build_raw("<new@x>"))
    junk.add(1, build_raw("<spam@x>"), flags=(r"\Seen",))
    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)

    assert [labels for _, labels in gmail.inserted] == [["INBOX", "UNREAD"], ["SPAM"]]

    # プロセスを再起動しても、取り込み済みのメールは二度と送られない
    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)
    assert len(gmail.inserted) == 2

    # 再起動後に届いた新着はきちんと転送される
    inbox.add(3, build_raw("<after-restart@x>"))
    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)
    assert len(gmail.inserted) == 3
    assert gmail.inserted[-1][1] == ["INBOX", "UNREAD"]


def test_state_file_is_reused_not_recreated(tmp_path):
    """状態ファイルが残っている限り、再起動でブートストラップし直さない."""
    db_path = str(tmp_path / "state.db")
    config = make_config(
        state_backend="sqlite",
        state_db_path=db_path,
        safety_sync_seconds=0.05,
        debounce_seconds=0.0,
    )
    inbox = FakeMailbox("INBOX")
    source = FakeImapSource({"INBOX": inbox}, special_use={r"\Junk": None})
    gmail = FakeGmail()

    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)
    inbox.add(1, build_raw("<a@x>"))
    run_one_cycle(config, SqliteStateStore(db_path), source, gmail)

    assert len(gmail.inserted) == 1
    state = SqliteStateStore(db_path).get_mailbox_state(state_key(config.icloud_username, "INBOX"))
    assert state is not None and state.last_uid == 1
