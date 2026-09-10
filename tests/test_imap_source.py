from __future__ import annotations

import datetime as dt

import pytest

from mailtransport.imap_source import (
    ImapError,
    ImapSource,
    Mailbox,
    _parse_internaldate,
    date_header_of,
    message_id_of,
    quote_mailbox,
)


class FakeConn:
    """imaplib.IMAP4_SSL の応答を模したテスト用コネクション."""

    def __init__(self, responses: dict, capabilities: tuple[str, ...] = ()) -> None:
        self.responses = responses
        self.capabilities = capabilities
        self.calls: list[tuple] = []

    def list(self):
        return self.responses["LIST"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        return self.responses[command.upper()]


def source_with(conn, readonly: bool = True) -> ImapSource:
    source = ImapSource("h", 993, "u", "p")
    source._conn = conn
    source._selected = "INBOX"
    source._selected_readonly = readonly
    return source


def test_quote_mailbox_escapes_special_characters():
    assert quote_mailbox("Junk") == '"Junk"'
    assert quote_mailbox('od"d\\name') == '"od\\"d\\\\name"'


def test_list_mailboxes_parses_flags_and_names():
    conn = FakeConn(
        {
            "LIST": (
                "OK",
                [
                    rb'(\HasNoChildren) "/" "INBOX"',
                    rb'(\HasNoChildren \Junk) "/" "Junk"',
                    rb'(\HasNoChildren \Trash) "/" "Deleted Messages"',
                ],
            )
        }
    )

    mailboxes = source_with(conn).list_mailboxes()

    assert [m.name for m in mailboxes] == ["INBOX", "Junk", "Deleted Messages"]
    assert mailboxes[1].has_flag(r"\Junk") is True
    assert mailboxes[0].has_flag(r"\Junk") is False


def test_resolve_mailbox_finds_junk_by_special_use_flag():
    """迷惑メールの名前はロケール依存なので、フラグで探せることを確認する."""
    conn = FakeConn(
        {"LIST": ("OK", [rb'(\HasNoChildren) "/" "INBOX"', '(\\Junk) "/" "迷惑メール"'.encode()])}
    )
    source = source_with(conn)

    assert source.resolve_mailbox(r"\Junk") == "迷惑メール"
    assert source.resolve_mailbox("INBOX") == "INBOX"
    assert source.resolve_mailbox(r"\Archive") is None


def test_search_uids_after_drops_the_sentinel_result():
    """IMAP の "n:*" は該当なしでも最大 UID を返すため、必ず絞り込む."""
    conn = FakeConn({"SEARCH": ("OK", [b"42"])})
    source = source_with(conn)

    assert source.search_uids_after(42) == []
    assert source.search_uids_after(41) == [42]


def test_search_uids_are_sorted_and_deduplicated():
    conn = FakeConn({"SEARCH": ("OK", [b"12 10 11", b" 11"])})

    assert source_with(conn).search_uids_after(9) == [10, 11, 12]


def test_fetch_metadata_parses_size_and_flags():
    conn = FakeConn(
        {
            "FETCH": (
                "OK",
                [
                    rb"1 (UID 10 RFC822.SIZE 2048 FLAGS (\Seen)"
                    rb' INTERNALDATE "01-May-2024 12:00:00 +0900")',
                    rb"2 (UID 11 RFC822.SIZE 99 FLAGS ())",
                ],
            )
        }
    )

    metas = source_with(conn).fetch_metadata([10, 11])

    assert [m.uid for m in metas] == [10, 11]
    assert metas[0].size == 2048 and metas[0].seen is True
    assert metas[0].internaldate == dt.datetime(
        2024, 5, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))
    )
    assert metas[1].seen is False


def test_fetch_raw_returns_the_literal_body():
    body = b"Subject: hi\r\n\r\nbody"
    conn = FakeConn({"FETCH": ("OK", [(b"1 (BODY[] {19}", body), b")"])})
    source = source_with(conn)

    assert source.fetch_raw(10) == body
    # 既読フラグを立てないよう BODY.PEEK を使っていること
    assert "BODY.PEEK[]" in conn.calls[0][1][1]


def test_fetch_raw_returns_none_when_message_vanished():
    conn = FakeConn({"FETCH": ("OK", [None])})

    assert source_with(conn).fetch_raw(10) is None


def test_message_id_and_date_headers_are_extracted():
    raw = b"Message-ID: <abc@example.com>\r\nDate: Wed, 01 May 2024 12:00:00 +0900\r\n\r\nhi"

    assert message_id_of(raw) == "<abc@example.com>"
    assert date_header_of(raw).utcoffset() == dt.timedelta(hours=9)
    assert message_id_of(b"Subject: none\r\n\r\nhi") is None


def test_internaldate_parser_rejects_garbage():
    assert _parse_internaldate(b"not a date") is None


def test_mailbox_flag_comparison_is_case_insensitive():
    assert Mailbox("Junk", (r"\junk",)).has_flag(r"\Junk") is True


def test_move_uids_prefers_the_move_command():
    conn = FakeConn({"MOVE": ("OK", [b"done"])}, capabilities=("IMAP4rev1", "MOVE", "UIDPLUS"))

    moved = source_with(conn, readonly=False).move_uids([10, 12], "Deleted Messages")

    assert moved == 2
    assert conn.calls == [("MOVE", ("10,12", '"Deleted Messages"'))]


def test_move_uids_falls_back_to_copy_and_expunge():
    """MOVE 非対応のサーバーでは COPY してから元を削除する."""
    conn = FakeConn(
        {"COPY": ("OK", [b""]), "STORE": ("OK", [b""]), "EXPUNGE": ("OK", [b""])},
        capabilities=("IMAP4rev1", "UIDPLUS"),
    )

    moved = source_with(conn, readonly=False).move_uids([10], "Trash")

    assert moved == 1
    assert [command for command, _ in conn.calls] == ["COPY", "STORE", "EXPUNGE"]
    assert conn.calls[1][1] == ("10", "+FLAGS", r"(\Deleted)")
    # 他のクライアントが立てた \Deleted を巻き込まないよう UID 指定で消す
    assert conn.calls[2][1] == ("10",)


def test_move_uids_skips_expunge_without_uidplus():
    """UID 指定で消せないなら、素の EXPUNGE は撃たずにコピーだけで止める."""
    conn = FakeConn({"COPY": ("OK", [b""]), "STORE": ("OK", [b""])}, capabilities=("IMAP4rev1",))

    assert source_with(conn, readonly=False).move_uids([10], "Trash") == 1
    assert [command for command, _ in conn.calls] == ["COPY", "STORE"]


def test_move_uids_splits_long_uid_lists():
    conn = FakeConn({"MOVE": ("OK", [b""])}, capabilities=("MOVE",))

    moved = source_with(conn, readonly=False).move_uids(list(range(1, 451)), "Trash")

    assert moved == 450
    assert len(conn.calls) == 3  # 200 + 200 + 50


def test_move_uids_refuses_a_readonly_selection():
    conn = FakeConn({}, capabilities=("MOVE",))

    with pytest.raises(ImapError):
        source_with(conn).move_uids([10], "Trash")
    assert conn.calls == []


def test_move_uids_does_nothing_without_uids():
    conn = FakeConn({}, capabilities=("MOVE",))

    assert source_with(conn, readonly=False).move_uids([], "Trash") == 0
    assert conn.calls == []


def test_move_uids_raises_when_the_server_says_no():
    conn = FakeConn({"MOVE": ("NO", [b"over quota"])}, capabilities=("MOVE",))

    with pytest.raises(ImapError):
        source_with(conn, readonly=False).move_uids([10], "Trash")


class FakeSSLConn:
    """login 後にしか全機能を明かさないサーバーの模倣."""

    def __init__(self, host, port, timeout=None):
        self.capabilities = ("IMAP4REV1", "IDLE")
        self.logged_in = None
        self.capability_error = False

    def login(self, user, password):
        self.logged_in = (user, password)
        return ("OK", [b"LOGIN completed"])

    def capability(self):
        if self.capability_error:
            raise OSError("接続が切れました")
        assert self.logged_in is not None
        return ("OK", [b"IMAP4rev1 IDLE MOVE UIDPLUS"])


def connect_with(monkeypatch, prepare=None) -> ImapSource:
    def factory(host, port, timeout=None):
        conn = FakeSSLConn(host, port, timeout)
        if prepare:
            prepare(conn)
        return conn

    monkeypatch.setattr("mailtransport.imap_source.imaplib.IMAP4_SSL", factory)
    source = ImapSource("imap.mail.me.com", 993, "u", "p")
    source.connect()
    return source


def test_connect_refreshes_capabilities_after_login(monkeypatch):
    """imaplib は認証前の CAPABILITY しか持たないので取り直す.

    MOVE / UIDPLUS を認証後にしか出さないサーバーがあり、取り直さないと
    ゴミ箱への移動が非効率な経路に落ちてしまう。
    """
    source = connect_with(monkeypatch)

    assert source.has_capability("MOVE") is True
    assert source.has_capability("UIDPLUS") is True


def test_connect_keeps_going_when_capability_fails(monkeypatch):
    source = connect_with(monkeypatch, prepare=lambda conn: setattr(conn, "capability_error", True))

    assert source.has_capability("IDLE") is True  # 接続時のものを使い続ける
    assert source.has_capability("MOVE") is False


def test_move_uids_reports_progress_per_chunk():
    """分割して送るので、途中で失敗しても移せたぶんを呼び出し元に伝える."""

    class FlakyConn(FakeConn):
        def uid(self, command, *args):
            self.calls.append((command, args))
            if len(self.calls) == 2:  # 2 つ目のチャンクで切れる
                raise OSError("接続が切れました")
            return ("OK", [b""])

    conn = FlakyConn({}, capabilities=("MOVE",))
    counted: list[int] = []

    with pytest.raises(OSError):
        source_with(conn, readonly=False).move_uids(
            list(range(1, 401)), "Trash", on_moved=counted.append
        )

    assert counted == [200]  # 1 つ目のチャンクは本当に移動できている


class ClosingConn(FakeConn):
    """CLOSE / UNSELECT / LOGOUT の呼ばれ方だけを見るためのコネクション."""

    def close(self):
        self.calls.append(("CLOSE", ()))

    def unselect(self):
        self.calls.append(("UNSELECT", ()))

    def logout(self):
        self.calls.append(("LOGOUT", ()))


def test_close_uses_unselect_after_a_writable_selection():
    """読み書きで開いた CLOSE は \\Deleted のメールを暗黙に削除してしまう."""
    conn = ClosingConn({}, capabilities=("UNSELECT",))

    source_with(conn, readonly=False).close()

    assert [command for command, _ in conn.calls] == ["UNSELECT", "LOGOUT"]


def test_close_skips_close_when_unselect_is_missing():
    """UNSELECT が無いなら、何も消さない LOGOUT だけで閉じる.

    UIDPLUS が無くて削除を見送ったメールを、CLOSE で消してしまわないため。
    """
    conn = ClosingConn({}, capabilities=("IMAP4REV1",))

    source_with(conn, readonly=False).close()

    assert [command for command, _ in conn.calls] == ["LOGOUT"]


def test_close_still_closes_a_readonly_selection():
    conn = ClosingConn({}, capabilities=("IMAP4REV1",))

    source_with(conn).close()

    assert [command for command, _ in conn.calls] == ["CLOSE", "LOGOUT"]
