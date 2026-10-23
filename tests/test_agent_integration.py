"""A whole video through the toolbox, with real ffmpeg (SPEC-agent 13, fase A).

Marked slow. This is the phase's acceptance test: driving the tools by hand, in
order, must produce what `clipper run` produces - and the review must measure the
result for real, ebur128 and signalstats included.

Run with: uv run pytest -m slow
"""

from __future__ import annotations

import json
import shutil

import pytest

from clipper.agent import tools
from clipper.agent.tools.base import ToolContext
from clipper.config import load_settings
from clipper.state import State
from tests.test_render_integration import make_source, make_transcript

pytestmark = pytest.mark.slow

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg e ffprobe sao necessarios",
)

VIDEO_ID = "itest_agent"


@pytest.fixture
def ctx(tmp_path):
    config = tmp_path / "config.yaml"
    # `reframe: blur` is what config.yaml ships today: the whole frame is kept and
    # face tracking is off (SPEC-agent 2.1). The profile decides it, never the agent.
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\n"
        "render:\n  preset: ultrafast\n  crf: 28\n"
        "profiles:\n  short:\n    reframe: blur\n",
        encoding="utf-8",
    )
    settings = load_settings(config)

    video_dir = settings.video_dir(VIDEO_ID)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=40)
    transcript = make_transcript(VIDEO_ID, 40)
    (video_dir / "transcript.json").write_text(transcript.model_dump_json(), encoding="utf-8")
    (video_dir / "source.info.json").write_text(
        json.dumps(
            {
                "id": VIDEO_ID,
                "title": "Video sintetico",
                "uploader": "Canal",
                "duration": 40,
                "webpage_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
                "width": 1280,
                "height": 720,
            }
        ),
        encoding="utf-8",
    )
    State(settings.state_db).start_run("20260910-120000-itest", VIDEO_ID)
    return ToolContext(
        settings=settings,
        run_id="20260910-120000-itest",
        enabled_profiles=("short",),
    )


def answer(ctx, name, **payload):
    result = tools.get(name).call(ctx, payload)
    assert not result.is_error, result.text
    return json.loads(result.text)


@needs_ffmpeg
def test_the_toolbox_takes_a_video_from_outline_to_a_reviewed_render(ctx):
    outline = answer(ctx, "get_outline", video_id=VIDEO_ID)
    assert outline["blocks"], "o mapa do video nao pode sair vazio"

    looked = answer(ctx, "inspect_range", video_id=VIDEO_ID, start=2.0, end=26.0)
    assert looked["text"]

    snapped = answer(
        ctx,
        "snap_clip",
        video_id=VIDEO_ID,
        profile="short",
        clip={"segments": [[2.0, 26.0]]},
    )
    assert snapped["accepted"] is True

    committed = answer(
        ctx,
        "commit_clips",
        video_id=VIDEO_ID,
        profile="short",
        clips=[{"segments": snapped["segments"], "topic": "teste", "score": 90}],
        overrides={"subtitle_style": "karaoke"},
    )
    clip_id = committed["clips"][0]["clip_id"]

    rendered = answer(ctx, "render_clip", video_id=VIDEO_ID, profile="short", index=1)
    assert (rendered["width"], rendered["height"]) == (1080, 1920)
    assert rendered["reused"] is False

    # Rendering again reuses the file instead of encoding it twice - the same
    # idempotency the linear pipeline has (SPEC 3.1).
    again = answer(ctx, "render_clip", video_id=VIDEO_ID, profile="short", index=1)
    assert again["reused"] is True

    review = answer(ctx, "review_render", clip_id=clip_id)
    names = {check["check"] for check in review["checks"]}
    assert {"duration", "resolution", "audio_track", "loudness", "caption_sync"} <= names
    # The face checks stay dormant: nothing is cropped (SPEC-agent 2.1).
    assert "face_in_frame" not in names
    assert any("reframe" in note for note in review["skipped"])

    failures = [check for check in review["checks"] if check["status"] == "fail"]
    assert not failures, failures
    assert review["verdict"] in ("ok", "warn")

    written = answer(
        ctx,
        "set_metadata",
        clip_id=clip_id,
        title="um titulo no tom do canal",
        description="o que o corte entrega",
        hashtags=["#teste"],
    )
    assert written["title"] == "um titulo no tom do canal"

    plan = answer(ctx, "publish_clips", video_id=VIDEO_ID, profiles=["short"], dry_run=True)
    assert [item["clip_id"] for item in plan["published"]] == [clip_id]
    assert plan["skipped"] == []

    done = answer(
        ctx,
        "finish_run",
        video_id=VIDEO_ID,
        summary="Um short entregue a partir do video sintetico.",
        delivered=[clip_id],
    )
    assert done["delivered"] == 1


@needs_ffmpeg
def test_the_review_measures_loudness_and_the_first_frame_for_real(ctx):
    answer(
        ctx,
        "commit_clips",
        video_id=VIDEO_ID,
        profile="short",
        clips=[{"segments": [[2.0, 26.0]], "topic": "teste", "score": 90}],
    )
    answer(ctx, "render_clip", video_id=VIDEO_ID, profile="short", index=1)
    review = answer(ctx, "review_render", clip_id=f"{VIDEO_ID}/short/1")

    loudness = next(c for c in review["checks"] if c["check"] == "loudness")
    assert "LUFS" in loudness["value"] and loudness["status"] == "ok"

    frame = next(c for c in review["checks"] if c["check"] == "first_frame")
    assert frame["status"] == "ok" and "luma" in frame["value"]
