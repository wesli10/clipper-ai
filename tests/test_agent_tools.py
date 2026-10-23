"""The agent's toolbox: schemas, validation and the handlers (SPEC-agent 5).

No model and no network: the tools are exercised exactly as the runtime will
call them, with a workspace built by hand.
"""

from __future__ import annotations

import json

import pytest

from clipper.agent import tools
from clipper.agent.tools.base import ToolContext, truncate
from clipper.agent.tools.select import OVERRIDES_NAME
from clipper.config import load_settings
from clipper.models import (
    ClipCandidate,
    ClipSet,
    ClipStatus,
    Segment,
    SelectionResponse,
    SourceInfo,
    TimeRange,
    Transcript,
    Word,
)
from clipper.state import State
from clipper.utils.errors import InputError

VIDEO_ID = "vid0000001"


def spoken(seg_id: int, start: float, text: str, length: float) -> Segment:
    tokens = text.split()
    step = length / len(tokens)
    words = [
        Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step - 0.02, 3))
        for i, t in enumerate(tokens)
    ]
    return Segment(id=seg_id, start=start, end=round(start + length, 3), text=text, words=words)


def sample_transcript() -> Transcript:
    """Twenty sentences, 28 s each, separated by a two-second pause."""
    segments = [
        spoken(i, i * 30.0, f"assunto numero {i} explicado com calma ate o fim.", 28.0)
        for i in range(20)
    ]
    return Transcript(video_id=VIDEO_ID, language="pt", duration=600.0, segments=segments)


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    return load_settings(config)


@pytest.fixture
def workspace(settings):
    video_dir = settings.video_dir(VIDEO_ID)
    video_dir.mkdir(parents=True)
    (video_dir / "source.mp4").write_bytes(b"x")
    (video_dir / "transcript.json").write_text(
        sample_transcript().model_dump_json(), encoding="utf-8"
    )
    State(settings.state_db).upsert_video(
        SourceInfo(
            video_id=VIDEO_ID,
            title="Video de teste",
            channel="Canal",
            duration=600.0,
            url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
            width=1920,
            height=1080,
        )
    )
    return VIDEO_ID


@pytest.fixture
def ctx(settings):
    return ToolContext(settings=settings, run_id="20260910-120000-vid", enabled_profiles=("short",))


def call(ctx, name, **payload):
    return tools.get(name).call(ctx, payload)


def answer(ctx, name, **payload):
    result = call(ctx, name, **payload)
    assert not result.is_error, result.text
    return json.loads(result.text)


# --- registry -------------------------------------------------------------- #


def test_every_tool_is_registered_once_with_a_usable_schema():
    assert len(tools.SPECS) == len(tools.registry()) == 16
    for spec in tools.SPECS:
        schema = spec.input_schema
        assert schema["additionalProperties"] is False, spec.name
        assert spec.description.startswith(("Use", "Chame", "Ultima")), spec.name
        assert spec.output_limit > 0


def test_an_unknown_tool_is_an_input_error():
    with pytest.raises(InputError, match="ferramenta desconhecida"):
        tools.get("render_everything")


def test_truncation_says_that_it_truncated():
    assert truncate("abc", 10) == "abc"
    cut = truncate("a" * 100, 30)
    assert len(cut) == 30 and cut.endswith('…"[truncado]"')


def test_bad_input_comes_back_as_a_tool_error_not_an_exception(ctx, workspace):
    result = call(ctx, "inspect_range", video_id=workspace, start=100.0, end=10.0)
    assert result.is_error and "entrada invalida" in result.text


def test_an_unexpected_field_is_refused(ctx, workspace):
    result = call(ctx, "get_outline", video_id=workspace, depth=3)
    assert result.is_error and "depth" in result.text


def test_a_missing_video_is_an_actionable_error(ctx):
    result = call(ctx, "get_outline", video_id="naoexiste")
    assert result.is_error and "transcricao nao encontrada" in result.text


# --- list_profiles --------------------------------------------------------- #


def test_list_profiles_only_shows_what_the_channel_enabled(ctx):
    payload = answer(ctx, "list_profiles")
    assert [p["name"] for p in payload["profiles"]] == ["short"]
    assert "n_clips" in payload["overridable"]
    assert "reframe" not in payload["overridable"]


def test_a_disabled_profile_does_not_exist_for_the_agent(ctx, workspace):
    clip = {"segments": [[0, 30]]}
    result = call(ctx, "snap_clip", video_id=workspace, profile="long", clip=clip)
    assert result.is_error and "desabilitado" in result.text


# --- get_video / outline / inspect ----------------------------------------- #


def test_get_video_wants_exactly_one_of_url_or_id(ctx, workspace):
    assert call(ctx, "get_video").is_error
    assert call(ctx, "get_video", url="u", video_id=workspace).is_error


def test_get_video_reads_a_workspace_that_is_already_there(ctx, workspace):
    payload = answer(ctx, "get_video", video_id=workspace)
    assert payload["title"] == "Video de teste" and payload["reused"] is True


def test_outline_splits_on_the_widest_pauses(ctx, workspace):
    payload = answer(ctx, "get_outline", video_id=workspace, max_blocks=5)
    assert payload["n_sentences"] == 20
    assert 1 <= len(payload["blocks"]) <= 5
    assert payload["blocks"][0]["first_sentence"].startswith("assunto numero 0")
    # Blocks tile the video: no gap and no overlap between neighbours.
    for previous, following in zip(payload["blocks"], payload["blocks"][1:], strict=False):
        assert previous["end"] <= following["start"]


def test_inspect_reports_the_text_with_margin_and_the_boundaries(ctx, workspace):
    payload = answer(ctx, "inspect_range", video_id=workspace, start=60.0, end=88.0, margin_s=10.0)
    assert "assunto numero 2" in payload["text"]
    assert "assunto numero 1" in payload["before"]
    assert payload["boundaries"]["opens_well"] is True
    assert payload["boundaries"]["is_closing"] is True
    assert payload["boundaries"]["suggested_end"] >= 88.0 - 1.0


# --- snap_clip ------------------------------------------------------------- #


def test_snap_clip_moves_the_edges_and_says_so(ctx, workspace):
    payload = answer(
        ctx, "snap_clip", video_id=workspace, profile="short", clip={"segments": [[61.5, 86.0]]}
    )
    assert payload["accepted"] is True
    assert payload["changed"] is True
    start, end = payload["segments"][0]
    assert start <= 61.5 and end >= 86.0


def test_snap_clip_refuses_what_the_profile_would_refuse(ctx, workspace):
    payload = answer(
        ctx, "snap_clip", video_id=workspace, profile="short", clip={"segments": [[60.0, 65.0]]}
    )
    assert payload["accepted"] is False
    assert "duracao" in payload["reason"] or "minimo" in payload["reason"]


def test_snap_clip_checks_the_quoted_hook_against_the_transcript(ctx, workspace):
    payload = answer(
        ctx,
        "snap_clip",
        video_id=workspace,
        profile="short",
        clip={"segments": [[60.0, 88.0]], "hook": "isso nunca foi dito no video"},
    )
    assert payload["accepted"] is False and "gancho" in payload["reason"]


# --- commit_clips ---------------------------------------------------------- #


def committed(ctx, workspace, **extra):
    return answer(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[
            {"segments": [[60.0, 88.0]], "topic": "primeiro", "score": 80},
            {"segments": [[150.0, 178.0]], "topic": "segundo", "score": 70},
        ],
        **extra,
    )


def test_commit_writes_the_clips_the_pipeline_reads(ctx, settings, workspace):
    payload = committed(ctx, workspace)
    assert [c["index"] for c in payload["clips"]] == [1, 2]
    assert payload["clips"][0]["clip_id"] == f"{workspace}/short/1"

    stored = ClipSet.model_validate_json(
        (settings.profile_dir(workspace, "short") / "clips.json").read_text(encoding="utf-8")
    )
    # Committed means approved: there is no human review in agent mode.
    assert [c.status for c in stored.clips] == [ClipStatus.APPROVED] * 2
    assert stored.clips[0].slug


def test_commit_marks_select_and_review_as_done(ctx, settings, workspace):
    committed(ctx, workspace)
    state = State(settings.state_db)
    assert state.is_done(workspace, "select", profile="short")
    assert state.is_done(workspace, "review", profile="short")


def test_commit_stores_the_overrides_next_to_the_clips(ctx, settings, workspace):
    payload = committed(ctx, workspace, overrides={"tail_roll_s": 2.0})
    assert payload["overrides"] == {"tail_roll_s": 2.0}
    stored = json.loads(
        (settings.profile_dir(workspace, "short") / OVERRIDES_NAME).read_text(encoding="utf-8")
    )
    assert stored == {"tail_roll_s": 2.0}


def test_commit_refuses_an_override_outside_the_whitelist(ctx, workspace):
    result = call(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[{"segments": [[60.0, 88.0]]}],
        overrides={"output_width": 720},
    )
    assert result.is_error and "output_width" in result.text


def test_commit_reports_what_the_boundary_rules_threw_out(ctx, workspace):
    payload = answer(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[
            {"segments": [[60.0, 88.0]], "topic": "bom"},
            {"segments": [[150.0, 155.0]], "topic": "curto demais"},
        ],
    )
    assert [c["topic"] for c in payload["clips"]] == ["bom"]
    assert payload["rejected"][0]["topic"] == "curto demais"


def test_commit_with_nothing_usable_is_an_error_with_the_reasons(ctx, workspace):
    result = call(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[{"segments": [[150.0, 155.0]], "topic": "curto"}],
    )
    assert result.is_error and "fronteira" in result.text


# --- find_candidates ------------------------------------------------------- #


def test_find_candidates_reuses_a_scan_instead_of_paying_for_it_again(ctx, settings, workspace):
    """The model reading the transcript is the expensive half; snapping is not."""
    out_dir = settings.profile_dir(workspace, "short")
    out_dir.mkdir(parents=True)
    (out_dir / "candidates.json").write_text(
        SelectionResponse(
            clips=[
                ClipCandidate(
                    segments=[TimeRange(start=60.0, end=88.0)], topic="guardado", score=90.0
                ),
                ClipCandidate(
                    segments=[TimeRange(start=150.0, end=154.0)], topic="curto", score=50.0
                ),
            ]
        ).model_dump_json(),
        encoding="utf-8",
    )
    payload = answer(ctx, "find_candidates", video_id=workspace, profile="short")
    assert payload["reused"] is True
    assert [c["topic"] for c in payload["accepted"]] == ["guardado"]
    assert payload["rejected"][0]["reason"]


# --- render / metadata / publish ------------------------------------------- #


def test_render_names_the_indices_that_exist(ctx, workspace):
    committed(ctx, workspace)
    result = call(ctx, "render_clip", video_id=workspace, profile="short", index=9)
    assert result.is_error and "existem: 1, 2" in result.text


def test_review_render_refuses_a_malformed_clip_id(ctx, workspace):
    result = call(ctx, "review_render", clip_id="short/1")
    assert result.is_error and "clip_id invalido" in result.text


def test_set_metadata_waits_for_the_render(ctx, workspace):
    committed(ctx, workspace)
    result = call(ctx, "set_metadata", clip_id=f"{workspace}/short/1", title="Um titulo")
    assert result.is_error and "ainda nao foi renderizado" in result.text


def test_publish_dry_run_says_what_is_missing_without_sending_anything(ctx, workspace):
    committed(ctx, workspace)
    payload = answer(ctx, "publish_clips", video_id=workspace, profiles=["short"], dry_run=True)
    assert payload["dry_run"] is True and payload["published"] == []
    assert all(item["reason"] == "sem render" for item in payload["skipped"])


def test_publish_refuses_to_ship_nothing(ctx, workspace):
    committed(ctx, workspace)
    result = call(ctx, "publish_clips", video_id=workspace, profiles=["short"])
    assert result.is_error and "nada para publicar" in result.text


# --- finish ---------------------------------------------------------------- #


def test_finish_writes_a_report_with_both_stories(ctx, settings, workspace):
    State(settings.state_db).start_run(ctx.run_id, workspace)
    State(settings.state_db).record_run_clip(
        ctx.run_id, workspace, "short", 1, renders=2, verdict="ok"
    )
    payload = answer(
        ctx,
        "finish_run",
        video_id=workspace,
        summary="Entreguei um short e descartei o resto por falta de fecho.",
        delivered=[f"{workspace}/short/1"],
        skipped=[{"what": f"{workspace}/short/2", "reason": "sem fecho"}],
    )
    report = (settings.video_dir(workspace) / "agent" / f"{ctx.run_id}.report.md").read_text(
        encoding="utf-8"
    )
    assert payload["delivered"] == 1
    assert "Entreguei um short" in report
    # What the runtime measured sits next to what the agent said it did.
    assert "| short 01 | 2 | ok |" in report
    assert State(settings.state_db).get_run(ctx.run_id).status == "done"


# --- the channel's render defaults (SPEC-agent 7) --------------------------- #


def _channel(**render_defaults):
    from clipper.agent.channel import Channel, ChannelProfile

    return Channel(
        profile=ChannelProfile(id="meucanal", profiles=["short"], render_defaults=render_defaults)
    )


def test_commit_applies_the_channel_defaults(settings, workspace):
    ctx = ToolContext(
        settings=settings,
        enabled_profiles=("short",),
        channel=_channel(short={"tail_roll_s": 2.0}),
    )
    payload = committed(ctx, workspace)
    assert payload["overrides"] == {"tail_roll_s": 2.0}


def test_what_the_agent_decides_for_this_video_wins_over_the_channel(settings, workspace):
    ctx = ToolContext(
        settings=settings,
        enabled_profiles=("short",),
        channel=_channel(short={"tail_roll_s": 2.0, "n_clips": 3}),
    )
    payload = committed(ctx, workspace, overrides={"tail_roll_s": 0.0})
    assert payload["overrides"] == {"tail_roll_s": 0.0, "n_clips": 3}


def test_a_channel_default_outside_the_whitelist_is_refused(settings, workspace):
    ctx = ToolContext(
        settings=settings,
        enabled_profiles=("short",),
        channel=_channel(short={"output_width": 720}),
    )
    result = call(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[{"segments": [[60.0, 88.0]]}],
    )
    assert result.is_error and "canal" in result.text


def test_snap_clip_is_about_boundaries_not_about_ranking(ctx, workspace):
    """A boundary test carries no score; refusing it for that answers nothing."""
    payload = answer(
        ctx,
        "snap_clip",
        video_id=workspace,
        profile="short",
        clip={"segments": [[60.0, 88.0]], "topic": "sem score"},
    )
    assert payload["accepted"] is True


def test_commit_still_refuses_a_clip_the_model_scored_low(ctx, settings, workspace):
    """There the score means something: it is the model's own read of the clip."""
    picky = settings.profile("short").model_copy(update={"min_score": 80.0})
    settings.profile_overrides["short"] = {"min_score": 80.0}
    del picky
    result = call(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[{"segments": [[60.0, 88.0]], "topic": "fraco", "score": 40}],
    )
    assert result.is_error and "score" in result.text


# --- the question in the bar above the frame -------------------------------- #


def test_the_candidate_carries_its_comment_question(ctx, settings, workspace):
    """A field the agent never sees is a field it never passes on."""
    out_dir = settings.profile_dir(workspace, "short")
    out_dir.mkdir(parents=True)
    (out_dir / "candidates.json").write_text(
        SelectionResponse(
            clips=[
                ClipCandidate(
                    segments=[TimeRange(start=60.0, end=88.0)],
                    topic="regras",
                    score=90.0,
                    comment_prompt="Na sua casa, +2 acumula?",
                )
            ]
        ).model_dump_json(),
        encoding="utf-8",
    )

    payload = answer(ctx, "find_candidates", video_id=workspace, profile="short")
    assert payload["accepted"][0]["comment_prompt"] == "Na sua casa, +2 acumula?"


def test_the_question_reaches_the_committed_clip(ctx, settings, workspace):
    answer(
        ctx,
        "commit_clips",
        video_id=workspace,
        profile="short",
        clips=[
            {
                "segments": [[60.0, 88.0]],
                "topic": "regras",
                "comment_prompt": "Na sua casa, +2 acumula?",
            }
        ],
    )
    stored = ClipSet.model_validate_json(
        (settings.profile_dir(workspace, "short") / "clips.json").read_text(encoding="utf-8")
    )
    assert stored.clips[0].comment_prompt == "Na sua casa, +2 acumula?"
