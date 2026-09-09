"""GitHub Actions から呼ばれる入口.

  python -m tools.pr_review

認証はサブスクリプションの OAuth トークン (CLAUDE_CODE_OAUTH_TOKEN) を使う。
API キーを使うと API のクレジットを別途消費してしまうため、意図的に避けている。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from tools.pr_review import comment, context, reviewer, triage

log = logging.getLogger("pr_review")

# 環境変数の「名前」であって値ではない。名前に token / key を含めると、
# ログ出力に流したときにコード走査が機微情報の流出と誤検出する。
SUBSCRIPTION_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
API_BILLING_ENV = "ANTHROPIC_API_KEY"


def _notice(message: str) -> None:
    print(f"::notice::{message}")


def _warn(message: str) -> None:
    print(f"::warning::{message}")


def _event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def ensure_subscription_auth() -> bool:
    """サブスクリプションの枠だけを使うようにする.

    API キーが環境に残っていると、そちらが使われて API のクレジットを
    消費しかねない。警告したうえで環境から外す (子プロセスは環境を継ぐ)。
    """
    if os.environ.pop(API_BILLING_ENV, None) is not None:
        _warn(
            "ANTHROPIC_API_KEY が設定されていましたが、API のクレジットを"
            "使わないよう無視します。CLAUDE_CODE_OAUTH_TOKEN を使ってください。"
        )
    return bool(os.environ.get(SUBSCRIPTION_ENV))


async def _review(pr_context, cwd: Path) -> tuple[triage.ModelChoice, reviewer.ReviewResult]:
    choice = await triage.choose(pr_context.summary(), triage.build_options())
    _notice(f"レビューに {choice.model} (effort={choice.effort}) を使います: {choice.reason}")

    with tempfile.TemporaryDirectory() as workdir:
        diff_path = Path(workdir) / "pull-request.diff"
        diff_path.write_text(pr_context.diff, encoding="utf-8")

        options = reviewer.build_options(choice.model, choice.effort, cwd=cwd)
        result = await reviewer.run(reviewer.build_prompt(pr_context, diff_path), options)
    return choice, result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not ensure_subscription_auth():
        # トークンが無いのは「まだ設定していない」だけのことが多い。
        # レビューは必須チェックではないので、赤くせず知らせるにとどめる。
        _warn("CLAUDE_CODE_OAUTH_TOKEN が未設定のためレビューを行いません。")
        return 0

    event = _event()
    pull_request = event.get("pull_request") or {}
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    pr_number = pull_request.get("number")

    base_sha = (pull_request.get("base") or {}).get("sha")
    head_sha = (pull_request.get("head") or {}).get("sha")
    if not (pr_number and base_sha and head_sha):
        _warn("プルリクエストの情報を取得できませんでした。")
        return 0

    pr_context = context.collect(
        base_sha=base_sha,
        head_sha=head_sha,
        title=pull_request.get("title") or "",
        body=pull_request.get("body") or "",
    )
    if not pr_context.files:
        _notice("差分が無いためレビューを行いません。")
        return 0

    choice, result = asyncio.run(_review(pr_context, Path.cwd()))

    if not result.ok:
        _warn(f"レビューセッションが正常終了しませんでした (status={result.status})。")
    if not result.text.strip():
        _warn("レビュー結果が空でした。")
        return 0
    if result.cost_usd:
        _notice(f"レビューの費用: ${result.cost_usd:.4f}")

    if not token:
        _warn("GITHUB_TOKEN が無いため投稿しません。結果は下に出力します。")
        print(result.text)
        return 0

    action = comment.publish(repo, pr_number, token, comment.render(result.text, choice, head_sha))
    _notice(f"レビューを{'更新' if action == 'updated' else '投稿'}しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
