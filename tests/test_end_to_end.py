"""The whole pipeline on a synthetic video, with a mocked LLM (SPEC 11.2).

Marked slow: it runs real ffmpeg. No network and no API are involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from clipper import pipeline
from clipper.config import load_settings
from clipper.llm.base import BaseLLMProvider
from clipper.models import Segment, SourceInfo, Transcript, Word
from clipper.state import StageStatus, State

pytestmark = pytest.mark.slow

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg e ffprobe sao necessarios",
)

VIDEO_ID = "e2e0000001"
SECONDS = 180


class ScriptedProvider(BaseLLMProvider):
    """Answers selection with fixed intervals and metadata with fixed copy."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system: str, user: str) -> str:
        self.calls += 1
        if "metadados de publicacao" in system:
            # One answer covers every clip of the profile (batched packaging).
            return json.dumps(
                {
                    "items": [
                        {
                            "index": index,
                            "title": f"O ponto principal do episodio {index}",
                            "hook_line": "Comece por aqui",
                            "description": "Uma explicacao curta do trecho.",
                            "chapter_titles": ["Abertura", "Fechamento"],
                            "hashtags": ["Produtividade", "#foco"],
                            "tags": ["produtividade", "foco"],
                        }
                        for index in (1, 2)
                    ]
                }
            )
        if "long" in user or "melhores momentos" in user:
            return json.dumps(
                {
                    "clips": [
                        {
                            "segments": [
                                {"start": 0.0, "end": 60.0},
                                {"start": 100.0, "end": 160.0},
                            ],
                            "hook": "frase numero 0",
                            "reason": "tema fechado",
                            "score": 88,
                            "self_contained": True,
                            "topic": "tema principal",
                            "chapters": [
                                {"offset_hint": "abertura"},
                                {"offset_hint": "fechamento"},
                            ],
                        }
                    ]
                }
            )
        return json.dumps(
            {
                "clips": [
                    {
                        "segments": [{"start": 10.0, "end": 50.0}],
                        "hook": "frase numero 5",
                        "reason": "gancho forte",
                        "score": 91,
                        "self_contained": True,
                        "topic": "o ponto principal",
                    }
                ]
            }
        )


def make_source(path, seconds: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=1920x1080:rate=30:duration={seconds}",
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
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def make_transcript(seconds: int) -> Transcript:
    segments = []
    for i in range(seconds // 5):
        start = i * 5.0
        text = f"frase numero {i} explicando um ponto do assunto."
        tokens = text.split()
        step = 4.6 / len(tokens)
        words = [
            Word(text=t, start=round(start + j * step, 3), end=round(start + (j + 1) * step, 3))
            for j, t in enumerate(tokens)
        ]
        segments.append(Segment(id=i, start=start, end=start + 4.6, text=text, words=words))
    return Transcript(video_id=VIDEO_ID, language="pt", duration=float(seconds), segments=segments)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    drive.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\n"
        "render:\n  preset: ultrafast\n  crf: 30\n"
        f"deliver:\n  drive_root: {drive}\n"
        "profiles:\n"
        "  default: [short, long]\n"
        "  short:\n    n_clips: 1\n    reframe: center\n"
        "  long:\n    n_clips: 1\n    duration_min: 60\n    duration_target: 100\n",
        encoding="utf-8",
    )
    settings = load_settings(config)

    video_dir = settings.video_dir(VIDEO_ID)
    video_dir.mkdir(parents=True)
    make_source(video_dir / "source.mp4", SECONDS)
    (video_dir / "transcript.json").write_text(
        make_transcript(SECONDS).model_dump_json(), encoding="utf-8"
    )
    info = SourceInfo(
        video_id=VIDEO_ID,
        title="Episodio de teste",
        channel="Canal de Teste",
        duration=float(SECONDS),
        url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        upload_date="20260115",
        width=1920,
        height=1080,
    )
    state = State(settings.state_db)
    state.upsert_video(info)
    state.set_stage(VIDEO_ID, "ingest", StageStatus.DONE)
    state.set_stage(VIDEO_ID, "transcribe", StageStatus.DONE)

    provider = ScriptedProvider()
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: provider)
    return settings, drive, provider


@needs_ffmpeg
def test_full_pipeline_delivers_both_profiles(workspace):
    settings, drive, _provider = workspace

    for profile_name in ("short", "long"):
        pipeline.select(settings, VIDEO_ID, profile_name)
        pipeline.review(settings, VIDEO_ID, profile_name, mode="auto")
        pipeline.render(settings, VIDEO_ID, profile_name)
        pipeline.package(settings, VIDEO_ID, profile_name)
    pipeline.deliver(settings, VIDEO_ID, ["short", "long"])

    target = drive / "Clipper" / "arquivo" / "canal-de-teste" / "2026-01-15_episodio-de-teste"
    assert target.is_dir()

    short_dirs = sorted((target / "shorts").iterdir())
    long_dirs = sorted((target / "longs").iterdir())
    assert len(short_dirs) == 1
    assert len(long_dirs) == 1

    for clip_dir in (*short_dirs, *long_dirs):
        for name in ("metadata.json", "metadata.md", "thumbnail.jpg", "subtitles.ass"):
            assert (clip_dir / name).exists(), f"{clip_dir.name} sem {name}"
        # The video is not in the archive at all (SPEC 8.1): it travels to the
        # publishing queue, and one copy of it is all the Drive should hold.
        assert list(clip_dir.glob("*.mp4")) == [], f"{clip_dir.name} levou video"
        assert not (clip_dir / "intermediates").exists()

    # The queue: one folder per day, the destination inside it, and the video
    # under its own title - YouTube pre-fills the title field from the filename.
    day = date.today() + timedelta(days=1)
    queue_day = drive / "Clipper" / "publicar" / day.isoformat()
    videos = sorted(path.name for path in queue_day.rglob("*.mp4"))
    assert len(videos) == 2, videos
    assert "clip.mp4" not in videos
    assert {path.parent.name for path in queue_day.rglob("*.mp4")} == {"so-tiktok"}

    plan = (queue_day / "PUBLICAR.md").read_text(encoding="utf-8")
    assert f"Publicar em {day.isoformat()}" in plan
    assert "**destino:** tiktok" in plan
    for name in videos:
        assert name.removesuffix(".mp4") in plan

    index = (target / "INDEX.md").read_text(encoding="utf-8")
    assert "## short" in index and "## long" in index
    assert "Episodio de teste" in index

    long_markdown = (long_dirs[0] / "metadata.md").read_text(encoding="utf-8")
    assert "00:00 Abertura" in long_markdown
    assert "Fechamento" in long_markdown
    assert "Fonte: https://www.youtube.com/watch?v=" in long_markdown


@needs_ffmpeg
def test_rerunning_the_pipeline_redoes_nothing(workspace):
    settings, _drive, provider = workspace

    pipeline.select(settings, VIDEO_ID, "short")
    pipeline.review(settings, VIDEO_ID, "short", mode="auto")
    pipeline.render(settings, VIDEO_ID, "short")
    pipeline.package(settings, VIDEO_ID, "short")
    calls_after_first = provider.calls

    clip = pipeline.load_clips(settings, VIDEO_ID, "short").clips[0]
    video_path = settings.profile_dir(VIDEO_ID, "short") / "clips" / clip.dir_name / "clip.mp4"
    stamp = video_path.stat().st_mtime_ns

    pipeline.select(settings, VIDEO_ID, "short")
    pipeline.review(settings, VIDEO_ID, "short", mode="auto")
    pipeline.render(settings, VIDEO_ID, "short")
    pipeline.package(settings, VIDEO_ID, "short")

    assert provider.calls == calls_after_first, "o LLM foi chamado de novo"
    assert video_path.stat().st_mtime_ns == stamp, "o video foi re-renderizado"


@needs_ffmpeg
def test_long_clip_joins_its_two_intervals(workspace):
    settings, _drive, _provider = workspace

    pipeline.select(settings, VIDEO_ID, "long")
    pipeline.review(settings, VIDEO_ID, "long", mode="auto")
    pipeline.render(settings, VIDEO_ID, "long")

    clip = pipeline.load_clips(settings, VIDEO_ID, "long").clips[0]
    assert len(clip.segments) == 2
    assert len(clip.offsets) == 2
    assert clip.offsets[0] == 0.0
    assert clip.offsets[1] > 50.0

    from clipper.video import ffmpeg

    path = settings.profile_dir(VIDEO_ID, "long") / "clips" / clip.dir_name / "clip.mp4"
    media = ffmpeg.probe(path)
    assert (media.width, media.height) == (1920, 1080)
    assert media.has_audio
    expected = sum(r.duration for r in clip.segments) - settings.render.transition_s
    assert media.duration == pytest.approx(expected, abs=0.3)
