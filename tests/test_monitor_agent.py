"""The agent session as the monitor reads it (SPEC-agent 9).

Built from synthetic JSONL: the reader is a projection, so a known trail must
produce a known page.
"""

from __future__ import annotations

import json

import pytest

from clipper.agent.budget import Budget
from clipper.agent.log import RunLog
from clipper.config import AgentSettings, load_settings
from clipper.monitor.agent import RunReader, RunStore, describe, run_paths

RUN_ID = "20260910-120000-vid0000001"
VIDEO_ID = "vid0000001"


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    return load_settings(config)


@pytest.fixture
def log_path(settings):
    return settings.agent_dir(VIDEO_ID) / f"{RUN_ID}.jsonl"


@pytest.fixture
def budget():
    return Budget(limits=AgentSettings())


def write_run(log_path, budget, *, finish: bool = True):
    """A small but complete run: two clips, one of them re-rendered."""
    with RunLog(log_path, RUN_ID) as log:
        log.run_start(VIDEO_ID, url="https://youtu.be/x", model="claude-opus-5", budget=budget)
        log.turn("plano: dois shorts deste video", budget=budget)

        log.tool_start("commit_clips", {"video_id": VIDEO_ID, "profile": "short", "clips": [1, 2]})
        log.tool_call(
            "commit_clips",
            {"video_id": VIDEO_ID, "profile": "short", "clips": [{}, {}]},
            output=json.dumps(
                {
                    "video_id": VIDEO_ID,
                    "profile": "short",
                    "clips": [
                        {"clip_id": f"{VIDEO_ID}/short/1", "index": 1, "topic": "primeiro"},
                        {"clip_id": f"{VIDEO_ID}/short/2", "index": 2, "topic": "segundo"},
                    ],
                }
            ),
            duration_ms=40,
            is_error=False,
            budget=budget,
        )

        for index in (1, 2):
            budget.spend("render_clip", {"profile": "short", "index": index})
            log.tool_start("render_clip", {"profile": "short", "index": index})
            log.tool_call(
                "render_clip",
                {"video_id": VIDEO_ID, "profile": "short", "index": index},
                output=json.dumps({"clip_id": f"{VIDEO_ID}/short/{index}", "reused": False}),
                duration_ms=120_000,
                is_error=False,
                budget=budget,
            )

        log.tool_start("review_render", {"clip_id": f"{VIDEO_ID}/short/1"})
        log.tool_call(
            "review_render",
            {"clip_id": f"{VIDEO_ID}/short/1"},
            output=json.dumps(
                {
                    "clip_id": f"{VIDEO_ID}/short/1",
                    "verdict": "fail",
                    "checks": [
                        {"check": "duration", "status": "ok", "value": "28s"},
                        {
                            "check": "loudness",
                            "status": "fail",
                            "value": "-23.0 LUFS",
                            "fix": "refazer o render",
                        },
                    ],
                }
            ),
            duration_ms=800,
            is_error=False,
            budget=budget,
        )

        log.tool_start("snap_clip", {"clip": {"segments": [[60.0, 88.0]]}})
        log.tool_call(
            "snap_clip",
            {"video_id": VIDEO_ID, "profile": "short", "clip": {"segments": [[60.0, 88.0]]}},
            output=json.dumps({"accepted": True, "segments": [[59.8, 88.4]], "changed": True}),
            duration_ms=15,
            is_error=False,
            budget=budget,
        )

        log.budget_deny("render_clip", {"profile": "short", "index": 1}, "orcamento esgotado")
        # Started and never closed: with `finish=False` this is what a run whose
        # process died looks like, and the page must still show it.
        log.tool_start("publish_clips", {"video_id": VIDEO_ID, "profiles": ["short"]})
        if finish:
            log.tool_call(
                "finish_run",
                {
                    "video_id": VIDEO_ID,
                    "summary": "um short entregue",
                    "delivered": [f"{VIDEO_ID}/short/2"],
                    "skipped": [{"what": f"{VIDEO_ID}/short/1", "reason": "loudness"}],
                },
                output=json.dumps({"run_id": RUN_ID, "delivered": 1}),
                duration_ms=5,
                is_error=False,
                budget=budget,
            )
            log.run_end("done", budget=budget, cost_usd=1.23)
    return log_path


# --- reading --------------------------------------------------------------- #


def test_a_finished_run_reads_back_as_a_whole_session(log_path, budget):
    write_run(log_path, budget)
    view = RunReader(path=log_path).refresh()

    assert view.run_id == RUN_ID and view.video_id == VIDEO_ID
    assert view.state == "concluida" and view.cost_usd == 1.23
    assert view.model == "claude-opus-5"
    assert view.turns == 1
    assert [line.name for line in view.budget][:2] == ["respostas do modelo", "renders"]
    assert view.delivered == [f"{VIDEO_ID}/short/2"]
    assert view.skipped == [f"{VIDEO_ID}/short/1: loudness"]


def test_each_clip_carries_what_the_run_did_to_it(log_path, budget):
    write_run(log_path, budget)
    view = RunReader(path=log_path).refresh()

    first, second = view.clips
    assert first.clip_id == f"{VIDEO_ID}/short/1" and first.topic == "primeiro"
    assert first.renders == 1 and first.verdict == "fail"
    # Only what did not pass travels: an all-ok laudo is not worth the bytes.
    assert [check.check for check in first.checks] == ["loudness"]
    assert second.stage == "renderizado"


def test_a_moved_boundary_is_kept_with_both_sides(log_path, budget):
    write_run(log_path, budget)
    view = RunReader(path=log_path).refresh()

    change = view.boundary_changes[0]
    assert change.before == [(60.0, 88.0)] and change.after == [(59.8, 88.4)]
    assert change.accepted is True


def test_the_trail_reads_like_a_list_of_decisions(log_path, budget):
    write_run(log_path, budget)
    view = RunReader(path=log_path).refresh()

    calls = [(step.tool, step.summary) for step in view.trail]
    assert ("commit_clips", "short: 2 cortes") in calls
    assert ("render_clip", "short #1") in calls
    assert view.trail[-1].tool == "finish_run"


def test_a_refusal_shows_up_as_an_error_with_its_reason(log_path, budget):
    write_run(log_path, budget)
    view = RunReader(path=log_path).refresh()

    denial = next(step for step in view.errors if step.kind == "budget_deny")
    assert denial.error == "orcamento esgotado" and denial.tool == "render_clip"


def test_a_run_whose_process_died_still_reads_as_a_session(log_path, budget):
    write_run(log_path, budget, finish=False)
    view = RunReader(path=log_path).refresh()

    assert view.state == "rodando"
    # The last tool never closed, so the page can show it as still in flight.
    assert view.now.tool == "publish_clips"
    assert view.now.since_s >= 0.0
    assert view.idle_s >= 0.0


def test_reading_twice_costs_nothing_and_says_the_same_thing(log_path, budget):
    write_run(log_path, budget)
    reader = RunReader(path=log_path)
    first = reader.refresh().model_dump(exclude={"idle_s", "elapsed_s", "now"})
    offset = reader.offset
    second = reader.refresh().model_dump(exclude={"idle_s", "elapsed_s", "now"})

    assert first == second
    assert reader.offset == offset  # nada novo foi lido


def test_a_growing_log_is_read_incrementally(log_path, budget):
    """The monitor rebuilds every 0,7 s; only the new bytes may be parsed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with RunLog(log_path, RUN_ID) as log:
        log.run_start(VIDEO_ID, url="u", model="m", budget=budget)
        reader = RunReader(path=log_path)
        reader.refresh()
        first_offset = reader.offset

        log.turn("segunda decisao", budget=budget)
        view = reader.refresh()

    assert reader.offset > first_offset
    assert view.turns == 1 and view.now.text == "segunda decisao"


def test_a_half_written_line_is_left_for_the_next_read(log_path, budget):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"type": "run_start", "seq": 1, "video_id": "v"}\n{"type": "tu', "utf-8")
    reader = RunReader(path=log_path)

    view = reader.refresh()
    assert view.video_id == "v"

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write('rn", "seq": 2, "text": "agora sim"}\n')
    assert reader.refresh().now.text == "agora sim"


def test_a_replaced_log_starts_the_reading_over(log_path, budget):
    write_run(log_path, budget)
    reader = RunReader(path=log_path)
    reader.refresh()

    log_path.write_text('{"type": "run_start", "seq": 1, "video_id": "outro"}\n', "utf-8")
    view = reader.refresh()

    assert view.video_id == "outro" and view.trail == []


# --- the store and the listing --------------------------------------------- #


def test_runs_of_a_video_come_back_newest_first(settings, budget):
    directory = settings.agent_dir(VIDEO_ID)
    for run_id in ("20260910-090000-vid", "20260910-180000-vid"):
        with RunLog(directory / f"{run_id}.jsonl", run_id) as log:
            log.run_start(VIDEO_ID, url="u", model="m", budget=budget)

    assert [path.stem for path in run_paths(directory)] == [
        "20260910-180000-vid",
        "20260910-090000-vid",
    ]
    listing = RunStore().listing(directory)
    assert [run.run_id for run in listing] == ["20260910-180000-vid", "20260910-090000-vid"]
    # The listing is a listing: no trail, so it stays small however long the run was.
    assert all(run.trail == [] for run in listing)


def test_the_summary_the_snapshot_carries_is_small(log_path, budget):
    write_run(log_path, budget)
    full = RunStore().read(log_path)
    small = full.summary()

    assert len(small.trail) == 5 and small.trail_total == len(full.trail)
    assert small.clips == full.clips
    assert small.boundary_changes == []


def test_arguments_are_described_in_words_not_json():
    assert describe("inspect_range", {"start": 62.0, "end": 88.0}) == "01:02 - 01:28"
    assert describe("render_clip", {"profile": "short", "index": 2, "force": True}) == (
        "short #2 (refazendo)"
    )
    assert describe("publish_clips", {"profiles": ["short"], "dry_run": True}) == "short (ensaio)"
    assert describe("find_candidates", {"profile": "quiz", "focus": "CLT"}) == (
        "perfil quiz, foco: CLT"
    )


# --- what the snapshot carries --------------------------------------------- #


def test_the_snapshot_carries_the_newest_session_of_each_video(settings, log_path, budget):
    from clipper.models import SourceInfo
    from clipper.monitor.snapshot import build_snapshot
    from clipper.state import StageStatus, State

    state = State(settings.state_db)
    state.upsert_video(
        SourceInfo(video_id=VIDEO_ID, title="T", channel="C", duration=600.0, url="u")
    )
    state.set_stage(VIDEO_ID, "ingest", StageStatus.DONE)
    write_run(log_path, budget)

    snapshot = build_snapshot(settings, runs=RunStore())
    video = next(v for v in snapshot.videos if v.video_id == VIDEO_ID)

    assert video.agent is not None
    assert video.agent.run_id == RUN_ID and video.agent.state == "concluida"
    assert len(video.agent.trail) == 5  # so the SSE push stays small


def test_a_video_without_a_run_carries_no_session(settings):
    from clipper.models import SourceInfo
    from clipper.monitor.snapshot import build_snapshot
    from clipper.state import State

    State(settings.state_db).upsert_video(
        SourceInfo(video_id="outro", title="T", channel="C", duration=1.0, url="u")
    )
    snapshot = build_snapshot(settings, runs=RunStore())
    assert all(video.agent is None for video in snapshot.videos)


@pytest.mark.parametrize(
    "run_id",
    ["", "../../etc/passwd", "20260910-120000-../x", "nao-e-um-id", "20260910-120000-" + "x" * 40],
)
def test_a_run_id_that_is_not_one_is_refused(run_id):
    from clipper.monitor.server import RUN_ID_RE

    assert RUN_ID_RE.fullmatch(run_id) is None


def test_a_real_run_id_passes_the_guard():
    from clipper.monitor.server import RUN_ID_RE

    assert RUN_ID_RE.fullmatch(RUN_ID) is not None


def test_recommitting_the_list_keeps_what_the_other_clips_already_cost(log_path, budget):
    """The agent fixes one clip and re-commits all of them; the rest keep their history."""
    write_run(log_path, budget, finish=False)
    with RunLog(log_path, RUN_ID) as log:
        log.tool_call(
            "commit_clips",
            {"video_id": VIDEO_ID, "profile": "short", "clips": [{}, {}]},
            output=json.dumps(
                {
                    "video_id": VIDEO_ID,
                    "profile": "short",
                    "clips": [
                        {"clip_id": f"{VIDEO_ID}/short/1", "index": 1, "topic": "primeiro"},
                        {"clip_id": f"{VIDEO_ID}/short/2", "index": 2, "topic": "segundo"},
                    ],
                }
            ),
            duration_ms=10,
            is_error=False,
            budget=budget,
        )

    view = RunReader(path=log_path).refresh()
    first = next(c for c in view.clips if c.clip_id == f"{VIDEO_ID}/short/1")
    assert first.renders == 1 and first.verdict == "fail"
    assert [check.check for check in first.checks] == ["loudness"]
