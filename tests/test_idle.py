"""IMAP IDLE (push 受信) のテスト."""

from __future__ import annotations

import time

import pytest

from mailtransport.imap_source import ImapError, ImapSource


class FakeSocket:
    def __init__(self) -> None:
        self.timeout = 60.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class IdleConn:
    """IDLE 中のサーバー応答を再生するテスト用コネクション.

    ``lines`` の要素が TimeoutError クラスなら、その時点で読み取りタイムアウトが
    起きたものとして扱う。
    """

    def __init__(self, lines, capabilities=("IMAP4REV1", "IDLE")) -> None:
        self.lines = list(lines)
        self.capabilities = capabilities
        self.sent: list[bytes] = []
        self._sock = FakeSocket()
        self._tag = 0

    def _new_tag(self) -> bytes:
        self._tag += 1
        return b"A%03d" % self._tag

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def socket(self):
        return self._sock

    def readline(self) -> bytes:
        if not self.lines:
            return b""
        return self.lines.pop(0)


class SilentIdleConn(IdleConn):
    """新着が来ないサーバー. DONE を受け取るまで沈黙し続ける."""

    def __init__(self) -> None:
        super().__init__([b"+ idling\r\n"])

    def send(self, data: bytes) -> None:
        super().send(data)
        if data == b"DONE\r\n":
            self.lines.append(b"A001 OK IDLE terminated\r\n")

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        # 実際のソケットと同じく、指定時間だけ待ってからタイムアウトする
        time.sleep(min(self._sock.timeout or 0.01, 0.02))
        raise TimeoutError()


def source_with(conn) -> ImapSource:
    source = ImapSource("h", 993, "u", "p")
    source._conn = conn
    return source


def test_has_capability_is_case_insensitive():
    source = source_with(IdleConn([], capabilities=("imap4rev1", "idle")))

    assert source.has_capability("IDLE") is True
    assert source.has_capability("MOVE") is False


def test_idle_wakes_on_exists_and_closes_cleanly():
    conn = IdleConn([b"+ idling\r\n", b"* 12 EXISTS\r\n", b"A001 OK IDLE terminated\r\n"])

    assert source_with(conn).idle_wait(timeout=60) is True
    assert conn.sent[0] == b"A001 IDLE\r\n"
    # 通知を受けたら必ず DONE を送って通常状態に戻す
    assert conn.sent[-1] == b"DONE\r\n"


def test_idle_ignores_server_keepalive():
    """ "* OK Still here" のような keepalive では起こさない."""
    conn = IdleConn(
        [
            b"+ idling\r\n",
            b"* OK Still here\r\n",
            b"* 3 RECENT\r\n",
            b"A001 OK IDLE terminated\r\n",
        ]
    )

    assert source_with(conn).idle_wait(timeout=60) is True


def test_idle_returns_false_on_timeout():
    """通知が来ないまま制限時間が過ぎたら False (呼び出し側が張り直す)."""
    conn = SilentIdleConn()

    assert source_with(conn).idle_wait(timeout=0.05, poll_step=0.01) is False
    assert b"DONE\r\n" in conn.sent


def test_idle_restores_the_socket_timeout():
    conn = IdleConn([b"+ idling\r\n", b"* 1 EXISTS\r\n", b"A001 OK done\r\n"])
    source_with(conn).idle_wait(timeout=60)

    assert conn.socket().gettimeout() == 60.0


def test_idle_raises_when_the_server_refuses():
    conn = IdleConn([b"A001 BAD Command unknown\r\n"])

    with pytest.raises(ImapError, match="IDLE を開始できませんでした"):
        source_with(conn).idle_wait(timeout=60)


def test_idle_raises_when_the_connection_drops():
    conn = IdleConn([b"+ idling\r\n", b""])

    with pytest.raises(ImapError, match="切断"):
        source_with(conn).idle_wait(timeout=60)
    # 切れた接続に DONE を送りつけない
    assert b"DONE\r\n" not in conn.sent


def test_idle_raises_on_bye():
    conn = IdleConn([b"+ idling\r\n", b"* BYE Logging out\r\n"])

    with pytest.raises(ImapError, match="接続を閉じました"):
        source_with(conn).idle_wait(timeout=60)


def test_idle_drain_gives_up_on_a_flood_of_responses():
    """DONE の完了応答が来ないまま無限に読み続けない."""
    conn = IdleConn([b"+ idling\r\n", b"* 1 EXISTS\r\n"] + [b"* 1 FETCH ()\r\n"] * 2000)

    with pytest.raises(ImapError, match="終了応答"):
        source_with(conn).idle_wait(timeout=60)
