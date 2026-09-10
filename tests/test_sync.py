from __future__ import annotations

import pytest

from conftest import FakeGmail, FakeImapSource, FakeMailbox, build_raw, make_config
from mailtransport.config import Route
from mailtransport.state import MailboxState
from mailtransport.sync import dedupe_key, state_key, sync_once


def make_source(inbox_msgs=(), junk_msgs=()):
    inbox = FakeMailbox("INBOX")
    junk = FakeMailbox("Junk")
    for uid, raw, flags in inbox_msgs:
        inbox.add(uid, raw, flags)
    for uid, raw, flags in junk_msgs:
        junk.add(uid, raw, flags)
    source = FakeImapSource({"INBOX": inbox, "Junk": junk}, special_use={r"\Junk": "Junk"})
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
