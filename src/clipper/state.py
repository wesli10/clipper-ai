"""Pipeline state in SQLite - what is already done, per video and per (video, profile).

Idempotency (SPEC 3.1) is decided here plus a file-existence check in each stage.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from clipper.models import ClipMetadata, SourceInfo

#: Stages that run once per video.
VIDEO_STAGES: tuple[str, ...] = ("ingest", "transcribe")
#: Stages that run once per (video, profile).
PROFILE_STAGES: tuple[str, ...] = ("select", "review", "render", "package", "deliver")
ALL_STAGES: tuple[str, ...] = VIDEO_STAGES + PROFILE_STAGES

#: Sentinel stored in the `profile` column for video-level stages.
NO_PROFILE = "-"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id   TEXT PRIMARY KEY,
    url        TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    channel    TEXT NOT NULL DEFAULT '',
    duration   REAL NOT NULL DEFAULT 0,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    language   TEXT,
    upload_date TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stages (
    video_id   TEXT NOT NULL,
    profile    TEXT NOT NULL DEFAULT '-',
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (video_id, profile, stage)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    video_id   TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    turns      INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS publish_queue (
    video_id     TEXT NOT NULL,
    profile      TEXT NOT NULL,
    clip_index   INTEGER NOT NULL,
    publish_date TEXT NOT NULL,
    destinations TEXT NOT NULL DEFAULT '',
    filename     TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT '',
    archive_path TEXT NOT NULL DEFAULT '',
    metadata     TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (video_id, profile, clip_index)
);
CREATE INDEX IF NOT EXISTS publish_queue_by_day ON publish_queue (publish_date);
CREATE TABLE IF NOT EXISTS run_clips (
    run_id     TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    profile    TEXT NOT NULL,
    clip_index INTEGER NOT NULL,
    renders    INTEGER NOT NULL DEFAULT 0,
    verdict    TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, profile, clip_index)
);
"""


class RunStatus(StrEnum):
    """Where an agent run ended up (SPEC-agent 8.4)."""

    RUNNING = "running"
    DONE = "done"
    #: The loop was cut short by a budget or by a crash, but what was ready shipped.
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One agent run, as the monitor and the eval report read it back."""

    run_id: str
    video_id: str
    channel: str
    status: RunStatus
    detail: str
    turns: int
    cost_usd: float
    started_at: str
    ended_at: str


@dataclass(frozen=True, slots=True)
class RunClipRecord:
    """What a run did to one clip: how many renders it cost and how it ended."""

    run_id: str
    video_id: str
    profile: str
    clip_index: int
    renders: int
    verdict: str
    detail: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class QueuedClip:
    """One clip waiting in the publishing queue (SPEC 8.1).

    The date and the destinations are decided once, when the clip is delivered,
    and never recomputed: a clip already sitting in Thursday's folder must not
    move to Friday because another video was processed in between.

    ``metadata`` is a copy of the delivered ``metadata.json``. The workspace is
    wiped after delivery and reading every file back from the Drive takes a
    minute, so the queue carries what the publishing plan needs to print.
    """

    video_id: str
    profile: str
    clip_index: int
    publish_date: date
    destinations: tuple[str, ...]
    filename: str
    channel: str
    archive_path: str
    metadata: ClipMetadata
    created_at: str


@dataclass(frozen=True, slots=True)
class StageRecord:
    video_id: str
    profile: str
    stage: str
    status: StageStatus
    detail: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    """Thin wrapper over the SQLite state database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
        if "upload_date" not in existing:
            conn.execute("ALTER TABLE videos ADD COLUMN upload_date TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- videos ------------------------------------------------------------ #

    def upsert_video(self, info: SourceInfo) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO videos (video_id, url, title, channel, duration, width, height,
                                    language, upload_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    url=excluded.url, title=excluded.title, channel=excluded.channel,
                    duration=excluded.duration, width=excluded.width, height=excluded.height,
                    language=excluded.language, upload_date=excluded.upload_date
                """,
                (
                    info.video_id,
                    info.url,
                    info.title,
                    info.channel,
                    info.duration,
                    info.width,
                    info.height,
                    info.language,
                    info.upload_date,
                    _now(),
                ),
            )

    def get_video(self, video_id: str) -> SourceInfo | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        if row is None:
            return None
        return SourceInfo(
            video_id=row["video_id"],
            title=row["title"],
            channel=row["channel"],
            duration=row["duration"],
            url=row["url"],
            language=row["language"],
            width=row["width"],
            height=row["height"],
            upload_date=row["upload_date"],
        )

    def list_videos(self) -> list[SourceInfo]:
        with self._connect() as conn:
            rows = conn.execute("SELECT video_id FROM videos ORDER BY created_at").fetchall()
        videos = [self.get_video(r["video_id"]) for r in rows]
        return [v for v in videos if v is not None]

    def delete_video(self, video_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM stages WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM run_clips WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM runs WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
        # `publish_queue` is deliberately left alone: it says what is sitting in
        # the Drive waiting to be published, which cleaning the workspace does
        # not change. `unqueue` is the way to take a clip out of the queue.

    # -- stages ------------------------------------------------------------ #

    def set_stage(
        self,
        video_id: str,
        stage: str,
        status: StageStatus,
        *,
        profile: str | None = None,
        detail: str = "",
    ) -> None:
        if stage not in ALL_STAGES:
            raise ValueError(f"estagio desconhecido: {stage}")
        key_profile = profile or NO_PROFILE
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stages (video_id, profile, stage, status, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, profile, stage) DO UPDATE SET
                    status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at
                """,
                (video_id, key_profile, stage, str(status), detail, _now()),
            )

    def get_stage(
        self, video_id: str, stage: str, *, profile: str | None = None
    ) -> StageRecord | None:
        key_profile = profile or NO_PROFILE
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stages WHERE video_id = ? AND profile = ? AND stage = ?",
                (video_id, key_profile, stage),
            ).fetchone()
        if row is None:
            return None
        return StageRecord(
            video_id=row["video_id"],
            profile=row["profile"],
            stage=row["stage"],
            status=StageStatus(row["status"]),
            detail=row["detail"],
            updated_at=row["updated_at"],
        )

    def is_done(self, video_id: str, stage: str, *, profile: str | None = None) -> bool:
        record = self.get_stage(video_id, stage, profile=profile)
        return record is not None and record.status is StageStatus.DONE

    def clear_stage(self, video_id: str, stage: str, *, profile: str | None = None) -> None:
        key_profile = profile or NO_PROFILE
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM stages WHERE video_id = ? AND profile = ? AND stage = ?",
                (video_id, key_profile, stage),
            )

    # -- agent runs -------------------------------------------------------- #

    def start_run(self, run_id: str, video_id: str, *, channel: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, video_id, channel, status, started_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status, started_at=excluded.started_at, ended_at=''
                """,
                (run_id, video_id, channel, str(RunStatus.RUNNING), _now()),
            )

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        detail: str = "",
        turns: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET status = ?, detail = ?, turns = ?, cost_usd = ?, ended_at = ?
                WHERE run_id = ?
                """,
                (str(status), detail, turns, cost_usd, _now(), run_id),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else _run_record(row)

    def list_runs(self, video_id: str | None = None, *, limit: int = 50) -> list[RunRecord]:
        """Most recent first, so the monitor can show the run that matters on top."""
        query = "SELECT * FROM runs"
        params: tuple[str | int, ...] = ()
        if video_id is not None:
            query += " WHERE video_id = ?"
            params = (video_id,)
        # The run id starts with the timestamp, so it breaks a tie the same way
        # the clock would - two runs in the same second keep a stable order.
        query += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, limit)).fetchall()
        return [_run_record(row) for row in rows]

    def record_run_clip(
        self,
        run_id: str,
        video_id: str,
        profile: str,
        clip_index: int,
        *,
        renders: int = 0,
        verdict: str = "",
        detail: str = "",
    ) -> None:
        """Upsert one clip's outcome; ``renders`` accumulates across calls."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_clips
                    (run_id, video_id, profile, clip_index, renders, verdict, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, profile, clip_index) DO UPDATE SET
                    renders = run_clips.renders + excluded.renders,
                    verdict = excluded.verdict,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (run_id, video_id, profile, clip_index, renders, verdict, detail, _now()),
            )

    def run_clips(self, run_id: str) -> list[RunClipRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_clips WHERE run_id = ? ORDER BY profile, clip_index",
                (run_id,),
            ).fetchall()
        return [
            RunClipRecord(
                run_id=r["run_id"],
                video_id=r["video_id"],
                profile=r["profile"],
                clip_index=r["clip_index"],
                renders=r["renders"],
                verdict=r["verdict"],
                detail=r["detail"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # -- publishing queue --------------------------------------------------- #

    def queue_clip(
        self,
        video_id: str,
        profile: str,
        clip_index: int,
        *,
        publish_date: date,
        destinations: Sequence[str],
        filename: str,
        channel: str = "",
        archive_path: str = "",
        metadata: ClipMetadata,
    ) -> QueuedClip:
        """Put a clip in the queue, or return the place it already has."""
        existing = self.queued_clip(video_id, profile, clip_index)
        if existing is not None:
            return existing
        entry = QueuedClip(
            video_id=video_id,
            profile=profile,
            clip_index=clip_index,
            publish_date=publish_date,
            destinations=tuple(destinations),
            filename=filename,
            channel=channel,
            archive_path=archive_path,
            metadata=metadata,
            created_at=_now(),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO publish_queue (video_id, profile, clip_index, publish_date,
                                           destinations, filename, channel, archive_path,
                                           metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.video_id,
                    entry.profile,
                    entry.clip_index,
                    entry.publish_date.isoformat(),
                    ",".join(entry.destinations),
                    entry.filename,
                    entry.channel,
                    entry.archive_path,
                    entry.metadata.model_dump_json(),
                    entry.created_at,
                ),
            )
        return entry

    def queued_clip(self, video_id: str, profile: str, clip_index: int) -> QueuedClip | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM publish_queue
                WHERE video_id = ? AND profile = ? AND clip_index = ?
                """,
                (video_id, profile, clip_index),
            ).fetchone()
        return _queued_clip(row) if row is not None else None

    def queue(self, *, since: date | None = None, channel: str | None = None) -> list[QueuedClip]:
        """Everything in the queue, in publishing order."""
        sql = "SELECT * FROM publish_queue"
        params: list[str] = []
        where: list[str] = []
        if since is not None:
            where.append("publish_date >= ?")
            params.append(since.isoformat())
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY publish_date, profile, clip_index, filename"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        entries = [_queued_clip(row) for row in rows]
        return [entry for entry in entries if entry is not None]

    def unqueue(
        self,
        video_id: str,
        *,
        profile: str | None = None,
        clip_index: int | None = None,
    ) -> int:
        """Drop clips from the queue; returns how many rows were removed.

        Narrows from a whole video down to one clip, which is the granularity a
        clip is published at.
        """
        sql = "DELETE FROM publish_queue WHERE video_id = ?"
        params: list[str | int] = [video_id]
        if profile is not None:
            sql += " AND profile = ?"
            params.append(profile)
        if clip_index is not None:
            sql += " AND clip_index = ?"
            params.append(clip_index)
        with self._connect() as conn:
            return int(conn.execute(sql, params).rowcount)

    def stages_for(self, video_id: str) -> list[StageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stages WHERE video_id = ? ORDER BY profile, stage", (video_id,)
            ).fetchall()
        return [
            StageRecord(
                video_id=r["video_id"],
                profile=r["profile"],
                stage=r["stage"],
                status=StageStatus(r["status"]),
                detail=r["detail"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]


def _queued_clip(row: sqlite3.Row) -> QueuedClip | None:
    """A queue row, or nothing when its metadata copy cannot be read back."""
    try:
        metadata = ClipMetadata.model_validate_json(row["metadata"])
    except ValueError:
        return None
    destinations = tuple(d for d in row["destinations"].split(",") if d)
    return QueuedClip(
        video_id=row["video_id"],
        profile=row["profile"],
        clip_index=row["clip_index"],
        publish_date=date.fromisoformat(row["publish_date"]),
        destinations=destinations,
        filename=row["filename"],
        channel=row["channel"],
        archive_path=row["archive_path"],
        metadata=metadata,
        created_at=row["created_at"],
    )


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        video_id=row["video_id"],
        channel=row["channel"],
        status=RunStatus(row["status"]),
        detail=row["detail"],
        turns=row["turns"],
        cost_usd=row["cost_usd"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )
