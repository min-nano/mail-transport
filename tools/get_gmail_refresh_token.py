#!/usr/bin/env python3
"""Gmail API 用のリフレッシュトークンを取得する (手元の PC で 1 回だけ実行する).

前提:
  1. GCP コンソールで OAuth 同意画面を構成し、「本番環境」に公開しておく
     (「テスト」のままだとリフレッシュトークンが 7 日で失効する)
  2. 「デスクトップ アプリ」種別の OAuth クライアント ID を作成し、
     client_secret JSON をダウンロードしておく

使い方:
    pip install google-auth-oauthlib
    python tools/get_gmail_refresh_token.py --client-secret ~/Downloads/client_secret.json

出力された JSON をそのまま Secret Manager に登録する。
"""

from __future__ import annotations

import argparse
import json
import sys

# 取得するのは「挿入」権限のみ。閲覧・変更・送信の権限は要求しない。
SCOPES = ["https://www.googleapis.com/auth/gmail.insert"]


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
    parser.add_argument("--out", help="結果 JSON の書き出し先 (省略時は標準出力)")
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
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"{args.out} に書き出しました (取り扱い注意)", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
