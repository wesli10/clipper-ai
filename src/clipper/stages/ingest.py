"""Stage 1a - download the source video and extract audio (SPEC 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipper.config import Settings
from clipper.models import SourceInfo
from clipper.sources.base import VideoSource
from clipper.sources.youtube import YouTubeSource
from clipper.utils.errors import ExternalError, InputError
from clipper.utils.logging import stage_logger
from clipper.utils.text import format_timestamp
from clipper.video import ffmpeg

#: Every known source, in priority order (SPEC 5.2).
SOURCES: tuple[VideoSource, ...] = (YouTubeSource(),)


@dataclass(frozen=True, slots=True)
class IngestResult:
    info: SourceInfo
    video_dir: Path
    source_path: Path
    audio_path: Path
    reused: bool


def pick_source(url: str) -> VideoSource:
    """Choose the source that handles ``url`` (SPEC 5.2)."""
    for source in SOURCES:
        if source.matches(url):
            return source
    raise InputError(
        f"nenhuma fonte de video reconhece esta URL: {url}\n"
        "No MVP apenas links do YouTube sao suportados."
    )


def ingest(settings: Settings, url: str, *, force: bool = False) -> IngestResult:
    """Download ``url`` into the workspace and produce ``audio.wav``."""
    source = pick_source(url)
    video_id = source.video_id(url)

    video_dir = settings.video_dir(video_id)
    source_path = video_dir / "source.mp4"
    audio_path = video_dir / "audio.wav"
    logger = stage_logger(settings.logs_dir(video_id), "ingest")

    if not force and source_path.exists() and audio_path.exists():
        info = _cached_info(video_dir, source, url)
        logger.info("ingest reaproveitado de %s", video_dir)
        return IngestResult(info, video_dir, source_path, audio_path, reused=True)

    probed = source.probe(url)
    _check_duration(probed, settings.max_source_duration)

    if force:
        # yt-dlp skips a file it has already written, so `--force` has to clear the
        # way first - otherwise a re-ingest silently keeps the old download, even
        # when the format selector would now choose differently.
        for stale in _existing_downloads(video_dir):
            logger.info("removendo download anterior: %s", stale)
            stale.unlink()

    logger.info("baixando %s (%s)", probed.title, format_timestamp(probed.duration))

    info = source.fetch(url, video_dir, logger=logger)
    downloaded = _find_downloaded(video_dir)
    if downloaded != source_path:
        downloaded.replace(source_path)

    media = ffmpeg.probe(source_path, logger=logger)
    if not media.has_video:
        raise ExternalError(f"o arquivo baixado nao tem faixa de video: {source_path}")
    info = info.model_copy(
        update={
            "width": media.width or info.width,
            "height": media.height or info.height,
            "duration": media.duration or info.duration,
        }
    )
    _check_duration(info, settings.max_source_duration)

    ffmpeg.extract_audio(source_path, audio_path, logger=logger)
    logger.info("ingest concluido: %s (%dx%d)", source_path, info.width, info.height)
    return IngestResult(info, video_dir, source_path, audio_path, reused=False)


def _check_duration(info: SourceInfo, limit: float) -> None:
    if info.duration and info.duration > limit:
        raise InputError(
            f"video de {format_timestamp(info.duration)} excede o limite de "
            f"{format_timestamp(limit)} (config: max_source_duration)"
        )


def _existing_downloads(video_dir: Path) -> list[Path]:
    """Media files a previous ingest left behind (never the info json)."""
    return [
        p
        for p in video_dir.glob("source.*")
        if p.is_file() and p.suffixes[-2:] != [".info", ".json"] and p.suffix != ".json"
    ]


def _find_downloaded(video_dir: Path) -> Path:
    candidates = sorted(
        (p for p in video_dir.glob("source.*") if p.suffix not in (".json", ".part")),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise ExternalError(f"yt-dlp nao deixou nenhum arquivo em {video_dir}")
    return candidates[0]


def _cached_info(video_dir: Path, source: VideoSource, url: str) -> SourceInfo:
    """Rebuild :class:`SourceInfo` from the cached ``source.info.json``."""
    import json

    info_path = video_dir / "source.info.json"
    if info_path.exists():
        raw = json.loads(info_path.read_text(encoding="utf-8"))
        video_id = str(raw.get("id") or source.video_id(url))
        return SourceInfo(
            video_id=video_id,
            title=str(raw.get("title") or ""),
            channel=str(raw.get("uploader") or raw.get("channel") or ""),
            duration=float(raw.get("duration") or 0.0),
            url=str(raw.get("webpage_url") or url),
            language=raw.get("language"),
            width=int(raw.get("width") or 0),
            height=int(raw.get("height") or 0),
            upload_date=raw.get("upload_date"),
        )
    return source.probe(url)
