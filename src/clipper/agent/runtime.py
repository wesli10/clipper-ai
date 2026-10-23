"""The loop (SPEC-agent 8).

The model decides; this decides what the model is allowed to do. It owns the
limits, the log, the timeouts and the ending - including the ending the model
does not get to: when a fuse blows, the runtime publishes what is already
rendered and reviewed instead of leaving it on the machine.

The loop itself is :func:`drive`, which talks to a :class:`Session` - a protocol,
not the SDK. That is what lets the whole policy be tested with scripted messages,
no model and no network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from clipper.agent.budget import Budget
from clipper.agent.channel import Channel, as_prompt
from clipper.agent.channel import resolve as resolve_channel
from clipper.agent.control import (
    MAX_PAUSE_S,
    POLL_INTERVAL_S,
    ControlWatcher,
    clip_id_of,
    control_path,
)
from clipper.agent.log import RunLog
from clipper.agent.tools import SPECS, ToolContext, ToolResult, ToolSpec
from clipper.config import Settings
from clipper.state import RunStatus, State
from clipper.utils.console import console, info, success, warn
from clipper.utils.errors import ExternalError
from clipper.utils.logging import stage_logger

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
SYSTEM_PROMPT_NAME = "agent_system.md"

#: How many times the runtime nudges a model that stopped without finishing.
MAX_NUDGES = 2
#: Two identical failures in a row are a loop; the third attempt is refused.
MAX_IDENTICAL_FAILURES = 2


# --------------------------------------------------------------------------- #
# What a run produced
# --------------------------------------------------------------------------- #


@dataclass
class RunProgress:
    """What the agent actually got done, as the tools report it."""

    committed: dict[str, int] = field(default_factory=dict)
    rendered: int = 0
    published: bool = False
    finished: bool = False
    failures: dict[str, int] = field(default_factory=dict)
    #: Set when you asked the run to stop from the monitor (SPEC-agent 9.5).
    stop_requested: str = ""
    publish_on_stop: bool = True

    def profiles(self) -> list[str]:
        return [name for name, count in self.committed.items() if count]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    video_id: str
    status: RunStatus
    reason: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    report_path: Path | None = None


def new_run_id(video_id: str) -> str:
    """``<data>-<hora>-<video>``: sorts by time and says which video it was."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{video_id[:12]}"


# --------------------------------------------------------------------------- #
# The session the loop talks to
# --------------------------------------------------------------------------- #


class Session(Protocol):
    """What the loop needs from a client - the SDK's shape, minus everything else."""

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...


def message_text(message: Any) -> str:
    """The text blocks of an assistant message, if it is one.

    Duck-typed rather than isinstance: the loop is tested with scripted messages
    and must not import the SDK to do it, and a block type added later should be
    ignored, not crash a run.
    """
    blocks = getattr(message, "content", None)
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(getattr(block, "text", "")) for block in blocks if getattr(block, "text", "")
    ).strip()


def is_assistant(message: Any) -> bool:
    """An answer from the model - not a tool result coming back.

    The SDK's user and assistant messages both carry a ``content`` list and there
    is no discriminator field on either, so duck-typing on ``content`` alone
    counted every tool result as an answer: a run with sixteen calls reported
    forty turns and was cut off before committing anything. Only the assistant
    message carries the model that produced it.
    """
    return (
        isinstance(getattr(message, "content", None), list)
        and hasattr(message, "model")
        and not is_result(message)
    )


def is_result(message: Any) -> bool:
    return getattr(message, "type", None) == "result" or hasattr(message, "total_cost_usd")


@dataclass(frozen=True)
class ResultInfo:
    subtype: str = ""
    text: str = ""
    terminal_reason: str = ""
    #: Straight from the SDK: the cache fields matter as much as `input_tokens`,
    #: which on a cached conversation is a handful of tokens (measured).
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    turns: int = 0


def result_info(message: Any) -> ResultInfo:
    usage = getattr(message, "usage", None) or {}
    return ResultInfo(
        subtype=str(getattr(message, "subtype", "") or ""),
        text=str(getattr(message, "result", "") or ""),
        terminal_reason=str(getattr(message, "terminal_reason", "") or ""),
        usage=dict(usage),
        cost_usd=float(getattr(message, "total_cost_usd", 0.0) or 0.0),
        turns=int(getattr(message, "num_turns", 0) or 0),
    )


@dataclass(frozen=True, slots=True)
class LoopEnd:
    """Why the loop stopped."""

    reason: str = ""
    interrupted: bool = False
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)


NUDGE = (
    "Voce parou sem chamar finish_run. Se ainda ha o que fazer, continue; se nao, "
    "publique o que estiver pronto e chame finish_run com o resumo da run."
)

BUDGET_WARNING = (
    "Aviso do runtime: {reason}. Nao comece nada novo - descarte com drop_clip o que "
    "nao vai usar, publique o que ja esta pronto e chame finish_run."
)


def warning_for(reason: str) -> str:
    return "\n\n" + BUDGET_WARNING.format(reason=reason)


async def drive(
    session: Session,
    opening: str,
    *,
    budget: Budget,
    log: RunLog,
    progress: RunProgress,
    on_text: Callable[[str], None] | None = None,
) -> LoopEnd:
    """Run the conversation until it finishes, or until a fuse blows."""
    prompt = opening
    nudges = 0
    cost = 0.0
    usage = {"input_tokens": 0, "output_tokens": 0}

    while True:
        await session.query(prompt)
        end: LoopEnd | None = None

        async for message in session.receive_response():
            if is_result(message):
                info_ = result_info(message)
                budget.note_usage(info_.usage)
                # The SDK counts a whole query as one turn; ours is one answer
                # from the model, so take whichever saw more.
                budget.turns = max(budget.turns, info_.turns)
                cost += info_.cost_usd
                for key, value in info_.usage.items():
                    if isinstance(value, int):
                        usage[key] = usage.get(key, 0) + value
                if info_.subtype and info_.subtype != "success":
                    end = LoopEnd(
                        reason=info_.terminal_reason or f"a sessao terminou em '{info_.subtype}'",
                        cost_usd=cost,
                        usage=dict(usage),
                    )
                continue

            if is_assistant(message):
                budget.note_turn()
                text = message_text(message)
                if text:
                    log.turn(text, budget=budget)
                    if on_text is not None:
                        on_text(text)

            # A stop asked for from the page is seen here, between messages: the
            # call that carried it has already come back refused.
            if progress.stop_requested:
                await session.interrupt()
                return LoopEnd(
                    reason=progress.stop_requested,
                    interrupted=True,
                    cost_usd=cost,
                    usage=dict(usage),
                )

            # The wall clock is the one limit that can blow mid-turn, and a run
            # that has already overrun has no business starting another render.
            spent = budget.exhausted()
            if spent is not None:
                await session.interrupt()
                return LoopEnd(reason=spent, interrupted=True, cost_usd=cost, usage=dict(usage))

        if end is not None:
            return end

        spent = budget.exhausted()
        if spent is not None:
            return LoopEnd(reason=spent, cost_usd=cost, usage=dict(usage))

        if progress.finished:
            return LoopEnd(cost_usd=cost, usage=dict(usage))

        if nudges >= MAX_NUDGES:
            return LoopEnd(
                reason="o agent parou sem finalizar, mesmo depois de ser chamado de volta",
                cost_usd=cost,
                usage=dict(usage),
            )
        nudges += 1
        prompt = NUDGE


# --------------------------------------------------------------------------- #
# Tools, wrapped for the SDK
# --------------------------------------------------------------------------- #


def _signature(tool: str, args: dict[str, Any]) -> str:
    return f"{tool}:{sorted(args.items())!r}"


async def wait_while_paused(
    watcher: ControlWatcher | None, *, log: RunLog, tool: str, args: dict[str, Any]
) -> str:
    """Hold here while the run is paused; return a reason when it must stop.

    Blocking is the point: refusing instead would spend a turn and a slice of
    context every couple of seconds, so a paused run would eat its own budget
    standing still.
    """
    if watcher is None:
        return ""
    control = watcher.current()
    if control.stopping:
        return f"parada pedida por voce no monitor ({control.by})"
    if not control.paused:
        return ""

    log.event("paused", tool=tool, input=args)
    waited = 0.0
    while control.paused and waited < MAX_PAUSE_S:
        await asyncio.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        control = watcher.current()
    log.event("resumed", tool=tool, waited_s=round(waited, 1))
    if control.stopping:
        return f"parada pedida por voce no monitor ({control.by})"
    if control.paused:
        return f"pausada ha {waited / 60:.0f} min sem retomar"
    return ""


async def call_tool(
    spec: ToolSpec,
    args: dict[str, Any],
    *,
    ctx: ToolContext,
    budget: Budget,
    log: RunLog,
    progress: RunProgress,
    watcher: ControlWatcher | None = None,
) -> ToolResult:
    """One tool call: refuse, time out, record, and never raise."""
    if watcher is not None:
        ctx.vetoed.clear()
        ctx.vetoed.update(watcher.vetoed())
        target = clip_id_of(spec.name, args)
        if target and target in ctx.vetoed:
            reason = f"o corte {target} foi vetado por voce no monitor; siga sem ele"
            log.budget_deny(spec.name, args, reason)
            return ToolResult(content=[{"type": "text", "text": reason}], is_error=True)

    stopping = await wait_while_paused(watcher, log=log, tool=spec.name, args=args)
    if stopping:
        progress.stop_requested = stopping
        if watcher is not None:
            progress.publish_on_stop = watcher.current().publish_on_stop
        log.budget_deny(spec.name, args, stopping)
        return ToolResult(content=[{"type": "text", "text": stopping}], is_error=True)

    refusal = budget.refusal(spec.name, args)
    if refusal is not None:
        log.budget_deny(spec.name, args, refusal)
        return ToolResult(content=[{"type": "text", "text": refusal}], is_error=True)

    signature = _signature(spec.name, args)
    if progress.failures.get(signature, 0) >= MAX_IDENTICAL_FAILURES:
        reason = (
            f"'{spec.name}' ja falhou {MAX_IDENTICAL_FAILURES} vezes com esses mesmos "
            "argumentos; mude de abordagem em vez de repetir"
        )
        log.budget_deny(spec.name, args, reason)
        return ToolResult(content=[{"type": "text", "text": reason}], is_error=True)

    log.tool_start(spec.name, args)
    started = time.monotonic()
    timeout = budget.timeout_for(spec.name)
    try:
        result = await asyncio.wait_for(asyncio.to_thread(spec.call, ctx, args), timeout=timeout)
    except TimeoutError:
        result = ToolResult(
            content=[
                {
                    "type": "text",
                    "text": f"{spec.name}: passou de {timeout / 60:.0f} min e foi abandonada",
                }
            ],
            is_error=True,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    budget.note_call()
    if not result.is_error:
        budget.spend(spec.name, args, reused=result.reused)
        _note_progress(spec.name, args, result, progress)
        progress.failures.pop(signature, None)
    else:
        progress.failures[signature] = progress.failures.get(signature, 0) + 1

    # Riding along with the answer, not as a turn of its own: a warning that
    # costs a turn to deliver spends what it is trying to save.
    landing = budget.landing()
    if landing is not None and not progress.finished:
        log.event("budget_warning", reason=landing)
        result = result.model_copy(
            update={"content": [*result.content, {"type": "text", "text": warning_for(landing)}]}
        )

    log.tool_call(
        spec.name,
        args,
        output=result.text,
        duration_ms=duration_ms,
        is_error=result.is_error,
        budget=budget,
    )
    return result


def _note_progress(
    tool: str, args: dict[str, Any], result: ToolResult, progress: RunProgress
) -> None:
    if tool == "commit_clips":
        profile = str(args.get("profile", ""))
        progress.committed[profile] = len(args.get("clips") or [])
    elif tool == "render_clip" and not result.reused:
        progress.rendered += 1
    elif tool == "publish_clips" and not args.get("dry_run"):
        progress.published = True
    elif tool == "finish_run":
        progress.finished = True


def sdk_tools(
    ctx: ToolContext,
    budget: Budget,
    log: RunLog,
    progress: RunProgress,
    watcher: ControlWatcher | None = None,
) -> list[Any]:
    """Wrap every registered tool for the in-process MCP server."""
    from claude_agent_sdk import ToolAnnotations, tool

    wrapped: list[Any] = []
    for spec in SPECS:

        def make(spec: ToolSpec = spec) -> Any:
            @tool(
                spec.name,
                spec.description,
                spec.input_schema,
                annotations=ToolAnnotations(readOnlyHint=spec.read_only),
            )
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = await call_tool(
                    spec,
                    args,
                    ctx=ctx,
                    budget=budget,
                    log=log,
                    progress=progress,
                    watcher=watcher,
                )
                return result.for_sdk()

            return handler

        wrapped.append(make())
    return wrapped


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def system_prompt(settings: Settings) -> str:
    path = settings.prompts_dir / SYSTEM_PROMPT_NAME
    if not path.exists():
        path = PROMPTS_DIR / SYSTEM_PROMPT_NAME
    if not path.exists():
        raise ExternalError(f"prompt do agent nao encontrado: {path}")
    return path.read_text(encoding="utf-8")


def opening_prompt(
    settings: Settings,
    *,
    url: str,
    video_id: str,
    profiles: list[str],
    budget: Budget,
    dry_run: bool,
    done: list[str],
    channel: Channel | None = None,
) -> str:
    """The first message: the job, the state of the workspace and the fuses.

    The budget is spelled out in numbers because a model that knows it has ten
    renders spends them differently from one that finds out by being refused.
    """
    limits = budget.limits
    lines = [
        "Faca os cortes deste video.",
        "",
        f"- url: {url}" if url else f"- video ja no workspace: {video_id}",
        f"- video_id: {video_id}",
        f"- formatos habilitados: {', '.join(profiles)}",
        "",
        "Orcamento desta run (o runtime recusa a chamada quando acaba):",
        f"- {limits.max_renders} renders no total, {limits.max_clip_renders} por corte",
        f"- {limits.max_scans_per_profile} varreduras (find_candidates) por perfil",
        f"- {limits.max_turns} turnos e {limits.wall_clock_s / 60:.0f} minutos de relogio",
        "",
        "Quando um desses limites estiver perto, o runtime avisa junto da resposta de uma "
        "ferramenta. A partir do aviso nao comece nada novo: feche o que da para fechar.",
    ]
    block = as_prompt(channel)
    if block:
        lines += ["", block]
    if done:
        lines += ["", "Ja feito neste video (nao refaca a toa):", *[f"- {item}" for item in done]]
    if dry_run:
        lines += [
            "",
            "Esta run e um ensaio: publish_clips so aceita dry_run=true, nada vai para o Drive.",
        ]
    return "\n".join(lines)


def _already_done(settings: Settings, video_id: str) -> list[str]:
    state = State(settings.state_db)
    return [
        f"{record.stage}{'' if record.profile == '-' else ' (' + record.profile + ')'}: "
        f"{record.status}{' - ' + record.detail if record.detail else ''}"
        for record in state.stages_for(video_id)
        if str(record.status) == "done"
    ]


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


async def _run_loop(
    settings: Settings,
    opening: str,
    *,
    ctx: ToolContext,
    budget: Budget,
    log: RunLog,
    progress: RunProgress,
) -> LoopEnd:
    """Build the SDK session and hand it to :func:`drive`."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ExternalError(
            "claude-agent-sdk nao esta instalado. Rode: uv sync --extra agent "
            "(e mantenha os outros extras que voce usa, ex.: --extra gpu --extra vision)"
        ) from exc

    # The log already knows where this run's files live; deriving the video id
    # back out of the run id would be guesswork (they both contain dashes).
    watcher = ControlWatcher(control_path(log.path.parent, ctx.run_id))
    server = create_sdk_mcp_server(
        name="clipper", version="1.0.0", tools=sdk_tools(ctx, budget, log, progress, watcher)
    )

    # The budget gate lives in `call_tool`, not in a `can_use_tool` callback: an
    # `allowed_tools` entry that allows a whole tool auto-approves it before the
    # callback is consulted, so the callback would never run. Refusing inside the
    # handler has the same effect - the work does not happen, the reason reaches
    # the model as the tool's answer, and the loop carries on.
    options = ClaudeAgentOptions(
        model=settings.agent.model or None,
        system_prompt=system_prompt(settings),
        mcp_servers={"clipper": server},
        allowed_tools=[f"mcp__clipper__{spec.name}" for spec in SPECS],
        # No built-in tools and no settings from disk: the model executes nothing
        # and never sees this repository's CLAUDE.md (SPEC-agent 3).
        tools=[],
        setting_sources=[],
        max_turns=settings.agent.max_turns,
        cwd=str(settings.workspace.expanduser()),
    )

    async with ClaudeSDKClient(options=options) as client:
        end = await drive(
            client,
            opening,
            budget=budget,
            log=log,
            progress=progress,
            on_text=lambda text: console.print(f"[dim]{text[:400]}[/dim]"),
        )
    return end


def close_run(
    ctx: ToolContext,
    *,
    progress: RunProgress,
    budget: Budget,
    log: RunLog,
    video_id: str,
    dry_run: bool,
    reason: str,
) -> Path | None:
    """What the model did not do: ship what is ready, close the stages, write the report.

    A clip that is rendered, reviewed and packaged is finished work; leaving it
    in the workspace because the loop ran out of turns would be the runtime
    losing it, not the agent (SPEC-agent 8.2).
    """
    from clipper.agent import tools

    profiles = progress.profiles()
    if profiles and not progress.published and not dry_run:
        warn("o agent nao publicou; entregando o que ja esta pronto")
        result = tools.get("publish_clips").call(ctx, {"video_id": video_id, "profiles": profiles})
        log.tool_call(
            "publish_clips",
            {"video_id": video_id, "profiles": profiles, "by": "runtime"},
            output=result.text,
            duration_ms=0,
            is_error=result.is_error,
            budget=budget,
        )
        progress.published = not result.is_error

    if not progress.finished:
        summary = reason or "a run terminou sem o agent chamar finish_run"
        result = tools.get("finish_run").call(
            ctx,
            {
                "video_id": video_id,
                "summary": f"Encerrada pelo runtime: {summary}.",
                "delivered": [],
            },
        )
        log.tool_call(
            "finish_run",
            {"video_id": video_id, "by": "runtime"},
            output=result.text,
            duration_ms=0,
            is_error=result.is_error,
            budget=budget,
        )

    close_stages(ctx, video_id, log)
    return _close_report(ctx, video_id)


def close_stages(ctx: ToolContext, video_id: str, log: RunLog | None = None) -> None:
    """No stage of this video is running any more, because the run is not.

    Whatever ended the loop, a row left saying `running` is a claim about a process
    that no longer exists - and the monitor believes the row (SPEC-agent 8.4).
    """
    from clipper.agent.progress import reconcile

    for note in reconcile(ctx, video_id):
        warn(note)
        if log is not None:
            log.event("stage_reconciled", detail=note)


def _close_report(ctx: ToolContext, video_id: str) -> Path | None:
    """Append the run's real numbers to the report the agent wrote.

    The agent calls `finish_run` while the loop is still going, when turns and
    cost are necessarily zero - a report that says "0 turnos, US$ 0,00" about a
    run that just spent an hour is worse than no numbers at all.
    """
    path = ctx.settings.agent_dir(video_id) / f"{ctx.run_id}.report.md"
    if not path.exists():
        return None
    run = State(ctx.settings.state_db).get_run(ctx.run_id)
    if run is None:
        return path

    section = [
        "## Run",
        "",
        f"- inicio: {run.started_at}",
        f"- fim: {run.ended_at or '-'} ({run.status})",
        f"- respostas do modelo: {run.turns}",
        f"- custo estimado: US$ {run.cost_usd:.2f}",
    ]
    if run.detail:
        section.append(f"- encerrada por: {run.detail}")
    body = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(body + "\n\n" + "\n".join(section) + "\n", encoding="utf-8")
    return path


def run_agent(
    settings: Settings,
    url: str,
    *,
    video_id: str | None = None,
    profiles: list[str] | None = None,
    dry_run: bool = False,
    channel: str = "",
) -> RunOutcome:
    """One video, one run. Synchronous: the CLI has no event loop of its own."""
    from clipper.stages.ingest import pick_source

    resolved = video_id or pick_source(url).video_id(url)
    run_id = new_run_id(resolved)
    active = resolve_channel(settings, channel or None)
    # Explicit --profile, then the channel's formats, then the config's default.
    enabled = profiles or (active.profile.profiles if active else []) or settings.agent_profiles()
    for name in enabled:
        settings.profile(name)

    state = State(settings.state_db)
    state.start_run(run_id, resolved, channel=active.id if active else "")

    budget = Budget(limits=settings.agent)
    progress = RunProgress()
    logger = stage_logger(settings.logs_dir(resolved), "agent")
    ctx = ToolContext(
        settings=settings,
        run_id=run_id,
        enabled_profiles=tuple(enabled),
        logger=logger,
        dry_run=dry_run,
        channel=active,
    )

    console.rule(f"agent {run_id}")
    info(
        f"[dim]perfis: {', '.join(enabled)} | canal: {active.id if active else 'nenhum'} "
        f"| modelo: {settings.agent.model or 'padrao'}[/dim]"
    )

    log = RunLog(settings.agent_dir(resolved) / f"{run_id}.jsonl", run_id)
    log.run_start(resolved, url=url, model=settings.agent.model, budget=budget)
    opening = opening_prompt(
        settings,
        url=url,
        video_id=resolved,
        profiles=enabled,
        budget=budget,
        dry_run=dry_run,
        done=_already_done(settings, resolved),
        channel=active,
    )

    status = RunStatus.DONE
    end = LoopEnd()
    report_path: Path | None = None
    try:
        end = asyncio.run(
            _run_loop(settings, opening, ctx=ctx, budget=budget, log=log, progress=progress)
        )
        if end.reason:
            status = RunStatus.INCOMPLETE
    except KeyboardInterrupt:
        status = RunStatus.CANCELLED
        end = LoopEnd(reason="cancelada por voce", interrupted=True)
    except Exception as exc:
        status = RunStatus.FAILED
        end = LoopEnd(reason=str(exc))
        log.event("run_error", error=str(exc)[:500])
        state.finish_run(run_id, status, detail=str(exc)[:200], turns=budget.turns)
        # close_run is skipped on a crash, so this is the only chance to stop the
        # stage rows claiming a pipeline that just died is still working.
        close_stages(ctx, resolved, log)
        log.run_end(str(status), reason=end.reason, budget=budget)
        log.close()
        raise
    finally:
        if status is not RunStatus.FAILED:
            # Closed first: the report is written from this row, and a report
            # saying "0 turnos, US$ 0,00" is worse than no report at all.
            state.finish_run(
                run_id,
                status,
                detail=end.reason[:200],
                turns=budget.turns,
                cost_usd=end.cost_usd,
            )
            report_path = close_run(
                ctx,
                progress=progress,
                budget=budget,
                log=log,
                video_id=resolved,
                # A stop that asked for nothing to be shipped is honoured here:
                # it is the only way to call off a run that is mid-delivery.
                dry_run=dry_run or not progress.publish_on_stop,
                reason=end.reason,
            )
            log.run_end(
                str(status),
                reason=end.reason,
                budget=budget,
                cost_usd=end.cost_usd,
                usage=dict(end.usage),
            )
            log.close()

    if status is RunStatus.DONE:
        success(f"run {run_id} concluida em {budget.turns} turnos")
    else:
        warn(f"run {run_id}: {end.reason}")
    return RunOutcome(
        run_id=run_id,
        video_id=resolved,
        status=status,
        reason=end.reason,
        turns=budget.turns,
        cost_usd=end.cost_usd,
        report_path=report_path,
    )
