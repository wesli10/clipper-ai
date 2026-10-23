"""The baseline, rebuilt from what is already in the Drive (SPEC-agent 11.1).

Every delivered clip carries its own provenance - ``source_url`` and
``source_segments`` in the metadata - so the list of what the linear pipeline
chose can be read back from the delivery itself. Nothing has to have been kept.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from clipper.config import Settings
from clipper.evals.models import Baseline, BaselineClip, BaselineVideo
from clipper.utils.errors import InputError


def build(
    settings: Settings,
    *,
    channel: str | None = None,
    logger: logging.Logger | None = None,
) -> Baseline:
    """Read the delivered metadata and group it by source video."""
    from clipper.stages.publish import _fetch_metadata_rclone, collect, video_id_of

    temporary: Path | None = None
    if settings.deliver.mode == "rclone":
        root = _fetch_metadata_rclone(
            settings.deliver.rclone_remote, settings.deliver.root_folder, logger=logger
        )
        temporary = root
        source = f"{settings.deliver.rclone_remote}{settings.deliver.root_folder}"
    else:
        root = settings.deliver.drive_path / settings.deliver.root_folder
        source = str(root)
        if not root.exists():
            raise InputError(f"pasta de entrega nao encontrada: {root}")

    try:
        items = collect(root, channel=channel)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

    videos: dict[str, BaselineVideo] = {}
    for item in items:
        url = item.metadata.source_url.split("&t=")[0]
        video_id = video_id_of(url)
        if not video_id:
            continue
        video = videos.get(video_id)
        if video is None:
            video = BaselineVideo(video_id=video_id, url=url, title=item.video_folder)
            videos[video_id] = video
        video.clips.append(
            BaselineClip(
                profile=settings.deliver.profile_for(item.profile_folder),
                title=item.metadata.title,
                segments=[(round(s, 2), round(e, 2)) for s, e in item.metadata.source_segments],
                folder=item.clip_folder,
            )
        )

    ordered = sorted(videos.values(), key=lambda v: len(v.clips), reverse=True)
    return Baseline(
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        source=source,
        videos=ordered,
    )


def save(baseline: Baseline, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    return path


def load(path: Path) -> Baseline:
    if not path.exists():
        raise InputError(f"baseline nao encontrado: {path} (rode 'clipper eval baseline')")
    return Baseline.model_validate_json(path.read_text(encoding="utf-8"))
