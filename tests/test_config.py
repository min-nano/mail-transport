from __future__ import annotations

import json

import pytest

from mailtransport.config import Route, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(dict(__import__("os").environ)):
        if name.startswith(("ICLOUD_", "GMAIL_", "ROUTES", "INITIAL_IMPORT", "DRY_RUN", "TRASH_")):
            monkeypatch.delenv(name, raising=False)


def base_env(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "you@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv(
        "GMAIL_OAUTH_JSON",
        json.dumps({"client_id": "cid", "client_secret": "cs", "refresh_token": "rt"}),
    )


def test_defaults_route_inbox_only(monkeypatch):
    """既定では受信トレイだけを転送し、迷惑メールは対象外にする."""
    base_env(monkeypatch)
    config = load_config()

    assert config.routes == (Route("INBOX", ("INBOX",)),)
    # Apple の表示どおり空白入りで貼られても通るようにする
    assert config.icloud_app_password == "abcdefghijklmnop"
    assert config.icloud_host == "imap.mail.me.com"


def test_forwarded_mail_is_trashed_by_default(monkeypatch):
    """iCloud の容量を空けるため、既定では転送後にゴミ箱へ移す."""
    base_env(monkeypatch)
    config = load_config()

    assert config.trash_after_forward is True
    # ゴミ箱の名前もロケール依存 (Trash / Deleted Messages) なのでフラグで探す
    assert config.trash_mailbox == r"\Trash"


def test_trashing_can_be_turned_off_and_retargeted(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("TRASH_AFTER_FORWARD", "false")
    monkeypatch.setenv("TRASH_MAILBOX", "Archive")
    config = load_config()

    assert config.trash_after_forward is False
    assert config.trash_mailbox == "Archive"


def test_routes_can_be_overridden(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("ROUTES", json.dumps([{"source": "Archive", "labels": "ARCHIVE_X"}]))

    assert load_config().routes == (Route("Archive", ("ARCHIVE_X",)),)


def test_client_secret_json_can_be_pasted_as_is(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv(
        "GMAIL_OAUTH_JSON",
        json.dumps(
            {"installed": {"client_id": "cid", "client_secret": "cs"}, "refresh_token": "rt"}
        ),
    )

    auth = load_config().gmail_auth
    assert (auth.client_id, auth.client_secret, auth.refresh_token) == ("cid", "cs", "rt")


def test_missing_password_is_reported(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.delenv("ICLOUD_APP_PASSWORD")

    with pytest.raises(ValueError, match="ICLOUD_APP_PASSWORD"):
        load_config()


def test_missing_refresh_token_is_reported(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "cid", "client_secret": "cs"}))

    with pytest.raises(ValueError, match="refresh_token"):
        load_config()


def test_invalid_initial_import_is_rejected(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("INITIAL_IMPORT", "maybe")

    with pytest.raises(ValueError, match="INITIAL_IMPORT"):
        load_config()


def test_route_without_labels_is_rejected(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("ROUTES", json.dumps([{"source": "INBOX"}]))

    with pytest.raises(ValueError, match="labels"):
        load_config()
