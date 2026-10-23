"""Tool plumbing: schema, validation, error handling and the truncated answer.

The model never executes anything - it asks, and this is where the asking is
checked before anything runs (SPEC-agent 3). A tool that raises is a bug; a tool
that fails answers with ``is_error`` so the loop can react and carry on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from clipper.agent.channel import Channel
from clipper.config import Settings
from clipper.profiles import Profile, ProfileError
from clipper.state import State
from clipper.utils.errors import ClipperError, InputError

#: Appended when an answer had to be cut; the model needs to know it is partial.
TRUNCATION_NOTE = '…"[truncado]"'


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a handler may touch. Nothing global, so tests build their own."""

    settings: Settings
    run_id: str = ""
    #: Formats this channel publishes. A profile outside it does not exist for
    #: the agent (SPEC-agent 2.1) - long is off until the channel says otherwise.
    enabled_profiles: tuple[str, ...] = ()
    logger: logging.Logger | None = None
    #: A rehearsal: publish_clips refuses to send anything, whatever it is asked.
    dry_run: bool = False
    #: The publishing channel of this run, when one is configured (SPEC-agent 7).
    channel: Channel | None = None
    #: Clips you vetoed from the monitor. Shared by reference with the runtime,
    #: which refreshes it from the control file before every call (SPEC-agent 9.5).
    vetoed: set[str] = field(default_factory=set)

    def state(self) -> State:
        return State(self.settings.state_db)

    def profile(self, name: str) -> Profile:
        """Resolve a profile the channel actually publishes."""
        if self.enabled_profiles and name not in self.enabled_profiles:
            raise InputError(
                f"o perfil '{name}' esta desabilitado para este canal "
                f"(habilitados: {', '.join(self.enabled_profiles)})"
            )
        try:
            return self.settings.profile(name)
        except ProfileError as exc:
            raise InputError(str(exc)) from exc

    def log(self, message: str, *args: object) -> None:
        if self.logger is not None:
            self.logger.info(message, *args)


class ToolResult(BaseModel):
    """The MCP shape, so the runtime can hand it straight to the SDK."""

    model_config = ConfigDict(extra="forbid")

    content: list[dict[str, str]]
    is_error: bool = False
    #: Nothing expensive happened - the answer came from disk. The runtime reads
    #: this to keep a reused render from costing a render of the budget.
    reused: bool = False

    @property
    def text(self) -> str:
        return "".join(block.get("text", "") for block in self.content)

    def for_sdk(self) -> dict[str, object]:
        """The shape the in-process MCP server forwards: content and is_error."""
        return {"content": list(self.content), "is_error": self.is_error}


def text_result(payload: str, *, is_error: bool = False) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": payload}], is_error=is_error)


def error_result(message: str) -> ToolResult:
    return text_result(message, is_error=True)


def truncate(text: str, limit: int) -> str:
    """Cut an answer to the tool's ceiling, saying so."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATION_NOTE))] + TRUNCATION_NOTE


def readable_errors(exc: ValidationError) -> str:
    """Pydantic's report, as a line the model can act on."""
    parts = []
    for error in exc.errors():
        where = ".".join(str(item) for item in error["loc"]) or "(raiz)"
        parts.append(f"{where}: {error['msg']}")
    return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool: what it is for, what it accepts, and what it may answer."""

    name: str
    #: Oriented at *when to use it*, not at what it does (SPEC-agent 5).
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], BaseModel]
    output_limit: int
    #: No side effects, so the SDK may run it alongside other read-only calls.
    read_only: bool = True

    @property
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema straight from the pydantic model - one source of truth."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def call(self, ctx: ToolContext, raw: dict[str, Any]) -> ToolResult:
        """Validate, run, and turn anything that goes wrong into a tool error."""
        try:
            args = self.input_model.model_validate(raw)
        except ValidationError as exc:
            return error_result(f"entrada invalida para '{self.name}': {readable_errors(exc)}")

        try:
            payload = self.handler(ctx, args)
        except (ClipperError, ProfileError, ValueError) as exc:
            if ctx.logger is not None:
                ctx.logger.warning("%s falhou: %s", self.name, exc)
            return error_result(f"{self.name}: {exc}")
        except Exception as exc:  # uma quebra nao pode derrubar a run (SPEC-agent 3)
            if ctx.logger is not None:
                ctx.logger.exception("%s quebrou", self.name)
            return error_result(f"{self.name}: erro inesperado ({type(exc).__name__}): {exc}")

        body = truncate(payload.model_dump_json(exclude_none=True), self.output_limit)
        reused = bool(getattr(payload, "reused", False))
        return text_result(body).model_copy(update={"reused": reused})


def parse_clip_id(clip_id: str) -> tuple[str, str, int]:
    """``<video_id>/<perfil>/<indice>`` into its three parts."""
    parts = clip_id.strip().split("/")
    if len(parts) != 3 or not all(parts):
        raise InputError(f"clip_id invalido: {clip_id!r} (use <video_id>/<perfil>/<indice>)")
    try:
        index = int(parts[2])
    except ValueError as exc:
        raise InputError(f"clip_id invalido: {clip_id!r} (o indice deve ser um numero)") from exc
    return parts[0], parts[1], index
