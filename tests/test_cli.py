"""CLI のテスト."""

from __future__ import annotations

import json

from mailtransport.cli import main
from mailtransport.state import SqliteStateStore


def test_leftovers_are_listed_without_any_credentials(tmp_path, capsys, monkeypatch):
    """受信トレイを手で整理する前に見るものなので、資格情報なしで読めること."""
    path = tmp_path / "state.db"
    store = SqliteStateStore(str(path))
    store.record_leftover("k", 7, "INBOX", "unverified", "Gmail に gm1 が見つかりません")
    monkeypatch.setenv("STATE_DB_PATH", str(path))

    assert main(["--env-file", str(tmp_path / "absent.env"), "--leftovers"]) == 0

    rows = json.loads(capsys.readouterr().out)
    assert [(row["uid"], row["reason"], row["mailbox"]) for row in rows] == [
        (7, "unverified", "INBOX")
    ]


def test_missing_state_file_is_reported(tmp_path, monkeypatch):
    """打ち間違えた場合に、空の状態ファイルを作って「0 件」と答えない."""
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "nope.db"))

    assert main(["--env-file", str(tmp_path / "absent.env"), "--leftovers"]) == 2
    assert not (tmp_path / "nope.db").exists()
