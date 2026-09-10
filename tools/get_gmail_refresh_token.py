#!/usr/bin/env python3
"""Gmail API 用のリフレッシュトークンを取得する (手元の PC で 1 回だけ実行する).

前提:
  1. GCP コンソールで OAuth 同意画面を構成し、「本番環境」に公開しておく
     (「テスト」のままだとリフレッシュトークンが 7 日で失効する)
  2. 「デスクトップ アプリ」種別の OAuth クライアント ID を作成し、
     client_secret JSON をダウンロードしておく

使い方:
    pip install google-auth-oauthlib
    python tools/get_gmail_refresh_token.py \
        --client-secret ~/Downloads/client_secret.json \
        --out deploy/gmail_oauth.json

出力された JSON をそのまま Secret Manager に登録する。
client secret とリフレッシュトークンを含むため、標準出力には出さず
所有者だけが読めるファイル (0600) にのみ書き出す。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 取得するのは「挿入」と「メタデータの閲覧」のみ。本文の閲覧・変更・削除・
# 送信の権限は要求しない。metadata は、挿入したメールを ID で引き直して
# 「Gmail に確かに入った」と確認するために使う (iCloud の原本を消す前の確認)。
SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.metadata",
]

# 所有者のみ読み書き。秘密情報を置くファイルの権限。
_SECRET_FILE_MODE = 0o600


def write_secret_file(path: str, text: str) -> None:
    """秘密情報を所有者だけが読めるファイルとして書き出す.

    既定の ``open()`` は umask 次第で他ユーザーにも読めるファイルを作るため、
    権限を明示して開く。``O_NOFOLLOW`` は、出力先がシンボリックリンクに
    すり替えられていた場合にリンク先へ書き込んでしまうのを防ぐ
    (対応していない環境では単に無視される)。
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, _SECRET_FILE_MODE)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        # open のモード指定は新規作成時にしか効かない。既存ファイルへ
        # 上書きするときも、緩い権限のまま秘密情報を置かないよう絞り直す。
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), _SECRET_FILE_MODE)
        handle.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secret",
        required=True,
        help="デスクトップアプリ種別の OAuth クライアント JSON のパス",
    )
    parser.add_argument("--port", type=int, default=0, help="ローカル受信ポート (既定: 空きポート)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="ブラウザを自動起動せず URL を表示するだけにする",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="結果 JSON の書き出し先 (0600 で作成される。標準出力には出さない)",
    )
    args = parser.parse_args(argv)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib が必要です: pip install google-auth-oauthlib", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    credentials = flow.run_local_server(
        port=args.port,
        open_browser=not args.no_browser,
        # 必ずリフレッシュトークンが返るようにする
        access_type="offline",
        prompt="consent",
    )

    if not credentials.refresh_token:
        print(
            "リフレッシュトークンが取得できませんでした。"
            "既に許可済みの場合は https://myaccount.google.com/permissions で"
            "アクセス権を削除してからやり直してください。",
            file=sys.stderr,
        )
        return 1

    payload = {
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        write_secret_file(args.out, text)
    except OSError as exc:
        # 例外の文字列にも秘密情報は入らない (パスと errno だけ)。
        print(f"{args.out} に書き出せませんでした: {exc}", file=sys.stderr)
        return 1
    print(f"{args.out} に書き出しました (取り扱い注意)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
