"""常駐デーモン (IDLE 監視 + 同期ループ) のテスト."""

from __future__ import annotations

import threading

from conftest import make_config
from mailtransport.config import Route
from mailtransport.daemon import MailboxWatcher, run_daemon
from mailtransport.imap_source import ImapError
from mailtransport.state import MemoryStateStore
from mailtransport.sync import RouteReport, SyncReport

# テストが実サーバーを待たないよう、定期同期の間隔は極小にしておく
FAST = {"safety_sync_seconds": 0.05, "debounce_seconds": 0.0}


def report(forwarded: int = 0, has_more: bool = False) -> SyncReport:
    result = SyncReport()
    result.routes.append(
        RouteReport(source="INBOX", forwarded=forwarded, remaining=5 if has_more else 0)
    )
    return result


class RecordingSync:
    """呼ばれた回数を数え、指定回数に達したら停止イベントを立てる同期関数の代役."""

    def __init__(self, stop: threading.Event, stop_after: int, results=None) -> None:
        self.stop = stop
        self.stop_after = stop_after
        self.results = list(results or [])
        self.calls = 0

    def __call__(self, config, store):
        self.calls += 1
        result = self.results.pop(0) if self.results else report()
        if self.calls >= self.stop_after:
            self.stop.set()
        if isinstance(result, Exception):
            raise result
        return result


class FakeWatcher:
    def __init__(self, route: Route) -> None:
        self.route = route
        self.started = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def join(self, timeout=None) -> None:
        self.joined = True


def run(config, sync, stop, watchers: list | None = None, store=None) -> int:
    def factory(route: Route) -> FakeWatcher:
        watcher = FakeWatcher(route)
        if watchers is not None:
            watchers.append(watcher)
        return watcher

    return run_daemon(
        config,
        store if store is not None else MemoryStateStore(),
        stop=stop,
        sync_fn=sync,
        watcher_factory=factory,
        max_cycles=10,
    )


def test_daemon_syncs_once_at_startup():
    """起動直後に、止まっていた間に届いたメールを取り込む."""
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=1)

    assert run(make_config(**FAST), sync, stop) == 0
    assert sync.calls == 1


def test_daemon_keeps_going_when_a_sync_fails():
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=2, results=[RuntimeError("一時的な失敗")])

    exit_code = run(make_config(**FAST), sync, stop)

    assert sync.calls == 2  # 失敗しても次の契機で再試行する
    assert exit_code == 1


def test_daemon_continues_immediately_when_mail_remains():
    """1 回で処理しきれなかったぶんは定期同期を待たずに続けて処理する."""
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=3, results=[report(has_more=True), report(has_more=True)])
    # 定期同期に頼れない長さにしておき、has_more による再実行だけで進むことを見る
    config = make_config(safety_sync_seconds=3600, debounce_seconds=0.0)

    run(config, sync, stop)

    assert sync.calls == 3


def test_daemon_starts_and_joins_every_watcher():
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=1)
    watchers: list[FakeWatcher] = []
    routes = (Route("INBOX", ("INBOX",)), Route("Archive", ("ARCHIVE_X",)))

    run(make_config(routes=routes, **FAST), sync, stop, watchers)

    assert [w.route.source for w in watchers] == ["INBOX", "Archive"]
    assert all(w.started and w.joined for w in watchers)
    assert stop.is_set()


def test_expired_dedupe_records_are_purged():
    class PurgingStore(MemoryStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.purges = 0

        def purge_expired(self) -> int:
            self.purges += 1
            return 0

    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=1)
    store = PurgingStore()

    run_daemon(
        make_config(**FAST),
        store,
        stop=stop,
        sync_fn=sync,
        watcher_factory=FakeWatcher,
        max_cycles=1,
    )

    assert store.purges == 1


# --- MailboxWatcher -------------------------------------------------------


class FakeIdleSource:
    def __init__(self, mailbox="INBOX", idle=True, events=(), fail_after=None, stop=None) -> None:
        self.mailbox = mailbox
        self._idle = idle
        self.events = list(events)
        self.fail_after = fail_after
        self._stop = stop
        self.selected = None
        self.idle_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resolve_mailbox(self, source):
        return self.mailbox

    def select(self, mailbox):
        self.selected = mailbox
        return (1, 1)

    def has_capability(self, name):
        return self._idle and name.upper() == "IDLE"

    def idle_wait(self, timeout, poll_step=30.0):
        self.idle_calls += 1
        if self.fail_after is not None and self.idle_calls > self.fail_after:
            raise ImapError("接続が切れました")
        if self.events:
            return self.events.pop(0)
        if self._stop is not None:
            self._stop.set()  # 出し切ったら監視を終える (テストが空回りしないように)
        return False


def start_watcher(config, route, source, stop, trigger):
    watcher = MailboxWatcher(config, route, trigger, stop, lambda: source)
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    return watcher, thread


def test_watcher_triggers_a_sync_on_new_mail():
    stop, trigger = threading.Event(), threading.Event()
    source = FakeIdleSource(events=[False, True], stop=stop)
    watcher, thread = start_watcher(
        make_config(), Route("INBOX", ("INBOX",)), source, stop, trigger
    )

    assert trigger.wait(timeout=5) is True
    stop.set()
    thread.join(timeout=5)
    assert watcher.idle_supported is True
    assert source.selected == "INBOX"


def test_watcher_falls_back_to_polling_without_idle():
    """IDLE 非対応のサーバーでは接続を保持せず定期的に同期を促す."""
    stop, trigger = threading.Event(), threading.Event()
    source = FakeIdleSource(idle=False)
    watcher, thread = start_watcher(
        make_config(poll_interval_seconds=0.01), Route("INBOX", ("INBOX",)), source, stop, trigger
    )

    assert trigger.wait(timeout=5) is True
    stop.set()
    thread.join(timeout=5)
    assert watcher.idle_supported is False
    assert source.idle_calls == 0


def test_watcher_requests_a_sync_after_a_disconnect():
    """接続が切れている間の新着を取りこぼさないよう、復帰時に同期させる."""
    stop, trigger = threading.Event(), threading.Event()
    source = FakeIdleSource(fail_after=0)
    watcher, thread = start_watcher(
        make_config(reconnect_backoff_max_seconds=1),
        Route("INBOX", ("INBOX",)),
        source,
        stop,
        trigger,
    )

    assert trigger.wait(timeout=5) is True
    stop.set()
    thread.join(timeout=5)
    assert watcher.failures >= 1


def test_watcher_treats_a_missing_mailbox_as_a_failure():
    stop, trigger = threading.Event(), threading.Event()
    source = FakeIdleSource(mailbox=None)
    watcher, thread = start_watcher(
        make_config(reconnect_backoff_max_seconds=1),
        Route(r"\Archive", ("ARCHIVE_X",)),
        source,
        stop,
        trigger,
    )

    assert trigger.wait(timeout=5) is True
    stop.set()
    thread.join(timeout=5)
    assert watcher.failures >= 1


# --- デプロイ時の再起動まわり ---------------------------------------------


def test_stale_lock_from_a_killed_process_is_released_at_startup():
    """強制終了で残ったロックのせいで、再起動後の同期が止まらないこと.

    デプロイのたびにプロセスを入れ替えるので、途中で SIGKILL された場合に
    LOCK_TTL_SECONDS のあいだ何も転送できなくなると実害が大きい。
    """
    store = MemoryStateStore()
    store.acquire_lock("sync", 600, "killed-process")  # 前回のプロセスが握ったまま
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=1)

    run(make_config(**FAST), sync, stop, store=store)

    assert sync.calls == 1
    assert store.acquire_lock("sync", 60, "anyone") is True


def test_shutdown_is_prompt_even_between_scheduled_syncs():
    """SIGTERM から実際に止まるまで定期同期の間隔ぶん待たされないこと.

    待たされると systemd の停止待ちがタイムアウトし、強制終了されて
    ロックが残る (上のテストの状況を招く)。
    """
    stop = threading.Event()
    sync = RecordingSync(stop, stop_after=999)  # 自分では止まらない
    # 定期同期の間隔を長く取り、停止要求だけで抜けられることを見る
    config = make_config(safety_sync_seconds=3600, debounce_seconds=0.0)

    threading.Timer(0.2, stop.set).start()
    finished = threading.Event()

    def go():
        run(config, sync, stop)
        finished.set()

    threading.Thread(target=go, daemon=True).start()

    assert finished.wait(timeout=10) is True


def test_force_release_reports_whether_a_lock_existed():
    store = MemoryStateStore()

    assert store.force_release_lock("sync") is False
    store.acquire_lock("sync", 600, "someone")
    assert store.force_release_lock("sync") is True
