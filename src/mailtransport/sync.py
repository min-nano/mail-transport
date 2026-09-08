"""iCloud → Gmail 同期の中核ロジック."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time

from mailtransport.config import Config, Route
from mailtransport.imap_source import ImapSource, MessageMeta, message_id_of
from mailtransport.state import MailboxState, StateStore, new_holder_id

log = logging.getLogger(__name__)

LOCK_NAME = "sync"
_UNSAFE_DOC_ID = re.compile(r"[^A-Za-z0-9_.-]")


@dataclasses.dataclass
class RouteReport:
    source: str
    mailbox: str | None = None
    bootstrapped: bool = False
    scanned: int = 0
    forwarded: int = 0
    duplicates: int = 0
    skipped_too_large: int = 0
    remaining: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SyncReport:
    routes: list[RouteReport] = dataclasses.field(default_factory=list)
    locked_out: bool = False
    duration_seconds: float = 0.0

    @property
    def forwarded(self) -> int:
        return sum(r.forwarded for r in self.routes)

    @property
    def has_more(self) -> bool:
        """1 回の実行で処理しきれなかったメールが残っているか."""
        return any(r.remaining > 0 for r in self.routes)

    @property
    def failed(self) -> bool:
        return any(r.error for r in self.routes)

    def to_dict(self) -> dict:
        return {
            "forwarded": self.forwarded,
            "locked_out": self.locked_out,
            "has_more": self.has_more,
            "duration_seconds": round(self.duration_seconds, 3),
            "routes": [r.to_dict() for r in self.routes],
        }


def state_key(username: str, mailbox: str) -> str:
    """状態ストアのキーとして安全な文字列を作る."""
    digest = hashlib.sha256(f"{username}\x00{mailbox}".encode()).hexdigest()[:16]
    readable = _UNSAFE_DOC_ID.sub("_", f"{username}__{mailbox}")[:100]
    return f"{readable}__{digest}"


def dedupe_key(username: str, raw: bytes) -> str:
    """同一メールを二重に取り込まないためのキー.

    Message-ID があればそれを、無ければ本文全体のハッシュを使う。
    Gmail の insert は冪等ではないため、この判定が二重取り込みの最後の砦になる。
    """
    mid = message_id_of(raw)
    basis = mid.encode("utf-8", errors="replace") if mid else hashlib.sha256(raw).digest()
    return hashlib.sha256(username.encode() + b"\x00" + basis).hexdigest()


def _labels_for(route: Route, meta: MessageMeta) -> list[str]:
    labels = list(route.labels)
    # iCloud 側で未読のものは Gmail でも未読にする
    if not meta.seen:
        labels.append("UNREAD")
    if meta.flagged and "STARRED" not in labels:
        labels.append("STARRED")
    return labels


def sync_once(
    config: Config,
    store: StateStore,
    gmail_factory=None,
    source_factory=None,
) -> SyncReport:
    """1 回分の同期を実行する.

    1 回あたりの処理量と実行時間に上限を設け、大量のメールが溜まっていても
    ひとつの同期に居座らないようにしている。処理しきれなかった分は次の契機で
    継続される (``SyncReport.has_more`` が立つ)。
    """
    started = time.monotonic()
    deadline = started + config.run_budget_seconds
    report = SyncReport()

    holder = new_holder_id()
    if not store.acquire_lock(LOCK_NAME, config.lock_ttl_seconds, holder):
        log.info("別の同期が実行中のためスキップします")
        report.locked_out = True
        report.duration_seconds = time.monotonic() - started
        return report

    try:
        gmail = None
        if not config.dry_run:
            if gmail_factory is None:
                from mailtransport.gmail_sink import GmailSink

                def gmail_factory():  # noqa: E306 - 遅延生成でトークン取得を 1 回に抑える
                    return GmailSink(config.gmail_auth, config.gmail_user_id)

            gmail = gmail_factory()

        if source_factory is None:

            def source_factory():
                return ImapSource(
                    host=config.icloud_host,
                    port=config.icloud_port,
                    username=config.icloud_username,
                    password=config.icloud_app_password,
                    timeout=config.imap_timeout_seconds,
                )

        with source_factory() as source:
            for route in config.routes:
                route_report = _sync_route(config, store, source, gmail, route, deadline)
                report.routes.append(route_report)
    finally:
        store.release_lock(LOCK_NAME, holder)

    report.duration_seconds = time.monotonic() - started
    return report


def _sync_route(
    config: Config,
    store: StateStore,
    source: ImapSource,
    gmail,
    route: Route,
    deadline: float,
) -> RouteReport:
    report = RouteReport(source=route.source)
    try:
        mailbox = source.resolve_mailbox(route.source)
        if mailbox is None:
            report.error = f"メールボックスが見つかりません: {route.source}"
            log.warning(report.error)
            return report
        report.mailbox = mailbox

        uidvalidity, uidnext = source.select(mailbox)
        key = state_key(config.icloud_username, mailbox)
        state = store.get_mailbox_state(key)

        if state is None or state.uidvalidity != uidvalidity:
            state = _bootstrap(config, store, key, state, uidvalidity, uidnext, mailbox)
            report.bootstrapped = True
            if config.initial_import != "all":
                return report

        uids = source.search_uids_after(state.last_uid)
        report.scanned = len(uids)
        if not uids:
            return report

        batch = uids[: config.max_messages_per_run]
        report.remaining = len(uids) - len(batch)
        metas = source.fetch_metadata(batch)

        for meta in metas:
            if time.monotonic() >= deadline:
                log.info("実行時間の上限に達したため中断します (次回の実行で継続)")
                report.remaining += len(batch) - batch.index(meta.uid)
                break

            if meta.size and meta.size > config.max_message_bytes:
                log.warning(
                    "サイズ上限を超えるメールをスキップします",
                    extra={
                        "extra_fields": {"uid": meta.uid, "size": meta.size, "mailbox": mailbox}
                    },
                )
                report.skipped_too_large += 1
                _advance(config, store, key, state, meta.uid)
                continue

            raw = source.fetch_raw(meta.uid)
            if raw is None:
                _advance(config, store, key, state, meta.uid)
                continue

            dkey = dedupe_key(config.icloud_username, raw)
            if store.is_seen(dkey):
                log.info(
                    "取り込み済みのためスキップします",
                    extra={"extra_fields": {"uid": meta.uid, "mailbox": mailbox}},
                )
                report.duplicates += 1
                _advance(config, store, key, state, meta.uid)
                continue

            labels = _labels_for(route, meta)
            if config.dry_run:
                log.info(
                    "[DRY_RUN] 転送をスキップしました",
                    extra={
                        "extra_fields": {
                            "uid": meta.uid,
                            "mailbox": mailbox,
                            "labels": labels,
                            "size": meta.size,
                        }
                    },
                )
                report.forwarded += 1
                state.last_uid = meta.uid
                continue

            result = gmail.insert(raw, labels)
            # 先に「取り込み済み」を記録してから位置を進める。逆順だと、間で
            # 落ちたときに同じメールを二重取り込みしてしまう。
            store.mark_seen(dkey, config.seen_retention_days)
            _advance(config, store, key, state, meta.uid)
            report.forwarded += 1
            log.info(
                "メールを転送しました",
                extra={
                    "extra_fields": {
                        "uid": meta.uid,
                        "mailbox": mailbox,
                        "labels": labels,
                        "gmail_message_id": result.message_id,
                        "size": meta.size,
                    }
                },
            )
    except Exception as exc:  # 1 経路の失敗で他の経路を止めない
        report.error = f"{type(exc).__name__}: {exc}"
        log.exception("経路 %s の同期に失敗しました", route.source)
    return report


def _bootstrap(
    config: Config,
    store: StateStore,
    key: str,
    previous: MailboxState | None,
    uidvalidity: int,
    uidnext: int,
    mailbox: str,
) -> MailboxState:
    """初回、または UIDVALIDITY 変更時の同期位置を決める."""
    if previous is not None:
        # UIDVALIDITY が変わると UID の対応関係が失われる。過去分を全部取り込むと
        # 大量の重複を生むため、現在地から再開して警告だけ出す。
        log.warning(
            "UIDVALIDITY が変わりました。現在位置から再開します",
            extra={
                "extra_fields": {
                    "mailbox": mailbox,
                    "old": previous.uidvalidity,
                    "new": uidvalidity,
                }
            },
        )
        start = max(uidnext - 1, 0)
    elif config.initial_import == "all":
        start = 0
    else:
        start = max(uidnext - 1, 0)

    state = MailboxState(uidvalidity=uidvalidity, last_uid=start)
    if not config.dry_run:
        store.put_mailbox_state(key, state)
    log.info(
        "同期位置を初期化しました",
        extra={"extra_fields": {"mailbox": mailbox, "uidvalidity": uidvalidity, "last_uid": start}},
    )
    return state


def _advance(config: Config, store: StateStore, key: str, state: MailboxState, uid: int) -> None:
    """1 通処理するたびに位置を確定させ、途中終了時の再処理を最小化する."""
    if uid <= state.last_uid:
        return
    state.last_uid = uid
    if not config.dry_run:
        store.put_mailbox_state(key, state)
