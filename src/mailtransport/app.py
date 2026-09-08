"""Cloud Run 用の HTTP エントリポイント.

Cloud Scheduler から OIDC 認証付きで ``POST /sync`` を叩かれる想定。
サービス自体は ``--no-allow-unauthenticated`` で公開し、呼び出し元は
Cloud Run 起動元 (roles/run.invoker) を持つサービスアカウントだけに絞る。
アプリ側に共有シークレットを持たせないぶん安全で、鍵の管理も不要になる。
"""

from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify

from mailtransport.logging_setup import setup_logging

setup_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)

_lock = threading.Lock()
_config = None
_store = None
_gmail = None


def _get_config():
    global _config
    if _config is None:
        from mailtransport.config import load_config

        _config = load_config()
    return _config


def _get_store(config):
    global _store
    if _store is None:
        from mailtransport.state import build_state_store

        _store = build_state_store(config)
    return _store


def _get_gmail(config):
    """インスタンス内で Gmail クライアントを使い回し、トークン更新を減らす."""
    global _gmail
    if _gmail is None:
        from mailtransport.gmail_sink import GmailSink

        _gmail = GmailSink(config.gmail_auth, config.gmail_user_id)
    return _gmail


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/sync", methods=["POST", "GET"])
def sync():
    from mailtransport.sync import sync_once

    # Cloud Run のインスタンスは同時実行されうるが、Firestore のロックに加えて
    # プロセス内でも直列化しておく。
    with _lock:
        try:
            config = _get_config()
        except ValueError as exc:
            log.error("設定エラー: %s", exc)
            return jsonify({"status": "config_error", "message": str(exc)}), 500

        store = _get_store(config)
        report = sync_once(
            config,
            store,
            gmail_factory=None if config.dry_run else (lambda: _get_gmail(config)),
        )

    payload = report.to_dict()
    payload["status"] = "error" if report.failed else "ok"
    log.info("同期が完了しました", extra={"extra_fields": payload})
    return jsonify(payload), (500 if report.failed else 200)


@app.get("/")
def index():
    return jsonify({"service": "mail-transport", "endpoints": ["/sync", "/healthz"]})
