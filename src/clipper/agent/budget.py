"""The run's fuses (SPEC-agent 8.2).

Two kinds of limit, on purpose. A **soft** one refuses the tool and lets the loop
carry on - out of renders still means the agent can publish what it has and
finish. A **hard** one ends the loop, and the runtime closes the run itself so
work that was already done is not stranded on the machine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from clipper.config import AgentSettings

#: How much of a hard limit may be spent before the run should be closing.
LANDING_SHARE = 0.85


class BudgetLine(BaseModel):
    """One limit as the log and the monitor show it."""

    name: str
    used: float
    limit: float
    unit: str = ""

    @property
    def share(self) -> float:
        return self.used / self.limit if self.limit else 0.0


class BudgetView(BaseModel):
    lines: list[BudgetLine] = Field(default_factory=list)

    def line(self, name: str) -> BudgetLine | None:
        return next((item for item in self.lines if item.name == name), None)


@dataclass
class Budget:
    """Counters for one run. Nothing here talks to the model or to disk."""

    limits: AgentSettings
    started_at: float = field(default_factory=time.monotonic)
    turns: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    renders: int = 0
    publishes: int = 0
    cache_read_tokens: int = 0
    calls: int = 0
    warned: bool = False
    clip_renders: dict[str, int] = field(default_factory=dict)
    scans: dict[str, int] = field(default_factory=dict)

    # -- clocks ------------------------------------------------------------ #

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def timeout_for(self, tool: str) -> float:
        """How long one tool may take before it is abandoned."""
        return {
            "transcribe_video": self.limits.transcribe_timeout_s,
            "render_clip": self.limits.render_timeout_s,
            "publish_clips": self.limits.publish_timeout_s,
            "find_candidates": self.limits.publish_timeout_s,
        }.get(tool, self.limits.tool_timeout_s)

    # -- soft limits: refuse the call, keep the loop ------------------------ #

    def refusal(self, tool: str, args: dict[str, Any]) -> str | None:
        """Why this call may not run, or ``None`` when it may."""
        if tool == "render_clip":
            if self.renders >= self.limits.max_renders:
                return (
                    f"orcamento de renders esgotado ({self.renders}/{self.limits.max_renders}); "
                    "publique o que ja esta pronto e finalize"
                )
            key = self._clip_key(args)
            spent = self.clip_renders.get(key, 0)
            if spent >= self.limits.max_clip_renders:
                return (
                    f"o corte {key} ja foi renderizado {spent} vezes "
                    f"(maximo {self.limits.max_clip_renders}); mude de abordagem ou siga sem ele"
                )
        if tool == "find_candidates":
            profile = str(args.get("profile", ""))
            spent = self.scans.get(profile, 0)
            if spent >= self.limits.max_scans_per_profile:
                return (
                    f"o perfil '{profile}' ja foi varrido {spent} vezes "
                    f"(maximo {self.limits.max_scans_per_profile}); trabalhe com o que voltou, "
                    "inclusive com os rejeitados e o motivo de cada um"
                )
        if (
            tool == "publish_clips"
            and not args.get("dry_run")
            and self.publishes >= self.limits.max_publishes
        ):
            return (
                f"ja houve {self.publishes} publicacao(oes) nesta run "
                f"(maximo {self.limits.max_publishes}); finalize com finish_run"
            )
        return None

    def spend(self, tool: str, args: dict[str, Any], *, reused: bool = False) -> None:
        """Record a call that actually ran.

        A render that reused the file on disk is not a render: the expensive
        thing did not happen, and counting it would punish idempotency.
        """
        if tool == "render_clip" and not reused:
            self.renders += 1
            key = self._clip_key(args)
            self.clip_renders[key] = self.clip_renders.get(key, 0) + 1
        elif tool == "find_candidates" and not reused:
            profile = str(args.get("profile", ""))
            self.scans[profile] = self.scans.get(profile, 0) + 1
        elif tool == "publish_clips" and not args.get("dry_run"):
            self.publishes += 1

    @staticmethod
    def _clip_key(args: dict[str, Any]) -> str:
        return f"{args.get('profile', '?')}/{args.get('index', '?')}"

    # -- hard limits: end the loop ------------------------------------------ #

    def note_turn(self) -> None:
        """One answer from the model. Counted here, not from the SDK's report.

        The SDK sends a single result message per query, so counting those left
        a forty-turn run reporting one turn - measured on the first real run.
        """
        self.turns += 1

    def note_usage(self, usage: dict[str, Any]) -> None:
        """Tokens of one query.

        ``input_tokens`` alone is nearly always a handful: on a cached
        conversation everything real sits in the cache fields. What is charged as
        fresh input is the uncached part plus what was written to the cache; what
        is read back from it is an order of magnitude cheaper and is reported on
        its own line.
        """
        fresh = int(usage.get("input_tokens", 0) or 0)
        written = int(usage.get("cache_creation_input_tokens", 0) or 0)
        read = int(usage.get("cache_read_input_tokens", 0) or 0)
        self.input_tokens += max(0, fresh + written)
        self.cache_read_tokens += max(0, read)
        self.output_tokens += max(0, int(usage.get("output_tokens", 0) or 0))

    def note_call(self) -> None:
        """One tool call that ran. The closest thing we own to the SDK's turn."""
        self.calls += 1

    def landing(self) -> str | None:
        """The first hard limit close enough that the run should be closing.

        Said once, and only as a note on a tool's answer. A run cut by a hard
        limit is interrupted mid-sentence: it has no turn left to publish, to
        write its channel notes or to tell the report what it left out - and
        that account is the one thing the runtime cannot reproduce for it.
        """
        if self.warned:
            return None
        reason = self._nearly_spent()
        if reason is None:
            return None
        self.warned = True
        return reason

    def _nearly_spent(self) -> str | None:
        limits = self.limits
        # Turns belong to the SDK, which counts them its own way; tool calls are
        # ours and track them closely enough to see the wall coming (measured:
        # a run cut at max_turns had made 50 calls, one that was not made 44).
        if self.calls >= limits.max_turns - limits.landing_margin:
            return f"restam poucos turnos ({self.calls} chamadas de {limits.max_turns})"
        if self.output_tokens >= LANDING_SHARE * limits.max_output_tokens:
            return f"tokens de saida quase no fim ({self.output_tokens}/{limits.max_output_tokens})"
        if self.input_tokens >= LANDING_SHARE * limits.max_input_tokens:
            return f"tokens de entrada quase no fim ({self.input_tokens}/{limits.max_input_tokens})"
        if self.elapsed_s >= LANDING_SHARE * limits.wall_clock_s:
            return (
                f"tempo quase no fim "
                f"({self.elapsed_s / 60:.0f} min de {limits.wall_clock_s / 60:.0f})"
            )
        return None

    def exhausted(self) -> str | None:
        """Why the loop must stop now, or ``None`` to carry on.

        Turns are deliberately not here. ``max_turns`` is enforced by the SDK,
        which owns the definition of a turn; counting our own and interrupting on
        it cut a run short at sixteen tool calls, and an interrupt also loses the
        usage report that only arrives with the final message.
        """
        if self.output_tokens >= self.limits.max_output_tokens:
            return (
                f"limite de tokens de saida atingido "
                f"({self.output_tokens}/{self.limits.max_output_tokens})"
            )
        if self.input_tokens >= self.limits.max_input_tokens:
            return (
                f"limite de tokens de entrada atingido "
                f"({self.input_tokens}/{self.limits.max_input_tokens})"
            )
        if self.elapsed_s >= self.limits.wall_clock_s:
            return (
                f"limite de tempo atingido "
                f"({self.elapsed_s / 60:.0f} min de {self.limits.wall_clock_s / 60:.0f})"
            )
        return None

    # -- reporting ---------------------------------------------------------- #

    def renders_left(self) -> int:
        return max(0, self.limits.max_renders - self.renders)

    def view(self) -> BudgetView:
        return BudgetView(
            lines=[
                # No limit of ours: the SDK owns max_turns and counts turns its
                # own way (a run of 39 calls reported 75 answers and was not
                # stopped). A bar against our number would be fiction.
                BudgetLine(name="respostas do modelo", used=self.turns, limit=0),
                BudgetLine(name="renders", used=self.renders, limit=self.limits.max_renders),
                BudgetLine(
                    name="tokens de saida",
                    used=self.output_tokens,
                    limit=self.limits.max_output_tokens,
                    unit="tokens",
                ),
                BudgetLine(
                    name="tokens de entrada",
                    used=self.input_tokens,
                    limit=self.limits.max_input_tokens,
                    unit="tokens",
                ),
                # Reported, never limited: it is the cheap tenth of the bill.
                BudgetLine(
                    name="leitura de cache",
                    used=self.cache_read_tokens,
                    limit=0,
                    unit="tokens",
                ),
                BudgetLine(
                    name="publicacoes", used=self.publishes, limit=self.limits.max_publishes
                ),
                BudgetLine(
                    name="tempo",
                    used=round(self.elapsed_s / 60, 1),
                    limit=round(self.limits.wall_clock_s / 60, 1),
                    unit="min",
                ),
            ]
        )
