"""Pipeline orchestration and idempotency - SPEC 3.1 and 9."""

from __future__ import annotations

import json
import time

import pytest

from clipper import pipeline
from clipper.config import load_settings
from clipper.llm.base import BaseLLMProvider
from clipper.models import (
    Clip,
    ClipSet,
    ClipStatus,
    Segment,
    SourceInfo,
    TimeRange,
    Transcript,
    Word,
)
from clipper.state import PROFILE_STAGES, StageStatus, State
from clipper.utils.errors import ClipperError


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    return load_settings(config)


def seeded_transcript(video_id: str) -> Transcript:
    segments = []
    for i in range(20):
        start = i * 30.0
        text = f"assunto numero {i} explicado com calma."
        tokens = text.split()
        step = 28.0 / len(tokens)
        words = [
            Word(text=t, start=round(start + j * step, 3), end=round(start + (j + 1) * step, 3))
            for j, t in enumerate(tokens)
        ]
        segments.append(Segment(id=i, start=start, end=start + 28.0, text=text, words=words))
    return Transcript(video_id=video_id, language="pt", duration=600.0, segments=segments)


@pytest.fixture
def seeded(settings):
    """A workspace that already has a source and a transcript."""
    video_id = "vid0000001"
    video_dir = settings.video_dir(video_id)
    video_dir.mkdir(parents=True)
    (video_dir / "source.mp4").write_bytes(b"x")
    (video_dir / "audio.wav").write_bytes(b"x")
    (video_dir / "transcript.json").write_text(
        seeded_transcript(video_id).model_dump_json(), encoding="utf-8"
    )
    (video_dir / "source.info.json").write_text(
        json.dumps(
            {
                "id": video_id,
                "title": "Video de teste",
                "uploader": "Canal",
                "duration": 600,
                "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                "upload_date": "20260115",
                "width": 1920,
                "height": 1080,
            }
        ),
        encoding="utf-8",
    )
    state = State(settings.state_db)
    state.upsert_video(
        SourceInfo(
            video_id=video_id,
            title="Video de teste",
            channel="Canal",
            duration=600.0,
            url=f"https://www.youtube.com/watch?v={video_id}",
            upload_date="20260115",
            width=1920,
            height=1080,
        )
    )
    state.set_stage(video_id, "ingest", StageStatus.DONE)
    state.set_stage(video_id, "transcribe", StageStatus.DONE)
    return video_id


class CountingProvider(BaseLLMProvider):
    name = "counting"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, system: str, user: str) -> str:
        self.calls += 1
        return json.dumps(self.payload)


SELECTION = {
    "clips": [
        {"segments": [{"start": 0.0, "end": 58.0}], "score": 90, "topic": "primeiro"},
        {"segments": [{"start": 300.0, "end": 358.0}], "score": 80, "topic": "segundo"},
    ]
}


# --- select ---------------------------------------------------------------- #


def test_select_writes_candidates_and_clips(settings, seeded, monkeypatch):
    provider = CountingProvider(SELECTION)
    monkeypatch.setattr(pipeline, "build_provider", lambda *a, **k: provider, raising=False)
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: provider)

    clip_set = pipeline.select(settings, seeded, "short")
    profile_dir = settings.profile_dir(seeded, "short")

    assert (profile_dir / "candidates.json").exists()
    assert (profile_dir / "clips.json").exists()
    assert len(clip_set.clips) == 2
    assert State(settings.state_db).is_done(seeded, "select", profile="short")


def test_select_is_idempotent(settings, seeded, monkeypatch):
    provider = CountingProvider(SELECTION)
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: provider)

    pipeline.select(settings, seeded, "short")
    pipeline.select(settings, seeded, "short")
    assert provider.calls == 1


def test_force_reruns_select(settings, seeded, monkeypatch):
    provider = CountingProvider(SELECTION)
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: provider)

    pipeline.select(settings, seeded, "short")
    pipeline.select(settings, seeded, "short", force=True)
    assert provider.calls == 2


def test_select_records_a_failure(settings, seeded, monkeypatch):
    class Broken(BaseLLMProvider):
        name = "broken"

        def generate(self, system: str, user: str) -> str:
            raise RuntimeError("sem rede")

    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: Broken())
    with pytest.raises(RuntimeError):
        pipeline.select(settings, seeded, "short")
    record = State(settings.state_db).get_stage(seeded, "select", profile="short")
    assert record.status is StageStatus.FAILED


def test_profiles_do_not_share_selection(settings, seeded, monkeypatch):
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: CountingProvider(SELECTION))
    pipeline.select(settings, seeded, "short")
    state = State(settings.state_db)
    assert state.is_done(seeded, "select", profile="short")
    assert not state.is_done(seeded, "select", profile="long")


# --- guards ---------------------------------------------------------------- #


def test_select_without_a_transcript_is_actionable(settings):
    video_dir = settings.video_dir("nada")
    video_dir.mkdir(parents=True)
    with pytest.raises(ClipperError, match="clipper transcribe"):
        pipeline.select(settings, "nada", "short")


def test_render_without_a_selection_is_actionable(settings, seeded):
    with pytest.raises(ClipperError, match="clipper select"):
        pipeline.render(settings, seeded, "short")


def test_transcribe_without_ingest_is_actionable(settings):
    with pytest.raises(ClipperError, match="clipper ingest"):
        pipeline.transcribe(settings, "ausente")


def test_deliver_without_renders_is_actionable(settings, seeded, monkeypatch):
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: CountingProvider(SELECTION))
    pipeline.select(settings, seeded, "short")
    with pytest.raises(ClipperError, match="nada para entregar"):
        pipeline.deliver(settings, seeded, ["short"])


# --- renderable ------------------------------------------------------------ #


def test_rejected_clips_are_not_rendered():
    clip_set = ClipSet(
        video_id="v",
        profile="short",
        clips=[
            Clip(
                index=1,
                slug="a",
                profile="short",
                segments=[TimeRange(start=0, end=30)],
                status=ClipStatus.APPROVED,
            ),
            Clip(
                index=2,
                slug="b",
                profile="short",
                segments=[TimeRange(start=60, end=90)],
                status=ClipStatus.REJECTED,
            ),
            Clip(index=3, slug="c", profile="short", segments=[TimeRange(start=120, end=150)]),
        ],
    )
    assert [c.index for c in pipeline.renderable(clip_set)] == [1, 3]


def test_only_clip_selects_one():
    clip_set = ClipSet(
        video_id="v",
        profile="short",
        clips=[
            Clip(
                index=i,
                slug=f"c{i}",
                profile="short",
                segments=[TimeRange(start=i * 60, end=i * 60 + 30)],
            )
            for i in (1, 2, 3)
        ],
    )
    assert [c.index for c in pipeline.renderable(clip_set, 2)] == [2]
    with pytest.raises(ClipperError, match="indice 9"):
        pipeline.renderable(clip_set, 9)


# --- clean ----------------------------------------------------------------- #


def test_clean_removes_everything(settings, seeded):
    pipeline.clean_video(settings, seeded)
    assert not settings.video_dir(seeded).exists()
    assert State(settings.state_db).get_video(seeded) is None


def test_clean_keep_source_preserves_the_expensive_artefacts(settings, seeded, monkeypatch):
    monkeypatch.setattr("clipper.llm.build_provider", lambda *a, **k: CountingProvider(SELECTION))
    pipeline.select(settings, seeded, "short")

    pipeline.clean_video(settings, seeded, keep_source=True)
    video_dir = settings.video_dir(seeded)

    assert (video_dir / "source.mp4").exists()
    assert (video_dir / "transcript.json").exists()
    assert not (video_dir / "profiles").exists()

    state = State(settings.state_db)
    assert state.is_done(seeded, "transcribe")
    for stage in PROFILE_STAGES:
        assert not state.is_done(seeded, stage, profile="short")


def test_clean_on_an_unknown_video_is_an_error(settings):
    with pytest.raises(ClipperError, match="nao esta no workspace"):
        pipeline.clean_video(settings, "fantasma")


def test_render_removes_clips_from_a_previous_selection(settings, seeded):
    """A new selection makes new slugs; the old directories must not linger."""
    from clipper.models import Clip, ClipSet, TimeRange

    clips_dir = settings.profile_dir(seeded, "short") / "clips"
    for name in ("01_tema-antigo", "02_outro-antigo", "01_tema-novo"):
        (clips_dir / name).mkdir(parents=True)
        (clips_dir / name / "clip.mp4").write_bytes(b"x")

    clip_set = ClipSet(
        video_id=seeded,
        profile="short",
        clips=[
            Clip(
                index=1,
                slug="tema-novo",
                profile="short",
                segments=[TimeRange(start=0, end=30)],
            )
        ],
    )
    pipeline.prune_stale_clips(settings, seeded, "short", clip_set)

    assert sorted(p.name for p in clips_dir.iterdir()) == ["01_tema-novo"]


def test_pruning_is_a_no_op_without_a_clips_directory(settings, seeded):
    from clipper.models import ClipSet

    pipeline.prune_stale_clips(settings, seeded, "short", ClipSet(video_id=seeded, profile="short"))


# --- disk housekeeping ------------------------------------------------------ #


def test_delivered_videos_needs_every_rendered_profile_shipped(settings, seeded):
    state = State(settings.state_db)
    state.set_stage(seeded, "render", StageStatus.DONE, profile="short")
    state.set_stage(seeded, "render", StageStatus.DONE, profile="long")

    state.set_stage(seeded, "deliver", StageStatus.DONE, profile="short")
    assert pipeline.delivered_videos(settings) == []

    state.set_stage(seeded, "deliver", StageStatus.DONE, profile="long")
    assert pipeline.delivered_videos(settings) == [seeded]


def test_a_video_that_was_never_rendered_is_not_delivered(settings, seeded):
    assert pipeline.delivered_videos(settings) == []


def test_clean_delivered_removes_only_shipped_videos(settings, seeded):
    from clipper.models import SourceInfo

    state = State(settings.state_db)
    state.set_stage(seeded, "render", StageStatus.DONE, profile="short")
    state.set_stage(seeded, "deliver", StageStatus.DONE, profile="short")

    other = "vid0000002"
    settings.video_dir(other).mkdir(parents=True)
    (settings.video_dir(other) / "source.mp4").write_bytes(b"x")
    state.upsert_video(SourceInfo(video_id=other, title="T", channel="C", duration=1.0, url="u"))
    state.set_stage(other, "render", StageStatus.DONE, profile="short")

    pipeline.clean_delivered(settings)

    assert not settings.video_dir(seeded).exists()
    assert settings.video_dir(other).exists()


def test_clean_delivered_can_keep_the_source(settings, seeded):
    state = State(settings.state_db)
    state.set_stage(seeded, "render", StageStatus.DONE, profile="short")
    state.set_stage(seeded, "deliver", StageStatus.DONE, profile="short")

    pipeline.clean_delivered(settings, keep_source=True)

    assert (settings.video_dir(seeded) / "source.mp4").exists()
    assert (settings.video_dir(seeded) / "transcript.json").exists()


def test_human_readable_sizes():
    assert pipeline.human(512) == "512 B"
    assert pipeline.human(2 * 1024**2) == "2 MB"
    assert pipeline.human(3 * 1024**3) == "3 GB"


def test_clips_render_concurrently(settings, seeded, monkeypatch):
    """Rendering one clip at a time left most of the CPU idle."""
    import threading

    from clipper.models import Clip, ClipSet, TimeRange
    from clipper.stages.render import RenderResult

    clip_set = ClipSet(
        video_id=seeded,
        profile="short",
        clips=[
            Clip(
                index=i,
                slug=f"c{i}",
                profile="short",
                segments=[TimeRange(start=i * 60, end=i * 60 + 30)],
            )
            for i in range(1, 7)
        ],
    )
    pipeline.save_clips(settings, clip_set)

    peak = 0
    running = 0
    lock = threading.Lock()

    def slow_render(settings_, video_id_, clip, profile, transcript, **kwargs):
        nonlocal peak, running
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        directory = settings_.profile_dir(video_id_, profile.name) / "clips" / clip.dir_name
        return RenderResult(
            clip_dir=directory,
            video_path=directory / "clip.mp4",
            thumbnail_path=directory / "thumbnail.jpg",
            subtitles_path=None,
            offsets=[0.0],
            duration=30.0,
            width=1080,
            height=1920,
            degraded=False,
        )

    monkeypatch.setattr("clipper.stages.render.render_clip", slow_render)
    pipeline.render(settings, seeded, "short")

    assert peak > 1, "os renders rodaram em fila"


# --- unattended batch -------------------------------------------------------- #


def test_the_queue_file_ignores_comments_and_blanks(tmp_path):
    queue = tmp_path / "fila.txt"
    queue.write_text(
        "# meus videos\n"
        "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
        "\n"
        "https://youtu.be/bbbbbbbbbbb   # esse e curto\n"
        "https://www.youtube.com/watch?v=aaaaaaaaaaa\n",  # duplicate
        encoding="utf-8",
    )
    assert pipeline.read_queue(queue) == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://youtu.be/bbbbbbbbbbb",
    ]


def test_a_missing_queue_file_is_actionable(tmp_path):
    with pytest.raises(ClipperError, match="fila nao encontrado"):
        pipeline.read_queue(tmp_path / "ausente.txt")


def test_the_queue_is_reread_so_it_can_grow_while_running(settings, tmp_path, monkeypatch):
    """Appending to the file mid-run is the point of leaving a queue behind."""
    queue = tmp_path / "fila.txt"
    queue.write_text("https://www.youtube.com/watch?v=aaaaaaaaaaa\n", encoding="utf-8")
    seen: list[str] = []

    def fake_run(settings_, url, profiles, **kwargs):
        seen.append(url)
        if len(seen) == 1:
            with queue.open("a", encoding="utf-8") as handle:
                handle.write("https://www.youtube.com/watch?v=bbbbbbbbbbb\n")
        return url[-11:]

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(pipeline, "clean_video", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "directory_size", lambda _p: 0)

    results = pipeline.batch(settings, queue, ["short"])
    assert seen == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
    ]
    assert [r.status for r in results] == ["ok", "ok"]


def test_a_failure_does_not_stop_the_queue(settings, monkeypatch):
    urls = [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        "https://www.youtube.com/watch?v=ccccccccccc",
    ]

    def fake_run(settings_, url, profiles, **kwargs):
        if "bbbb" in url:
            raise RuntimeError("yt-dlp caiu")
        return url[-11:]

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(pipeline, "clean_video", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "directory_size", lambda _p: 0)

    results = pipeline.batch(settings, urls, ["short"])
    assert [r.status for r in results] == ["ok", "falhou", "ok"]
    assert "yt-dlp caiu" in results[1].detail


def test_stop_on_error_halts_the_queue(settings, monkeypatch):
    urls = [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
    ]
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    results = pipeline.batch(settings, urls, ["short"], stop_on_error=True)
    assert len(results) == 1


def test_each_video_is_cleaned_after_delivery(settings, monkeypatch):
    """Unattended, an uncleaned queue fills the disk."""
    cleaned: list[str] = []
    monkeypatch.setattr(pipeline, "run", lambda s, url, p, **k: url[-11:])
    monkeypatch.setattr(pipeline, "clean_video", lambda s, vid, **k: cleaned.append(vid))
    monkeypatch.setattr(pipeline, "directory_size", lambda _p: 1024)

    pipeline.batch(settings, ["https://www.youtube.com/watch?v=aaaaaaaaaaa"], ["short"])
    assert cleaned == ["aaaaaaaaaaa"]


def test_keep_skips_the_cleanup(settings, monkeypatch):
    cleaned: list[str] = []
    monkeypatch.setattr(pipeline, "run", lambda s, url, p, **k: url[-11:])
    monkeypatch.setattr(pipeline, "clean_video", lambda s, vid, **k: cleaned.append(vid))

    pipeline.batch(
        settings, ["https://www.youtube.com/watch?v=aaaaaaaaaaa"], ["short"], clean=False
    )
    assert cleaned == []


def test_the_queue_stops_when_the_disk_runs_low(settings, monkeypatch):
    monkeypatch.setattr(pipeline, "free_space_gb", lambda _p: 3.0)
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: "x")

    results = pipeline.batch(
        settings, ["https://www.youtube.com/watch?v=aaaaaaaaaaa"], ["short"], min_free_gb=15.0
    )
    assert results[0].status == "sem espaco"
    assert "3.0 GB" in results[0].detail


def test_an_already_delivered_video_is_skipped(settings, monkeypatch):
    """A queue re-run must not redo work that already reached the Drive."""
    video_id = "aaaaaaaaaaa"  # a real YouTube id is 11 characters
    state = State(settings.state_db)
    state.upsert_video(SourceInfo(video_id=video_id, title="T", channel="C", duration=1.0, url="u"))
    state.set_stage(video_id, "render", StageStatus.DONE, profile="short")
    state.set_stage(video_id, "deliver", StageStatus.DONE, profile="short")

    called: list[str] = []
    monkeypatch.setattr(pipeline, "run", lambda s, url, p, **k: called.append(url))

    results = pipeline.batch(settings, [f"https://www.youtube.com/watch?v={video_id}"], ["short"])
    assert called == []
    assert results[0].status == "ja entregue"


def test_an_unsupported_url_is_recorded_not_raised(settings, monkeypatch):
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: "x")
    results = pipeline.batch(settings, ["https://vimeo.com/12345"], ["short"])
    assert results[0].status == "invalido"
