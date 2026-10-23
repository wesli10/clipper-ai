"""Builds the monitor snapshot from `state.db`, the workspace and the queue file.

Everything here is read-only with respect to the pipeline: the database is opened
in `mode=ro` so a running batch is never blocked, and the only files written are
the monitor's own cache under ``workspace/.monitor``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from clipper.agent.channel import available as available_channels
from clipper.config import Settings
from clipper.evals.report import NoteRow
from clipper.models import Clip, ClipSet, ClipStatus, ReviewRequest
from clipper.monitor.agent import RunStore
from clipper.monitor.models import (
    STALLED,
    ClipView,
    EvalClipView,
    EvalRoundView,
    ProfileView,
    QueueItem,
    Snapshot,
    StageView,
    VideoView,
)
from clipper.stages.review import REQUEST_NAME as REVIEW_REQUEST_NAME
from clipper.state import NO_PROFILE, PROFILE_STAGES, VIDEO_STAGES, StageRecord, StageStatus
from clipper.utils.errors import ClipperError

#: Lines of the active log kept in the snapshot.
LOG_LINES = 60
#: How much of the log file is read to find those lines.
LOG_TAIL_BYTES = 32 * 1024
#: Minimum age of a snapshot before it is rebuilt (seconds).
REFRESH_S = 0.7
#: A log touched more recently than this means the stage is working right now.
LIVE_WINDOW_S = 40.0
#: Directories inside the workspace that are not videos.
NOT_VIDEOS = frozenset({"cache", ".monitor"})


# --------------------------------------------------------------------------- #
# state.db, read-only
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DbState:
    """What the state database says, without touching it."""

    videos: dict[str, dict[str, str | float]] = field(default_factory=dict)
    stages: dict[str, list[StageRecord]] = field(default_factory=dict)
    error: str = ""


def read_state(db_path: Path) -> DbState:
    """Read videos and stage records. A locked or missing db yields an empty state."""
    if not db_path.exists():
        return DbState()
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return DbState(error=str(exc))
    conn.row_factory = sqlite3.Row
    try:
        videos: dict[str, dict[str, str | float]] = {}
        for row in conn.execute("SELECT * FROM videos ORDER BY created_at"):
            videos[str(row["video_id"])] = {
                "video_id": str(row["video_id"]),
                "title": str(row["title"] or ""),
                "channel": str(row["channel"] or ""),
                "url": str(row["url"] or ""),
                "duration": float(row["duration"] or 0.0),
            }
        stages: dict[str, list[StageRecord]] = {}
        for row in conn.execute("SELECT * FROM stages"):
            record = StageRecord(
                video_id=str(row["video_id"]),
                profile=str(row["profile"]),
                stage=str(row["stage"]),
                status=StageStatus(str(row["status"])),
                detail=str(row["detail"] or ""),
                updated_at=str(row["updated_at"] or ""),
            )
            stages.setdefault(record.video_id, []).append(record)
    except sqlite3.Error as exc:
        return DbState(error=str(exc))
    finally:
        conn.close()
    return DbState(videos=videos, stages=stages)


# --------------------------------------------------------------------------- #
# History: what happened to videos already wiped from the workspace
# --------------------------------------------------------------------------- #


class History:
    """Remembers finished videos, because `batch --clean` erases their workspace.

    The pipeline deletes both the directory and the database row once a video is
    delivered, so without this the page would forget every clip it just shipped.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "history.json"
        self.thumbs = root / "thumbs"
        self.videos: dict[str, VideoView] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, value in (raw.get("videos") or {}).items():
            try:
                self.videos[str(key)] = VideoView.model_validate(value)
            except ValueError:
                continue

    def save(self) -> None:
        payload = {"videos": {k: v.model_dump() for k, v in self.videos.items()}}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass

    def remember(self, videos: list[VideoView]) -> bool:
        """Store the live videos; returns True when the file needs rewriting."""
        changed = False
        for video in videos:
            if video.from_history:
                continue
            kept = video.model_copy(update={"log": [], "log_name": "", "from_history": True})
            if self.videos.get(video.video_id) != kept:
                self.videos[video.video_id] = kept
                changed = True
        return changed

    def thumbnail_path(self, video_id: str, profile: str, dir_name: str) -> Path:
        return self.thumbs / video_id / profile / f"{dir_name}.jpg"

    def has_thumbnail(self, video_id: str, profile: str, dir_name: str) -> bool:
        return self.thumbnail_path(video_id, profile, dir_name).exists()

    def keep_thumbnail(self, clip: ClipView, source: Path) -> None:
        """Copy a clip thumbnail aside so it survives the workspace cleanup."""
        target = self.thumbnail_path(clip.video_id, clip.profile, clip.dir_name)
        try:
            if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def read_queue_lines(path: Path) -> list[str]:
    """URLs of the queue file, in order, ignoring comments (same rules as `batch`)."""
    if not path.exists():
        return []
    urls: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and line not in urls:
            urls.append(line)
    return urls


def video_id_for(url: str) -> str:
    from clipper.stages.ingest import pick_source

    try:
        return pick_source(url).video_id(url)
    except ClipperError:
        return ""


def pipeline_processes() -> list[str]:
    """Command lines of the clipper processes running now (Linux only)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    mine = os.getpid()
    found: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
        if not parts or "monitor" in parts:
            continue
        if not any(Path(p).name == "clipper" for p in parts):
            continue
        command = " ".join(parts)
        if command not in found:
            found.append(command)
    return found


def tail(path: Path, lines: int = LOG_LINES) -> list[str]:
    """Last ``lines`` lines of a log file, without reading the whole thing."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
            chunk = handle.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", "replace")
    if size > LOG_TAIL_BYTES:
        text = text.split("\n", 1)[-1]
    return [line for line in text.splitlines() if line.strip()][-lines:]


def latest_log(logs_dir: Path) -> Path | None:
    """The log file written most recently - the stage that is talking right now."""
    if not logs_dir.is_dir():
        return None
    candidates = [p for p in logs_dir.glob("*.log") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True, slots=True)
class HotStage:
    """The stage whose log file is being written right now."""

    stage: str
    profile: str
    detail: str = "em andamento"

    def matches(self, stage: str, profile: str) -> bool:
        if stage != self.stage:
            return False
        if stage in VIDEO_STAGES:
            return profile == NO_PROFILE
        return self.profile in ("", profile)


def hot_stage(logs_dir: Path, video_dir: Path) -> HotStage | None:
    """Which stage a just-written log file belongs to, if any is still warm."""
    path = latest_log(logs_dir)
    if path is None:
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > LIVE_WINDOW_S:
        return None
    stage, _, profile = path.stem.partition("_")
    if stage not in (*VIDEO_STAGES, *PROFILE_STAGES):
        return None
    detail = "em andamento"
    if stage == "ingest":
        detail = _download_detail(video_dir) or "baixando"
    elif stage == "deliver":
        detail = "enviando para o Drive"
    return HotStage(stage=stage, profile=profile, detail=detail)


def _download_detail(video_dir: Path) -> str:
    """How much of the source file yt-dlp has already written."""
    candidates = [p for p in video_dir.glob("source.*") if p.is_file() and p.suffix != ".json"]
    if not candidates:
        return ""
    biggest = max(candidates, key=lambda p: p.stat().st_size)
    return f"baixando, {biggest.stat().st_size / 1048576:.0f} MB"


def _stage_views(
    records: dict[tuple[str, str], StageRecord],
    profile: str,
    names: tuple[str, ...],
    hot: HotStage | None = None,
    *,
    live: bool = True,
) -> list[StageView]:
    """One view per stage, with the live override of :func:`hot_stage` applied.

    `ingest` and `deliver` do not write a `running` row - the first has no video
    row yet and the second only reports once the upload is over - so without the
    log-based override the page would show a stalled pipeline while yt-dlp or
    rclone are working.

    The override in the other direction matters as much: with no pipeline process
    on the machine, a row still saying `running` is left over from a run that died
    without closing it, and believing it lights the whole page up over nothing.
    """
    views: list[StageView] = []
    for name in names:
        record = records.get((profile, name))
        view = StageView(
            name=name,
            status=str(record.status) if record else "pending",
            detail=record.detail if record else "",
            updated_at=record.updated_at if record else "",
        )
        if view.status == "pending" and hot is not None and hot.matches(name, profile):
            view.status = str(StageStatus.RUNNING)
            view.detail = view.detail or hot.detail
        elif view.status == str(StageStatus.RUNNING) and not live:
            view.status = STALLED
        views.append(view)
    return views


def _clip_node(
    clip_status: ClipStatus,
    stages: dict[str, StageView],
    *,
    review_done: bool,
    rendered: bool,
    packaged: bool,
    delivered: bool,
) -> tuple[str, bool]:
    """Where the clip sits, and whether that stage is working on it right now."""
    if clip_status is ClipStatus.REJECTED:
        return "review", False
    done = ["select"]
    if review_done:
        done.append("review")
    if rendered:
        done.append("render")
    if packaged:
        done.append("package")
    if delivered:
        return "deliver", False
    following = PROFILE_STAGES[PROFILE_STAGES.index(done[-1]) + 1]
    return following, stages[following].status == str(StageStatus.RUNNING)


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


def build_snapshot(
    settings: Settings,
    *,
    queue_path: Path | None = None,
    history: History | None = None,
    runs: RunStore | None = None,
) -> Snapshot:
    """One consistent read of everything the page shows."""
    db = read_state(settings.state_db)
    processes = pipeline_processes()

    videos: list[VideoView] = []
    for video_id in _known_videos(settings, db):
        meta = db.videos.get(video_id) or _workspace_meta(settings, video_id)
        videos.append(
            _build_video(
                settings,
                video_id,
                meta,
                db.stages.get(video_id, []),
                history,
                live=bool(processes),
                runs=runs,
            )
        )

    if history is not None:
        for clip in (c for v in videos for p in v.profiles for c in p.clips):
            source = settings.profile_dir(clip.video_id, clip.profile) / "clips" / clip.dir_name
            thumbnail = source / "thumbnail.jpg"
            if thumbnail.exists():
                history.keep_thumbnail(clip, thumbnail)
        if history.remember(videos):
            history.save()
        known = {v.video_id for v in videos}
        for video_id, remembered in history.videos.items():
            if video_id not in known:
                videos.append(remembered)

    videos.sort(key=lambda v: (v.active, v.updated_at), reverse=True)
    pending = _pending_reviews(settings, videos)
    clips = [c for v in videos for p in v.profiles for c in p.clips]
    running_stage = any(
        s.status == str(StageStatus.RUNNING) for v in videos for s in _all_stages(v)
    )

    return Snapshot(
        generated_at=time.time(),
        running=bool(processes) or running_stage,
        processes=processes,
        workspace=str(settings.workspace.expanduser()),
        queue_path=str(queue_path) if queue_path else "",
        queue=_build_queue(queue_path, videos, live=bool(processes)),
        videos=videos,
        video_stages=list(VIDEO_STAGES),
        profile_stages=list(PROFILE_STAGES),
        channels=available_channels(settings),
        evals=eval_rounds(settings),
        clips_total=len(clips),
        clips_delivered=sum(1 for c in clips if c.delivered),
        pending_reviews=pending,
        error=db.error,
    )


def eval_rounds(settings: Settings) -> list[EvalRoundView]:
    """Every round under `evals/` that has a `notas.md`, newest name last.

    Read straight off disk rather than cached with the rest of the snapshot: the
    file is also editable by hand, and a page showing a stale mark would invite
    you to overwrite what you just typed into the file.
    """
    from clipper.evals.report import NOTES_NAME, read_rows

    root = settings.evals_dir.expanduser()
    if not root.is_dir():
        return []
    rounds: list[EvalRoundView] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        notes = directory / NOTES_NAME
        if not notes.is_file():
            continue
        clips = [_eval_clip(settings, row) for row in read_rows(notes)]
        marks = [c.note for c in clips if c.note]
        rounds.append(
            EvalRoundView(
                name=directory.name,
                path=str(notes),
                clips=clips,
                marked=len(marks),
                mean_note=round(sum(marks) / len(marks), 2) if marks else 0.0,
            )
        )
    return rounds


def _eval_clip(settings: Settings, row: NoteRow) -> EvalClipView:
    """Match a row of `notas.md` back to the clip it is about, when it is still there."""
    view = EvalClipView(clip_id=row.clip_id, title=row.title, note=row.note, comment=row.comment)
    parts = row.clip_id.split("/")
    if len(parts) != 3 or not parts[2].isdigit():
        return view
    video_id, profile, index = parts[0], parts[1], int(parts[2])
    view = view.model_copy(update={"video_id": video_id, "profile": profile, "index": index})

    path = settings.profile_dir(video_id, profile) / "clips.json"
    if not path.exists():
        return view
    try:
        clip_set = ClipSet.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return view
    clip = next((c for c in clip_set.clips if c.index == index), None)
    if clip is None:
        return view
    return view.model_copy(
        update={
            "dir_name": clip.dir_name,
            "duration": clip.duration,
            "dropped": clip.status is ClipStatus.REJECTED,
            "playable": (path.parent / "clips" / clip.dir_name / "clip.mp4").exists(),
        }
    )


def _pending_reviews(settings: Settings, videos: list[VideoView]) -> list[ReviewRequest]:
    """The `review_request.json` files sitting in the workspace right now."""
    requests: list[ReviewRequest] = []
    for video in videos:
        if video.from_history:
            continue
        for profile in video.profiles:
            if not profile.awaiting_review:
                continue
            path = settings.profile_dir(video.video_id, profile.name) / REVIEW_REQUEST_NAME
            try:
                requests.append(ReviewRequest.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    requests.sort(key=lambda r: r.created_at)
    return requests


def _all_stages(video: VideoView) -> list[StageView]:
    return [*video.stages, *(s for p in video.profiles for s in p.stages)]


def _build_queue(
    queue_path: Path | None, videos: list[VideoView], *, live: bool = True
) -> list[QueueItem]:
    """The queue file, said back with what the pipeline did to each line.

    A video between two stages has no `running` row for a moment, which is why
    `live` decides the fallback: with a pipeline on the machine that gap is still
    "em producao"; with nothing running it is a queue that stopped.
    """
    if queue_path is None:
        return []
    by_id = {v.video_id: v for v in videos}
    items: list[QueueItem] = []
    for position, url in enumerate(read_queue_lines(queue_path), start=1):
        video_id = video_id_for(url)
        video = by_id.get(video_id)
        if video is None:
            status = "fila" if video_id else "invalido"
        elif video.failed:
            status = "falhou"
        elif video.active:
            status = "processando"
        elif video.delivered:
            status = "entregue"
        else:
            status = "processando" if live else STALLED
        items.append(
            QueueItem(
                position=position,
                url=url,
                video_id=video_id,
                title=video.title if video else "",
                status=status,
            )
        )
    return items


def _known_videos(settings: Settings, db: DbState) -> list[str]:
    """Every video the workspace knows about, database row or not.

    A video being downloaded has a directory and a log before it has a row, and
    that is exactly the moment the page should already be showing it.
    """
    ids = list(db.videos)
    for video_id in db.stages:
        if video_id not in ids:
            ids.append(video_id)
    root = settings.workspace.expanduser()
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in NOT_VIDEOS or child.name.startswith("."):
                continue
            if child.name in ids:
                continue
            if (child / "logs").is_dir() or (child / "source.info.json").exists():
                ids.append(child.name)
    return ids


def _workspace_meta(settings: Settings, video_id: str) -> dict[str, str | float]:
    """Metadata for a video with no database row yet, from ``source.info.json``."""
    path = settings.video_dir(video_id) / "source.info.json"
    meta: dict[str, str | float] = {
        "video_id": video_id,
        "title": "",
        "channel": "",
        "url": "",
        "duration": 0.0,
    }
    if not path.exists():
        return meta
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return meta
    meta["title"] = str(raw.get("title") or "")
    meta["channel"] = str(raw.get("uploader") or raw.get("channel") or "")
    meta["url"] = str(raw.get("webpage_url") or "")
    meta["duration"] = float(raw.get("duration") or 0.0)
    return meta


def _build_video(
    settings: Settings,
    video_id: str,
    meta: dict[str, str | float],
    records: list[StageRecord],
    history: History | None,
    *,
    live: bool,
    runs: RunStore | None = None,
) -> VideoView:
    by_key = {(r.profile, r.stage): r for r in records}
    hot = hot_stage(settings.logs_dir(video_id), settings.video_dir(video_id)) if live else None
    stages = _stage_views(by_key, NO_PROFILE, VIDEO_STAGES, hot, live=live)

    seen = {r.profile for r in records if r.profile != NO_PROFILE}
    names = sorted(seen | set(settings.default_profiles))
    profiles = [
        _build_profile(settings, video_id, name, by_key, history, hot, live=live) for name in names
    ]

    node = "profiles"
    for stage in stages:
        if stage.status != str(StageStatus.DONE):
            node = stage.name
            break
    every = [*stages, *(s for p in profiles for s in p.stages)]
    active = any(s.status == str(StageStatus.RUNNING) for s in every)
    failed = any(s.status == str(StageStatus.FAILED) for s in every)
    delivered = any(
        s.name == "deliver" and s.status == str(StageStatus.DONE)
        for p in profiles
        for s in p.stages
    )

    log_path = latest_log(settings.logs_dir(video_id))
    updated = max((r.updated_at for r in records), default="")

    # Only the newest run, and only its summary: the whole trail would blow up
    # every SSE push, and the page fetches it on demand (SPEC-agent 9.3).
    session = runs.latest(settings.agent_dir(video_id)) if runs is not None else None

    return VideoView(
        video_id=video_id,
        title=str(meta.get("title") or ""),
        channel=str(meta.get("channel") or ""),
        url=str(meta.get("url") or ""),
        duration=float(meta.get("duration") or 0.0),
        stages=stages,
        profiles=profiles,
        node=node,
        active=active,
        failed=failed,
        delivered=delivered,
        log_name=log_path.stem if log_path else "",
        log=tail(log_path) if log_path else [],
        updated_at=_epoch(updated),
        agent=session.summary() if session is not None else None,
    )


def _epoch(timestamp: str) -> float:
    if not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return 0.0


def _build_profile(
    settings: Settings,
    video_id: str,
    name: str,
    by_key: dict[tuple[str, str], StageRecord],
    history: History | None,
    hot: HotStage | None,
    *,
    live: bool = True,
) -> ProfileView:
    stages = _stage_views(by_key, name, PROFILE_STAGES, hot, live=live)
    by_name = {s.name: s for s in stages}
    review_done = by_name["review"].status == str(StageStatus.DONE)
    delivered_stage = by_name["deliver"].status == str(StageStatus.DONE)

    profile_dir = settings.profile_dir(video_id, name)
    awaiting = (profile_dir / REVIEW_REQUEST_NAME).exists()
    clips_path = profile_dir / "clips.json"
    clips: list[ClipView] = []
    if clips_path.exists():
        try:
            clip_set = ClipSet.model_validate_json(clips_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            clip_set = ClipSet(video_id=video_id, profile=name)
        for clip in clip_set.clips:
            clips.append(
                _build_clip(
                    profile_dir,
                    clip_set.video_id,
                    name,
                    clip,
                    by_name,
                    review_done=review_done,
                    delivered_stage=delivered_stage,
                    history=history,
                )
            )
    return ProfileView(name=name, stages=stages, clips=clips, awaiting_review=awaiting)


def _build_clip(
    profile_dir: Path,
    video_id: str,
    profile: str,
    clip: Clip,
    stages: dict[str, StageView],
    *,
    review_done: bool,
    delivered_stage: bool,
    history: History | None,
) -> ClipView:
    clip_dir = profile_dir / "clips" / clip.dir_name
    video_file = clip_dir / "clip.mp4"
    metadata_file = clip_dir / "metadata.json"
    thumbnail = clip_dir / "thumbnail.jpg"

    rendered = video_file.exists()
    packaged = metadata_file.exists()
    title = clip.topic or clip.hook
    if packaged:
        try:
            raw = json.loads(metadata_file.read_text(encoding="utf-8"))
            title = str(raw.get("title") or title)
        except (OSError, json.JSONDecodeError):
            pass

    view = ClipView(
        video_id=video_id,
        profile=profile,
        index=clip.index,
        slug=clip.slug,
        dir_name=clip.dir_name,
        title=title,
        topic=clip.topic,
        hook=clip.hook,
        score=clip.score,
        duration=clip.duration,
        status=str(clip.status),
        rendered=rendered,
        packaged=packaged,
        delivered=delivered_stage and rendered and packaged,
        size_bytes=video_file.stat().st_size if rendered else 0,
        has_thumbnail=thumbnail.exists()
        or (history is not None and history.has_thumbnail(video_id, profile, clip.dir_name)),
        segments=[(s.start, s.end) for s in clip.segments],
        updated_at=video_file.stat().st_mtime if rendered else 0.0,
    )
    node, active = _clip_node(
        clip.status,
        stages,
        review_done=review_done,
        rendered=rendered,
        packaged=packaged,
        delivered=view.delivered,
    )
    return view.model_copy(update={"node": node, "active": active})


# --------------------------------------------------------------------------- #
# Cached source shared by every connected browser
# --------------------------------------------------------------------------- #


class SnapshotSource:
    """Builds at most one snapshot per ``REFRESH_S``, however many clients ask."""

    def __init__(self, settings: Settings, *, queue_path: Path | None = None) -> None:
        self.settings = settings
        self.queue_path = queue_path
        self.history = History(settings.workspace.expanduser() / ".monitor")
        self.runs = RunStore()
        self._lock = threading.Lock()
        self._cached: Snapshot | None = None
        self._built_at = 0.0

    def invalidate(self) -> None:
        """Drop the cache after a write, so the page sees its own change at once."""
        with self._lock:
            self._built_at = 0.0

    def get(self, *, max_age: float = REFRESH_S) -> Snapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._built_at > max_age:
                self._cached = build_snapshot(
                    self.settings,
                    queue_path=self.queue_path,
                    history=self.history,
                    runs=self.runs,
                )
                self._built_at = now
            return self._cached
