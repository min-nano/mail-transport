"""tools/get_gmail_refresh_token.py の確認.

秘密情報 (client secret / リフレッシュトークン) を標準出力に出さず、
所有者だけが読めるファイルへ書き出すことを担保する。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import types

import pytest
from tools.get_gmail_refresh_token import main

SECRET = "1//super-secret-refresh-token"


class FakeCredentials:
    client_id = "cid.apps.googleusercontent.com"
    client_secret = "GOCSPX-client-secret"
    token_uri = "https://oauth2.googleapis.com/token"

    def __init__(self, refresh_token: str | None = SECRET) -> None:
        self.refresh_token = refresh_token


class FakeFlow:
    def __init__(self, credentials: FakeCredentials) -> None:
        self._credentials = credentials

    def run_local_server(self, **kwargs):
        # リフレッシュトークンが必ず返る設定で呼ばれること
        assert kwargs["access_type"] == "offline"
        assert kwargs["prompt"] == "consent"
        return self._credentials


@pytest.fixture
def fake_oauth(monkeypatch):
    """google_auth_oauthlib を差し替えて OAuth のやりとりを省く."""

    holder = FakeCredentials()

    class InstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            assert scopes == ["https://www.googleapis.com/auth/gmail.insert"]
            return FakeFlow(holder)

    package = types.ModuleType("google_auth_oauthlib")
    module = types.ModuleType("google_auth_oauthlib.flow")
    module.InstalledAppFlow = InstalledAppFlow
    package.flow = module
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", package)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", module)
    return holder


def test_out_is_required():
    """出力先を省けない (省けたら標準出力へ出す経路が復活してしまう)."""
    with pytest.raises(SystemExit) as exc:
        main(["--client-secret", "client_secret.json"])
    assert exc.value.code == 2


def test_writes_secret_only_to_file(tmp_path, capsys, fake_oauth):
    out = tmp_path / "gmail_oauth.json"

    assert main(["--client-secret", "cs.json", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["refresh_token"] == SECRET
    assert payload["client_secret"] == FakeCredentials.client_secret

    captured = capsys.readouterr()
    # 秘密情報はどちらのストリームにも出さない
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert FakeCredentials.client_secret not in captured.out
    assert FakeCredentials.client_secret not in captured.err
    assert captured.out == ""
    assert str(out) in captured.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX の権限ビットが無い環境では確認しない")
def test_secret_file_is_owner_only(tmp_path, fake_oauth):
    out = tmp_path / "gmail_oauth.json"
    # umask が緩くても、他ユーザーから読めるファイルにしない
    previous = os.umask(0)
    try:
        assert main(["--client-secret", "cs.json", "--out", str(out)]) == 0
    finally:
        os.umask(previous)

    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX の権限ビットが無い環境では確認しない")
def test_existing_loose_file_is_tightened(tmp_path, fake_oauth):
    out = tmp_path / "gmail_oauth.json"
    out.write_text("old\n", encoding="utf-8")
    os.chmod(out, 0o644)

    assert main(["--client-secret", "cs.json", "--out", str(out)]) == 0

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert json.loads(out.read_text(encoding="utf-8"))["refresh_token"] == SECRET


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW が無い環境では確認しない")
def test_symlinked_out_is_refused(tmp_path, capsys, fake_oauth):
    """出力先がシンボリックリンクなら、リンク先へ秘密情報を書かない."""
    target = tmp_path / "target.json"
    target.write_text("untouched\n", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    assert main(["--client-secret", "cs.json", "--out", str(link)]) == 1

    assert target.read_text(encoding="utf-8") == "untouched\n"
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_missing_refresh_token_reports_failure(tmp_path, capsys, fake_oauth):
    fake_oauth.refresh_token = None
    out = tmp_path / "gmail_oauth.json"

    assert main(["--client-secret", "cs.json", "--out", str(out)]) == 1

    assert not out.exists()
    assert "リフレッシュトークンが取得できませんでした" in capsys.readouterr().err
