from __future__ import annotations

import datetime as dt

import pytest

from mailtransport.config import Config, GmailAuth, Route
from mailtransport.imap_source import MessageMeta


def make_config(**overrides) -> Config:
    base = dict(
        icloud_username="you@icloud.com",
        icloud_app_password="abcd-efgh-ijkl-mnop",
        icloud_host="imap.mail.me.com",
        icloud_port=993,
        gmail_auth=GmailAuth(client_id="cid", client_secret="cs", refresh_token="rt"),
        gmail_user_id="me",
        routes=(Route("INBOX", ("INBOX",)),),
        trash_after_forward=True,
        trash_mailbox="\\Trash",
        project_id="proj",
        seen_retention_days=30,
        max_messages_per_run=40,
        max_message_bytes=35 * 1024 * 1024,
        lock_ttl_seconds=540,
        run_budget_seconds=240,
        initial_import="none",
        dry_run=False,
        imap_timeout_seconds=60,
        state_backend="memory",
        state_db_path=":memory:",
        idle_enabled=True,
        idle_refresh_seconds=1500,
        poll_interval_seconds=60,
        safety_sync_seconds=300,
        debounce_seconds=0.0,
        reconnect_backoff_max_seconds=300,
    )
    base.update(overrides)
    return Config(**base)


def build_raw(message_id: str, subject: str = "テスト", body: str = "本文") -> bytes:
    date = dt.datetime(2024, 5, 1, 12, 0, tzinfo=dt.UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    return (
        f"Message-ID: {message_id}\r\n"
        f"From: sender@example.com\r\n"
        f"To: you@icloud.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"\r\n{body}\r\n"
    ).encode()


class FakeMailbox:
    def __init__(self, name: str, uidvalidity: int = 100) -> None:
        self.name = name
        self.uidvalidity = uidvalidity
        self.messages: dict[int, tuple[MessageMeta, bytes]] = {}

    def add(self, uid: int, raw: bytes, flags: tuple[str, ...] = ()) -> None:
        self.messages[uid] = (MessageMeta(uid=uid, size=len(raw), flags=flags), raw)

    @property
    def uidnext(self) -> int:
        return (max(self.messages) + 1) if self.messages else 1


class FakeImapSource:
    """ImapSource と同じインターフェースを持つテスト用の差し替え."""

    def __init__(
        self, mailboxes: dict[str, FakeMailbox], special_use: dict[str, str] | None = None
    ):
        self.mailboxes = mailboxes
        self.special_use = special_use or {}
        self.selected: FakeMailbox | None = None
        self.readonly = True
        self.fetched: list[int] = []
        self.moved: list[tuple[list[int], str]] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def resolve_mailbox(self, source: str) -> str | None:
        if source.startswith("\\"):
            return self.special_use.get(source)
        return source if source in self.mailboxes else None

    def select(self, mailbox: str, readonly: bool = True) -> tuple[int, int]:
        self.selected = self.mailboxes[mailbox]
        self.readonly = readonly
        return self.selected.uidvalidity, self.selected.uidnext

    def search_uids_after(self, last_uid: int) -> list[int]:
        assert self.selected is not None
        return sorted(uid for uid in self.selected.messages if uid > last_uid)

    def fetch_metadata(self, uids: list[int]) -> list[MessageMeta]:
        assert self.selected is not None
        return [self.selected.messages[uid][0] for uid in uids if uid in self.selected.messages]

    def fetch_raw(self, uid: int) -> bytes | None:
        assert self.selected is not None
        self.fetched.append(uid)
        entry = self.selected.messages.get(uid)
        return entry[1] if entry else None

    def move_uids(self, uids: list[int], destination: str, on_moved=None) -> int:
        assert self.selected is not None
        if self.readonly:
            raise AssertionError("読み取り専用で SELECT したまま移動しようとしています")
        if destination not in self.mailboxes:
            raise KeyError(destination)
        target = self.mailboxes[destination]
        moved = 0
        for uid in uids:
            entry = self.selected.messages.pop(uid, None)
            if entry is None:
                continue
            target.add(target.uidnext, entry[1], entry[0].flags)
            moved += 1
        self.moved.append((list(uids), destination))
        if on_moved is not None and moved:
            on_moved(moved)
        return moved


class FakeGmail:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.inserted: list[tuple[bytes, list[str]]] = []
        self.fail_on = fail_on or set()

    def insert(self, raw: bytes, label_ids: list[str]):
        from mailtransport.gmail_sink import InsertResult

        if len(self.inserted) in self.fail_on:
            raise RuntimeError("Gmail への挿入に失敗しました")
        self.inserted.append((raw, list(label_ids)))
        return InsertResult(message_id=f"gm{len(self.inserted)}", thread_id="th1")


@pytest.fixture
def store():
    from mailtransport.state import MemoryStateStore

    return MemoryStateStore()
