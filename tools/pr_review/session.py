"""Claude Agent SDK のセッションを回す共通部分.

判定 (triage) もレビュー本体も同じ流れなので、ここにまとめる。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SessionOutcome:
    texts: list[str]
    structured: Any = None
    status: str = "error"
    cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def final_text(self) -> str:
        """最後にまとまった出力. 途中の思考や経過報告ではなく結論を採る."""
        for text in reversed(self.texts):
            if text.strip():
                return text.strip()
        return ""


def is_result(message) -> bool:
    """終了メッセージかどうか.

    型名ではなく持っている属性で判定する。SDK の型を import せずに済み、
    型名が変わっても、知らないメッセージが増えても落ちない。
    """
    return hasattr(message, "subtype") and hasattr(message, "result")


def texts_of(message) -> list[str]:
    """メッセージから本文だけを拾う.

    思考ブロック (.thinking) やツール結果 (.text を持たない) は自然に外れる。
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return []
    texts = (getattr(block, "text", None) for block in content)
    return [text for text in texts if isinstance(text, str) and text.strip()]


def cost_of(message) -> float | None:
    """実行費用を取り出す.

    サブスクリプションで認証している場合は報告されないことがあるので、
    取れなくても構わない扱いにする。
    """
    direct = getattr(message, "total_cost_usd", None)
    if isinstance(direct, (int, float)):
        return float(direct)
    metadata = getattr(message, "cost_metadata", None)
    nested = getattr(metadata, "total_cost_usd", None) if metadata else None
    return float(nested) if isinstance(nested, (int, float)) else None


async def run(prompt: str, options, query_fn=None) -> SessionOutcome:
    if query_fn is None:
        from claude_agent_sdk import query as query_fn

    outcome = SessionOutcome(texts=[])
    async for message in query_fn(prompt=prompt, options=options):
        if is_result(message):
            outcome.status = getattr(message, "subtype", "error") or "error"
            if getattr(message, "result", None):
                outcome.texts.append(message.result)
            outcome.structured = getattr(message, "structured_output", None)
            outcome.cost_usd = cost_of(message)
        else:
            outcome.texts.extend(texts_of(message))
    return outcome
