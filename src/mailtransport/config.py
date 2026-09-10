"""環境変数ベースの設定."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# iCloud のどのメールボックスを Gmail のどのラベルへ入れるかの既定マッピング。
# Gmail API の messages.insert はスパムフィルタを通さず、ここで指定したラベルが
# そのまま適用されるため、受信トレイのメールは受信トレイのまま届く。
#
# 迷惑メール (\Junk) は移動対象に含めない。iCloud 側で迷惑メールと判定された
# ものを Gmail に持ち込んでも読まないため、受信トレイのみを転送する。
# 必要なら ROUTES に {"source": "\\Junk", "labels": ["SPAM"]} を足せば戻せる。
DEFAULT_ROUTES: list[dict] = [
    {"source": "INBOX", "labels": ["INBOX"]},
]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - 設定ミスは即座に落とす
        raise ValueError(f"環境変数 {name} は整数である必要があります: {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_secret(direct_env: str, secret_ref_env: str) -> str | None:
    """値を環境変数から直接、なければ Secret Manager のリソース名経由で取得する.

    VM 上では ``*_SECRET`` にシークレット名を渡し、値は実行時に VM の
    サービスアカウントで取得する (ディスクに秘密情報を置かないため)。
    ローカル実行では direct_env に値を直接入れる。
    """
    value = _env(direct_env)
    if value:
        return value
    ref = _env(secret_ref_env)
    if not ref:
        return None
    from mailtransport.secrets import access_secret

    return access_secret(ref)


@dataclass(frozen=True)
class Route:
    """1 つの iCloud メールボックスから Gmail ラベルへの転送経路."""

    source: str
    labels: tuple[str, ...]

    @property
    def is_special_use(self) -> bool:
        """``\\Junk`` のような IMAP 特殊用途フラグ指定かどうか."""
        return self.source.startswith("\\")


@dataclass(frozen=True)
class GmailAuth:
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class Config:
    icloud_username: str
    icloud_app_password: str
    icloud_host: str
    icloud_port: int
    gmail_auth: GmailAuth
    gmail_user_id: str
    routes: tuple[Route, ...]
    trash_after_forward: bool
    trash_mailbox: str
    project_id: str | None
    seen_retention_days: int
    max_messages_per_run: int
    max_message_bytes: int
    lock_ttl_seconds: int
    run_budget_seconds: int
    initial_import: str
    dry_run: bool
    imap_timeout_seconds: int
    state_backend: str
    state_db_path: str
    idle_enabled: bool
    idle_refresh_seconds: int
    poll_interval_seconds: int
    safety_sync_seconds: int
    debounce_seconds: float
    reconnect_backoff_max_seconds: int
    metadata: dict = field(default_factory=dict)


def _parse_routes() -> tuple[Route, ...]:
    raw = _env("ROUTES")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"環境変数 ROUTES が JSON として不正です: {exc}") from exc
    else:
        parsed = DEFAULT_ROUTES
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("ROUTES は空でないリストである必要があります")

    routes: list[Route] = []
    for entry in parsed:
        if not isinstance(entry, dict) or "source" not in entry:
            raise ValueError(f"ROUTES の要素が不正です: {entry!r}")
        labels = entry.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        if not labels:
            raise ValueError(f"ROUTES の {entry['source']!r} に labels がありません")
        routes.append(Route(source=str(entry["source"]), labels=tuple(str(x) for x in labels)))
    return tuple(routes)


def _parse_gmail_auth() -> GmailAuth:
    """Gmail の OAuth クレデンシャルを読み込む.

    ``GMAIL_OAUTH_JSON`` に ``{"client_id","client_secret","refresh_token"}`` を
    含む JSON を渡す方式を推奨（Secret Manager の 1 シークレットで完結するため）。
    個別の環境変数でも指定できる。
    """
    blob = _resolve_secret("GMAIL_OAUTH_JSON", "GMAIL_OAUTH_JSON_SECRET")
    data: dict = {}
    if blob:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GMAIL_OAUTH_JSON が JSON として不正です: {exc}") from exc
        # gcloud で作成した client_secret.json をそのまま貼れるようにする
        if "installed" in data or "web" in data:
            inner = data.get("installed") or data.get("web") or {}
            merged = {k: v for k, v in data.items() if k not in {"installed", "web"}}
            merged.update(inner)
            data = merged

    client_id = _env("GMAIL_CLIENT_ID") or data.get("client_id")
    client_secret = _resolve_secret(
        "GMAIL_CLIENT_SECRET", "GMAIL_CLIENT_SECRET_SECRET"
    ) or data.get("client_secret")
    refresh_token = _resolve_secret(
        "GMAIL_REFRESH_TOKEN", "GMAIL_REFRESH_TOKEN_SECRET"
    ) or data.get("refresh_token")

    missing = [
        name
        for name, value in (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("refresh_token", refresh_token),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Gmail の OAuth 情報が不足しています: "
            + ", ".join(missing)
            + " (GMAIL_OAUTH_JSON もしくは個別の環境変数を設定してください)"
        )
    return GmailAuth(
        client_id=str(client_id),
        client_secret=str(client_secret),
        refresh_token=str(refresh_token),
        token_uri=str(data.get("token_uri") or "https://oauth2.googleapis.com/token"),
    )


def load_config() -> Config:
    """環境変数から設定を構築する. 不足があれば ValueError を送出する."""
    username = _env("ICLOUD_USERNAME")
    password = _resolve_secret("ICLOUD_APP_PASSWORD", "ICLOUD_APP_PASSWORD_SECRET")
    if not username:
        raise ValueError("環境変数 ICLOUD_USERNAME が未設定です")
    if not password:
        raise ValueError("ICLOUD_APP_PASSWORD (アプリ用パスワード) が未設定です")

    initial_import = (_env("INITIAL_IMPORT", "none") or "none").strip().lower()
    if initial_import not in {"none", "all"}:
        raise ValueError("INITIAL_IMPORT は 'none' か 'all' を指定してください")

    state_backend = (_env("STATE_BACKEND", "sqlite") or "sqlite").strip().lower()
    if state_backend not in {"sqlite", "memory"}:
        raise ValueError("STATE_BACKEND は 'sqlite' か 'memory' を指定してください")

    return Config(
        icloud_username=username,
        # アプリ用パスワードは Apple の表示に合わせて空白入りで貼られることが多い
        icloud_app_password=password.replace(" ", "").strip(),
        icloud_host=_env("ICLOUD_IMAP_HOST", "imap.mail.me.com"),
        icloud_port=_env_int("ICLOUD_IMAP_PORT", 993),
        gmail_auth=_parse_gmail_auth(),
        gmail_user_id=_env("GMAIL_USER_ID", "me"),
        routes=_parse_routes(),
        # 転送が終わったメールは iCloud に残さない (容量を空けるため)
        trash_after_forward=_env_bool("TRASH_AFTER_FORWARD", True),
        trash_mailbox=_env("TRASH_MAILBOX", r"\Trash"),
        project_id=_env("GOOGLE_CLOUD_PROJECT") or _env("GCP_PROJECT"),
        seen_retention_days=_env_int("SEEN_RETENTION_DAYS", 30),
        max_messages_per_run=_env_int("MAX_MESSAGES_PER_RUN", 40),
        # iCloud の送受信上限は 20MB 前後。Gmail insert は 50MB まで受け付ける。
        max_message_bytes=_env_int("MAX_MESSAGE_BYTES", 35 * 1024 * 1024),
        lock_ttl_seconds=_env_int("LOCK_TTL_SECONDS", 540),
        # 1 回の同期が長引いても、次の新着通知に反応できるよう打ち切る
        run_budget_seconds=_env_int("RUN_BUDGET_SECONDS", 240),
        initial_import=initial_import,
        dry_run=_env_bool("DRY_RUN", False),
        imap_timeout_seconds=_env_int("IMAP_TIMEOUT_SECONDS", 60),
        state_backend=state_backend,
        state_db_path=_env("STATE_DB_PATH", "./state.db"),
        idle_enabled=_env_bool("IDLE_ENABLED", True),
        # RFC 2177 は 29 分以内に IDLE を張り直すことを求めている
        idle_refresh_seconds=_env_int("IDLE_REFRESH_SECONDS", 1500),
        # IDLE が使えないサーバー向けのフォールバック間隔
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 60),
        # IDLE の取りこぼしに備えた定期同期
        safety_sync_seconds=_env_int("SAFETY_SYNC_SECONDS", 300),
        # 連続到着をまとめるための待ち時間
        debounce_seconds=float(_env("DEBOUNCE_SECONDS", "2") or 2),
        reconnect_backoff_max_seconds=_env_int("RECONNECT_BACKOFF_MAX_SECONDS", 300),
    )
