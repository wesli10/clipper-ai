from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.models import Clip, ClipSet, ClipStatus, SourceInfo, TimeRange
from clipper.monitor.models import STALLED
from clipper.monitor.server import _safe_name
from clipper.monitor.snapshot import History, build_snapshot, hot_stage, read_queue_lines, tail
from clipper.state import StageStatus, State

VIDEO_ID = "vtZXoKcm1Yz"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # evals_dir points into the tmp dir on purpose: a unit test that reads the
    # repository's own eval rounds is not testing the same thing twice running.
    return Settings(
        workspace=tmp_path / "workspace",
        default_profiles=["short", "long"],
        evals_dir=tmp_path / "evals",
    )


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clipper process on the machine, which is what makes `running` believable."""
    monkeypatch.setattr(
        "clipper.monitor.snapshot.pipeline_processes", lambda: ["clipper batch fila.txt"]
    )


def make_clip(index: int, slug: str, status: ClipStatus = ClipStatus.APPROVED) -> Clip:
    return Clip(
        index=index,
        slug=slug,
        profile="short",
        segments=[TimeRange(start=10.0, end=40.0)],
        hook="gancho",
        topic=f"assunto {index}",
        score=88.0,
        status=status,
    )


def write_clips(settings: Settings, profile: str, clips: list[Clip]) -> None:
    path = settings.profile_dir(VIDEO_ID, profile) / "clips.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    clip_set = ClipSet(video_id=VIDEO_ID, profile=profile, clips=clips)
    path.write_text(clip_set.model_dump_json(), encoding="utf-8")


def render_clip(settings: Settings, profile: str, clip: Clip, *, metadata: bool = False) -> Path:
    clip_dir = settings.profile_dir(VIDEO_ID, profile) / "clips" / clip.dir_name
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip.mp4").write_bytes(b"0" * 2048)
    (clip_dir / "thumbnail.jpg").write_bytes(b"jpeg")
    if metadata:
        (clip_dir / "metadata.json").write_text(
            json.dumps({"title": "Titulo final", "profile": profile}), encoding="utf-8"
        )
    return clip_dir


def seed_state(settings: Settings) -> State:
    state = State(settings.state_db)
    state.upsert_video(
        SourceInfo(
            video_id=VIDEO_ID,
            title="Video de teste",
            channel="Canal",
            duration=600.0,
            url=f"https://youtu.be/{VIDEO_ID}",
        )
    )
    state.set_stage(VIDEO_ID, "ingest", StageStatus.DONE)
    state.set_stage(VIDEO_ID, "transcribe", StageStatus.DONE)
    return state


def test_clip_walks_the_stages(settings: Settings, live: None) -> None:
    state = seed_state(settings)
    clip = make_clip(1, "primeiro")
    write_clips(settings, "short", [clip])
    state.set_stage(VIDEO_ID, "select", StageStatus.DONE, profile="short")

    snapshot = build_snapshot(settings)
    short = next(p for p in snapshot.videos[0].profiles if p.name == "short")
    assert [c.node for c in short.clips] == ["review"]

    state.set_stage(VIDEO_ID, "review", StageStatus.DONE, profile="short")
    state.set_stage(VIDEO_ID, "render", StageStatus.RUNNING, profile="short")
    only = build_snapshot(settings).videos[0].profiles
    waiting = next(p for p in only if p.name == "short").clips[0]
    assert waiting.node == "render" and waiting.active and not waiting.rendered

    render_clip(settings, "short", clip)
    state.set_stage(VIDEO_ID, "render", StageStatus.DONE, profile="short")
    rendered = next(
        p for p in build_snapshot(settings).videos[0].profiles if p.name == "short"
    ).clips[0]
    assert rendered.rendered and rendered.node == "package" and rendered.size_bytes == 2048

    render_clip(settings, "short", clip, metadata=True)
    state.set_stage(VIDEO_ID, "package", StageStatus.DONE, profile="short")
    state.set_stage(VIDEO_ID, "deliver", StageStatus.DONE, profile="short")
    done = next(p for p in build_snapshot(settings).videos[0].profiles if p.name == "short").clips[
        0
    ]
    assert done.node == "deliver" and done.delivered and done.title == "Titulo final"


def test_rejected_clip_stops_at_review(settings: Settings) -> None:
    state = seed_state(settings)
    write_clips(settings, "short", [make_clip(1, "cortado", ClipStatus.REJECTED)])
    state.set_stage(VIDEO_ID, "select", StageStatus.DONE, profile="short")
    state.set_stage(VIDEO_ID, "review", StageStatus.DONE, profile="short")

    clip = next(p for p in build_snapshot(settings).videos[0].profiles if p.name == "short").clips[
        0
    ]
    assert clip.node == "review" and not clip.active and clip.status == "rejected"


def test_queue_status_follows_the_pipeline(settings: Settings, tmp_path: Path, live: None) -> None:
    seed_state(settings)
    queue = tmp_path / "fila.txt"
    queue.write_text(
        "# comentario\n"
        f"https://www.youtube.com/watch?v={VIDEO_ID}\n"
        "\n"
        "https://www.youtube.com/watch?v=outroVideoX\n"
        "nao-e-url\n",
        encoding="utf-8",
    )
    assert len(read_queue_lines(queue)) == 3

    snapshot = build_snapshot(settings, queue_path=queue)
    by_position = {item.position: item for item in snapshot.queue}
    assert by_position[1].video_id == VIDEO_ID
    assert by_position[1].status == "processando"
    assert by_position[2].status == "fila"
    assert by_position[3].status == "invalido"


def test_history_survives_a_cleaned_workspace(settings: Settings) -> None:
    state = seed_state(settings)
    clip = make_clip(1, "entregue")
    write_clips(settings, "short", [clip])
    render_clip(settings, "short", clip, metadata=True)
    for stage in ("select", "review", "render", "package", "deliver"):
        state.set_stage(VIDEO_ID, stage, StageStatus.DONE, profile="short")

    history = History(settings.workspace.expanduser() / ".monitor")
    before = build_snapshot(settings, history=history)
    assert before.clips_delivered == 1

    # `batch --clean` wipes both the directory and the database row.
    state.delete_video(VIDEO_ID)
    import shutil

    shutil.rmtree(settings.video_dir(VIDEO_ID))

    after = build_snapshot(settings, history=History(settings.workspace.expanduser() / ".monitor"))
    remembered = next(v for v in after.videos if v.video_id == VIDEO_ID)
    assert remembered.from_history and after.clips_delivered == 1
    assert history.has_thumbnail(VIDEO_ID, "short", clip.dir_name)


def test_download_shows_up_before_the_database_row(settings: Settings) -> None:
    logs = settings.logs_dir(VIDEO_ID)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "ingest.log").write_text("baixando\n", encoding="utf-8")
    (settings.video_dir(VIDEO_ID) / "source.mp4.part").write_bytes(b"0" * (3 * 1048576))

    hot = hot_stage(logs, settings.video_dir(VIDEO_ID))
    assert hot is not None and hot.stage == "ingest" and "MB" in hot.detail

    snapshot = build_snapshot(settings)
    assert [v.video_id for v in snapshot.videos] == [VIDEO_ID]
    assert snapshot.videos[0].stages[0].name == "ingest"


def test_hot_stage_ignores_old_logs(settings: Settings) -> None:
    logs = settings.logs_dir(VIDEO_ID)
    logs.mkdir(parents=True, exist_ok=True)
    old = logs / "render_short.log"
    old.write_text("linha\n", encoding="utf-8")
    import os

    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    assert hot_stage(logs, settings.video_dir(VIDEO_ID)) is None


# --- a run that ended without closing its rows (SPEC 15.2) ----------------- #


def test_a_running_stage_with_nothing_running_is_shown_as_stopped(settings: Settings) -> None:
    """What the page was doing wrong: believing a row written by a run that is over."""
    state = seed_state(settings)
    clip = make_clip(1, "meio-do-caminho")
    write_clips(settings, "short", [clip])
    render_clip(settings, "short", clip)
    for stage in ("select", "review", "render"):
        state.set_stage(VIDEO_ID, stage, StageStatus.DONE, profile="short")
    state.set_stage(VIDEO_ID, "package", StageStatus.RUNNING, profile="short", detail="3/4 cortes")

    snapshot = build_snapshot(settings)
    video = snapshot.videos[0]
    package = next(
        s for p in video.profiles if p.name == "short" for s in p.stages if s.name == "package"
    )

    assert package.status == STALLED
    # The partial still shows: it is the only thing that says how far it got.
    assert package.detail == "3/4 cortes"
    assert not video.active
    assert not snapshot.running


def test_the_same_row_is_running_while_a_process_is_up(settings: Settings, live: None) -> None:
    state = seed_state(settings)
    write_clips(settings, "short", [make_clip(1, "em-curso")])
    state.set_stage(VIDEO_ID, "package", StageStatus.RUNNING, profile="short", detail="3/4 cortes")

    snapshot = build_snapshot(settings)
    package = next(
        s
        for p in snapshot.videos[0].profiles
        if p.name == "short"
        for s in p.stages
        if s.name == "package"
    )

    assert package.status == str(StageStatus.RUNNING)
    assert snapshot.videos[0].active


def test_a_stopped_video_is_not_called_processing_in_the_queue(
    settings: Settings, tmp_path: Path
) -> None:
    seed_state(settings)
    queue = tmp_path / "fila.txt"
    queue.write_text(f"https://www.youtube.com/watch?v={VIDEO_ID}\n", encoding="utf-8")

    snapshot = build_snapshot(settings, queue_path=queue)

    assert snapshot.queue[0].status == STALLED


def test_tail_reads_the_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"linha {i}" for i in range(5000)), encoding="utf-8")
    lines = tail(path, lines=10)
    assert len(lines) == 10 and lines[-1] == "linha 4999"


@pytest.mark.parametrize("value", ["../etc", "a/b", "", ".hidden", "..", "c:\\windows"])
def test_safe_name_rejects_traversal(value: str) -> None:
    assert _safe_name(value) == ""


def test_safe_name_accepts_a_clip_directory() -> None:
    assert _safe_name("01_meu-corte") == "01_meu-corte"
