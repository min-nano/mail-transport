"""ローカル実行用 CLI: ``python -m mailtransport.cli``.

デプロイ前に .env を読み込んだ状態で疎通確認するために使う。
``--dry-run`` を付けると Gmail への挿入も状態の保存も行わない。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from mailtransport.logging_setup import setup_logging

log = logging.getLogger(__name__)


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(name.strip(), value)


def _print_leftovers(path: str) -> int:
    """転送されずに iCloud へ残ったメールを一覧する."""
    from mailtransport.state import SqliteStateStore

    if not os.path.exists(path):
        log.error("状態ファイルが見つかりません: %s (STATE_DB_PATH で指定できます)", path)
        return 2
    rows = [row.to_dict() for row in SqliteStateStore(path).list_leftovers()]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="iCloud から Gmail へメールを転送する")
    parser.add_argument("--env-file", default=".env", help="読み込む環境変数ファイル")
    parser.add_argument("--dry-run", action="store_true", help="転送も状態保存も行わない")
    parser.add_argument("--loop", type=int, default=0, help="指定秒間隔で繰り返す (0 で 1 回のみ)")
    parser.add_argument(
        "--leftovers",
        action="store_true",
        help="転送されずに iCloud に残っているメールを一覧して終了する",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="常駐して IMAP IDLE で新着を待ち受ける (常時起動ホスト向け)",
    )
    args = parser.parse_args(argv)

    _load_env_file(args.env_file)
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    setup_logging()

    if args.leftovers:
        # 受信トレイを手で整理する前に、Gmail に無いメールが混じっていないか見る。
        # 状態ファイルを読むだけなので、Gmail や iCloud の資格情報は要らない。
        return _print_leftovers(os.environ.get("STATE_DB_PATH") or "./state.db")

    from mailtransport.config import load_config
    from mailtransport.state import build_state_store
    from mailtransport.sync import sync_once

    try:
        config = load_config()
    except ValueError as exc:
        log.error("設定エラー: %s", exc)
        return 2

    store = build_state_store(config)

    if args.daemon:
        import threading

        from mailtransport.daemon import install_signal_handlers, run_daemon

        stop = threading.Event()
        install_signal_handlers(stop)
        return run_daemon(config, store, stop=stop)

    import time

    while True:
        report = sync_once(config, store)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if args.loop <= 0:
            return 1 if report.failed else 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
