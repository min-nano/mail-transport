"""iCloud → Gmail 同期の中核ロジック."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time

from mailtransport.config import VERIFY_FORCE, VERIFY_OFF, Config, Route
from mailtransport.imap_source import ImapError, ImapSource, MessageMeta, message_id_of
from mailtransport.state import MailboxState, StateStore, new_holder_id

log = logging.getLogger(__name__)

LOCK_NAME = "sync"
_UNSAFE_DOC_ID = re.compile(r"[^A-Za-z0-9_.-]")

# 転送されずに iCloud へ残したメールに付ける理由。``--leftovers`` に出る。
REASON_TOO_LARGE = "too_large"
REASON_DUPLICATE = "duplicate"
REASON_FETCH_FAILED = "fetch_failed"
REASON_METADATA_MISSING = "metadata_missing"
REASON_UNVERIFIED = "unverified"
REASON_TRASH_FAILED = "trash_failed"
REASON_PRE_EXISTING = "pre_existing"


@dataclasses.dataclass
class RouteReport:
    source: str
    mailbox: str | None = None
    bootstrapped: bool = False
    scanned: int = 0
    forwarded: int = 0
    duplicates: int = 0
    skipped_too_large: int = 0
    missing_metadata: int = 0
    verified: int = 0
    unverified: int = 0
    kept_pre_existing: int = 0
    trashed: int = 0
    remaining: int = 0
    error: str | None = None
    trash_error: str | None = None

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
    def trashed(self) -> int:
        return sum(r.trashed for r in self.routes)

    @property
    def unverified(self) -> int:
        return sum(r.unverified for r in self.routes)

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
            "trashed": self.trashed,
            "unverified": self.unverified,
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
    # ゴミ箱へ移すには書き込み可能な SELECT が要る。DRY_RUN では iCloud 側を触らない。
    trashing = route.trashes(config.trash_after_forward) and not config.dry_run
    trash_uids: list[int] = []
    try:
        resolved = source.resolve_mailbox(route.source)
        if resolved is None:
            report.error = f"メールボックスが見つかりません: {route.source}"
            log.warning(report.error)
            return report
        mailbox = resolved
        report.mailbox = mailbox

        uidvalidity, uidnext = source.select(mailbox, readonly=not trashing)
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
        _report_missing_metadata(config, store, key, mailbox, batch, metas, report)

        # iCloud 側に触らないなら確認する必要もない (API 呼び出しを増やさない)
        verifier = _Verifier(gmail, config.verify_before_trash if trashing else VERIFY_OFF)
        try:
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
                    _leave_behind(
                        config, store, key, mailbox, meta.uid, REASON_TOO_LARGE, str(meta.size)
                    )
                    _advance(config, store, key, state, meta.uid)
                    continue

                raw = source.fetch_raw(meta.uid)
                if raw is None:
                    _leave_behind(config, store, key, mailbox, meta.uid, REASON_FETCH_FAILED)
                    _advance(config, store, key, state, meta.uid)
                    continue

                dkey = dedupe_key(config.icloud_username, raw)
                if store.is_seen(dkey):
                    log.info(
                        "取り込み済みのためスキップします",
                        extra={"extra_fields": {"uid": meta.uid, "mailbox": mailbox}},
                    )
                    report.duplicates += 1
                    _leave_behind(config, store, key, mailbox, meta.uid, REASON_DUPLICATE)
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
                if trashing:
                    decision = _decide_trash(config, state, meta, result, verifier)
                    if decision.verified:
                        report.verified += 1
                    if decision.trash:
                        # 移動はまとめて 1 コマンドで行うため、ここでは控えるだけにする
                        trash_uids.append(meta.uid)
                        continue
                    if decision.reason == REASON_PRE_EXISTING:
                        report.kept_pre_existing += 1
                    else:
                        report.unverified += 1
                    log.warning(
                        "転送済みですが iCloud に原本を残します",
                        extra={
                            "extra_fields": {
                                "uid": meta.uid,
                                "mailbox": mailbox,
                                "gmail_message_id": result.message_id,
                                "reason": decision.reason,
                                "detail": decision.detail,
                            }
                        },
                    )
                    _leave_behind(
                        config, store, key, mailbox, meta.uid, decision.reason, decision.detail
                    )
        finally:
            # 例外や時間切れで抜けても、転送済みのぶんは iCloud から片付ける
            _move_to_trash(config, store, source, key, trash_uids, mailbox, report)
    except Exception as exc:  # 1 経路の失敗で他の経路を止めない
        report.error = f"{type(exc).__name__}: {exc}"
        log.exception("経路 %s の同期に失敗しました", route.source)
    return report


@dataclasses.dataclass(frozen=True)
class _TrashDecision:
    """1 通を iCloud のゴミ箱へ移してよいかの判断."""

    trash: bool
    verified: bool = False
    reason: str = ""
    detail: str | None = None


class _Verifier:
    """挿入したメールを Gmail から読み戻して確認する.

    ``insert`` の応答は「受け付けた」ことしか示さない。iCloud の原本を消す前に
    Gmail 側で引き直し、実際に存在することを確かめる。

    トークンに ``gmail.metadata`` が無いと確認できない。``auto`` では 1 度だけ
    警告して従来どおり移す (insert だけで発行した既存のトークンでも動き続ける)。
    ``force`` では確認できないメールを iCloud に残す。
    """

    def __init__(self, gmail, mode: str) -> None:
        self._gmail = gmail
        self._mode = mode
        self._unavailable = gmail is None or not hasattr(gmail, "verify")
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self._mode != VERIFY_OFF

    def check(self, message_id: str) -> _TrashDecision:
        if not self.enabled:
            return _TrashDecision(trash=True)
        if self._unavailable:
            return self._without_verification("トークンに gmail.metadata がありません")

        from mailtransport.gmail_sink import GmailError, GmailScopeError

        try:
            found = self._gmail.verify(message_id)
        except GmailScopeError as exc:
            self._unavailable = True
            return self._without_verification(str(exc))
        except GmailError as exc:
            return _TrashDecision(trash=False, reason=REASON_UNVERIFIED, detail=str(exc))
        if found is None:
            return _TrashDecision(
                trash=False,
                reason=REASON_UNVERIFIED,
                detail=f"Gmail に {message_id} が見つかりません",
            )
        return _TrashDecision(trash=True, verified=True)

    def _without_verification(self, detail: str) -> _TrashDecision:
        """確認する手段が無いときの扱い."""
        if not self._warned:
            self._warned = True
            log.warning(
                "Gmail 側の確認ができません (%s)。"
                "tools/get_gmail_refresh_token.py でトークンを取り直すと、"
                "iCloud の原本を消す前に Gmail に入ったことを確認できます",
                detail,
            )
        if self._mode == VERIFY_FORCE:
            return _TrashDecision(trash=False, reason=REASON_UNVERIFIED, detail=detail)
        return _TrashDecision(trash=True)


def _decide_trash(
    config: Config,
    state: MailboxState,
    meta: MessageMeta,
    result,
    verifier: _Verifier,
) -> _TrashDecision:
    """転送できた 1 通を iCloud のゴミ箱へ移してよいか決める."""
    if (
        state.import_floor
        and meta.uid <= state.import_floor
        and not config.trash_existing_on_initial_import
    ):
        # 稼働開始前から溜まっていたメール。Gmail 側を人が確認する前に
        # 受信トレイが空になるのを避けるため、移動は利用者の判断に委ねる。
        return _TrashDecision(
            trash=False,
            reason=REASON_PRE_EXISTING,
            detail=f"初回取り込みの対象 (UID <= {state.import_floor})",
        )
    return verifier.check(result.message_id)


def _report_missing_metadata(
    config: Config,
    store: StateStore,
    key: str,
    mailbox: str,
    batch: list[int],
    metas: list[MessageMeta],
    report: RouteReport,
) -> None:
    """``FETCH`` の応答に出てこなかった UID を黙って捨てない.

    同期位置は後続の UID で追い越されるため、記録しないと二度と見に来ない。
    """
    seen = {meta.uid for meta in metas}
    missing = [uid for uid in batch if uid not in seen]
    if not missing:
        return
    report.missing_metadata = len(missing)
    log.warning(
        "メタデータを取得できなかった UID があります (iCloud に残ります)",
        extra={"extra_fields": {"mailbox": mailbox, "uids": missing}},
    )
    for uid in missing:
        _leave_behind(config, store, key, mailbox, uid, REASON_METADATA_MISSING)


def _leave_behind(
    config: Config,
    store: StateStore,
    key: str,
    mailbox: str,
    uid: int,
    reason: str,
    detail: str | None = None,
) -> None:
    """iCloud 側に残したメールを理由付きで控える (``--leftovers`` で見る)."""
    if config.dry_run:
        return
    store.record_leftover(key, uid, mailbox, reason, detail)


def _move_to_trash(
    config: Config,
    store: StateStore,
    source: ImapSource,
    key: str,
    uids: list[int],
    mailbox: str,
    report: RouteReport,
) -> None:
    """転送し終えたメールを iCloud のゴミ箱へ移す.

    iCloud の容量を空けるための後始末。Gmail への取り込みは済んでいるので、
    ここで失敗しても転送そのものは成功扱いにする (次回の実行では同期位置が
    先に進んでいるため、移し損ねたメールは iCloud に残る)。
    """
    if not uids:
        return

    moved_total = 0

    def count(moved: int) -> None:
        # 分割して送るので、途中で失敗しても移せたぶんは報告に残す
        nonlocal moved_total
        moved_total += moved
        report.trashed += moved

    pending = list(uids)
    try:
        trash = source.resolve_mailbox(config.trash_mailbox)
        if trash is None:
            raise ImapError(f"ゴミ箱が見つかりません: {config.trash_mailbox}")
        if trash == mailbox:
            raise ImapError(f"ゴミ箱が転送元と同じです: {trash}")
        source.move_uids(uids, trash, on_moved=count)
    except Exception as exc:
        report.trash_error = f"{type(exc).__name__}: {exc}"
        # 分割送信の途中で落ちた場合、先頭から moved_total 通までは移動済み
        for uid in pending[moved_total:]:
            _leave_behind(config, store, key, mailbox, uid, REASON_TRASH_FAILED, report.trash_error)
        log.warning(
            "転送済みメールをゴミ箱へ移しきれませんでした (移せなかったぶんは iCloud に残ります)",
            extra={
                "extra_fields": {
                    "mailbox": mailbox,
                    "uids": pending,
                    "moved": moved_total,
                    "error": report.trash_error,
                }
            },
        )
        return
    finally:
        uids.clear()
    log.info(
        "転送済みメールをゴミ箱へ移しました",
        extra={
            "extra_fields": {
                "mailbox": mailbox,
                "trash": trash,
                "count": moved_total,
            }
        },
    )


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
    here = max(uidnext - 1, 0)
    import_floor = 0
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
        start = here
        if not config.dry_run:
            # 古い UID を指したままの記録は意味を失う
            store.clear_leftovers(key)
    elif config.initial_import == "all":
        start = 0
        # ここより下は稼働開始前から iCloud にあったメール。転送はするが、
        # Gmail 側に全部入ったことを人が確認する前に受信トレイが空になると
        # 取り返しがつかないので、既定ではゴミ箱へ移さない。
        import_floor = here
    else:
        start = here

    state = MailboxState(uidvalidity=uidvalidity, last_uid=start, import_floor=import_floor)
    if not config.dry_run:
        store.put_mailbox_state(key, state)
    log.info(
        "同期位置を初期化しました",
        extra={
            "extra_fields": {
                "mailbox": mailbox,
                "uidvalidity": uidvalidity,
                "last_uid": start,
                "import_floor": import_floor,
            }
        },
    )
    if import_floor and not config.trash_existing_on_initial_import:
        log.info(
            "既存メール (UID <= %s) は転送しますが iCloud のゴミ箱へは移しません。"
            "Gmail 側で件数を確認したあと、iCloud の受信トレイは手で整理してください "
            "(TRASH_EXISTING_ON_INITIAL_IMPORT=true で移動させることもできます)",
            import_floor,
        )
    return state


def _advance(config: Config, store: StateStore, key: str, state: MailboxState, uid: int) -> None:
    """1 通処理するたびに位置を確定させ、途中終了時の再処理を最小化する."""
    if uid <= state.last_uid:
        return
    state.last_uid = uid
    if not config.dry_run:
        store.put_mailbox_state(key, state)
