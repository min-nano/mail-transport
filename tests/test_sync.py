from __future__ import annotations

import pytest

from conftest import (
    FakeGmail,
    FakeImapSource,
    FakeMailbox,
    LegacyFakeGmail,
    build_raw,
    make_config,
)
from mailtransport.config import Route
from mailtransport.imap_source import ImapError
from mailtransport.state import MailboxState
from mailtransport.sync import dedupe_key, state_key, sync_once


def make_source(inbox_msgs=(), junk_msgs=()):
    inbox = FakeMailbox("INBOX")
    junk = FakeMailbox("Junk")
    trash = FakeMailbox("Deleted Messages")
    for uid, raw, flags in inbox_msgs:
        inbox.add(uid, raw, flags)
    for uid, raw, flags in junk_msgs:
        junk.add(uid, raw, flags)
    source = FakeImapSource(
        {"INBOX": inbox, "Junk": junk, "Deleted Messages": trash},
        special_use={r"\Junk": "Junk", r"\Trash": "Deleted Messages"},
    )
    source.trash = trash
    return source, inbox, junk


def run(config, store, source, gmail):
    return sync_once(config, store, gmail_factory=lambda: gmail, source_factory=lambda: source)


def test_initial_run_does_not_import_backlog(store):
    """既定 (INITIAL_IMPORT=none) では稼働開始前のメールを取り込まない."""
    source, inbox, _ = make_source(
        inbox_msgs=[(1, build_raw("<a@x>"), ()), (2, build_raw("<b@x>"), ())]
    )
    gmail = FakeGmail()

    report = run(make_config(), store, source, gmail)

    assert gmail.inserted == []
    assert all(r.bootstrapped for r in report.routes)
    saved = store.get_mailbox_state(state_key("you@icloud.com", "INBOX"))
    assert saved == MailboxState(uidvalidity=100, last_uid=inbox.uidnext - 1)


def test_initial_import_all_takes_existing_mail(store):
    source, _, _ = make_source(
        inbox_msgs=[(1, build_raw("<a@x>"), ()), (2, build_raw("<b@x>"), ())]
    )
    gmail = FakeGmail()

    report = run(make_config(initial_import="all"), store, source, gmail)

    assert report.forwarded == 2
    assert len(gmail.inserted) == 2


def test_new_mail_is_forwarded_with_correct_labels(store):
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)  # ブートストラップ

    inbox.add(1, build_raw("<new@x>"))  # 未読
    inbox.add(2, build_raw("<read@x>"), flags=(r"\Seen",))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    labels = [labels for _, labels in gmail.inserted]
    assert labels[0] == ["INBOX", "UNREAD"]
    assert labels[1] == ["INBOX"]  # iCloud で既読なら Gmail でも既読


def test_junk_mail_is_not_forwarded(store):
    """迷惑メールは移動対象外. 受信トレイのメールだけを転送する."""
    config = make_config()
    source, inbox, junk = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)  # ブートストラップ

    inbox.add(1, build_raw("<new@x>"))
    junk.add(1, build_raw("<spam@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert [labels for _, labels in gmail.inserted] == [["INBOX", "UNREAD"]]
    assert [r.source for r in report.routes] == ["INBOX"]
    # 迷惑メールは開かないので、本文の取得すら行わない
    assert source.fetched == [1]
    assert store.get_mailbox_state(state_key("you@icloud.com", "Junk")) is None


def test_junk_can_be_added_back_through_routes(store):
    """必要なら ROUTES で迷惑メールの経路を足せる (既定では入っていない)."""
    config = make_config(routes=(Route("INBOX", ("INBOX",)), Route(r"\Junk", ("SPAM",))))
    source, inbox, junk = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<new@x>"))
    junk.add(1, build_raw("<spam@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert [labels for _, labels in gmail.inserted] == [["INBOX", "UNREAD"], ["SPAM", "UNREAD"]]


def test_starred_mail_gets_starred_label(store):
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<flag@x>"), flags=(r"\Seen", r"\Flagged"))
    run(config, store, source, gmail)

    assert gmail.inserted[0][1] == ["INBOX", "STARRED"]


def test_already_forwarded_mail_is_not_duplicated(store):
    """UID が変わっても Message-ID が同じなら二重取り込みしない."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    raw = build_raw("<dup@x>")
    inbox.add(1, raw)
    run(config, store, source, gmail)
    assert len(gmail.inserted) == 1

    # 同じメールが別 UID で現れた (再配達、フォルダ移動など)
    inbox.add(9, raw)
    report = run(config, store, source, gmail)

    assert len(gmail.inserted) == 1
    assert report.routes[0].duplicates == 1


def test_progress_is_persisted_per_message(store):
    """途中で失敗しても、成功済みのメールは再送されない."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(fail_on={1})  # 2 通目で失敗させる
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<ok@x>"))
    inbox.add(2, build_raw("<ng@x>"))
    report = run(config, store, source, gmail)

    assert report.routes[0].error is not None
    assert len(gmail.inserted) == 1
    state = store.get_mailbox_state(state_key("you@icloud.com", "INBOX"))
    assert state.last_uid == 1  # 失敗した UID 2 は進めない

    gmail.fail_on.clear()
    run(config, store, source, gmail)
    assert [labels for _, labels in gmail.inserted] == [["INBOX", "UNREAD"], ["INBOX", "UNREAD"]]


def test_oversized_mail_is_skipped_and_does_not_block(store):
    config = make_config(max_message_bytes=10)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<big@x>", body="x" * 500))
    inbox.add(2, build_raw("<small@x>"))
    config = make_config(max_message_bytes=200)
    report = run(config, store, source, gmail)

    assert report.routes[0].skipped_too_large == 1
    assert len(gmail.inserted) == 1  # 大きすぎるメールで詰まらない


def test_max_messages_per_run_defers_the_rest(store):
    config = make_config(max_messages_per_run=2)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    for uid in range(1, 6):
        inbox.add(uid, build_raw(f"<m{uid}@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert report.routes[0].remaining == 3
    assert report.has_more is True

    run(config, store, source, gmail)
    assert len(gmail.inserted) == 4


def test_uidvalidity_change_resets_without_reimporting(store):
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)
    inbox.add(1, build_raw("<a@x>"))
    run(config, store, source, gmail)
    assert len(gmail.inserted) == 1

    # サーバ側でメールボックスが作り直された
    inbox.uidvalidity = 999
    inbox.messages.clear()
    inbox.add(1, build_raw("<b@x>"))
    report = run(config, store, source, gmail)

    assert report.routes[0].bootstrapped is True
    assert len(gmail.inserted) == 1  # 過去分の再取り込みは行わない
    assert store.get_mailbox_state(state_key("you@icloud.com", "INBOX")).uidvalidity == 999


def test_missing_mailbox_does_not_break_the_other_routes(store):
    config = make_config(routes=(Route("INBOX", ("INBOX",)), Route("Archive", ("ARCHIVE_X",))))
    inbox = FakeMailbox("INBOX")
    source = FakeImapSource({"INBOX": inbox}, special_use={})
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    archive_report = next(r for r in report.routes if r.source == "Archive")
    assert archive_report.error is not None


def test_concurrent_run_is_locked_out(store):
    config = make_config()
    source, _, _ = make_source()
    gmail = FakeGmail()
    store.acquire_lock("sync", 300, "someone-else")

    report = run(config, store, source, gmail)

    assert report.locked_out is True
    assert gmail.inserted == []


def test_dry_run_touches_nothing(store):
    config = make_config(dry_run=True, initial_import="all")
    source, inbox, _ = make_source(inbox_msgs=[(1, build_raw("<a@x>"), ())])
    gmail = FakeGmail()

    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert gmail.inserted == []
    assert store.get_mailbox_state(state_key("you@icloud.com", "INBOX")) is None


def test_lock_is_released_even_on_failure(store):
    config = make_config()

    class Boom:
        def __enter__(self):
            raise RuntimeError("接続できません")

        def __exit__(self, *exc):
            return False

    with pytest.raises(RuntimeError):
        sync_once(config, store, gmail_factory=lambda: FakeGmail(), source_factory=Boom)

    assert store.acquire_lock("sync", 10, "next-run") is True


def test_dedupe_key_falls_back_to_body_hash():
    without_id = b"From: a@example.com\r\nSubject: no id\r\n\r\nbody\r\n"
    other = b"From: a@example.com\r\nSubject: no id\r\n\r\nbody2\r\n"
    assert dedupe_key("u", without_id) == dedupe_key("u", without_id)
    assert dedupe_key("u", without_id) != dedupe_key("u", other)
    # 同じメールでもアカウントが違えば別扱い
    assert dedupe_key("u", without_id) != dedupe_key("v", without_id)


def test_state_key_is_a_safe_document_id():
    key = state_key("you@icloud.com", "迷惑メール/古い")
    assert "/" not in key and len(key) <= 120
    assert key != state_key("you@icloud.com", "INBOX")


def test_forwarded_mail_is_moved_to_trash(store):
    """転送が済んだメールは iCloud の受信トレイに残さない (容量を空けるため)."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)  # ブートストラップ

    inbox.add(1, build_raw("<new@x>"))
    inbox.add(2, build_raw("<new2@x>"), flags=(r"\Seen",))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert report.trashed == 2
    assert report.routes[0].trash_error is None
    assert inbox.messages == {}
    # 中身はゴミ箱に移っているだけで、消えてはいない
    assert [raw for _, raw in source.trash.messages.values()] == [
        build_raw("<new@x>"),
        build_raw("<new2@x>"),
    ]
    # 移動は 1 コマンドにまとめる
    assert source.moved == [([1, 2], "Deleted Messages")]


def test_trash_move_needs_a_writable_selection(store):
    """ゴミ箱へ移す設定のときは読み書き可能な SELECT にする."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    run(config, store, source, gmail)

    assert source.readonly is False


def test_trash_after_forward_can_be_disabled(store):
    """TRASH_AFTER_FORWARD=false なら iCloud 側は読み取り専用のまま触らない."""
    config = make_config(trash_after_forward=False)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 0
    assert source.readonly is True
    assert source.moved == []
    assert set(inbox.messages) == {1}


def test_only_forwarded_mail_is_trashed(store):
    """転送していないメール (サイズ超過・取り込み済み) は受信トレイに残す."""
    config = make_config(max_message_bytes=200)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    dup = build_raw("<dup@x>")
    inbox.add(1, dup)
    run(config, store, source, gmail)  # 1 通目を転送してゴミ箱へ

    inbox.add(2, build_raw("<big@x>", body="x" * 500))  # サイズ超過
    inbox.add(3, dup)  # 取り込み済み (Message-ID が同じ)
    inbox.add(4, build_raw("<ok@x>"))
    report = run(config, store, source, gmail)

    assert report.routes[0].skipped_too_large == 1
    assert report.routes[0].duplicates == 1
    assert report.trashed == 1
    assert set(inbox.messages) == {2, 3}


def test_trash_failure_does_not_fail_the_forward(store):
    """ゴミ箱へ移せなくても転送は成功扱い. 次回に再送しないため."""
    config = make_config(trash_mailbox=r"\Trash")
    inbox = FakeMailbox("INBOX")
    source = FakeImapSource({"INBOX": inbox}, special_use={})  # ゴミ箱が見つからない
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.failed is False
    assert report.trashed == 0
    assert "ゴミ箱が見つかりません" in report.routes[0].trash_error
    assert set(inbox.messages) == {1}


def test_trash_runs_even_when_a_later_message_fails(store):
    """途中で挿入に失敗しても、転送できたぶんはゴミ箱へ移す."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(fail_on={1})  # 2 通目で失敗させる
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<ok@x>"))
    inbox.add(2, build_raw("<ng@x>"))
    report = run(config, store, source, gmail)

    assert report.routes[0].error is not None
    assert report.trashed == 1
    assert set(inbox.messages) == {2}


def test_dry_run_does_not_move_anything(store):
    config = make_config(dry_run=True, initial_import="all")
    source, inbox, _ = make_source(inbox_msgs=[(1, build_raw("<a@x>"), ())])
    gmail = FakeGmail()

    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 0
    assert source.readonly is True
    assert set(inbox.messages) == {1}


class HalfMovingSource(FakeImapSource):
    """前半だけ移し終えたところで切れるサーバー."""

    def move_uids(self, uids, destination, on_moved=None):
        super().move_uids(uids[: len(uids) // 2], destination, on_moved=on_moved)
        raise ImapError("移動の途中で接続が切れました")


def test_partially_moved_mail_is_counted(store):
    """途中で失敗しても、移せたぶんは「移した」と報告する.

    まとめて送る UID を分割しているので、前半だけ成功することがある。
    失敗のひとことで片付けると、ログとレポートが実態とずれる。
    """
    config = make_config()
    inbox, trash = FakeMailbox("INBOX"), FakeMailbox("Deleted Messages")
    source = HalfMovingSource(
        {"INBOX": inbox, "Deleted Messages": trash},
        special_use={r"\Trash": "Deleted Messages"},
    )
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    inbox.add(2, build_raw("<b@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert report.trashed == 1  # 前半の 1 通は本当にゴミ箱へ行っている
    assert report.routes[0].trash_error is not None
    assert set(inbox.messages) == {2}
    assert len(trash.messages) == 1


# --- Gmail 側の確認 (#21) ------------------------------------------------


def test_forwarded_mail_is_verified_before_being_trashed(store):
    """iCloud の原本を消す前に、Gmail から読み戻せることを確かめる."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert gmail.verified == ["gm1"]
    assert report.routes[0].verified == 1
    assert report.trashed == 1
    assert inbox.messages == {}


def test_unverified_mail_stays_in_icloud(store):
    """insert が成功しても Gmail で見つからなければ原本を残す.

    ゴミ箱は 30 日で空になるので、確認できないまま移すと Gmail 側に
    無かったときに復元できない (#21)。
    """
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(lose={"gm1"})
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 0
    assert report.unverified == 1
    assert set(inbox.messages) == {1}
    leftovers = store.list_leftovers()
    assert [(row.uid, row.reason) for row in leftovers] == [(1, "unverified")]


def test_verification_failure_leaves_only_the_affected_mail(store):
    """確認できたぶんは片付け、できなかったぶんだけ残す."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(lose={"gm2"})
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    inbox.add(2, build_raw("<b@x>"))
    inbox.add(3, build_raw("<c@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 3
    assert report.trashed == 2
    assert report.unverified == 1
    assert set(inbox.messages) == {2}


def test_verification_can_be_turned_off(store):
    config = make_config(verify_before_trash="off")
    source, inbox, _ = make_source()
    gmail = FakeGmail(lose={"gm1"})
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert gmail.verified == []
    assert report.trashed == 1
    assert inbox.messages == {}


def test_verification_is_skipped_when_not_trashing(store):
    """iCloud 側に触らないなら確認する必要もない (API 呼び出しを増やさない)."""
    config = make_config(trash_after_forward=False)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    run(config, store, source, gmail)

    assert gmail.verified == []


def test_auto_mode_keeps_working_without_the_metadata_scope(store):
    """既存のトークン (insert のみ) でも、警告だけ出して従来どおり動く."""
    config = make_config(verify_before_trash="auto")
    source, inbox, _ = make_source()
    gmail = LegacyFakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 1
    assert report.unverified == 0
    assert inbox.messages == {}


def test_force_mode_keeps_mail_without_the_metadata_scope(store):
    """VERIFY_BEFORE_TRASH=true なら、確認できない以上は原本を消さない."""
    config = make_config(verify_before_trash="force")
    source, inbox, _ = make_source()
    gmail = LegacyFakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 0
    assert report.unverified == 1
    assert set(inbox.messages) == {1}


def test_scope_error_is_only_reported_once(store):
    """スコープ不足は再試行しても直らないので、毎通は問い合わせない."""
    from mailtransport.gmail_sink import GmailScopeError

    config = make_config(verify_before_trash="auto")
    source, inbox, _ = make_source()
    gmail = FakeGmail(verify_error=GmailScopeError("スコープが足りません", status=403))
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    inbox.add(2, build_raw("<b@x>"))
    report = run(config, store, source, gmail)

    assert gmail.verified == ["gm1"]  # 1 通目で諦め、以降は問い合わせない
    assert report.trashed == 2


def test_transient_verification_error_keeps_the_original(store):
    """一時的な失敗でも、確認できていない以上は原本を残す (次回は触らない)."""
    from mailtransport.gmail_sink import GmailError

    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(verify_error=GmailError("一時的な失敗", status=503))
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 0
    assert set(inbox.messages) == {1}


# --- 初回の全件取り込み (#22) --------------------------------------------


def test_initial_import_does_not_trash_existing_mail(store):
    """稼働開始前からあったメールは、転送しても受信トレイから消さない.

    Gmail 側に全部入ったことを人が確認する前に受信トレイが空になると、
    ラベル設定のミスに気付いたときには手遅れになる (#22)。
    """
    config = make_config(initial_import="all")
    source, inbox, _ = make_source(
        inbox_msgs=[(1, build_raw("<a@x>"), ()), (2, build_raw("<b@x>"), ())]
    )
    gmail = FakeGmail()

    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert report.trashed == 0
    assert report.routes[0].kept_pre_existing == 2
    assert set(inbox.messages) == {1, 2}
    assert {row.reason for row in store.list_leftovers()} == {"pre_existing"}


def test_mail_arriving_after_the_initial_import_is_trashed(store):
    """境界より後に届いたメールは、これまでどおりゴミ箱へ移す."""
    config = make_config(initial_import="all")
    source, inbox, _ = make_source(inbox_msgs=[(1, build_raw("<old@x>"), ())])
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(2, build_raw("<new@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 1
    assert report.trashed == 1
    assert set(inbox.messages) == {1}


def test_existing_mail_can_be_trashed_on_request(store):
    """取り込みを始める前に立てておけば、既存メールもゴミ箱へ移る."""
    config = make_config(initial_import="all", trash_existing_on_initial_import=True)
    source, inbox, _ = make_source(inbox_msgs=[(1, build_raw("<a@x>"), ())])
    gmail = FakeGmail()

    report = run(config, store, source, gmail)

    assert report.trashed == 1
    assert inbox.messages == {}


def test_flag_flipped_after_the_import_does_not_reach_forwarded_mail(store):
    """取り込みが済んだあとで立てても、転送済みのメールには効かない.

    同期位置は 1 通ごとに進むので、転送済みの UID は次の UID SEARCH の範囲に
    入らない。README にはこの制約 (取り込み前に決めること) を書いてある。
    """
    source, inbox, _ = make_source(inbox_msgs=[(1, build_raw("<a@x>"), ())])
    gmail = FakeGmail()
    run(make_config(initial_import="all"), store, source, gmail)
    assert set(inbox.messages) == {1}

    report = run(
        make_config(initial_import="all", trash_existing_on_initial_import=True),
        store,
        source,
        gmail,
    )

    assert report.forwarded == 0
    assert report.trashed == 0
    assert set(inbox.messages) == {1}


# --- 受信トレイへ戻したメール (#24) --------------------------------------


def test_mail_moved_back_to_the_inbox_is_not_forwarded_again(store):
    """利用者が受信トレイへ戻したメールを再転送・再ゴミ箱行きにしない.

    IMAP では戻すと新しい UID が振られるので新着と区別が付かない。
    重複排除の記録を無期限に保つことで、二重取り込みを防ぐ (#24)。
    """
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    raw = build_raw("<again@x>")
    inbox.add(1, raw)
    run(config, store, source, gmail)
    assert inbox.messages == {}

    inbox.add(5, raw)  # 利用者がゴミ箱から受信トレイへ戻した
    report = run(config, store, source, gmail)

    assert report.forwarded == 0
    assert report.routes[0].duplicates == 1
    assert report.trashed == 0
    assert set(inbox.messages) == {5}  # 戻した操作を打ち消さない


# --- 経路ごとのゴミ箱設定 (#26) ------------------------------------------


def test_route_can_opt_out_of_trashing(store):
    """Gmail 側でも 30 日で消えるラベルの経路だけ、iCloud に原本を残せる."""
    config = make_config(
        routes=(Route("INBOX", ("INBOX",)), Route(r"\Junk", ("SPAM",), trash=False))
    )
    source, inbox, junk = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    junk.add(1, build_raw("<spam@x>"))
    report = run(config, store, source, gmail)

    assert report.forwarded == 2
    assert report.trashed == 1
    assert inbox.messages == {}
    assert set(junk.messages) == {1}


def test_route_can_opt_in_to_trashing(store):
    config = make_config(
        trash_after_forward=False, routes=(Route("INBOX", ("INBOX",), trash=True),)
    )
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    report = run(config, store, source, gmail)

    assert report.trashed == 1
    assert inbox.messages == {}


# --- iCloud に残るメールの可視化 (#25) -----------------------------------


def test_leftovers_record_why_mail_stayed_in_icloud(store):
    """受信トレイを手で整理する前に、残った理由を確かめられるようにする."""
    config = make_config(max_message_bytes=200)
    source, inbox, _ = make_source()
    gmail = FakeGmail()
    run(config, store, source, gmail)

    dup = build_raw("<dup@x>")
    inbox.add(1, dup)
    run(config, store, source, gmail)

    inbox.add(2, build_raw("<big@x>", body="x" * 500))
    inbox.add(3, dup)
    run(config, store, source, gmail)

    by_uid = {row.uid: row for row in store.list_leftovers()}
    assert by_uid[2].reason == "too_large"
    assert by_uid[2].mailbox == "INBOX"
    assert by_uid[3].reason == "duplicate"


def test_trash_failure_is_recorded_as_a_leftover(store):
    config = make_config()
    inbox = FakeMailbox("INBOX")
    source = FakeImapSource({"INBOX": inbox}, special_use={})  # ゴミ箱が見つからない
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    run(config, store, source, gmail)

    assert [(row.uid, row.reason) for row in store.list_leftovers()] == [(1, "trash_failed")]


class ForgetfulSource(FakeImapSource):
    """FETCH の応答に一部の UID が出てこないサーバー."""

    def fetch_metadata(self, uids):
        return super().fetch_metadata(uids)[1:]


def test_uids_missing_from_the_fetch_response_are_recorded(store):
    """応答から漏れた UID を黙って捨てない (同期位置は先へ進むため)."""
    config = make_config()
    inbox, trash = FakeMailbox("INBOX"), FakeMailbox("Deleted Messages")
    source = ForgetfulSource(
        {"INBOX": inbox, "Deleted Messages": trash},
        special_use={r"\Trash": "Deleted Messages"},
    )
    gmail = FakeGmail()
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<lost@x>"))
    inbox.add(2, build_raw("<ok@x>"))
    report = run(config, store, source, gmail)

    assert report.routes[0].missing_metadata == 1
    assert [(row.uid, row.reason) for row in store.list_leftovers()] == [(1, "metadata_missing")]


def test_leftovers_are_dropped_when_uidvalidity_changes(store):
    """UID の意味が変われば、UID で覚えていた記録も捨てる."""
    config = make_config()
    source, inbox, _ = make_source()
    gmail = FakeGmail(lose={"gm1"})
    run(config, store, source, gmail)

    inbox.add(1, build_raw("<a@x>"))
    run(config, store, source, gmail)
    assert store.list_leftovers()

    inbox.uidvalidity = 999
    run(config, store, source, gmail)

    assert store.list_leftovers() == []
