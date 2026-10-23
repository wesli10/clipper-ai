"""The loop, its fuses and its log (SPEC-agent 8).

No SDK and no model: :func:`drive` talks to a Session protocol, so the whole
policy - budgets, nudges, hard stops, logging - is exercised with scripted
messages.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from clipper.agent.budget import Budget
from clipper.agent.log import RunLog, redact
from clipper.agent.runtime import (
    MAX_IDENTICAL_FAILURES,
    MAX_NUDGES,
    NUDGE,
    RunProgress,
    call_tool,
    drive,
    new_run_id,
    opening_prompt,
    system_prompt,
)
from clipper.agent.tools import get as tools_get
from clipper.agent.tools.base import ToolContext, ToolSpec
from clipper.config import AgentSettings, load_settings

# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


@dataclass
class Block:
    text: str


@dataclass
class Assistant:
    """The SDK's assistant message: a `content` list *and* the model that wrote it."""

    content: list[Block]
    model: str = "claude-opus-5"


@dataclass
class User:
    """A tool result coming back. Same `content` list, no model - and not a turn."""

    content: list[Block]


@dataclass
class Result:
    type: str = "result"
    subtype: str = "success"
    result: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    num_turns: int = 1
    terminal_reason: str = ""


class FakeSession:
    """Replays one scripted answer per query."""

    def __init__(self, turns: list[list[Any]], *, on_query: Any = None) -> None:
        self.turns = turns
        self.queries: list[str] = []
        self.interrupted = False
        self.on_query = on_query

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self.on_query is not None:
            self.on_query(len(self.queries))

    async def _replay(self, messages: list[Any]):
        for message in messages:
            yield message

    def receive_response(self):
        index = min(len(self.queries) - 1, len(self.turns) - 1)
        return self._replay(self.turns[index])

    async def interrupt(self) -> None:
        self.interrupted = True


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    return load_settings(config)


@pytest.fixture
def budget():
    return Budget(limits=AgentSettings())


@pytest.fixture
def log(tmp_path):
    with RunLog(tmp_path / "run.jsonl", "20260910-120000-vid") as opened:
        yield opened


def lines(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_renders_run_out_and_the_refusal_tells_the_agent_what_to_do_instead(budget):
    budget.limits = AgentSettings(max_renders=1)
    args = {"video_id": "v", "profile": "short", "index": 1}

    assert budget.refusal("render_clip", args) is None
    budget.spend("render_clip", args)
    refusal = budget.refusal("render_clip", {**args, "index": 2})
    assert refusal is not None and "publique o que ja esta pronto" in refusal


def test_reusing_a_render_costs_nothing(budget):
    args = {"video_id": "v", "profile": "short", "index": 1}
    budget.spend("render_clip", args, reused=True)
    assert budget.renders == 0 and budget.renders_left() == budget.limits.max_renders


def test_a_clip_may_only_be_redone_so_many_times(budget):
    budget.limits = AgentSettings(max_clip_renders=2)
    args = {"video_id": "v", "profile": "short", "index": 1}
    budget.spend("render_clip", args)
    budget.spend("render_clip", args)

    assert budget.refusal("render_clip", args) is not None
    # Another clip is still fine: the limit is per clip, not per profile.
    assert budget.refusal("render_clip", {**args, "index": 2}) is None


def test_scanning_the_same_profile_again_is_refused_with_the_reason(budget):
    budget.limits = AgentSettings(max_scans_per_profile=1)
    budget.spend("find_candidates", {"profile": "short"})
    refusal = budget.refusal("find_candidates", {"profile": "short"})
    assert refusal is not None and "rejeitados" in refusal
    assert budget.refusal("find_candidates", {"profile": "quiz"}) is None


def test_a_rehearsal_publish_does_not_count(budget):
    budget.limits = AgentSettings(max_publishes=1)
    budget.spend("publish_clips", {"dry_run": True})
    assert budget.publishes == 0
    assert budget.refusal("publish_clips", {"dry_run": True}) is None


@pytest.mark.parametrize(
    ("limits", "usage", "expected"),
    [
        (AgentSettings(max_output_tokens=10), {"output_tokens": 11}, "tokens de saida"),
        (AgentSettings(max_input_tokens=10), {"input_tokens": 11}, "tokens de entrada"),
        (
            AgentSettings(max_input_tokens=10),
            {"input_tokens": 2, "cache_creation_input_tokens": 9},
            "tokens de entrada",
        ),
    ],
)
def test_hard_limits_name_what_blew(limits, usage, expected):
    budget = Budget(limits=limits)
    assert budget.exhausted() is None
    budget.note_usage(usage)
    reason = budget.exhausted()
    assert reason is not None and expected in reason


def test_turns_are_not_a_fuse_of_ours(budget):
    """The SDK owns `max_turns`; interrupting on our own count cut a run short."""
    for _ in range(budget.limits.max_turns * 2):
        budget.note_turn()
    assert budget.exhausted() is None


def test_what_is_read_back_from_the_cache_is_reported_apart(budget):
    """It is the cheap half of the bill; counting it as fresh input would lie."""
    budget.note_usage(
        {"input_tokens": 40, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 90_000}
    )
    assert budget.input_tokens == 1040
    assert budget.cache_read_tokens == 90_000
    assert budget.exhausted() is None
    line = budget.view().line("leitura de cache")
    assert line is not None and line.used == 90_000


def test_the_wall_clock_is_a_hard_limit_too():
    budget = Budget(limits=AgentSettings(wall_clock_s=0.0))
    assert "tempo" in (budget.exhausted() or "")


def test_a_render_gets_minutes_and_everything_else_gets_a_minute(budget):
    assert budget.timeout_for("render_clip") == budget.limits.render_timeout_s
    assert budget.timeout_for("transcribe_video") == budget.limits.transcribe_timeout_s
    assert budget.timeout_for("snap_clip") == budget.limits.tool_timeout_s


def test_the_view_reports_every_line_the_monitor_draws(budget):
    names = {line.name for line in budget.view().lines}
    assert {"respostas do modelo", "renders", "tokens de saida", "tempo"} <= names


# --------------------------------------------------------------------------- #
# Log
# --------------------------------------------------------------------------- #


def test_every_event_is_numbered_and_timestamped(log, budget):
    log.run_start("vid", url="u", model="claude-opus-5", budget=budget)
    log.turn("vou comecar pelo mapa do video", budget=budget)
    records = lines(log.path)

    assert [r["seq"] for r in records] == [1, 2]
    assert all(r["run_id"] == "20260910-120000-vid" for r in records)
    assert records[0]["type"] == "run_start" and records[1]["type"] == "turn"
    assert records[0]["budget"]["lines"]


def test_a_failed_call_is_logged_as_an_error_with_the_message(log, budget):
    log.tool_call(
        "render_clip",
        {"index": 1},
        output="render_clip: ffmpeg falhou",
        duration_ms=1200,
        is_error=True,
        budget=budget,
    )
    record = lines(log.path)[0]
    assert record["type"] == "tool_error"
    assert record["error"] == "render_clip: ffmpeg falhou"
    assert record["duration_ms"] == 1200
    assert record["budget"]["renders_left"] == budget.limits.max_renders


def test_a_long_answer_is_cut_and_says_so(log, budget):
    log.tool_call(
        "get_outline", {}, output="x" * 9000, duration_ms=1, is_error=False, budget=budget
    )
    record = lines(log.path)[0]
    assert record["output_truncated"] is True and len(record["output"]) == 4000


def test_a_key_that_leaks_into_a_message_is_redacted(log, budget):
    log.tool_call(
        "find_candidates",
        {},
        output="erro: chave AIzaSyB3xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx invalida",
        duration_ms=1,
        is_error=True,
        budget=budget,
    )
    assert "AIzaSy" not in log.path.read_text(encoding="utf-8")
    assert "[redigido]" in log.path.read_text(encoding="utf-8")
    assert redact("sk-ant-" + "a" * 30) == "[redigido]"


def test_the_log_is_readable_while_the_run_is_still_going(log, budget):
    """The monitor tails this file: a line must be on disk as soon as it happens."""
    log.turn("primeiro", budget=budget)
    assert len(lines(log.path)) == 1
    log.turn("segundo", budget=budget)
    assert len(lines(log.path)) == 2


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_a_run_that_finishes_by_itself_ends_without_a_reason(budget, log):
    progress = RunProgress(finished=True)
    session = FakeSession([[Assistant([Block("pronto")]), Result(total_cost_usd=0.5)]])

    end = run(drive(session, "faca os cortes", budget=budget, log=log, progress=progress))

    assert end.reason == "" and end.interrupted is False
    assert end.cost_usd == 0.5
    assert session.queries == ["faca os cortes"]
    assert [r["type"] for r in lines(log.path)] == ["turn"]


def test_an_agent_that_stops_without_finishing_is_called_back(budget, log):
    progress = RunProgress()
    session = FakeSession(
        [[Result()], [Result()]],
        on_query=lambda count: setattr(progress, "finished", count >= 2),
    )

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert end.reason == ""
    assert session.queries[1] == NUDGE


def test_it_gives_up_calling_back_after_a_couple_of_tries(budget, log):
    progress = RunProgress()
    session = FakeSession([[Result()]])

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert "sem finalizar" in end.reason
    assert len(session.queries) == MAX_NUDGES + 1


def test_the_loop_is_interrupted_the_moment_a_fuse_blows(log):
    budget = Budget(limits=AgentSettings(wall_clock_s=0.0))
    progress = RunProgress()
    session = FakeSession([[Assistant([Block("vou renderizar mais um")]), Result()]])

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert end.interrupted is True and "tempo" in end.reason
    assert session.interrupted is True


def test_a_tool_result_coming_back_is_not_a_turn(budget, log):
    """Both carry a `content` list; only one of them is the model speaking."""
    progress = RunProgress(finished=True)
    session = FakeSession(
        [[Assistant([Block("vou chamar")]), User([Block("resultado")]), Result()]]
    )

    run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert budget.turns == 1


def test_tokens_and_cost_come_from_the_result_message(budget, log):
    progress = RunProgress(finished=True)
    session = FakeSession(
        [
            [
                Assistant([Block("primeiro passo")]),
                Assistant([Block("segundo passo")]),
                Result(
                    usage={"input_tokens": 1200, "output_tokens": 800},
                    total_cost_usd=0.42,
                    num_turns=1,
                ),
            ]
        ]
    )

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert budget.input_tokens == 1200 and budget.output_tokens == 800
    # A turn is an answer from the model, not a query: the SDK reports one
    # result per query however many times the model spoke (measured on a real run).
    assert budget.turns == 2
    assert end.usage == {"input_tokens": 1200, "output_tokens": 800}
    assert end.cost_usd == 0.42


def test_a_session_that_ends_badly_ends_the_run_with_the_reason(budget, log):
    progress = RunProgress()
    session = FakeSession([[Result(subtype="failure", terminal_reason="max_turns_exceeded")]])

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert end.reason == "max_turns_exceeded"


def test_what_the_model_says_is_logged_but_not_its_empty_turns(budget, log):
    progress = RunProgress(finished=True)
    session = FakeSession(
        [[Assistant([Block("  ")]), Assistant([Block("plano: 3 shorts")]), Result()]]
    )

    run(drive(session, "comece", budget=budget, log=log, progress=progress))

    turns = [r for r in lines(log.path) if r["type"] == "turn"]
    assert [t["text"] for t in turns] == ["plano: 3 shorts"]


# --------------------------------------------------------------------------- #
# One tool call
# --------------------------------------------------------------------------- #


class Echo(BaseModel):
    value: str = "ok"
    reused: bool = False


class EchoInput(BaseModel):
    fail: bool = False
    sleep: float = 0.0
    reused: bool = False


def echo_spec(name: str = "render_clip") -> ToolSpec:
    def handler(ctx: ToolContext, args: EchoInput) -> Echo:
        import time as _time

        if args.sleep:
            _time.sleep(args.sleep)
        if args.fail:
            raise ValueError("nao deu")
        return Echo(reused=args.reused)

    return ToolSpec(
        name=name,
        description="Use para testar.",
        input_model=EchoInput,
        handler=handler,
        output_limit=200,
        read_only=False,
    )


def call(spec, args, *, ctx, budget, log, progress):
    return run(call_tool(spec, args, ctx=ctx, budget=budget, log=log, progress=progress))


def test_a_call_that_works_is_counted_and_logged(settings, budget, log):
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = call(
        echo_spec(), {"profile": "short"}, ctx=ctx, budget=budget, log=log, progress=progress
    )

    assert result.is_error is False
    assert budget.renders == 1 and progress.rendered == 1
    # Started before it ran, so the monitor can show it in flight; closed after.
    assert [r["type"] for r in lines(log.path)] == ["tool_start", "tool_call"]


def test_a_reused_answer_is_not_charged_to_the_budget(settings, budget, log):
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    call(
        echo_spec(),
        {"reused": True, "profile": "short"},
        ctx=ctx,
        budget=budget,
        log=log,
        progress=progress,
    )

    assert budget.renders == 0 and progress.rendered == 0


def test_a_refused_call_never_runs_and_is_logged_as_a_denial(settings, log):
    budget = Budget(limits=AgentSettings(max_renders=0))
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = call(
        echo_spec(), {"profile": "short"}, ctx=ctx, budget=budget, log=log, progress=progress
    )

    assert result.is_error and "orcamento de renders" in result.text
    # Refused before it started: no tool_start, so nothing shows as running.
    assert [r["type"] for r in lines(log.path)] == ["budget_deny"]


def test_the_same_failing_call_is_refused_on_the_third_try(settings, budget, log):
    ctx = ToolContext(settings=settings)
    progress = RunProgress()
    args = {"fail": True, "profile": "short"}

    for _ in range(MAX_IDENTICAL_FAILURES):
        assert call(echo_spec(), args, ctx=ctx, budget=budget, log=log, progress=progress).is_error

    refused = call(echo_spec(), args, ctx=ctx, budget=budget, log=log, progress=progress)
    assert "mude de abordagem" in refused.text
    assert lines(log.path)[-1]["type"] == "budget_deny"


def test_a_tool_that_hangs_is_abandoned_and_the_run_carries_on(settings, log):
    budget = Budget(limits=AgentSettings(tool_timeout_s=0.05))
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = call(
        echo_spec("snap_clip"),
        {"sleep": 0.5},
        ctx=ctx,
        budget=budget,
        log=log,
        progress=progress,
    )

    assert result.is_error and "abandonada" in result.text


def test_committing_and_publishing_are_remembered_as_progress(settings, budget, log):
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    call(
        echo_spec("commit_clips"),
        {"profile": "short"},
        ctx=ctx,
        budget=budget,
        log=log,
        progress=progress,
    )
    assert progress.profiles() == []  # nenhum corte na chamada de teste

    call(echo_spec("publish_clips"), {}, ctx=ctx, budget=budget, log=log, progress=progress)
    assert progress.published is True

    call(echo_spec("finish_run"), {}, ctx=ctx, budget=budget, log=log, progress=progress)
    assert progress.finished is True


# --------------------------------------------------------------------------- #
# Prompts and ids
# --------------------------------------------------------------------------- #


def test_the_opening_message_spells_out_the_budget(settings, budget):
    text = opening_prompt(
        settings,
        url="https://youtu.be/abc",
        video_id="abc",
        profiles=["short", "quiz"],
        budget=budget,
        dry_run=True,
        done=["transcribe: done"],
    )
    assert "short, quiz" in text
    assert f"{budget.limits.max_renders} renders" in text
    assert "ensaio" in text
    assert "transcribe: done" in text


def test_the_system_prompt_is_the_one_on_disk(settings):
    text = system_prompt(settings)
    assert "finish_run" in text and "snap_clip" in text


def test_a_run_id_sorts_by_time_and_names_the_video():
    first = new_run_id("RfCT-LJkfx8")
    assert first.endswith("RfCT-LJkfx8")
    assert len(first.split("-")[0]) == 8


# --------------------------------------------------------------------------- #
# Closing a run the agent did not close
# --------------------------------------------------------------------------- #


def test_the_runtime_writes_the_report_when_the_agent_never_finished(settings, budget, log):
    from clipper.agent.runtime import close_run

    ctx = ToolContext(settings=settings, run_id="20260910-120000-vid")
    settings.video_dir("vid").mkdir(parents=True)

    path = close_run(
        ctx,
        progress=RunProgress(),
        budget=budget,
        log=log,
        video_id="vid",
        dry_run=False,
        reason="limite de turnos atingido (40/40)",
    )

    assert path is not None and path.exists()
    report = path.read_text(encoding="utf-8")
    assert "Encerrada pelo runtime" in report and "limite de turnos" in report


def test_a_rehearsal_never_publishes_on_the_way_out(settings, budget, log):
    from clipper.agent.runtime import close_run

    ctx = ToolContext(settings=settings, run_id="20260910-120000-vid", dry_run=True)
    settings.video_dir("vid").mkdir(parents=True)
    progress = RunProgress(committed={"short": 2})

    close_run(
        ctx,
        progress=progress,
        budget=budget,
        log=log,
        video_id="vid",
        dry_run=True,
        reason="",
    )

    assert progress.published is False
    assert not any(r.get("tool") == "publish_clips" for r in lines(log.path))


# --------------------------------------------------------------------------- #
# Landing the run before a fuse blows (SPEC-agent 8.2)
# --------------------------------------------------------------------------- #


def test_the_warning_rides_along_with_the_tool_answer(settings, log):
    """Measured: both runs cut by max_turns were cut during the wrap-up."""
    budget = Budget(limits=AgentSettings(max_turns=10, landing_margin=8))
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    first = call(echo_spec(), {}, ctx=ctx, budget=budget, log=log, progress=progress)
    second = call(echo_spec(), {}, ctx=ctx, budget=budget, log=log, progress=progress)

    assert "Aviso do runtime" not in first.text
    assert "Aviso do runtime" in second.text
    assert "chame finish_run" in second.text
    assert second.is_error is False


def test_the_warning_is_said_once(settings, log):
    budget = Budget(limits=AgentSettings(max_turns=10, landing_margin=9))
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    said = [
        call(echo_spec(), {}, ctx=ctx, budget=budget, log=log, progress=progress).text
        for _ in range(4)
    ]

    assert sum("Aviso do runtime" in text for text in said) == 1


def test_a_run_that_already_finished_is_not_told_to_finish(settings, log):
    budget = Budget(limits=AgentSettings(max_turns=10, landing_margin=9))
    ctx = ToolContext(settings=settings)

    result = call(
        echo_spec(), {}, ctx=ctx, budget=budget, log=log, progress=RunProgress(finished=True)
    )

    assert "Aviso do runtime" not in result.text


def test_time_and_tokens_also_ask_for_a_landing():
    budget = Budget(limits=AgentSettings(max_output_tokens=1000))
    budget.note_usage({"output_tokens": 900})

    reason = budget.landing()
    assert reason is not None and "tokens de saida" in reason
    assert budget.landing() is None


def test_nothing_is_said_while_the_budget_is_healthy(budget):
    budget.calls = 5
    budget.note_usage({"output_tokens": 100})

    assert budget.landing() is None


def test_closing_the_run_leaves_no_stage_saying_running(settings, budget, log):
    """A row that outlives its run is a claim about a process that is gone."""
    from clipper.agent.runtime import close_run
    from clipper.state import StageStatus, State

    ctx = ToolContext(settings=settings, run_id="20260910-120000-vid")
    settings.video_dir("vid").mkdir(parents=True)
    state = State(settings.state_db)
    state.set_stage("vid", "package", StageStatus.RUNNING, profile="short", detail="3/4 cortes")

    close_run(
        ctx,
        progress=RunProgress(),
        budget=budget,
        log=log,
        video_id="vid",
        dry_run=True,
        reason="max_turns",
    )

    record = state.get_stage("vid", "package", profile="short")
    assert record is not None and record.status is StageStatus.PENDING
    assert "run encerrada" in record.detail


# --------------------------------------------------------------------------- #
# The SDK wiring (the one piece the fake session cannot cover)
# --------------------------------------------------------------------------- #

sdk = pytest.importorskip("claude_agent_sdk", reason="precisa do extra `agent`")


def test_every_tool_is_accepted_by_the_sdk_as_an_in_process_mcp_server(settings, budget, log):
    """Builds the real server and options - no model, no network, no quota."""
    from clipper.agent.runtime import sdk_tools
    from clipper.agent.tools import SPECS

    ctx = ToolContext(settings=settings, run_id="20260910-120000-vid")
    wrapped = sdk_tools(ctx, budget, log, RunProgress())
    assert len(wrapped) == len(SPECS)

    server = sdk.create_sdk_mcp_server(name="clipper", version="1.0.0", tools=wrapped)
    options = sdk.ClaudeAgentOptions(
        model=settings.agent.model,
        system_prompt=system_prompt(settings),
        mcp_servers={"clipper": server},
        allowed_tools=[f"mcp__clipper__{spec.name}" for spec in SPECS],
        tools=[],
        setting_sources=[],
        max_turns=settings.agent.max_turns,
        cwd=str(settings.workspace.expanduser()),
    )
    assert options.mcp_servers["clipper"] is server
    # No built-in tool and no settings from disk: the model executes nothing and
    # never sees this repository's CLAUDE.md (SPEC-agent 3).
    assert options.tools == [] and options.setting_sources == []


def test_a_wrapped_tool_answers_in_the_shape_the_sdk_forwards(settings, budget, log):
    from clipper.agent.runtime import sdk_tools

    ctx = ToolContext(settings=settings, enabled_profiles=("short",))
    wrapped = {tool.name: tool for tool in sdk_tools(ctx, budget, log, RunProgress())}

    answer = run(wrapped["list_profiles"].handler({}))
    assert set(answer) == {"content", "is_error"}
    assert answer["is_error"] is False
    assert "short" in answer["content"][0]["text"]


def test_the_opening_message_carries_the_channel_when_there_is_one(settings, budget):
    from clipper.agent.channel import Channel, ChannelProfile, TitleStyle

    channel = Channel(
        profile=ChannelProfile(
            id="meucanal",
            name="Meu Canal",
            profiles=["short"],
            title=TitleStyle(tone="direto, em minusculas"),
        )
    )
    text = opening_prompt(
        settings,
        url="https://youtu.be/abc",
        video_id="abc",
        profiles=["short"],
        budget=budget,
        dry_run=False,
        done=[],
        channel=channel,
    )
    assert "Meu Canal" in text and "direto, em minusculas" in text


def test_without_a_channel_the_opening_message_says_nothing_about_one(settings, budget):
    text = opening_prompt(
        settings,
        url="u",
        video_id="abc",
        profiles=["short"],
        budget=budget,
        dry_run=False,
        done=[],
    )
    assert "Canal:" not in text


def test_a_number_that_is_only_reported_carries_no_limit(budget):
    """A bar that fills against a limit nobody enforces is fiction."""
    budget.note_usage({"cache_read_input_tokens": 900_000})
    budget.note_turn()

    answers = budget.view().line("respostas do modelo")
    cache = budget.view().line("leitura de cache")
    assert answers is not None and answers.limit == 0 and answers.used == 1
    assert cache is not None and cache.limit == 0 and cache.used == 900_000


def test_the_report_gets_the_run_numbers_after_the_run_is_closed(settings, budget, log):
    """The agent writes the report mid-loop, when turns and cost are still zero."""
    from clipper.agent.runtime import close_run
    from clipper.state import RunStatus, State

    ctx = ToolContext(settings=settings, run_id="20260911-120000-vid")
    settings.video_dir("vid").mkdir(parents=True)
    state = State(settings.state_db)
    state.start_run(ctx.run_id, "vid")

    # The agent's own finish_run, while the loop is still going.
    tools_result = tools_get("finish_run").call(
        ctx, {"video_id": "vid", "summary": "Entreguei dois shorts do video."}
    )
    assert not tools_result.is_error
    report = settings.video_dir("vid") / "agent" / f"{ctx.run_id}.report.md"
    assert "## Run" not in report.read_text(encoding="utf-8")

    state.finish_run(ctx.run_id, RunStatus.DONE, turns=75, cost_usd=1.4459)
    close_run(
        ctx,
        progress=RunProgress(finished=True),
        budget=budget,
        log=log,
        video_id="vid",
        dry_run=True,
        reason="",
    )

    text = report.read_text(encoding="utf-8")
    assert "respostas do modelo: 75" in text
    assert "US$ 1.45" in text
    assert "(done)" in text


# --------------------------------------------------------------------------- #
# The operator's hand (SPEC-agent 9.5)
# --------------------------------------------------------------------------- #


@pytest.fixture
def watcher(tmp_path):
    from clipper.agent.control import ControlWatcher

    return ControlWatcher(tmp_path / "run.control.json")


def order(watcher, **fields):
    from clipper.agent.control import SessionControl, write_control

    write_control(watcher.path, SessionControl(**fields))


def test_a_paused_run_waits_instead_of_burning_turns(settings, budget, log, watcher, monkeypatch):
    """Refusing every couple of seconds would spend the budget standing still."""
    import clipper.agent.runtime as runtime_module

    order(watcher, action="pause")
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            order(watcher, action="run")

    monkeypatch.setattr(runtime_module.asyncio, "sleep", fake_sleep)
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = run(
        call_tool(
            echo_spec("snap_clip"),
            {},
            ctx=ctx,
            budget=budget,
            log=log,
            progress=progress,
            watcher=watcher,
        )
    )

    assert result.is_error is False  # a chamada aconteceu, depois da pausa
    assert len(slept) == 2
    kinds = [r["type"] for r in lines(log.path)]
    assert "paused" in kinds and "resumed" in kinds


def test_stopping_a_run_ends_the_call_and_the_loop(settings, budget, log, watcher):
    order(watcher, action="stop", publish_on_stop=False)
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = run(
        call_tool(
            echo_spec("render_clip"),
            {"profile": "short", "index": 1},
            ctx=ctx,
            budget=budget,
            log=log,
            progress=progress,
            watcher=watcher,
        )
    )

    assert result.is_error and "parada pedida por voce" in result.text
    assert progress.stop_requested and progress.publish_on_stop is False
    assert budget.renders == 0  # nada rodou


def test_the_loop_interrupts_as_soon_as_a_stop_is_seen(budget, log):
    progress = RunProgress(stop_requested="parada pedida por voce no monitor (monitor)")
    session = FakeSession([[Assistant([Block("seguindo")]), Result()]])

    end = run(drive(session, "comece", budget=budget, log=log, progress=progress))

    assert end.interrupted is True and session.interrupted is True
    assert "parada pedida" in end.reason


def test_a_vetoed_clip_is_not_rendered_again(settings, budget, log, watcher):
    order(watcher, vetoed=["vid/short/2"])
    ctx = ToolContext(settings=settings)
    progress = RunProgress()

    result = run(
        call_tool(
            echo_spec("render_clip"),
            {"video_id": "vid", "profile": "short", "index": 2},
            ctx=ctx,
            budget=budget,
            log=log,
            progress=progress,
            watcher=watcher,
        )
    )

    assert result.is_error and "vetado por voce" in result.text
    assert budget.renders == 0
    # And the veto reaches the tools, which is how publish_clips skips it.
    assert ctx.vetoed == {"vid/short/2"}


def test_another_clip_of_the_same_profile_is_untouched(settings, budget, log, watcher):
    order(watcher, vetoed=["vid/short/2"])
    ctx = ToolContext(settings=settings)

    result = run(
        call_tool(
            echo_spec("render_clip"),
            {"video_id": "vid", "profile": "short", "index": 1},
            ctx=ctx,
            budget=budget,
            log=log,
            progress=RunProgress(),
            watcher=watcher,
        )
    )

    assert result.is_error is False and budget.renders == 1
