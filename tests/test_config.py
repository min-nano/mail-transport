from __future__ import annotations

import json

import pytest

from mailtransport.config import Route, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(dict(__import__("os").environ)):
        if name.startswith(("ICLOUD_", "GMAIL_", "ROUTES", "INITIAL_IMPORT", "DRY_RUN")):
            monkeypatch.delenv(name, raising=False)


def base_env(monkeypatch):
    monkeypatch.setenv("ICLOUD_USERNAME", "you@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv(
        "GMAIL_OAUTH_JSON",
        json.dumps({"client_id": "cid", "client_secret": "cs", "refresh_token": "rt"}),
    )


def test_defaults_route_inbox_and_junk(monkeypatch):
    base_env(monkeypatch)
    config = load_config()

    assert config.routes == (Route("INBOX", ("INBOX",)), Route(r"\Junk", ("SPAM",)))
    # Apple の表示どおり空白入りで貼られても通るようにする
    assert config.icloud_app_password == "abcdefghijklmnop"
    assert config.icloud_host == "imap.mail.me.com"


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
