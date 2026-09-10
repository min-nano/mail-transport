from __future__ import annotations

import json
import logging

import pytest

from mailtransport.config import (
    VERIFY_AUTO,
    VERIFY_FORCE,
    VERIFY_OFF,
    Route,
    load_config,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(dict(__import__("os").environ)):
        if name.startswith(
            (
                "ICLOUD_",
                "GMAIL_",
                "ROUTES",
                "INITIAL_IMPORT",
                "DRY_RUN",
                "TRASH_",
                "VERIFY_",
                "SEEN_",
            )
        ):
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


def test_verification_is_enabled_by_default(monkeypatch):
    """既定では、確認できるなら確認してからゴミ箱へ移す (#21)."""
    base_env(monkeypatch)
    config = load_config()

    assert config.verify_before_trash == VERIFY_AUTO


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auto", VERIFY_AUTO),
        ("true", VERIFY_FORCE),
        ("1", VERIFY_FORCE),
        ("false", VERIFY_OFF),
        ("off", VERIFY_OFF),
    ],
)
def test_verify_before_trash_accepts_boolean_spellings(monkeypatch, raw, expected):
    base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_BEFORE_TRASH", raw)

    assert load_config().verify_before_trash == expected


def test_invalid_verify_before_trash_is_rejected(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_BEFORE_TRASH", "maybe")

    with pytest.raises(ValueError, match="VERIFY_BEFORE_TRASH"):
        load_config()


def test_dedupe_records_are_kept_forever_by_default(monkeypatch):
    """既定を無期限にして、受信トレイへ戻したメールの再転送を防ぐ (#24)."""
    base_env(monkeypatch)

    assert load_config().seen_retention_days == 0


def test_existing_mail_is_not_trashed_on_initial_import_by_default(monkeypatch):
    base_env(monkeypatch)

    assert load_config().trash_existing_on_initial_import is False


def test_route_can_override_trashing(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv(
        "ROUTES",
        json.dumps([{"source": "\\Junk", "labels": ["SPAM"], "trash": False}]),
    )
    config = load_config()

    assert config.routes == (Route("\\Junk", ("SPAM",), trash=False),)
    assert config.routes[0].trashes(True) is False
    assert Route("INBOX", ("INBOX",)).trashes(True) is True


def test_route_trash_must_be_boolean(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("ROUTES", json.dumps([{"source": "INBOX", "labels": ["INBOX"], "trash": 1}]))

    with pytest.raises(ValueError, match="trash"):
        load_config()


def test_spam_label_with_trashing_is_warned_about(monkeypatch, caplog):
    """Gmail も iCloud も 30 日で消す設定は、起動時に気付けるようにする (#26)."""
    base_env(monkeypatch)
    monkeypatch.setenv("ROUTES", json.dumps([{"source": "\\Junk", "labels": ["SPAM"]}]))

    with caplog.at_level(logging.WARNING, logger="mailtransport.config"):
        load_config()

    assert "30 日後にどちらにも残りません" in caplog.text


def test_no_warning_when_the_route_keeps_the_original(monkeypatch, caplog):
    base_env(monkeypatch)
    monkeypatch.setenv(
        "ROUTES",
        json.dumps([{"source": "\\Junk", "labels": ["SPAM"], "trash": False}]),
    )

    with caplog.at_level(logging.WARNING, logger="mailtransport.config"):
        load_config()

    assert caplog.text == ""
