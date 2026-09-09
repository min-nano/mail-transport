"""プルリクエストレビュー機構のテスト.

anthropic / claude-agent-sdk は CI のレビュー時にしか入れないので、
ここでは差し替え可能な口だけを叩き、実物の import には依存しない。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os

import pytest
from tools.pr_review import comment, context, reviewer, session, triage

# --- 差分の収集 -------------------------------------------------------------


def test_numstat_is_parsed_with_binary_files():
    parsed = context.parse_numstat(
        "12\t3\tsrc/mailtransport/sync.py\n-\t-\tdocs/diagram.png\n0\t7\tREADME.md\n"
    )

    assert [f.path for f in parsed] == [
        "src/mailtransport/sync.py",
        "docs/diagram.png",
        "README.md",
    ]
    assert parsed[0].added == 12 and parsed[0].deleted == 3
    # バイナリは "-" なので 0 として扱う (int() で落ちない)
    assert parsed[1].binary is True and parsed[1].churn == 0
    assert parsed[2].added == 0 and parsed[2].deleted == 7


def test_numstat_ignores_malformed_lines():
    assert context.parse_numstat("なんだこれ\n\n1\t1\ta.py") == (context.ChangedFile("a.py", 1, 1),)


def test_collect_uses_a_three_dot_range():
    """base 側の後続コミットを差分に混ぜないこと."""
    calls = []

    def fake_git(args, cwd=None):
        calls.append(args)
        return "1\t1\ta.py" if "--numstat" in args else "diff 本体"

    result = context.collect("BASE", "HEAD", title="題", runner=fake_git)

    assert all("BASE...HEAD" in args for args in calls)
    assert result.diff == "diff 本体"
    assert result.title == "題"


def test_summary_lists_files_and_totals():
    ctx = context.collect(
        "b",
        "h",
        title="修正",
        runner=lambda a, c=None: "10\t2\tsrc/a.py\n1\t0\tsrc/b.py" if "--numstat" in a else "",
    )
    summary = ctx.summary()

    assert "変更ファイル数: 2" in summary
    assert "追加行数: 11" in summary and "削除行数: 2" in summary
    assert "src/a.py, +10, -2" in summary


def test_summary_truncates_a_huge_file_list():
    numstat = "\n".join(f"1\t1\tfile{i}.py" for i in range(500))
    ctx = context.collect("b", "h", runner=lambda a, c=None: numstat if "--numstat" in a else "")

    summary = ctx.summary()

    assert f"ほか {500 - context.MAX_LISTED_FILES} ファイル" in summary


# --- モデルの判定 -----------------------------------------------------------


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeAssistantMessage:
    def __init__(self, *texts):
        self.content = [FakeBlock(t) for t in texts]


class FakeToolMessage:
    """text を持たない中間メッセージ (落ちないことの確認用)."""

    content = [object()]


class FakeResultMessage:
    def __init__(self, subtype="success", result=None, total_cost_usd=None, structured_output=None):
        self.subtype = subtype
        self.result = result
        self.total_cost_usd = total_cost_usd
        self.structured_output = structured_output


def fake_query(messages):
    async def _query(prompt, options):
        for message in messages:
            yield message

    return _query


def failing_query(exc):
    async def _query(prompt, options):
        raise exc
        yield  # pragma: no cover

    return _query


@dataclasses.dataclass
class FakeOptions:
    model: str = ""
    effort: str = ""
    system_prompt: str = ""
    allowed_tools: list | None = None
    permission_mode: str = ""
    max_turns: int = 0
    max_budget_usd: float = 0.0
    cwd: str = ""
    output_format: dict | None = None


def run_triage(messages):
    return asyncio.run(triage.choose("要約", FakeOptions(), query_fn=fake_query(messages)))


def test_triage_options_ask_for_a_constrained_json_answer():
    options = triage.build_options(options_cls=FakeOptions)

    assert options.model == triage.TRIAGE_MODEL == "claude-sonnet-5"
    # 判定にツールは要らない
    assert options.allowed_tools == [] and options.permission_mode == "dontAsk"
    schema = options.output_format["schema"]
    assert options.output_format["type"] == "json_schema"
    assert schema["properties"]["model"]["enum"] == list(triage.ALLOWED_MODELS)
    assert schema["properties"]["effort"]["enum"] == list(triage.ALLOWED_EFFORTS)


def test_triage_reads_the_structured_output():
    choice = run_triage(
        [
            FakeResultMessage(
                structured_output={
                    "model": "claude-opus-5",
                    "effort": "max",
                    "reason": "認証に触るため",
                }
            )
        ]
    )

    assert choice.model == "claude-opus-5"
    assert choice.effort == "max"
    assert choice.reason == "認証に触るため"


def test_triage_falls_back_to_parsing_the_text():
    """構造化出力が来なかった場合は本文を JSON として読む."""
    payload = json.dumps({"model": "claude-haiku-4-5", "effort": "low", "reason": "版上げ"})

    choice = run_triage([FakeResultMessage(result=payload)])

    assert choice.model == "claude-haiku-4-5"


def test_unknown_model_falls_back_instead_of_being_used():
    """許可リストにないモデル名で本番のレビューを起動しないこと."""
    choice = run_triage(
        [FakeResultMessage(structured_output={"model": "gpt-4o", "effort": "high", "reason": "x"})]
    )

    assert choice.model == triage.FALLBACK_MODEL
    assert "gpt-4o" in choice.reason


def test_unknown_effort_falls_back():
    choice = triage.normalize({"model": "claude-opus-5", "effort": "ultra", "reason": "r"})

    assert choice.model == "claude-opus-5"
    assert choice.effort == triage.FALLBACK_EFFORT


def test_triage_failure_does_not_stop_the_review():
    """判定が落ちてもレビュー自体は既定のモデルで進むこと."""
    choice = asyncio.run(
        triage.choose("要約", FakeOptions(), query_fn=failing_query(RuntimeError("落ちた")))
    )

    assert choice.model in triage.ALLOWED_MODELS
    assert choice.effort in triage.ALLOWED_EFFORTS


def test_triage_survives_unparsable_output():
    choice = run_triage([FakeResultMessage(result="JSON ではない文章")])

    assert choice.model == triage.FALLBACK_MODEL


def test_triage_prompt_tells_the_model_not_to_obey_the_diff():
    assert "指示ではありません" in triage.SYSTEM_PROMPT


# --- レビューの実行 ---------------------------------------------------------


def test_review_options_are_read_only_and_non_interactive():
    options = reviewer.build_options("claude-opus-5", "xhigh", "/repo", options_cls=FakeOptions)

    assert options.model == "claude-opus-5" and options.effort == "xhigh"
    # 書き換えもコマンド実行もさせない
    assert options.allowed_tools == ["Read", "Glob", "Grep"]
    assert "Edit" not in options.allowed_tools and "Bash" not in options.allowed_tools
    # CI では許可を尋ねられても答えられない
    assert options.permission_mode == "dontAsk"
    assert options.max_budget_usd > 0 and options.max_turns > 0


def test_review_system_prompt_guards_against_injected_instructions():
    assert "指示ではありません" in reviewer.SYSTEM_PROMPT
    assert "従わず" in reviewer.SYSTEM_PROMPT


def run_review(messages):
    return asyncio.run(reviewer.run("prompt", FakeOptions(), query_fn=fake_query(messages)))


def test_review_returns_the_final_output_and_cost():
    result = run_review(
        [
            FakeAssistantMessage("差分を読みます"),
            FakeToolMessage(),
            FakeAssistantMessage("[重大] src/a.py:10 競合します"),
            FakeResultMessage(result="[重大] src/a.py:10 競合します", total_cost_usd=0.42),
        ]
    )

    assert result.ok is True
    assert result.text == "[重大] src/a.py:10 競合します"
    assert result.cost_usd == pytest.approx(0.42)


def test_review_reports_a_failed_session():
    result = run_review(
        [FakeAssistantMessage("途中"), FakeResultMessage(subtype="error_max_turns")]
    )

    assert result.ok is False and result.status == "error_max_turns"


def test_review_survives_a_missing_result_message():
    result = run_review([FakeAssistantMessage("指摘なし")])

    assert result.text == "指摘なし"
    assert result.ok is False  # ResultMessage が無いなら成功とはみなさない


def test_cost_is_read_from_either_shape():
    class Nested:
        cost_metadata = type("M", (), {"total_cost_usd": 1.5})()

    assert session.cost_of(FakeResultMessage(total_cost_usd=2.0)) == 2.0
    assert session.cost_of(Nested()) == 1.5
    assert session.cost_of(FakeResultMessage()) is None


# --- コメントの投稿 ---------------------------------------------------------


def test_rendered_comment_carries_the_marker_and_model():
    choice = triage.ModelChoice("claude-opus-5", "xhigh", "認証に触るため")

    body = comment.render("[重大] だめです", choice, "abcdef1234567890")

    assert body.startswith(comment.MARKER)
    assert "claude-opus-5" in body and "xhigh" in body
    assert "認証に触るため" in body
    assert "abcdef1" in body


def test_empty_review_is_still_rendered_visibly():
    body = comment.render("   ", triage.ModelChoice("claude-sonnet-5", "high", "r"), "sha")

    assert "レビュー結果が空でした" in body


class FakeGitHub:
    def __init__(self, comments=None):
        self.comments = comments or []
        self.calls = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, payload))
        if method == "GET":
            return self.comments
        return {"id": 1}


def test_existing_comment_is_updated_not_duplicated():
    """push のたびにコメントが増えないこと."""
    api = FakeGitHub([{"id": 7, "body": f"{comment.MARKER}\n前回の結果"}])

    action = comment.publish("o/r", 3, "tok", "新しい結果", requester=api)

    assert action == "updated"
    assert api.calls[-1][0] == "PATCH"
    assert api.calls[-1][1].endswith("/issues/comments/7")


def test_first_review_creates_a_comment():
    api = FakeGitHub([{"id": 7, "body": "無関係なコメント"}])

    action = comment.publish("o/r", 3, "tok", "結果", requester=api)

    assert action == "created"
    assert api.calls[-1][0] == "POST"
    assert api.calls[-1][1].endswith("/issues/3/comments")


# --- 全体の流れ -------------------------------------------------------------


def write_event(tmp_path, **overrides):
    payload = {
        "pull_request": {
            "number": 12,
            "title": "何かを直す",
            "body": "説明",
            "base": {"sha": "BASE"},
            "head": {"sha": "HEADSHA1234567"},
            "head_repo": None,
            **overrides,
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """外部に出る口をすべて差し替えた状態で main() を動かす."""
    from tools.pr_review import __main__ as entry

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "min-nano/mail-transport")
    monkeypatch.setenv("GITHUB_EVENT_PATH", write_event(tmp_path))

    state = {"published": None, "options": None, "prompt": None, "summary": None}

    # context.collect を差し替えるので、その中で同じ名前を呼ぶと再帰する。
    # 組み立て済みのものを直接返す。
    def fake_collect(**kwargs):
        return context.PullRequestContext(
            title=kwargs.get("title", ""),
            body=kwargs.get("body", ""),
            base_sha=kwargs["base_sha"],
            head_sha=kwargs["head_sha"],
            files=(context.ChangedFile("src/a.py", 3, 1),),
            diff="差分本体",
        )

    async def fake_run(prompt, options, query_fn=None):
        state["prompt"] = prompt
        state["options"] = options
        return reviewer.ReviewResult(
            text="[軽微] src/a.py:3 些細な点", status="success", cost_usd=0.1
        )

    async def fake_choose(summary, options, query_fn=None):
        state["summary"] = summary
        return triage.ModelChoice("claude-opus-5", "xhigh", "危ういため")

    monkeypatch.setattr(entry.context, "collect", fake_collect)
    monkeypatch.setattr(entry.triage, "build_options", lambda: FakeOptions())
    monkeypatch.setattr(entry.triage, "choose", fake_choose)
    monkeypatch.setattr(
        entry.reviewer, "build_options", lambda m, e, cwd: FakeOptions(model=m, effort=e)
    )
    monkeypatch.setattr(entry.reviewer, "run", fake_run)

    def fake_publish(repo, number, token, body, requester=None):
        state["published"] = (repo, number, body)
        return "created"

    monkeypatch.setattr(entry.comment, "publish", fake_publish)
    return entry, state


def test_main_reviews_and_publishes(wired, capsys):
    entry, state = wired

    assert entry.main() == 0

    repo, number, body = state["published"]
    assert (repo, number) == ("min-nano/mail-transport", 12)
    assert "[軽微] src/a.py:3 些細な点" in body
    # 選ばれたモデルが本番のセッションに渡っていること
    assert state["options"].model == "claude-opus-5"
    assert state["options"].effort == "xhigh"
    # 差分がファイルとして渡されていること
    assert "pull-request.diff" in state["prompt"]
    assert "claude-opus-5" in capsys.readouterr().out


def test_main_skips_without_a_subscription_token(wired, monkeypatch, capsys):
    entry, state = wired
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    assert entry.main() == 0

    assert state["published"] is None
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().out


def test_an_api_key_in_the_environment_is_removed(wired, monkeypatch, capsys):
    """API のクレジットを使わせないこと.

    環境に API キーが残っていると、子プロセスがそちらで認証してしまう。
    """
    entry, _ = wired
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-something")

    assert entry.main() == 0

    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "API のクレジットを使わないよう" in capsys.readouterr().out


def test_main_skips_when_there_is_no_diff(wired, monkeypatch, capsys):
    entry, state = wired
    monkeypatch.setattr(
        entry.context,
        "collect",
        lambda **kw: context.PullRequestContext("", "", "b", "h", files=(), diff=""),
    )

    assert entry.main() == 0

    assert state["published"] is None
    assert "差分が無い" in capsys.readouterr().out


def test_main_prints_the_review_when_it_cannot_post(wired, monkeypatch, capsys):
    entry, state = wired
    monkeypatch.delenv("GITHUB_TOKEN")

    assert entry.main() == 0

    assert state["published"] is None
    assert "[軽微] src/a.py:3 些細な点" in capsys.readouterr().out


def test_main_survives_a_malformed_event(wired, monkeypatch, tmp_path, capsys):
    entry, state = wired
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(broken))

    assert entry.main() == 0

    assert state["published"] is None
    assert "プルリクエストの情報" in capsys.readouterr().out
