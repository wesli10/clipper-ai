from __future__ import annotations

import pytest

from clipper.models import SourceInfo
from clipper.state import NO_PROFILE, StageStatus, State


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.db")


def test_upsert_and_get_video(state):
    info = SourceInfo(
        video_id="abc", title="T", channel="C", duration=120.0, url="u", width=1920, height=1080
    )
    state.upsert_video(info)
    got = state.get_video("abc")
    assert got is not None and got.title == "T" and got.width == 1920
    state.upsert_video(info.model_copy(update={"title": "T2"}))
    assert state.get_video("abc").title == "T2"
    assert len(state.list_videos()) == 1


def test_video_and_profile_stages_are_independent(state):
    state.set_stage("abc", "transcribe", StageStatus.DONE)
    state.set_stage("abc", "select", StageStatus.DONE, profile="short")

    assert state.is_done("abc", "transcribe")
    assert state.is_done("abc", "select", profile="short")
    assert not state.is_done("abc", "select", profile="long")
    assert not state.is_done("abc", "render", profile="short")

    records = {(r.profile, r.stage) for r in state.stages_for("abc")}
    assert records == {(NO_PROFILE, "transcribe"), ("short", "select")}


def test_clear_and_failed_status(state):
    state.set_stage("abc", "render", StageStatus.FAILED, profile="short", detail="ffmpeg 1")
    record = state.get_stage("abc", "render", profile="short")
    assert record.status is StageStatus.FAILED and "ffmpeg" in record.detail
    assert not state.is_done("abc", "render", profile="short")
    state.clear_stage("abc", "render", profile="short")
    assert state.get_stage("abc", "render", profile="short") is None


def test_unknown_stage_is_rejected(state):
    with pytest.raises(ValueError, match="estagio desconhecido"):
        state.set_stage("abc", "teleport", StageStatus.DONE)


def test_delete_video_removes_stages(state):
    state.upsert_video(SourceInfo(video_id="abc", title="T", channel="C", duration=1.0, url="u"))
    state.set_stage("abc", "ingest", StageStatus.DONE)
    state.delete_video("abc")
    assert state.get_video("abc") is None
    assert state.stages_for("abc") == []


def test_upload_date_survives_a_round_trip(state):
    """The delivery folder is named after it (SPEC 8), so it must persist."""
    state.upsert_video(
        SourceInfo(
            video_id="abc",
            title="T",
            channel="C",
            duration=1.0,
            url="u",
            upload_date="20260115",
        )
    )
    assert state.get_video("abc").upload_date == "20260115"


def test_an_older_database_without_upload_date_is_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE videos (video_id TEXT PRIMARY KEY, url TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',"
            " duration REAL NOT NULL DEFAULT 0, width INTEGER NOT NULL DEFAULT 0,"
            " height INTEGER NOT NULL DEFAULT 0, language TEXT, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO videos (video_id, created_at) VALUES ('old', '2026-01-01T00:00:00')"
        )

    migrated = State(db)
    assert migrated.get_video("old").upload_date is None
    migrated.upsert_video(
        SourceInfo(
            video_id="old", title="T", channel="C", duration=1.0, url="u", upload_date="20260115"
        )
    )
    assert migrated.get_video("old").upload_date == "20260115"


# --- agent runs (SPEC-agent 8) --------------------------------------------- #


def test_a_run_is_recorded_from_start_to_finish(state):
    from clipper.state import RunStatus

    state.start_run("20260910-120000-vid", "vid", channel="meu-canal")
    running = state.get_run("20260910-120000-vid")
    assert running is not None and running.status is RunStatus.RUNNING
    assert running.channel == "meu-canal" and running.ended_at == ""

    state.finish_run(
        "20260910-120000-vid", RunStatus.INCOMPLETE, detail="orcamento", turns=12, cost_usd=1.5
    )
    done = state.get_run("20260910-120000-vid")
    assert done.status is RunStatus.INCOMPLETE
    assert (done.turns, done.cost_usd, done.detail) == (12, 1.5, "orcamento")
    assert done.ended_at


def test_runs_come_back_newest_first_and_filtered_by_video(state):
    state.start_run("20260910-100000-a", "vid_a")
    state.start_run("20260910-110000-b", "vid_b")
    state.start_run("20260910-120000-c", "vid_a")

    assert next(r.run_id for r in state.list_runs()) == "20260910-120000-c"
    assert [r.run_id for r in state.list_runs("vid_a")] == [
        "20260910-120000-c",
        "20260910-100000-a",
    ]


def test_renders_accumulate_while_the_verdict_is_replaced(state):
    state.start_run("run1", "vid")
    state.record_run_clip("run1", "vid", "short", 1, renders=1, verdict="fail", detail="loudness")
    state.record_run_clip("run1", "vid", "short", 1, renders=1, verdict="ok")

    clips = state.run_clips("run1")
    assert len(clips) == 1
    assert (clips[0].renders, clips[0].verdict, clips[0].detail) == (2, "ok", "")


def test_deleting_a_video_takes_its_runs_with_it(state):
    info = SourceInfo(video_id="vid", title="T", channel="C", duration=10.0, url="u")
    state.upsert_video(info)
    state.start_run("run1", "vid")
    state.record_run_clip("run1", "vid", "short", 1, renders=1)

    state.delete_video("vid")
    assert state.get_run("run1") is None
    assert state.run_clips("run1") == []
