"""The run's JSONL trail (SPEC-agent 8.3).

One line per event, appended and flushed as it happens: the monitor tails this
file while the run is going (SPEC-agent 9), and it is the only record of why the
agent did what it did. So it is written to be complete enough to rebuild the run
from, and nothing else is expected to remember.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from clipper.agent.budget import Budget

#: Tool answers are capped here; the tool's own ceiling is usually tighter.
OUTPUT_LIMIT = 4000
#: What the model said before calling something. Enough to see the reasoning.
TEXT_LIMIT = 2000

#: Last line of defence: no handler receives a key, but a library error message
#: can quote one. Redacting the serialized line catches it wherever it sits.
_SECRETS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{32,}"),
)
REDACTED = "[redigido]"


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(REDACTED, text)
    return text


def clip_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to ``limit``, saying whether anything was lost."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class RunLog:
    """Append-only JSONL for one run."""

    def __init__(self, path: Path, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self._handle = path.open("a", encoding="utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Write one event and return it, for whoever wants to show it too."""
        self.seq += 1
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "seq": self.seq,
            "type": kind,
            **fields,
        }
        line = redact(json.dumps(record, ensure_ascii=False, default=str))
        self._handle.write(line + "\n")
        # Flushed line by line: the monitor is reading this file as it is written,
        # and a run that dies must leave everything up to its last step behind.
        self._handle.flush()
        return record

    # -- the events of SPEC-agent 8.3 --------------------------------------- #

    def run_start(self, video_id: str, *, url: str, model: str, budget: Budget) -> None:
        self.event(
            "run_start",
            video_id=video_id,
            url=url,
            model=model,
            budget=budget.view().model_dump(),
        )

    def turn(self, text: str, *, budget: Budget) -> None:
        body, truncated = clip_text(text.strip(), TEXT_LIMIT)
        if not body:
            return
        self.event("turn", text=body, text_truncated=truncated, turns=budget.turns)

    def tool_start(self, tool: str, args: dict[str, Any]) -> None:
        """Logged before the call runs.

        Without it a twenty-minute render is indistinguishable from a stalled
        log: the monitor has nothing to put a stopwatch on (SPEC-agent 9.1).
        """
        self.event("tool_start", tool=tool, input=args)

    def tool_call(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        output: str,
        duration_ms: int,
        is_error: bool,
        budget: Budget,
    ) -> None:
        body, truncated = clip_text(output, OUTPUT_LIMIT)
        self.event(
            "tool_error" if is_error else "tool_call",
            tool=tool,
            input=args,
            output=body,
            output_truncated=truncated,
            duration_ms=duration_ms,
            error=body if is_error else None,
            budget={
                "renders_used": budget.renders,
                "renders_left": budget.renders_left(),
                "turns": budget.turns,
            },
        )

    def budget_deny(self, tool: str, args: dict[str, Any], reason: str) -> None:
        self.event("budget_deny", tool=tool, input=args, reason=reason)

    def run_end(
        self,
        status: str,
        *,
        reason: str = "",
        budget: Budget,
        cost_usd: float = 0.0,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.event(
            "run_end",
            status=status,
            reason=reason,
            budget=budget.view().model_dump(),
            elapsed_s=round(budget.elapsed_s, 1),
            cost_usd=round(cost_usd, 4),
            usage=usage or {},
        )
