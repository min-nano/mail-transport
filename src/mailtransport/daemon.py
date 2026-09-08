"""常時起動ホスト (GCE e2-micro など) で動かす常駐プロセス.

メールボックスごとに IMAP IDLE の接続を張り、新着通知を受けたら同期を走らせる。
Cloud Scheduler のポーリングと違い、到着から数秒で Gmail に届く。

IDLE の通知は取りこぼすことがある (接続断、サーバー側の都合) ため、
``SAFETY_SYNC_SECONDS`` ごとの定期同期を保険として併走させる。
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from mailtransport.config import Config, Route
from mailtransport.imap_source import ImapError, ImapSource
from mailtransport.state import StateStore
from mailtransport.sync import LOCK_NAME, sync_once

log = logging.getLogger(__name__)


class MailboxWatcher(threading.Thread):
    """1 つのメールボックスを監視し、変化があれば ``trigger`` を立てるスレッド.

    IMAP は 1 接続につき 1 メールボックスしか SELECT できないので、
    受信トレイと迷惑メールはそれぞれ別接続で待ち受ける。
    """

    def __init__(
        self,
        config: Config,
        route: Route,
        trigger: threading.Event,
        stop: threading.Event,
        source_factory=None,
    ) -> None:
        super().__init__(name=f"watcher-{route.source}", daemon=True)
        self._config = config
        self._route = route
        self._trigger = trigger
        self._stop = stop
        self._source_factory = source_factory or (lambda: _build_source(config))
        self.idle_supported: bool | None = None
        self.failures = 0

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._watch_once()
                backoff = 1.0
            except Exception as exc:
                self.failures += 1
                log.warning(
                    "監視接続が切れました。再接続します",
                    extra={
                        "extra_fields": {
                            "mailbox": self._route.source,
                            "error": f"{type(exc).__name__}: {exc}",
                            "retry_in": backoff,
                        }
                    },
                )
                # 接続断の直後は新着を取りこぼしている可能性があるので同期させる
                self._trigger.set()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self._config.reconnect_backoff_max_seconds)

    def _watch_once(self) -> None:
        with self._source_factory() as source:
            mailbox = source.resolve_mailbox(self._route.source)
            if mailbox is None:
                raise ImapError(f"メールボックスが見つかりません: {self._route.source}")
            source.select(mailbox)

            self.idle_supported = self._config.idle_enabled and source.has_capability("IDLE")
            if not self.idle_supported:
                # IDLE が使えないサーバー向けのフォールバック。接続を保持せず
                # 一定間隔で同期を促すだけにする。
                log.info(
                    "IDLE が使えないため定期ポーリングに切り替えます",
                    extra={"extra_fields": {"mailbox": mailbox}},
                )
                while not self._stop.is_set():
                    self._stop.wait(self._config.poll_interval_seconds)
                    if not self._stop.is_set():
                        self._trigger.set()
                return

            log.info("IDLE で新着を待機します", extra={"extra_fields": {"mailbox": mailbox}})
            while not self._stop.is_set():
                if source.idle_wait(self._config.idle_refresh_seconds):
                    log.info("新着の通知を受けました", extra={"extra_fields": {"mailbox": mailbox}})
                    self._trigger.set()


def _build_source(config: Config) -> ImapSource:
    return ImapSource(
        host=config.icloud_host,
        port=config.icloud_port,
        username=config.icloud_username,
        password=config.icloud_app_password,
        timeout=config.imap_timeout_seconds,
    )


def run_daemon(
    config: Config,
    store: StateStore,
    stop: threading.Event | None = None,
    sync_fn=sync_once,
    watcher_factory=None,
    max_cycles: int | None = None,
) -> int:
    """常駐ループ本体. 戻り値はプロセスの終了コード."""
    stop = stop or threading.Event()
    trigger = threading.Event()

    if watcher_factory is None:

        def watcher_factory(route: Route) -> MailboxWatcher:
            return MailboxWatcher(config, route, trigger, stop)

    watchers = [watcher_factory(route) for route in config.routes]
    for watcher in watchers:
        watcher.start()

    log.info(
        "常駐モードを開始しました",
        extra={
            "extra_fields": {
                "routes": [r.source for r in config.routes],
                "safety_sync_seconds": config.safety_sync_seconds,
                "idle_enabled": config.idle_enabled,
            }
        },
    )

    # 前回のプロセスが強制終了 (デプロイ時の再起動など) されると、同期ロックが
    # 期限切れまで残って新しいプロセスが何もできなくなる。常駐プロセスは 1 台に
    # 1 つしか動かないので、起動時に必ず外す。
    if _force_release_stale_lock(store):
        log.warning("前回の実行が残したロックを解放しました")

    exit_code = 0
    cycles = 0
    last_purge = 0.0
    trigger.set()  # 起動直後に取りこぼしぶんを取り込む
    try:
        while not stop.is_set():
            fired = _wait_for_work(trigger, stop, config.safety_sync_seconds)
            if stop.is_set():
                break
            trigger.clear()
            if fired and config.debounce_seconds > 0:
                # 数通が続けて届いたときに 1 回の同期でまとめて処理する
                stop.wait(config.debounce_seconds)
                trigger.clear()
                if stop.is_set():
                    break

            try:
                report = sync_fn(config, store)
                if report.forwarded or report.failed or report.locked_out:
                    log.info("同期が完了しました", extra={"extra_fields": report.to_dict()})
                else:
                    log.debug("同期が完了しました", extra={"extra_fields": report.to_dict()})
                if report.has_more:
                    # 1 回で処理しきれなかったぶんを続けて処理する
                    trigger.set()
            except Exception:
                exit_code = 1
                log.exception("同期に失敗しました。次の契機で再試行します")

            last_purge = _maybe_purge(store, last_purge)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
    finally:
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        log.info("常駐モードを終了しました")
    return exit_code


# 停止要求 (SIGTERM) に気付くまでの最大の遅れ
_STOP_CHECK_SECONDS = 1.0
_PURGE_INTERVAL_SECONDS = 6 * 60 * 60


def _wait_for_work(
    trigger: threading.Event,
    stop: threading.Event,
    timeout: float,
    step: float = _STOP_CHECK_SECONDS,
) -> bool:
    """新着の通知を待つ. 停止要求が来たら待たずに戻る.

    ``trigger.wait(timeout)`` だけだと停止要求に気付くのが定期同期の間隔ぶん
    遅れてしまい、systemd の停止待ちがタイムアウトして強制終了されてしまう。
    """
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if trigger.wait(min(step, remaining)):
            return True
    return False


def _force_release_stale_lock(store: StateStore) -> bool:
    force_release = getattr(store, "force_release_lock", None)
    if force_release is None:
        return False
    try:
        return bool(force_release(LOCK_NAME))
    except Exception:  # pragma: no cover - 解放できなくても TTL 切れで回復する
        log.warning("残留ロックの解放に失敗しました", exc_info=True)
        return False


def _maybe_purge(store: StateStore, last_purge: float) -> float:
    """重複排除レコードの掃除 (SQLite のときだけ意味がある)."""
    purge = getattr(store, "purge_expired", None)
    if purge is None:
        return last_purge
    now = time.monotonic()
    if last_purge and now - last_purge < _PURGE_INTERVAL_SECONDS:
        return last_purge
    try:
        removed = purge()
        if removed:
            log.info("期限切れの重複排除レコードを削除しました: %s 件", removed)
    except Exception:  # pragma: no cover - 掃除の失敗で止める理由はない
        log.warning("重複排除レコードの掃除に失敗しました", exc_info=True)
    return now


def install_signal_handlers(stop: threading.Event) -> None:
    """systemd からの SIGTERM で綺麗に止まるようにする."""

    def _handle(signum, _frame):
        log.info("シグナル %s を受け取りました。停止します", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except ValueError:  # pragma: no cover - メインスレッド以外では設定できない
            pass
