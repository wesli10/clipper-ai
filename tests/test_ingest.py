from __future__ import annotations

import json

import pytest

from clipper.config import load_settings
from clipper.models import SourceInfo
from clipper.stages import ingest as ingest_stage
from clipper.utils.errors import InputError


class FakeSource:
    """Stands in for YouTubeSource; never touches the network."""

    name = "fake"

    def __init__(self, info: SourceInfo) -> None:
        self.info = info
        self.fetched = 0

    def matches(self, url: str) -> bool:
        return True

    def video_id(self, url: str) -> str:
        return self.info.video_id

    def probe(self, url: str) -> SourceInfo:
        return self.info

    def fetch(self, url, dest, *, logger=None):
        self.fetched += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "source.mp4").write_bytes(b"fake")
        (dest / "source.info.json").write_text(
            json.dumps(
                {
                    "id": self.info.video_id,
                    "title": self.info.title,
                    "uploader": self.info.channel,
                    "duration": self.info.duration,
                    "webpage_url": self.info.url,
                    "width": self.info.width,
                    "height": self.info.height,
                }
            ),
            encoding="utf-8",
        )
        return self.info


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"workspace: {tmp_path / 'ws'}\nmax_source_duration: 600\n", encoding="utf-8")
    return load_settings(path)


@pytest.fixture
def info():
    return SourceInfo(
        video_id="vid0000001",
        title="Um video",
        channel="Canal",
        duration=300.0,
        url="https://www.youtube.com/watch?v=vid0000001",
        width=1920,
        height=1080,
    )


def test_rejects_video_longer_than_limit(settings, info, monkeypatch):
    long_info = info.model_copy(update={"duration": 7200.0})
    fake = FakeSource(long_info)
    monkeypatch.setattr(ingest_stage, "SOURCES", (fake,))
    with pytest.raises(InputError, match="excede o limite"):
        ingest_stage.ingest(settings, "https://example/x")
    assert fake.fetched == 0


def test_reuses_existing_download(settings, info, monkeypatch):
    fake = FakeSource(info)
    monkeypatch.setattr(ingest_stage, "SOURCES", (fake,))
    video_dir = settings.video_dir(info.video_id)
    video_dir.mkdir(parents=True)
    (video_dir / "source.mp4").write_bytes(b"x")
    (video_dir / "audio.wav").write_bytes(b"x")

    result = ingest_stage.ingest(settings, "https://example/x")
    assert result.reused is True
    assert fake.fetched == 0
    assert result.info.title == "Um video"


def test_force_clears_the_previous_download(settings, info, monkeypatch):
    """yt-dlp skips a file it already wrote, so --force has to delete it first."""
    fake = FakeSource(info)
    monkeypatch.setattr(ingest_stage, "SOURCES", (fake,))
    video_dir = settings.video_dir(info.video_id)
    video_dir.mkdir(parents=True)
    stale = video_dir / "source.mp4"
    stale.write_bytes(b"conteudo antigo em av1")
    (video_dir / "audio.wav").write_bytes(b"x")
    (video_dir / "source.info.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        ingest_stage.ffmpeg,
        "probe",
        lambda *a, **k: ingest_stage.ffmpeg.MediaInfo(
            width=1920, height=1080, duration=300.0, fps=30.0, has_audio=True, has_video=True
        ),
    )
    monkeypatch.setattr(ingest_stage.ffmpeg, "extract_audio", lambda *a, **k: None)

    result = ingest_stage.ingest(settings, "https://example/x", force=True)

    assert result.reused is False
    assert fake.fetched == 1
    assert stale.read_bytes() == b"fake"  # what FakeSource writes


def test_force_keeps_the_info_json_untouched(settings, info, monkeypatch):
    fake = FakeSource(info)
    monkeypatch.setattr(ingest_stage, "SOURCES", (fake,))
    video_dir = settings.video_dir(info.video_id)
    video_dir.mkdir(parents=True)
    (video_dir / "source.mp4").write_bytes(b"velho")
    (video_dir / "audio.wav").write_bytes(b"x")

    monkeypatch.setattr(
        ingest_stage.ffmpeg,
        "probe",
        lambda *a, **k: ingest_stage.ffmpeg.MediaInfo(
            width=1920, height=1080, duration=300.0, fps=30.0, has_audio=True, has_video=True
        ),
    )
    monkeypatch.setattr(ingest_stage.ffmpeg, "extract_audio", lambda *a, **k: None)
    ingest_stage.ingest(settings, "https://example/x", force=True)

    assert (video_dir / "source.info.json").exists()
