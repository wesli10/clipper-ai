"""Stage orchestration and idempotency (SPEC 3.1).

Ingest and transcription run once per video; everything after that runs once per
(video, profile). A stage is skipped when ``state.db`` says it is done *and* its
output files are on disk, unless ``force`` is set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING

from clipper.config import ReviewMode, Settings
from clipper.models import (
    Clip,
    ClipMetadata,
    ClipSet,
    ClipStatus,
    SelectionResponse,
    SourceInfo,
    Transcript,
)
from clipper.state import PROFILE_STAGES, QueuedClip, StageStatus, State
from clipper.utils.console import console, error, info, success, warn
from clipper.utils.errors import ClipperError
from clipper.utils.logging import stage_logger
from clipper.utils.text import format_timestamp

if TYPE_CHECKING:
    from clipper.stages.publish import PublishItem


def ingest(settings: Settings, url: str, *, force: bool = False) -> str:
    """Download the video and extract audio; returns the video id."""
    from clipper.stages.ingest import ingest as run_ingest
    from clipper.stages.ingest import pick_source

    state = State(settings.state_db)
    # Marked before the download starts, not after: `clipper monitor` has no other
    # way to tell a long yt-dlp run apart from a queue that is not moving.
    state.set_stage(pick_source(url).video_id(url), "ingest", StageStatus.RUNNING)
    try:
        result = run_ingest(settings, url, force=force)
    except Exception as exc:
        state.set_stage(
            pick_source(url).video_id(url), "ingest", StageStatus.FAILED, detail=str(exc)[:200]
        )
        raise
    state.upsert_video(result.info)
    state.set_stage(
        result.info.video_id,
        "ingest",
        StageStatus.DONE,
        detail=f"{result.info.width}x{result.info.height}",
    )

    if result.reused:
        info(f"[dim]ingest ja feito para {result.info.video_id}, reaproveitando[/dim]")
    else:
        success(
            f"{result.info.title} ({format_timestamp(result.info.duration)}, "
            f"{result.info.width}x{result.info.height}) em {result.video_dir}"
        )
    return result.info.video_id


def transcribe(
    settings: Settings, video_id: str, *, language: str | None = None, force: bool = False
) -> Transcript:
    """Transcribe the extracted audio with word-level timestamps."""
    from clipper.stages.transcribe import transcribe as run_transcribe

    state = State(settings.state_db)
    video_dir = settings.video_dir(video_id)
    if not video_dir.exists():
        raise ClipperError(
            f"video '{video_id}' nao esta no workspace; rode 'clipper ingest <url>' antes"
        )

    transcript_path = video_dir / "transcript.json"
    already = transcript_path.exists() and state.is_done(video_id, "transcribe")
    if already and not force:
        info(f"[dim]transcricao ja feita para {video_id}, reaproveitando[/dim]")
        return Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))

    state.set_stage(video_id, "transcribe", StageStatus.RUNNING)
    try:
        transcript = run_transcribe(
            settings, video_id, video_dir / "audio.wav", language=language, force=force
        )
    except Exception as exc:
        state.set_stage(video_id, "transcribe", StageStatus.FAILED, detail=str(exc)[:200])
        raise
    state.set_stage(
        video_id,
        "transcribe",
        StageStatus.DONE,
        detail=f"{transcript.language}, {len(transcript.segments)} segmentos",
    )
    success(
        f"transcricao: {len(transcript.segments)} segmentos, idioma {transcript.language}, "
        f"{format_timestamp(transcript.duration)}"
    )
    return transcript


def load_transcript(settings: Settings, video_id: str) -> Transcript:
    """Read ``transcript.json``, with an actionable error when it is missing."""
    path = settings.video_dir(video_id) / "transcript.json"
    if not path.exists():
        raise ClipperError(
            f"transcricao nao encontrada para '{video_id}'; rode 'clipper transcribe {video_id}'"
        )
    return Transcript.model_validate_json(path.read_text(encoding="utf-8"))


def select(
    settings: Settings,
    video_id: str,
    profile_name: str,
    *,
    n_clips: int | None = None,
    provider: str | None = None,
    wait: bool = True,
    force: bool = False,
) -> ClipSet:
    """Ask the LLM for candidates and snap them to real speech boundaries."""
    from clipper.llm import build_provider
    from clipper.stages.select import select_clips

    profile = settings.profile(profile_name)
    state = State(settings.state_db)
    out_dir = settings.profile_dir(video_id, profile_name)
    clips_path = out_dir / "clips.json"

    if (
        not force
        and clips_path.exists()
        and state.is_done(video_id, "select", profile=profile_name)
    ):
        info(f"[dim]select ja feito para {video_id}/{profile_name}, reaproveitando[/dim]")
        return ClipSet.model_validate_json(clips_path.read_text(encoding="utf-8"))

    transcript = load_transcript(settings, video_id)
    logger = stage_logger(settings.logs_dir(video_id), "select", profile_name)
    llm = build_provider(
        settings, provider, stage="select", logger=logger, request_dir=out_dir, wait=wait
    )

    state.set_stage(video_id, "select", StageStatus.RUNNING, profile=profile_name)
    try:
        result = select_clips(
            transcript,
            profile,
            llm,
            n_clips=n_clips,
            pad_before=settings.render.pad_before_s,
            pad_after=settings.render.pad_after_s,
            logger=logger,
        )
    except Exception as exc:
        state.set_stage(
            video_id, "select", StageStatus.FAILED, profile=profile_name, detail=str(exc)[:200]
        )
        raise

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "candidates.json").write_text(
        SelectionResponse(clips=result.raw).model_dump_json(indent=2), encoding="utf-8"
    )
    clip_set = ClipSet(video_id=video_id, profile=profile_name, clips=result.clips)
    clips_path.write_text(clip_set.model_dump_json(indent=2), encoding="utf-8")
    state.set_stage(
        video_id,
        "select",
        StageStatus.DONE,
        profile=profile_name,
        detail=f"{len(result.clips)} cortes",
    )

    success(f"{profile_name}: {len(result.clips)} cortes selecionados")
    for clip in result.clips:
        ranges = ", ".join(
            f"{format_timestamp(r.start)}-{format_timestamp(r.end)}" for r in clip.segments
        )
        console.print(
            f"  [bold]{clip.index:02d}[/bold] {clip.topic or clip.hook} "
            f"[dim]({ranges}, {clip.duration:.0f}s, score {clip.score:.0f})[/dim]"
        )
    for item in result.rejected:
        label = item.candidate.topic or item.candidate.hook or "sem titulo"
        console.print(f"  [dim]x  {label}: {item.reason}[/dim]")
    return clip_set


def load_clips(settings: Settings, video_id: str, profile_name: str) -> ClipSet:
    path = settings.profile_dir(video_id, profile_name) / "clips.json"
    if not path.exists():
        raise ClipperError(
            f"nenhum corte selecionado para {video_id}/{profile_name}; "
            f"rode 'clipper select {video_id} --profile {profile_name}'"
        )
    return ClipSet.model_validate_json(path.read_text(encoding="utf-8"))


def save_clips(settings: Settings, clip_set: ClipSet) -> None:
    path = settings.profile_dir(clip_set.video_id, clip_set.profile) / "clips.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clip_set.model_dump_json(indent=2), encoding="utf-8")


def renderable(clip_set: ClipSet, only_clip: int | None = None) -> list[Clip]:
    """Approved clips (or everything still pending, when review was skipped)."""
    clips = [c for c in clip_set.clips if c.status is not ClipStatus.REJECTED]
    if only_clip is not None:
        clips = [c for c in clips if c.index == only_clip]
        if not clips:
            raise ClipperError(f"nao existe corte de indice {only_clip} nesse perfil")
    return clips


def review(
    settings: Settings,
    video_id: str,
    profile_name: str,
    *,
    mode: ReviewMode = "ask",
    approve: list[int] | None = None,
    reject: list[int] | None = None,
    force: bool = False,
) -> ClipSet:
    """Human review of the selected clips (SPEC 7.1).

    ``ask`` prompts in the terminal, ``auto`` keeps the best ones by score and
    ``web`` hands the decision to the monitor page and waits for it.
    """
    from clipper.stages.review import review as run_review
    from clipper.stages.review import web_review

    profile = settings.profile(profile_name)
    state = State(settings.state_db)
    clip_set = load_clips(settings, video_id, profile_name)

    decided = bool(approve or reject)
    if not force and not decided and state.is_done(video_id, "review", profile=profile_name):
        approved = sum(1 for c in clip_set.clips if c.status is ClipStatus.APPROVED)
        info(f"[dim]revisao ja feita para {video_id}/{profile_name} ({approved} aprovados)[/dim]")
        return clip_set

    transcript = load_transcript(settings, video_id)
    source = settings.video_dir(video_id) / "source.mp4"

    reviewed: ClipSet | None
    if mode == "web" and not decided:
        try:
            about = source_info(settings, video_id)
        except ClipperError:
            about = None
        # The stage is marked running before the wait: otherwise the monitor -
        # the very place the decision is being asked for - shows a stalled queue.
        state.set_stage(
            video_id,
            "review",
            StageStatus.RUNNING,
            profile=profile_name,
            detail="aguardando sua revisao no monitor",
        )
        try:
            reviewed = web_review(
                clip_set,
                profile,
                transcript,
                settings.profile_dir(video_id, profile_name),
                video_title=about.title if about else "",
                channel=about.channel if about else "",
                monitor_url=settings.monitor.url,
                timeout_s=settings.review.timeout_s,
            )
        except Exception as exc:
            state.set_stage(
                video_id, "review", StageStatus.FAILED, profile=profile_name, detail=str(exc)[:200]
            )
            raise
    else:
        reviewed = run_review(
            clip_set,
            profile,
            transcript,
            source,
            auto=mode == "auto",
            approve=approve,
            reject=reject,
        )
    if reviewed is None:
        warn("revisao cancelada; nada foi salvo")
        return clip_set

    save_clips(settings, reviewed)
    approved = sum(1 for c in reviewed.clips if c.status is ClipStatus.APPROVED)
    state.set_stage(
        video_id,
        "review",
        StageStatus.DONE,
        profile=profile_name,
        detail=f"{approved} aprovados",
    )
    success(f"{profile_name}: {approved} cortes aprovados")
    return reviewed


def run(
    settings: Settings,
    url: str,
    profiles: list[str],
    *,
    review_mode: ReviewMode = "ask",
    language: str | None = None,
    provider: str | None = None,
    force: bool = False,
) -> str:
    """Full pipeline: ingest and transcribe once, then everything per profile.

    Every mode but ``auto`` stops at each profile's review, as SPEC 9 requires -
    in the terminal (``ask``) or in the monitor page (``web``).
    """
    video_id = ingest(settings, url, force=force)
    transcribe(settings, video_id, language=language, force=force)

    delivered: list[str] = []
    # Upload is as slow as rendering and needs none of the CPU, so a profile is
    # shipped in the background while the next one renders.
    with ThreadPoolExecutor(max_workers=1) as uploader:
        pending: list[Future[None]] = []
        for profile_name in profiles:
            console.rule(f"perfil {profile_name}")
            select(settings, video_id, profile_name, provider=provider, force=force)
            reviewed = review(settings, video_id, profile_name, mode=review_mode, force=force)
            if not any(c.status is ClipStatus.APPROVED for c in reviewed.clips):
                warn(f"{profile_name}: nenhum corte aprovado, pulando render")
                continue
            render(settings, video_id, profile_name, force=force)
            package(settings, video_id, profile_name, provider=provider, force=force)
            delivered.append(profile_name)
            pending.append(
                uploader.submit(deliver, settings, video_id, [profile_name], write_index=False)
            )

        for future in pending:
            future.result()

    if delivered:
        deliver_index(settings, video_id, delivered)
    else:
        warn("nada foi entregue: nenhum perfil chegou ao fim")
    return video_id


def render(
    settings: Settings,
    video_id: str,
    profile_name: str,
    *,
    only_clip: int | None = None,
    force: bool = False,
) -> ClipSet:
    """Render every approved clip of a profile."""
    from clipper.stages.render import render_clip

    profile = settings.profile(profile_name)
    state = State(settings.state_db)
    clip_set = load_clips(settings, video_id, profile_name)
    transcript = load_transcript(settings, video_id)
    logger = stage_logger(settings.logs_dir(video_id), "render", profile_name)

    clips = renderable(clip_set, only_clip)
    if not clips:
        warn(f"{profile_name}: nenhum corte aprovado para renderizar")
        return clip_set

    state.set_stage(video_id, "render", StageStatus.RUNNING, profile=profile_name)
    rendered = 0
    workers = max(1, min(settings.render.workers, len(clips)))
    try:
        # Clips are independent: each reads the source and writes its own
        # directory. Rendering them one at a time left most of the CPU idle.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    render_clip,
                    settings,
                    video_id,
                    clip,
                    profile,
                    transcript,
                    force=force,
                    logger=logger,
                ): clip
                for clip in clips
            }
            for future in as_completed(futures):
                clip = futures[future]
                result = future.result()
                clip.offsets = result.offsets
                rendered += 1
                note = " [yellow](modo degradado)[/yellow]" if result.degraded else ""
                success(
                    f"{profile_name} {clip.index:02d} {clip.topic or clip.slug}: "
                    f"{result.width}x{result.height}, {result.duration:.1f}s{note}"
                )
    except Exception as exc:
        state.set_stage(
            video_id, "render", StageStatus.FAILED, profile=profile_name, detail=str(exc)[:200]
        )
        raise

    save_clips(settings, clip_set)
    if only_clip is None:
        prune_stale_clips(settings, video_id, profile_name, clip_set)
    state.set_stage(
        video_id,
        "render",
        StageStatus.DONE,
        profile=profile_name,
        detail=f"{rendered} cortes",
    )
    return clip_set


def prune_stale_clips(
    settings: Settings, video_id: str, profile_name: str, clip_set: ClipSet
) -> None:
    """Drop clip directories left over from an earlier selection.

    A new selection produces different slugs, so the previous run's directories
    would sit next to the current ones for ever - and `deliver` would have no way
    to tell them apart.
    """
    root = settings.profile_dir(video_id, profile_name) / "clips"
    if not root.is_dir():
        return
    current = {clip.dir_name for clip in clip_set.clips}
    for child in root.iterdir():
        if child.is_dir() and child.name not in current:
            shutil.rmtree(child)
            info(f"[dim]removido corte obsoleto: {profile_name}/{child.name}[/dim]")


def source_info(settings: Settings, video_id: str) -> SourceInfo:
    """Video metadata from the state db, falling back to ``source.info.json``."""
    state = State(settings.state_db)
    info = state.get_video(video_id)
    if info is not None:
        return info
    path = settings.video_dir(video_id) / "source.info.json"
    if not path.exists():
        raise ClipperError(f"metadados do video '{video_id}' nao encontrados")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SourceInfo(
        video_id=video_id,
        title=str(raw.get("title") or ""),
        channel=str(raw.get("uploader") or raw.get("channel") or ""),
        duration=float(raw.get("duration") or 0.0),
        url=str(raw.get("webpage_url") or ""),
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        upload_date=raw.get("upload_date"),
    )


def package(
    settings: Settings,
    video_id: str,
    profile_name: str,
    *,
    provider: str | None = None,
    force: bool = False,
) -> list[ClipMetadata]:
    """Write ``metadata.json`` and ``metadata.md`` next to every rendered clip."""
    from clipper.llm import build_provider
    from clipper.stages.package import build_metadata_batch, to_markdown
    from clipper.stages.render import clip_dir_for

    profile = settings.profile(profile_name)
    state = State(settings.state_db)
    clip_set = load_clips(settings, video_id, profile_name)
    transcript = load_transcript(settings, video_id)
    source = source_info(settings, video_id)
    logger = stage_logger(settings.logs_dir(video_id), "package", profile_name)
    llm = build_provider(
        settings,
        provider,
        stage="package",
        logger=logger,
        request_dir=settings.profile_dir(video_id, profile_name),
    )

    state.set_stage(video_id, "package", StageStatus.RUNNING, profile=profile_name)
    written: list[ClipMetadata] = []
    pending: list[Clip] = []
    dirs: dict[int, Path] = {}

    for clip in renderable(clip_set):
        clip_dir = clip_dir_for(settings, video_id, profile, clip)
        if not (clip_dir / "clip.mp4").exists():
            warn(f"{profile_name} {clip.index:02d}: sem render ainda, pulando metadados")
            continue
        dirs[clip.index] = clip_dir
        metadata_path = clip_dir / "metadata.json"
        if metadata_path.exists() and not force:
            written.append(
                ClipMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
            )
            continue
        pending.append(clip)

    try:
        # One call for the whole profile: N times less per-call overhead, and the
        # model can tell the clips apart instead of writing the same title N times.
        produced = build_metadata_batch(pending, profile, source, transcript, llm, logger=logger)
        for clip in pending:
            metadata = produced.get(clip.index)
            if metadata is None:
                warn(f"{profile_name} {clip.index:02d}: o modelo nao devolveu metadados")
                continue
            clip_dir = dirs[clip.index]
            (clip_dir / "metadata.json").write_text(
                metadata.model_dump_json(indent=2), encoding="utf-8"
            )
            (clip_dir / "metadata.md").write_text(to_markdown(metadata), encoding="utf-8")
            written.append(metadata)
            success(f"{profile_name} {clip.index:02d}: {metadata.title}")
    except Exception as exc:
        state.set_stage(
            video_id, "package", StageStatus.FAILED, profile=profile_name, detail=str(exc)[:200]
        )
        raise

    state.set_stage(
        video_id, "package", StageStatus.DONE, profile=profile_name, detail=f"{len(written)} cortes"
    )
    return written


@dataclass(frozen=True, slots=True)
class ReadyClip:
    """A clip that is rendered, has metadata, and is about to be shipped."""

    profile: str
    clip_index: int
    clip_dir: Path
    metadata: ClipMetadata


def deliver(
    settings: Settings,
    video_id: str,
    profiles: list[str],
    *,
    write_index: bool = True,
    force: bool = False,
) -> None:
    """Ship every packaged clip: the video to the queue, the rest to the archive.

    The two halves of the delivery (SPEC 8.1) are shipped in one go - the archive
    keeps metadata, thumbnail and INDEX.md per video, and the queue gets the
    videos under the day each one is meant to go out.

    ``write_index`` is off when profiles are shipped one at a time: the index
    covers the whole video, so a per-profile write would overwrite the previous
    profile's section.
    """
    from clipper.stages.deliver import archive_dir, build_index, deliver_folder, deliver_rclone
    from clipper.stages.render import clip_dir_for

    del force
    state = State(settings.state_db)
    source = source_info(settings, video_id)
    logger = stage_logger(settings.logs_dir(video_id), "deliver")
    for profile_name in profiles:
        state.set_stage(video_id, "deliver", StageStatus.RUNNING, profile=profile_name)

    sources: list[tuple[str, Path]] = []
    ready: list[ReadyClip] = []
    per_profile: dict[str, list[tuple[ClipSet, ClipMetadata]]] = {}
    for profile_name in profiles:
        profile = settings.profile(profile_name)
        folder = settings.deliver.folder_for(profile_name)
        clip_set = load_clips(settings, video_id, profile_name)
        entries: list[tuple[ClipSet, ClipMetadata]] = []
        for clip in renderable(clip_set):
            clip_dir = clip_dir_for(settings, video_id, profile, clip)
            metadata_path = clip_dir / "metadata.json"
            if not (clip_dir / "clip.mp4").exists() or not metadata_path.exists():
                warn(f"{profile_name} {clip.index:02d}: sem render ou metadados, pulando")
                continue
            metadata = ClipMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
            sources.append((folder, clip_dir))
            ready.append(ReadyClip(profile_name, clip.index, clip_dir, metadata))
            entries.append((clip_set, metadata))
        per_profile[profile_name] = entries

    if not sources:
        raise ClipperError("nada para entregar: nenhum corte renderizado e empacotado")

    index = build_index(source, per_profile) if write_index else None
    relative = archive_dir(settings, source)
    try:
        if settings.deliver.mode == "rclone":
            archive_where = deliver_rclone(
                sources,
                settings.deliver.rclone_remote,
                relative,
                index=index,
                chunk_size=settings.deliver.rclone_chunk_size,
                transfers=settings.deliver.rclone_transfers,
                workers=settings.deliver.workers,
                logger=logger,
            )
        else:
            archive_where = str(
                deliver_folder(
                    sources, settings.deliver.drive_path, relative, index=index, logger=logger
                )
            )
        queued = queue_clips(settings, state, source, ready)
        queue_where = ship_queue(settings, state, queued, logger=logger)
    except Exception as exc:
        for profile_name in profiles:
            state.set_stage(
                video_id,
                "deliver",
                StageStatus.FAILED,
                profile=profile_name,
                detail=str(exc)[:200],
            )
        raise

    for profile_name in profiles:
        state.set_stage(
            video_id, "deliver", StageStatus.DONE, profile=profile_name, detail=queue_where
        )
    success(
        f"{len(sources)} cortes entregues: videos em {queue_where}, metadados em {archive_where}"
    )
    for line in queue_summary(queued):
        info(f"  {line}")


def queue_clips(
    settings: Settings,
    state: State,
    source: SourceInfo,
    ready: list[ReadyClip],
) -> list[tuple[QueuedClip, Path]]:
    """Give every new clip a day and a destination, keeping the ones it already has.

    The whole queue is read first: the date depends on how full the coming days
    already are, and a clip that is already queued keeps the day it has - a corte
    sitting in Thursday's folder must not move because another video was
    processed in between.
    """
    from clipper.stages.deliver import archive_dir, channel_slug, delivered_name
    from clipper.stages.queue import Calendar, destination_folder, render_source, unique_filename

    publish = settings.publish
    existing = state.queue()
    calendar = Calendar.of(existing, publish.daily_profiles)
    taken: dict[tuple[date, str], set[str]] = {}
    for entry in existing:
        key = (entry.publish_date, destination_folder(entry.destinations))
        taken.setdefault(key, set()).add(entry.filename)

    start = date.today() + timedelta(days=1)
    channel = channel_slug(source)
    archive = archive_dir(settings, source)
    queued: list[tuple[QueuedClip, Path]] = []
    for item in ready:
        already = state.queued_clip(source.video_id, item.profile, item.clip_index)
        if already is not None:
            queued.append((already, render_source(item.clip_dir)))
            continue
        destinations = publish.destinations_for(channel, item.profile)
        if not destinations:
            warn(
                f"{item.profile} {item.clip_index:02d}: sem destino para o canal "
                f"'{channel}' (publish.destinations), vai para {destination_folder(())}"
            )
        is_daily = item.profile in publish.daily_profiles
        when = calendar.next_free(
            is_daily=is_daily,
            start=start,
            per_day=publish.per_day,
            long_every_days=publish.long_every_days,
        )
        folder = destination_folder(destinations)
        filename = unique_filename(
            delivered_name(item.metadata.title), taken.get((when, folder), ())
        )
        entry = state.queue_clip(
            source.video_id,
            item.profile,
            item.clip_index,
            publish_date=when,
            destinations=destinations,
            filename=filename,
            channel=channel,
            archive_path=(
                f"{archive.as_posix()}/{settings.deliver.folder_for(item.profile)}/"
                f"{item.clip_dir.name}"
            ),
            metadata=item.metadata,
        )
        calendar.take(when, is_daily=is_daily)
        taken.setdefault((when, folder), set()).add(filename)
        queued.append((entry, render_source(item.clip_dir)))
    return queued


def ship_queue(
    settings: Settings,
    state: State,
    queued: list[tuple[QueuedClip, Path]],
    *,
    days: Sequence[date] | None = None,
    logger: Logger | None = None,
) -> str:
    """Lay the days out locally by hard link and send them to the Drive.

    Only the days these clips land on are touched, and the day's plan is rebuilt
    from the whole queue: a day that already holds another video's clips keeps
    them and gets a PUBLICAR.md that lists all of them.
    """
    from clipper.stages.deliver import queue_dir, upload_queue_folder, upload_queue_rclone
    from clipper.stages.publish import day_plan
    from clipper.stages.queue import stage_days

    touched = sorted({entry.publish_date for entry, _ in queued} | set(days or ()))
    if not touched:
        return ""
    whole_queue = state.queue()
    plans = {
        day: day_plan(settings, day, [c for c in whole_queue if c.publish_date == day])
        for day in touched
    }
    staging = settings.workspace.expanduser() / "queue-staging"
    shutil.rmtree(staging, ignore_errors=True)
    relative = queue_dir(settings)
    try:
        staged = stage_days(staging, queued, plans, plan_filename=settings.publish.plan_filename)
        directories = [day.directory for day in staged]
        if settings.deliver.mode == "rclone":
            return upload_queue_rclone(
                directories,
                settings.deliver.rclone_remote,
                relative,
                chunk_size=settings.deliver.rclone_chunk_size,
                workers=settings.deliver.workers,
                logger=logger,
            )
        return str(
            upload_queue_folder(directories, settings.deliver.drive_path, relative, logger=logger)
        )
    finally:
        # Hard links to every render in the queue: left behind, `clipper clean`
        # would free nothing.
        shutil.rmtree(staging, ignore_errors=True)


def queue_summary(queued: list[tuple[QueuedClip, Path]]) -> list[str]:
    """One line per day, for the end of a run."""
    from clipper.stages.queue import destination_folder

    by_day: dict[date, list[QueuedClip]] = {}
    for entry, _ in queued:
        by_day.setdefault(entry.publish_date, []).append(entry)
    lines = []
    for day in sorted(by_day):
        folders = ", ".join(
            sorted({destination_folder(entry.destinations) for entry in by_day[day]})
        )
        lines.append(f"{day.isoformat()}: {len(by_day[day])} corte(s) em {folders}")
    return lines


def deliver_index(settings: Settings, video_id: str, profiles: list[str]) -> None:
    """Write the video's INDEX.md once every profile has been shipped."""
    from clipper.stages.deliver import archive_dir, build_index, deliver_folder, deliver_rclone
    from clipper.stages.render import clip_dir_for

    source = source_info(settings, video_id)
    logger = stage_logger(settings.logs_dir(video_id), "deliver")
    per_profile: dict[str, list[tuple[ClipSet, ClipMetadata]]] = {}
    for profile_name in profiles:
        profile = settings.profile(profile_name)
        clip_set = load_clips(settings, video_id, profile_name)
        entries: list[tuple[ClipSet, ClipMetadata]] = []
        for clip in renderable(clip_set):
            path = clip_dir_for(settings, video_id, profile, clip) / "metadata.json"
            if path.exists():
                entries.append(
                    (clip_set, ClipMetadata.model_validate_json(path.read_text(encoding="utf-8")))
                )
        per_profile[profile_name] = entries

    index = build_index(source, per_profile)
    relative = archive_dir(settings, source)
    if settings.deliver.mode == "rclone":
        deliver_rclone([], settings.deliver.rclone_remote, relative, index=index, logger=logger)
    else:
        deliver_folder([], settings.deliver.drive_path, relative, index=index, logger=logger)


def clean_video(settings: Settings, video_id: str, *, keep_source: bool = False) -> None:
    """Remove workspace artefacts for ``video_id`` (SPEC 9)."""
    video_dir = settings.video_dir(video_id)
    if not video_dir.exists():
        raise ClipperError(f"video '{video_id}' nao esta no workspace ({video_dir})")

    state = State(settings.state_db)
    if not keep_source:
        shutil.rmtree(video_dir)
        state.delete_video(video_id)
        success(f"workspace de {video_id} removido")
        return

    keep = {"source.mp4", "source.info.json", "audio.wav", "transcript.json", "faces.json"}
    for child in video_dir.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for record in state.stages_for(video_id):
        if record.stage in PROFILE_STAGES:
            state.clear_stage(video_id, record.stage, profile=record.profile)
    success(f"workspace de {video_id} limpo (fonte e transcricao preservadas)")


def directory_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def delivered_videos(settings: Settings) -> list[str]:
    """Videos whose clips have already reached the Drive for every profile tried."""
    state = State(settings.state_db)
    done: list[str] = []
    for video in state.list_videos():
        records = state.stages_for(video.video_id)
        profiles = {r.profile for r in records if r.stage == "render"}
        if not profiles:
            continue
        if all(state.is_done(video.video_id, "deliver", profile=p) for p in profiles):
            done.append(video.video_id)
    return done


def disk_usage(settings: Settings) -> None:
    """What the workspace is holding, and how much of it is already delivered."""
    from rich.table import Table

    root = settings.workspace.expanduser()
    if not root.exists():
        info("workspace vazio")
        return

    shipped = set(delivered_videos(settings))
    state = State(settings.state_db)
    table = Table(title=f"workspace: {root}")
    table.add_column("video", style="bold")
    table.add_column("titulo")
    table.add_column("fonte", justify="right")
    table.add_column("cortes", justify="right")
    table.add_column("total", justify="right")
    table.add_column("entregue")

    total = 0
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        size = directory_size(child)
        total += size
        source = sum(
            f.stat().st_size for f in (child / "source.mp4", child / "audio.wav") if f.exists()
        )
        clips = directory_size(child / "profiles") if (child / "profiles").is_dir() else 0
        meta = state.get_video(child.name)
        table.add_row(
            child.name,
            (meta.title[:34] if meta else ""),
            human(source),
            human(clips),
            human(size),
            "[green]sim[/green]" if child.name in shipped else "[yellow]nao[/yellow]",
        )
    console.print(table)
    console.print(f"total: [bold]{human(total)}[/bold]")
    reclaimable = sum(directory_size(root / v) for v in shipped if (root / v).is_dir())
    if reclaimable:
        console.print(
            f"ja entregue e removivel: [bold]{human(reclaimable)}[/bold] "
            "([bold]clipper clean --delivered[/bold])"
        )


def clean_delivered(settings: Settings, *, keep_source: bool = False) -> None:
    """Remove every video already delivered."""
    shipped = delivered_videos(settings)
    if not shipped:
        info("nenhum video entregue ainda; nada a limpar")
        return
    root = settings.workspace.expanduser()
    freed = 0
    for video_id in shipped:
        directory = root / video_id
        if not directory.is_dir():
            continue
        before = directory_size(directory)
        clean_video(settings, video_id, keep_source=keep_source)
        after = directory_size(directory) if directory.is_dir() else 0
        freed += before - after
    success(f"{human(freed)} liberados de {len(shipped)} video(s)")


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One queued video and what became of it."""

    url: str
    video_id: str
    status: str
    detail: str = ""


def read_queue(path: Path) -> list[str]:
    """URLs from a queue file, ignoring blanks and ``#`` comments.

    Read fresh before each item, so the file can be appended to while a batch is
    running - that is the point of leaving a queue behind.
    """
    if not path.exists():
        raise ClipperError(f"arquivo de fila nao encontrado: {path}")
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and line not in urls:
            urls.append(line)
    return urls


def free_space_gb(path: Path) -> float:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free / 1024**3


def batch(
    settings: Settings,
    queue: Path | list[str],
    profiles: list[str],
    *,
    provider: str | None = None,
    review_mode: ReviewMode = "auto",
    clean: bool = True,
    min_free_gb: float = 15.0,
    stop_on_error: bool = False,
) -> list[BatchItem]:
    """Process a queue of videos unattended.

    ``auto`` by default: there is nobody at the terminal. With ``web`` the queue
    pauses on each profile until the monitor page answers, which is the way to
    review a whole queue without watching it run. Each video is cleaned from the
    workspace once it has been delivered, so a long queue costs the disk of one
    video rather than all of them.
    """
    from clipper.stages.ingest import pick_source

    results: list[BatchItem] = []
    processed: set[str] = set()

    while True:
        urls = read_queue(queue) if isinstance(queue, Path) else list(queue)
        remaining = [u for u in urls if u not in processed]
        if not remaining:
            break

        url = remaining[0]
        processed.add(url)

        try:
            video_id = pick_source(url).video_id(url)
        except ClipperError as exc:
            results.append(BatchItem(url, "", "invalido", str(exc)))
            error(f"{url}: {exc}")
            continue

        if video_id in set(delivered_videos(settings)):
            results.append(BatchItem(url, video_id, "ja entregue"))
            info(f"[dim]{video_id} ja entregue, pulando[/dim]")
            continue

        free = free_space_gb(settings.workspace.expanduser())
        if free < min_free_gb:
            detail = f"{free:.1f} GB livres, minimo {min_free_gb:.0f} GB"
            results.append(BatchItem(url, video_id, "sem espaco", detail))
            error(f"fila interrompida: {detail}")
            break

        console.rule(f"[bold]{video_id}[/bold]  ({len(results) + 1}/{len(urls)})")
        try:
            run(settings, url, profiles, review_mode=review_mode, provider=provider)
        except Exception as exc:  # one bad video must not stop the queue
            results.append(BatchItem(url, video_id, "falhou", str(exc)[:200]))
            error(f"{video_id}: {exc}")
            if stop_on_error:
                break
            continue

        detail = ""
        if clean:
            before = directory_size(settings.video_dir(video_id))
            clean_video(settings, video_id)
            detail = f"{human(before)} liberados"
        results.append(BatchItem(url, video_id, "ok", detail))

    _report_batch(results)
    return results


def _report_batch(results: list[BatchItem]) -> None:
    from rich.table import Table

    if not results:
        info("fila vazia")
        return
    table = Table(title="fila")
    table.add_column("video", style="bold")
    table.add_column("status")
    table.add_column("detalhe")
    colors = {"ok": "green", "ja entregue": "dim", "falhou": "red", "sem espaco": "red"}
    for item in results:
        color = colors.get(item.status, "yellow")
        table.add_row(
            item.video_id or item.url[:24],
            f"[{color}]{item.status}[/{color}]",
            item.detail,
        )
    console.print(table)
    ok = sum(1 for r in results if r.status == "ok")
    console.print(f"{ok}/{len(results)} processados com sucesso")


def publish_plan(
    settings: Settings,
    *,
    channel: str | None = None,
    start: date | None = None,
    thumbnails: bool = True,
    output: Path | None = None,
    all_days: bool = False,
) -> Path:
    """Write the whole queue as one file, in publishing order.

    Read from the queue in ``state.db``, not from the Drive: the dates and the
    destinations were decided when each clip was delivered, and a plan that
    recomputed them would disagree with the folders the clips are sitting in.
    """
    from clipper.stages.publish import queue_items, render_plan

    state = State(settings.state_db)
    since = None if all_days else (start or date.today())
    clips = state.queue(since=since, channel=channel)
    if not clips:
        raise ClipperError(
            "a fila esta vazia. Entregue um video com `clipper deliver`, ou adote o "
            "que ja esta no Drive com `clipper queue-migrate`."
        )
    items = queue_items(settings, clips, prefix=settings.publish.queue_folder)
    plan = render_plan(
        items,
        channel=channel,
        thumbnails=thumbnails,
        shorts_folder=settings.deliver.folder_for("short"),
    )
    destination = output or Path("PUBLICAR.md")
    destination.write_text(plan, encoding="utf-8")

    daily = sum(1 for clip in clips if clip.profile in settings.publish.daily_profiles)
    success(f"{len(clips)} cortes ({daily} shorts, {len(clips) - daily} longs) em {destination}")
    return destination


def queue_replan(settings: Settings, *, all_days: bool = False) -> list[date]:
    """Rewrite the PUBLICAR.md of every day in the queue, in the Drive.

    The plan of a day is a snapshot of the queue, so anything that changes the
    queue after the clips shipped - a clip taken out, another video landing on
    the same day - leaves it out of date. No video is uploaded again: only the
    plans travel.
    """
    state = State(settings.state_db)
    clips = state.queue(since=None if all_days else date.today())
    if not clips:
        info("a fila esta vazia")
        return []
    days = sorted({clip.publish_date for clip in clips})
    where = ship_queue(
        settings,
        state,
        [],
        days=days,
        logger=stage_logger(settings.workspace.expanduser(), "publish"),
    )
    success(f"{len(days)} planos reescritos em {where}")
    return days


def queue_remove(settings: Settings, terms: Sequence[str]) -> list[QueuedClip]:
    """Take clips out of the queue, matched by video id or by part of the title.

    The row is what says a clip is waiting in the Drive, so removing it is the
    only way to tell the queue that a clip is no longer to be published - a video
    that was dropped, or one published outside the plan. The file itself is not
    touched: the paths are printed so you can decide what to do with them.
    """
    state = State(settings.state_db)
    queue_root = f"{settings.deliver.root_folder}/{settings.publish.queue_folder}"
    removed: list[QueuedClip] = []
    for clip in state.queue():
        matched = any(
            term.casefold() == clip.video_id.casefold()
            or term.casefold() in clip.metadata.title.casefold()
            or term.casefold() in clip.filename.casefold()
            for term in terms
            if term
        )
        if matched:
            removed.append(clip)
    if not removed:
        warn("nenhum corte da fila casa com o que voce pediu")
        return removed

    from clipper.stages.queue import queue_path

    for clip in removed:
        state.unqueue(clip.video_id, profile=clip.profile, clip_index=clip.clip_index)
    for clip in removed:
        info(f"  {queue_root}/{queue_path(clip.publish_date, clip.destinations, clip.filename)}")
    success(f"{len(removed)} cortes fora da fila. Os arquivos acima continuam no Drive.")
    return removed


def queue_view(
    settings: Settings,
    *,
    channel: str | None = None,
    all_days: bool = False,
) -> None:
    """What is waiting in the Drive, day by day."""
    from rich.table import Table

    from clipper.stages.publish import WEEKDAYS_PT
    from clipper.stages.queue import destination_folder

    state = State(settings.state_db)
    clips = state.queue(since=None if all_days else date.today(), channel=channel)
    if not clips:
        info("a fila esta vazia")
        return

    queue_root = f"{settings.deliver.root_folder}/{settings.publish.queue_folder}"
    table = Table(title=f"Fila em {queue_root}")
    table.add_column("dia")
    table.add_column("destino")
    table.add_column("formato")
    table.add_column("titulo", overflow="fold")
    previous: date | None = None
    for clip in clips:
        day = ""
        if clip.publish_date != previous:
            day = f"{clip.publish_date.isoformat()} ({WEEKDAYS_PT[clip.publish_date.weekday()]})"
            previous = clip.publish_date
        table.add_row(
            day,
            destination_folder(clip.destinations),
            settings.deliver.folder_for(clip.profile),
            clip.metadata.title,
        )
    console.print(table)
    days = sorted({clip.publish_date for clip in clips})
    console.print(
        f"{len(clips)} cortes em {len(days)} dias, "
        f"de {days[0].isoformat()} a {days[-1].isoformat()}"
    )


@dataclass(frozen=True, slots=True)
class QueueMove:
    """One clip about to be adopted into the queue: where it is, where it goes."""

    old_path: str
    new_path: str
    video_id: str
    profile: str
    clip_index: int
    channel: str
    archive_path: str
    destinations: tuple[str, ...]
    filename: str
    publish_date: date
    metadata: ClipMetadata


def queue_migrate(
    settings: Settings,
    *,
    channel: str | None = None,
    start: date | None = None,
    skip: int = 0,
    skip_videos: Sequence[str] = (),
    apply: bool = False,
) -> list[QueueMove]:
    """Adopt a delivery made before the queue existed (SPEC 8.1).

    Reads the delivered metadata, dates every clip that is not in the queue yet,
    and moves its video into ``publicar/<dia>/<destino>/``. Inside the Drive the
    move is server-side, so nothing is uploaded again. What is left - metadata,
    thumbnails, INDEX.md - goes under ``arquivo/``, where new deliveries write it.

    Prints the plan and changes nothing unless ``apply``. Two ways to leave out
    what was already published, whose files then stay in the archive: ``skip``
    drops the first N clips of the plan, and ``skip_videos`` drops every clip of
    a video, matched by its id or by part of its folder name.
    """
    from clipper.stages.deliver import (
        delivered_name,
        move_file_folder,
        move_file_rclone,
        move_tree_folder,
        move_tree_rclone,
    )
    from clipper.stages.publish import (
        _fetch_metadata_rclone,
        collect,
        filenames_by_folder,
        list_videos_local,
        list_videos_rclone,
        publish_order,
        video_id_of,
    )
    from clipper.stages.queue import (
        Calendar,
        day_folder,
        destination_folder,
        unique_filename,
    )

    state = State(settings.state_db)
    logger = stage_logger(settings.workspace.expanduser(), "publish")
    publish = settings.publish
    root_folder = settings.deliver.root_folder
    is_rclone = settings.deliver.mode == "rclone"

    temporary: Path | None = None
    if is_rclone:
        root = _fetch_metadata_rclone(settings.deliver.rclone_remote, root_folder, logger=logger)
        temporary = root
        videos = list_videos_rclone(settings.deliver.rclone_remote, root_folder, logger=logger)
    else:
        root = settings.deliver.drive_path / root_folder
        if not root.exists():
            raise ClipperError(f"pasta de entrega nao encontrada: {root}")
        videos = list_videos_local(root)
    try:
        items = collect(root, channel=channel, filenames=filenames_by_folder(videos))
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

    ordered = publish_order(items, shorts_folder=settings.deliver.folder_for("short"))
    calendar = Calendar.of(state.queue(), publish.daily_profiles)
    taken: dict[tuple[date, str], set[str]] = {}
    for entry in state.queue():
        taken.setdefault((entry.publish_date, destination_folder(entry.destinations)), set()).add(
            entry.filename
        )

    when_from = start or (date.today() + timedelta(days=1))
    used: set[str] = set()
    moves: list[QueueMove] = []
    for position, item in enumerate(ordered, start=1):
        video_id = video_id_of(item.metadata.source_url)
        profile_name = settings.deliver.profile_for(item.profile_folder)
        clip_index = _clip_index_of(item.clip_folder)
        if not video_id or clip_index is None:
            warn(f"{item.folder}: nao deu para identificar video ou corte, pulando")
            continue
        if state.queued_clip(video_id, profile_name, clip_index) is not None:
            continue
        if _matches_any(item.video_folder, video_id, skip_videos):
            info(f"[dim]{item.video_folder}: vídeo já publicado, fica no arquivo[/dim]")
            continue
        if position <= skip:
            info(
                f"[dim]{position:02d}. {item.metadata.title} — ja publicado, fica no arquivo[/dim]"
            )
            continue
        source_path = _locate_video(item, videos, used, delivered_name(item.metadata.title))
        if source_path is None:
            warn(f"{item.folder}: video nao encontrado no Drive, pulando")
            continue
        used.add(source_path)

        destinations = publish.destinations_for(item.channel, profile_name)
        is_daily = profile_name in publish.daily_profiles
        day = calendar.next_free(
            is_daily=is_daily,
            start=when_from,
            per_day=publish.per_day,
            long_every_days=publish.long_every_days,
        )
        folder = destination_folder(destinations)
        filename = unique_filename(
            delivered_name(item.metadata.title), taken.get((day, folder), ())
        )
        calendar.take(day, is_daily=is_daily)
        taken.setdefault((day, folder), set()).add(filename)
        moves.append(
            QueueMove(
                old_path=f"{root_folder}/{source_path}",
                new_path=(
                    f"{root_folder}/{publish.queue_folder}/{day_folder(day)}/{folder}/{filename}"
                ),
                video_id=video_id,
                profile=profile_name,
                clip_index=clip_index,
                channel=item.channel,
                archive_path=f"{root_folder}/{publish.archive_folder}/{item.folder}",
                destinations=tuple(destinations),
                filename=filename,
                publish_date=day,
                metadata=item.metadata,
            )
        )

    _print_migration(settings, moves)
    if not moves:
        return moves
    if not apply:
        info("nada foi movido. Rode de novo com --aplicar para valer.")
        return moves

    for move in moves:
        if is_rclone:
            move_file_rclone(
                settings.deliver.rclone_remote, move.old_path, move.new_path, logger=logger
            )
        else:
            move_file_folder(
                settings.deliver.drive_path, move.old_path, move.new_path, logger=logger
            )
        # Written right after the move, one clip at a time: a crash leaves a
        # queue that says exactly what is already in the Drive.
        state.queue_clip(
            move.video_id,
            move.profile,
            move.clip_index,
            publish_date=move.publish_date,
            destinations=move.destinations,
            filename=move.filename,
            channel=move.channel,
            archive_path=move.archive_path,
            metadata=move.metadata,
        )

    # Whatever is left of the old per-video tree is history, and history lives in
    # the archive. Done after the videos moved out, so the paths above still hold.
    #
    # Only the folders that hold delivered clips move. A delivery root can have
    # other things in it - a benchmark left over, a folder someone made by hand -
    # and moving what we know nothing about would be the wrong kind of tidy.
    reserved = {publish.queue_folder, publish.archive_folder}
    channels = {item.channel for item in items if item.channel}
    for name in sorted(channels - reserved):
        old = f"{root_folder}/{name}"
        new = f"{root_folder}/{publish.archive_folder}/{name}"
        if is_rclone:
            move_tree_rclone(settings.deliver.rclone_remote, old, new, logger=logger)
        else:
            move_tree_folder(settings.deliver.drive_path, old, new, logger=logger)
        info(f"arquivo: {old} -> {new}")

    ship_queue(
        settings,
        state,
        [],
        days=sorted({move.publish_date for move in moves}),
        logger=logger,
    )
    success(f"{len(moves)} cortes na fila de {root_folder}/{publish.queue_folder}")
    return moves


def _matches_any(video_folder: str, video_id: str, wanted: Sequence[str]) -> bool:
    """Whether this clip's video is one the caller named.

    By id or by any part of the folder name, which is what a person reading the
    plan has in front of them.
    """
    return any(
        term.casefold() == video_id.casefold() or term.casefold() in video_folder.casefold()
        for term in wanted
        if term
    )


def _clip_index_of(clip_folder: str) -> int | None:
    """``03_veredito-do-relojoeiro`` -> 3."""
    head = clip_folder.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def _locate_video(
    item: PublishItem,
    videos: Sequence[str],
    used: set[str],
    expected: str,
) -> str | None:
    """The clip's video file in the delivery listing.

    Normally it sits in the clip's own folder. It can also have been pulled up
    into the profile folder by hand, which is exactly the navigation this change
    is undoing, so that case is matched by the name the clip would carry.
    """
    folder = item.folder
    if not folder:
        return None
    inside = [path for path in videos if path.startswith(f"{folder}/") and path not in used]
    if inside:
        return sorted(inside)[0]
    parent = folder.rpartition("/")[0]
    candidate = f"{parent}/{expected}"
    if candidate in videos and candidate not in used:
        return candidate
    return None


def _print_migration(settings: Settings, moves: list[QueueMove]) -> None:
    from rich.table import Table

    from clipper.stages.publish import WEEKDAYS_PT
    from clipper.stages.queue import destination_folder

    if not moves:
        info("nada novo para colocar na fila")
        return
    table = Table(title="O que vai para a fila")
    table.add_column("dia")
    table.add_column("destino")
    table.add_column("formato")
    table.add_column("titulo", overflow="fold")
    previous: date | None = None
    # Shown in publishing order, which is not the order the clips were read in.
    for move in sorted(moves, key=lambda m: (m.publish_date, m.profile, m.filename)):
        day = ""
        if move.publish_date != previous:
            day = f"{move.publish_date.isoformat()} ({WEEKDAYS_PT[move.publish_date.weekday()]})"
            previous = move.publish_date
        table.add_row(
            day,
            destination_folder(move.destinations),
            settings.deliver.folder_for(move.profile),
            move.metadata.title,
        )
    console.print(table)
    by_destination: dict[str, int] = {}
    for move in moves:
        folder = destination_folder(move.destinations)
        by_destination[folder] = by_destination.get(folder, 0) + 1
    days = sorted({move.publish_date for move in moves})
    console.print(
        f"{len(moves)} cortes em {len(days)} dias, "
        f"de {days[0].isoformat()} a {days[-1].isoformat()}: "
        + ", ".join(f"{count} em {folder}" for folder, count in sorted(by_destination.items()))
    )


def doctor(settings: Settings) -> None:
    """Check every external dependency and report what is missing."""
    from rich.table import Table

    table = Table(title="Dependencias externas")
    table.add_column("item", style="bold")
    table.add_column("status")
    table.add_column("detalhe")

    def add(name: str, ok: bool, detail: str, *, optional: bool = False) -> None:
        if ok:
            table.add_row(name, "[green]ok[/green]", detail)
        elif optional:
            table.add_row(name, "[yellow]opcional[/yellow]", detail)
        else:
            table.add_row(name, "[red]ausente[/red]", detail)

    for tool, hint, optional in (
        ("ffmpeg", "obrigatorio para render (apt install ffmpeg)", False),
        ("ffprobe", "obrigatorio para verificar a saida do render", False),
        ("yt-dlp", "obrigatorio para ingest (uv tool install yt-dlp)", False),
        ("rclone", "so para deliver.mode: rclone", True),
        ("nvidia-smi", "GPU NVIDIA para faster-whisper; sem ela, CPU (lento)", True),
        ("mpv", "preview na revisao (ffplay tambem serve)", True),
    ):
        path = shutil.which(tool)
        add(tool, path is not None, path or hint, optional=optional)

    if settings.deliver.mode == "rclone":
        from clipper.stages.deliver import rclone_remotes

        wanted = settings.deliver.rclone_remote.rstrip(":")
        remotes = rclone_remotes()
        add(
            f"remote rclone '{wanted}'",
            wanted in remotes,
            ", ".join(remotes) if remotes else f"rclone config create {wanted} drive scope drive",
        )
    else:
        root = settings.deliver.drive_path
        add("pasta do Drive", root.exists(), str(root))

    key = "definida" if settings.gemini_api_key else "ausente (.env GEMINI_API_KEY)"
    add(
        "GEMINI_API_KEY",
        settings.gemini_api_key is not None,
        key,
        optional=settings.llm.provider != "gemini",
    )

    claude = shutil.which(settings.llm.claude_binary)
    add(
        f"CLI '{settings.llm.claude_binary}'",
        claude is not None,
        claude or "so para llm.provider: claude (https://claude.com/code)",
        optional=settings.llm.provider != "claude",
    )

    fonts = _installed_fonts()
    for font in _profile_fonts(settings):
        found = any(font.lower() in f for f in fonts)
        add("fonte " + font, found, "fallback DejaVu Sans", optional=True)

    console.print(table)
    if shutil.which("ffprobe") is None:
        warn(
            "sem ffprobe a verificacao de saida do render nao roda; "
            "instale o pacote ffmpeg completo da sua distro"
        )


def _installed_fonts() -> list[str]:
    if shutil.which("fc-list") is None:
        return []
    try:
        out = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return []
    return [line.strip().lower() for line in out.stdout.splitlines()]


def _profile_fonts(settings: Settings) -> list[str]:
    fonts: list[str] = []
    for name in settings.profile_names():
        try:
            profile = settings.profile(name)
        except ValueError:
            continue
        if profile.subtitle.font not in fonts:
            fonts.append(profile.subtitle.font)
    return fonts


def _exists_nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0
