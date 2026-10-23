"""Stage rows that close, and clips the agent can actually drop (SPEC-agent 8.4.1).

`render` and `package` are written clip by clip, so they sit at `running` while one
is missing. The run that walks away from a rendered clip leaves them there forever -
which is how the monitor came to show two videos working with nothing running.
"""

from __future__ import annotations

import json

import pytest

from clipper.agent import tools
from clipper.agent.progress import STALLED_NOTE, count, reconcile
from clipper.agent.tools.base import ToolContext
from clipper.config import load_settings
from clipper.models import (
    Clip,
    ClipSet,
    ClipStatus,
    Segment,
    SourceInfo,
    TimeRange,
    Transcript,
    Word,
)
from clipper.state import StageStatus, State
from clipper.utils.errors import InputError

VIDEO_ID = "vid0000002"
RUN_ID = "20260911-120000-vid00000"


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    return load_settings(config)


@pytest.fixture
def two_clips(settings):
    """Two rendered clips of one profile; only the first one has metadata.

    That is the state a run ends in when a clip renders, fails the report for
    something that is in the source, and the agent moves on without it.
    """
    video_dir = settings.video_dir(VIDEO_ID)
    video_dir.mkdir(parents=True)
    # Long enough to survive the profile's duration rule, so commit_clips can be
    # driven here too; the words themselves only have to be quotable.
    spoken = [
        "entao ele contou um dois e a mesa inteira caiu na risada.",
        "depois disso ninguem mais conseguiu jogar serio naquela noite.",
        "e no fim das contas foi a melhor partida que a gente ja teve.",
    ]
    segments = []
    for index, text in enumerate(spoken):
        start = 60.0 + index * 12.0
        words = [
            Word(text=word, start=start + n * 0.5, end=start + n * 0.5 + 0.4)
            for n, word in enumerate(text.split())
        ]
        segments.append(Segment(id=index, start=start, end=words[-1].end, text=text, words=words))
    transcript = Transcript(video_id=VIDEO_ID, language="pt", duration=600.0, segments=segments)
    (video_dir / "transcript.json").write_text(transcript.model_dump_json(), encoding="utf-8")
    State(settings.state_db).upsert_video(
        SourceInfo(video_id=VIDEO_ID, title="T", channel="C", duration=600.0, url="u")
    )

    clips = [
        Clip(
            index=i,
            slug=f"corte{i}",
            profile="short",
            segments=[TimeRange(start=60.0, end=segments[-1].end)],
        )
        for i in (1, 2)
    ]
    profile_dir = settings.profile_dir(VIDEO_ID, "short")
    profile_dir.mkdir(parents=True)
    (profile_dir / "clips.json").write_text(
        ClipSet(video_id=VIDEO_ID, profile="short", clips=clips).model_dump_json(),
        encoding="utf-8",
    )
    for clip in clips:
        clip_dir = profile_dir / "clips" / clip.dir_name
        clip_dir.mkdir(parents=True)
        (clip_dir / "clip.mp4").write_bytes(b"x")
    return profile_dir


@pytest.fixture
def ctx(settings, two_clips):
    State(settings.state_db).start_run(RUN_ID, VIDEO_ID)
    return ToolContext(settings=settings, run_id=RUN_ID, enabled_profiles=("short",))


def answer(ctx, name, **payload):
    result = tools.get(name).call(ctx, payload)
    assert not result.is_error, result.text
    return json.loads(result.text)


def stage(ctx, name):
    record = ctx.state().get_stage(VIDEO_ID, name, profile="short")
    assert record is not None
    return record


def write_metadata(ctx, index):
    # No number in the title on purpose: set_metadata refuses one the clip never
    # says, and the point of these tests is the stage rows, not the title check.
    titles = {1: "o primeiro corte", 2: "o segundo corte"}
    return answer(ctx, "set_metadata", clip_id=f"{VIDEO_ID}/short/{index}", title=titles[index])


# --- the state the bug leaves behind --------------------------------------- #


def test_package_waits_while_a_rendered_clip_has_no_metadata(ctx):
    write_metadata(ctx, 1)

    record = stage(ctx, "package")
    assert record.status is StageStatus.RUNNING
    assert record.detail == "1/2 cortes"


def test_the_last_metadata_closes_the_stage(ctx):
    write_metadata(ctx, 1)
    write_metadata(ctx, 2)

    assert stage(ctx, "package").status is StageStatus.DONE


# --- drop_clip ------------------------------------------------------------- #


def test_dropping_the_clip_closes_the_stage_it_was_holding_open(ctx):
    write_metadata(ctx, 1)

    dropped = answer(
        ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="loudness -16,9 LUFS na fonte"
    )

    assert (dropped["remaining"], dropped["packaged"]) == (1, 1)
    assert dropped["reused"] is False
    assert stage(ctx, "package").status is StageStatus.DONE
    assert stage(ctx, "render").status is StageStatus.DONE


def test_a_dropped_clip_is_rejected_in_the_clip_set(ctx, settings, two_clips):
    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="nao se sustenta")

    clip_set = ClipSet.model_validate_json((two_clips / "clips.json").read_text(encoding="utf-8"))
    statuses = {clip.index: clip.status for clip in clip_set.clips}
    assert statuses[2] is ClipStatus.REJECTED
    assert statuses[1] is not ClipStatus.REJECTED
    # The render stays on disk to be looked at; it only leaves the working set.
    assert (two_clips / "clips" / "02_corte2" / "clip.mp4").exists()


def test_dropping_twice_says_nothing_happened(ctx):
    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="primeira vez")

    again = answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="de novo")

    assert again["reused"] is True


def test_the_run_report_records_why_the_clip_was_dropped(ctx):
    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="audio baixo na fonte")

    records = {r.clip_index: r for r in ctx.state().run_clips(RUN_ID)}
    assert records[2].verdict == "dropped"
    assert records[2].detail == "audio baixo na fonte"


def test_publishing_does_not_ship_a_dropped_clip(ctx):
    """The other half of the bug: without drop_clip the batch writer shipped it anyway."""
    write_metadata(ctx, 1)
    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="reprovado na fonte")

    plan = answer(ctx, "publish_clips", video_id=VIDEO_ID, profiles=["short"], dry_run=True)

    assert [item["clip_id"] for item in plan["published"]] == [f"{VIDEO_ID}/short/1"]
    assert plan["skipped"] == []


def test_a_clip_that_only_failed_loudness_is_not_dropped_but_re_rendered(ctx):
    """Measured: the clip dropped at -16.9 LUFS came back at -14.1 from the same source."""
    ctx.state().record_run_clip(
        RUN_ID, VIDEO_ID, "short", 2, renders=1, verdict="fail", detail="loudness=fail"
    )

    result = tools.get("drop_clip").call(
        ctx, {"clip_id": f"{VIDEO_ID}/short/2", "reason": "loudness -16,9 LUFS"}
    )

    assert result.is_error
    assert "render_clip(force=true)" in result.text
    clip_set = pipeline_clips(ctx)
    assert clip_set.clips[1].status is not ClipStatus.REJECTED


def test_the_same_clip_can_be_dropped_once_its_renders_are_spent(ctx, settings):
    ctx.state().record_run_clip(
        RUN_ID,
        VIDEO_ID,
        "short",
        2,
        renders=settings.agent.max_clip_renders,
        verdict="fail",
        detail="loudness=fail",
    )

    answer(
        ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="reprovou com o orcamento no fim"
    )

    assert pipeline_clips(ctx).clips[1].status is ClipStatus.REJECTED


def test_a_clip_that_failed_something_else_too_is_dropped_normally(ctx):
    ctx.state().record_run_clip(
        RUN_ID,
        VIDEO_ID,
        "short",
        2,
        renders=1,
        verdict="fail",
        detail="loudness=fail; caption_fit=fail",
    )

    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="legenda nao cabe")

    assert pipeline_clips(ctx).clips[1].status is ClipStatus.REJECTED


def pipeline_clips(ctx):
    from clipper import pipeline

    return pipeline.load_clips(ctx.settings, VIDEO_ID, "short")


# --- what a title is allowed to promise (SPEC 1.2) ------------------------- #


def test_a_title_promising_a_number_the_clip_never_says_is_refused(ctx):
    """The real one: a short titled "briguei por 10 mil" over a cut that never says it."""
    result = tools.get("set_metadata").call(
        ctx, {"clip_id": f"{VIDEO_ID}/short/1", "title": "briguei por 10 mil num carro"}
    )

    assert result.is_error
    assert "10" in result.text and "nao diz" in result.text


def test_a_title_backed_by_the_clip_is_written(ctx):
    written = answer(ctx, "set_metadata", clip_id=f"{VIDEO_ID}/short/1", title="ele contou um dois")

    assert written["title"] == "ele contou um dois"


def test_a_question_on_the_bar_is_held_to_the_same_rule(settings, two_clips):
    """The bar is read by someone who saw only the cut, so it may only ask about it."""
    from clipper.agent.tools.select import _check_questions
    from clipper.models import Transcript

    clip_set = ClipSet.model_validate_json((two_clips / "clips.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate_json(
        (settings.video_dir(VIDEO_ID) / "transcript.json").read_text(encoding="utf-8")
    )
    clip_set.clips[0].comment_prompt = "voce guardaria magoa por 10 mil reais?"

    with pytest.raises(InputError, match="nao diz"):
        _check_questions(clip_set, transcript)


def test_a_question_the_clip_can_answer_passes(settings, two_clips):
    from clipper.agent.tools.select import _check_questions
    from clipper.models import Transcript

    clip_set = ClipSet.model_validate_json((two_clips / "clips.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate_json(
        (settings.video_dir(VIDEO_ID) / "transcript.json").read_text(encoding="utf-8")
    )
    clip_set.clips[0].comment_prompt = "voce ja perdeu uma partida assim?"

    _check_questions(clip_set, transcript)


# --- reconcile ------------------------------------------------------------- #


def test_reconcile_parks_a_stage_the_run_left_half_written(ctx):
    write_metadata(ctx, 1)

    notes = reconcile(ctx, VIDEO_ID)

    record = stage(ctx, "package")
    assert record.status is StageStatus.PENDING
    assert record.detail == f"1/2 cortes; {STALLED_NOTE}"
    assert any("package" in note for note in notes)


def test_reconcile_closes_a_stage_whose_work_is_actually_done(ctx):
    write_metadata(ctx, 1)
    write_metadata(ctx, 2)
    # A row that lies the other way: the work is there, the writer died before saying so.
    ctx.state().set_stage(VIDEO_ID, "package", StageStatus.RUNNING, profile="short", detail="1/2")

    reconcile(ctx, VIDEO_ID)

    assert stage(ctx, "package").status is StageStatus.DONE


def test_reconcile_leaves_finished_stages_alone(ctx):
    write_metadata(ctx, 1)
    write_metadata(ctx, 2)
    before = stage(ctx, "package")

    assert reconcile(ctx, VIDEO_ID) == []
    assert stage(ctx, "package").updated_at == before.updated_at


def test_reconcile_parks_a_video_stage_it_cannot_count(ctx):
    ctx.state().set_stage(VIDEO_ID, "transcribe", StageStatus.RUNNING, detail="80%")

    reconcile(ctx, VIDEO_ID)

    record = ctx.state().get_stage(VIDEO_ID, "transcribe")
    assert record is not None
    assert record.status is StageStatus.PENDING
    assert record.detail == f"80%; {STALLED_NOTE}"


def test_counting_ignores_a_clip_that_was_dropped(ctx):
    answer(ctx, "drop_clip", clip_id=f"{VIDEO_ID}/short/2", reason="fora")

    counted = count(ctx, VIDEO_ID, "short")

    assert (counted.total, counted.rendered, counted.packaged) == (1, 1, 0)
