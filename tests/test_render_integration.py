"""Real ffmpeg renders - SPEC 11.1 (marked slow, skipped by default).

Run with: uv run pytest -m slow
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from clipper.config import load_settings
from clipper.models import (
    ChapterHint,
    Clip,
    Segment,
    TimeRange,
    Transcript,
    Word,
)
from clipper.stages.render import render_clip
from clipper.video import ffmpeg

pytestmark = pytest.mark.slow

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(
    not (HAS_FFMPEG and HAS_FFPROBE), reason="ffmpeg e ffprobe sao necessarios"
)


def make_source(path: str, *, width: int, height: int, seconds: int) -> None:
    """A synthetic 16:9 clip with a tone, so no network or CC0 asset is needed."""
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            path,
        ],
        check=True,
        capture_output=True,
    )


def make_transcript(video_id: str, seconds: int) -> Transcript:
    segments = []
    for i in range(seconds // 2):
        start = i * 2.0
        text = f"frase numero {i} do video de teste."
        tokens = text.split()
        step = 1.8 / len(tokens)
        words = [
            Word(text=t, start=round(start + j * step, 3), end=round(start + (j + 1) * step, 3))
            for j, t in enumerate(tokens)
        ]
        segments.append(Segment(id=i, start=start, end=start + 1.8, text=text, words=words))
    return Transcript(video_id=video_id, language="pt", duration=float(seconds), segments=segments)


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\nrender:\n  preset: ultrafast\n  crf: 28\n",
        encoding="utf-8",
    )
    return load_settings(config)


@needs_ffmpeg
def test_short_renders_vertical_with_burned_karaoke(workspace):
    video_id = "itest_short"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=30)

    transcript = make_transcript(video_id, 30)
    clip = Clip(
        index=1,
        slug="teste",
        profile="short",
        segments=[TimeRange(start=2.0, end=26.0)],
        topic="teste",
    )
    profile = workspace.profile("short").model_copy(update={"reframe": "center"})

    result = render_clip(workspace, video_id, clip, profile, transcript)

    assert (result.width, result.height) == (1080, 1920)
    assert result.duration == pytest.approx(24.0, abs=0.3)
    assert result.subtitles_path is not None and result.subtitles_path.exists()
    assert "\\k" in result.subtitles_path.read_text(encoding="utf-8")
    assert result.thumbnail_path.exists()
    assert ffmpeg.probe(result.video_path).has_audio


@needs_ffmpeg
def test_long_never_upscales_a_small_source(workspace):
    video_id = "itest_long_small"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=40)

    transcript = make_transcript(video_id, 40)
    clip = Clip(index=1, slug="teste", profile="long", segments=[TimeRange(start=0.0, end=36.0)])
    profile = workspace.profile("long").model_copy(
        update={"duration_min": 10.0, "duration_target": 30.0}
    )

    result = render_clip(workspace, video_id, clip, profile, transcript)
    assert (result.width, result.height) == (1280, 720)


@needs_ffmpeg
def test_long_joins_two_intervals_with_a_crossfade(workspace):
    video_id = "itest_long_multi"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1920, height=1080, seconds=60)

    transcript = make_transcript(video_id, 60)
    clip = Clip(
        index=1,
        slug="teste",
        profile="long",
        segments=[TimeRange(start=0.0, end=20.0), TimeRange(start=40.0, end=58.0)],
        chapters=[ChapterHint(offset_hint="a"), ChapterHint(offset_hint="b")],
    )
    profile = workspace.profile("long").model_copy(
        update={"duration_min": 10.0, "duration_target": 30.0}
    )

    result = render_clip(workspace, video_id, clip, profile, transcript)

    assert (result.width, result.height) == (1920, 1080)
    # 20 s + 18 s minus one 0.3 s crossfade
    assert result.duration == pytest.approx(37.7, abs=0.3)
    assert result.offsets == [0.0, pytest.approx(19.7)]

    ass = result.subtitles_path.read_text(encoding="utf-8")
    assert "\\k" not in ass  # classic style
    # a caption from the second interval must be shifted into the joined timeline
    assert "Dialogue:" in ass


@needs_ffmpeg
def test_quiz_holds_the_frame_and_burns_the_card(workspace):
    """The rendered file is the interval plus the hold, minus one crossfade."""
    video_id = "itest_quiz"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=30)

    transcript = make_transcript(video_id, 30)
    clip = Clip(
        index=1,
        slug="quiz",
        profile="quiz",
        segments=[TimeRange(start=2.0, end=26.0)],
        reveal_at=14.0,
        card_text="Chuta o numero.",
        topic="teste",
    )
    profile = workspace.profile("quiz")

    result = render_clip(workspace, video_id, clip, profile, transcript)

    hold = profile.card_seconds
    expected = 24.0 + hold - workspace.render.transition_s
    assert result.duration == pytest.approx(expected, abs=0.3)
    assert (result.width, result.height) == (1080, 1920)
    # One interval in, one chapter out: the split is a render detail.
    assert result.offsets == [0.0]

    assert result.subtitles_path is not None
    ass = result.subtitles_path.read_text(encoding="utf-8")
    assert "CHUTA O NUMERO." in ass
    assert ",Dim," in ass


@needs_ffmpeg
def test_an_outro_is_joined_after_the_clip_without_the_filter_chain(workspace, tmp_path):
    """A card made at the wrong size, frame rate and with no audio still joins."""
    video_id = "itest_outro"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=30)

    card = tmp_path / "outro.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "smptebars=s=720x1280:d=4:r=24",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(card),
        ],
        check=True,
        capture_output=True,
    )

    transcript = make_transcript(video_id, 30)
    clip = Clip(index=1, slug="outro", profile="short", segments=[TimeRange(start=2.0, end=22.0)])
    profile = workspace.profile("short").model_copy(update={"reframe": "center", "outro": card})

    result = render_clip(workspace, video_id, clip, profile, transcript)

    expected = 20.0 + 4.0 - workspace.render.transition_s
    assert result.duration == pytest.approx(expected, abs=0.3)
    assert (result.width, result.height) == (1080, 1920)


@needs_ffmpeg
def test_a_missing_outro_costs_the_card_not_the_clip(workspace, tmp_path):
    """The end card is cosmetic; the clip is not."""
    video_id = "itest_outro_missing"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=20)

    transcript = make_transcript(video_id, 20)
    clip = Clip(index=1, slug="x", profile="short", segments=[TimeRange(start=2.0, end=14.0)])
    profile = workspace.profile("short").model_copy(
        update={"reframe": "center", "outro": tmp_path / "nao-existe.mp4"}
    )

    result = render_clip(workspace, video_id, clip, profile, transcript)

    assert result.video_path.exists()
    assert result.degraded
    assert result.duration == pytest.approx(12.0, abs=0.3)


@needs_ffmpeg
def test_the_runway_lengthens_the_file_but_not_the_captions(workspace):
    """The chosen ending stays put; only the rendered tail is longer."""
    video_id = "itest_runway"
    video_dir = workspace.video_dir(video_id)
    video_dir.mkdir(parents=True)
    make_source(str(video_dir / "source.mp4"), width=1280, height=720, seconds=30)

    transcript = make_transcript(video_id, 30)
    clip = Clip(index=1, slug="x", profile="short", segments=[TimeRange(start=2.0, end=14.0)])
    profile = workspace.profile("short").model_copy(
        update={"reframe": "center", "outro": None, "tail_roll_s": 3.0}
    )

    result = render_clip(workspace, video_id, clip, profile, transcript)

    assert result.duration == pytest.approx(15.0, abs=0.3)
    assert result.subtitles_path is not None
    events = [
        line
        for line in result.subtitles_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:") and ",Default," in line
    ]
    # Every caption ends by 12s - the clip's own length - not by 15s.
    ends = [line.split(",")[2] for line in events]
    assert max(ends) < "0:00:12.10"
