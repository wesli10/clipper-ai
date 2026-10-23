"""The publishing queue - SPEC 8.1.

One folder per day in the Drive, the destination inside it, and the date of each
clip decided once and kept in `state.db`.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from clipper import pipeline
from clipper.config import ConfigError, load_settings
from clipper.models import Clip, ClipMetadata, ClipSet, ClipStatus, SourceInfo, TimeRange
from clipper.stages.queue import (
    Calendar,
    destination_folder,
    queue_path,
    stage_days,
    unique_filename,
)
from clipper.state import State

VIDEO_ID = "fila0000001"
TOMORROW = date.today() + timedelta(days=1)


def metadata(profile: str, title: str, chapters=()) -> ClipMetadata:
    return ClipMetadata(
        profile=profile,
        title=title,
        hook_line="gancho",
        description="Uma descricao.",
        chapters=list(chapters),
        hashtags=["#um"],
        tags=["tag"],
        thumbnail_text="TEXTO",
        thumbnail_concept="um conceito",
        source_url=f"https://www.youtube.com/watch?v={VIDEO_ID}&t=10s",
        source_segments=[(10.0, 70.0)],
        language="pt",
    )


# --- destination folders --------------------------------------------------- #


def test_one_destination_reads_as_an_instruction():
    assert destination_folder(["tiktok"]) == "so-tiktok"


def test_two_destinations_are_joined():
    assert destination_folder(["youtube", "tiktok"]) == "youtube-e-tiktok"


def test_a_clip_with_no_destination_is_visible_not_lost():
    assert destination_folder([]) == "sem-destino"


def test_the_queue_path_is_day_then_destination():
    assert queue_path(date(2026, 9, 13), ["tiktok"], "t.mp4") == "2026-09-13/so-tiktok/t.mp4"


# --- filenames ------------------------------------------------------------- #


def test_a_repeated_title_does_not_overwrite_the_other_clip():
    assert unique_filename("Titulo.mp4", ["Titulo.mp4"]) == "Titulo (2).mp4"
    assert unique_filename("Titulo.mp4", ["Titulo.mp4", "Titulo (2).mp4"]) == "Titulo (3).mp4"


def test_the_check_ignores_case_because_drive_does():
    assert unique_filename("Titulo.mp4", ["titulo.mp4"]) == "Titulo (2).mp4"


# --- the calendar ---------------------------------------------------------- #


def test_a_day_takes_per_day_shorts_and_then_moves_on():
    calendar = Calendar()
    days = []
    for _ in range(5):
        day = calendar.next_free(
            is_daily=True, start=date(2026, 9, 13), per_day=2, long_every_days=7
        )
        calendar.take(day, is_daily=True)
        days.append(day)
    assert days == [
        date(2026, 9, 13),
        date(2026, 9, 13),
        date(2026, 9, 14),
        date(2026, 9, 14),
        date(2026, 9, 15),
    ]


def test_longs_keep_a_week_from_each_other():
    calendar = Calendar()
    first = calendar.next_free(
        is_daily=False, start=date(2026, 9, 13), per_day=4, long_every_days=7
    )
    calendar.take(first, is_daily=False)
    second = calendar.next_free(
        is_daily=False, start=date(2026, 9, 13), per_day=4, long_every_days=7
    )
    assert (first, second) == (date(2026, 9, 13), date(2026, 9, 20))


def test_a_long_does_not_use_up_a_shorts_slot():
    calendar = Calendar()
    calendar.take(date(2026, 9, 13), is_daily=False)
    assert calendar.next_free(
        is_daily=True, start=date(2026, 9, 13), per_day=1, long_every_days=7
    ) == date(2026, 9, 13)


# --- staging --------------------------------------------------------------- #


def test_staging_links_the_render_under_its_title(tmp_path):
    render = tmp_path / "clips" / "01_corte" / "clip.mp4"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"video")
    entry = _queued(date(2026, 9, 13), ["youtube", "tiktok"], "Meu corte.mp4")

    staged = stage_days(tmp_path / "staging", [(entry, render)], {date(2026, 9, 13): "# plano"})

    target = tmp_path / "staging" / "2026-09-13" / "youtube-e-tiktok" / "Meu corte.mp4"
    assert target.read_bytes() == b"video"
    assert target.stat().st_ino == render.stat().st_ino, "deve ser hard link, nao copia"
    assert (tmp_path / "staging" / "2026-09-13" / "PUBLICAR.md").read_text() == "# plano"
    assert [day.when for day in staged] == [date(2026, 9, 13)]


def test_staging_a_day_that_only_needs_a_new_plan(tmp_path):
    staged = stage_days(tmp_path / "staging", [], {date(2026, 9, 13): "# plano"})
    assert [day.when for day in staged] == [date(2026, 9, 13)]
    assert (tmp_path / "staging" / "2026-09-13" / "PUBLICAR.md").exists()


def test_staging_without_the_render_is_an_error(tmp_path):
    entry = _queued(date(2026, 9, 13), ["tiktok"], "Meu corte.mp4")
    with pytest.raises(FileNotFoundError):
        stage_days(tmp_path / "staging", [(entry, tmp_path / "nao-existe.mp4")], {})


def _queued(when, destinations, filename):
    from clipper.state import QueuedClip

    return QueuedClip(
        video_id=VIDEO_ID,
        profile="short",
        clip_index=1,
        publish_date=when,
        destinations=tuple(destinations),
        filename=filename,
        channel="canal",
        archive_path="Clipper/arquivo/canal/v/shorts/01_corte",
        metadata=metadata("short", "Meu corte"),
        created_at="2026-09-12T00:00:00+00:00",
    )


# --- destinations from the config ------------------------------------------ #


def _settings_with(tmp_path, extra: str = ""):
    drive = tmp_path / "drive"
    drive.mkdir(exist_ok=True)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\ndeliver:\n  drive_root: {drive}\n{extra}",
        encoding="utf-8",
    )
    return load_settings(config)


DESTINATIONS = """publish:
  per_day: 2
  destinations:
    default: [tiktok]
    canal: [youtube, tiktok]
    canal/long: [youtube]
"""


def test_the_most_specific_destination_key_wins(tmp_path):
    publish = _settings_with(tmp_path, DESTINATIONS).publish
    assert publish.destinations_for("canal", "long") == ["youtube"]
    assert publish.destinations_for("canal", "short") == ["youtube", "tiktok"]
    assert publish.destinations_for("outro", "short") == ["tiktok"]


def test_a_destination_that_is_not_a_folder_name_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        _settings_with(tmp_path, "publish:\n  destinations:\n    default: [Tik Tok]\n")


# --- the queue in state.db ------------------------------------------------- #


def test_a_queued_clip_keeps_the_day_it_already_has(tmp_path):
    state = State(tmp_path / "state.db")
    first = state.queue_clip(
        VIDEO_ID,
        "short",
        1,
        publish_date=date(2026, 9, 13),
        destinations=["tiktok"],
        filename="a.mp4",
        metadata=metadata("short", "A"),
    )
    again = state.queue_clip(
        VIDEO_ID,
        "short",
        1,
        publish_date=date(2026, 9, 20),
        destinations=["youtube"],
        filename="b.mp4",
        metadata=metadata("short", "A"),
    )
    assert again == first
    assert [c.publish_date for c in state.queue()] == [date(2026, 9, 13)]


def test_cleaning_the_workspace_does_not_empty_the_queue(tmp_path):
    state = State(tmp_path / "state.db")
    state.upsert_video(SourceInfo(video_id=VIDEO_ID, title="t", channel="c", duration=1.0, url="u"))
    state.queue_clip(
        VIDEO_ID,
        "short",
        1,
        publish_date=date(2026, 9, 13),
        destinations=["tiktok"],
        filename="a.mp4",
        metadata=metadata("short", "A"),
    )
    state.delete_video(VIDEO_ID)
    assert len(state.queue()) == 1, "a fila descreve o Drive, nao o workspace"


def test_the_queue_can_be_read_from_a_date_and_by_channel(tmp_path):
    state = State(tmp_path / "state.db")
    for index, (day, channel) in enumerate(
        [(date(2026, 9, 10), "a"), (date(2026, 9, 20), "a"), (date(2026, 9, 21), "b")], start=1
    ):
        state.queue_clip(
            VIDEO_ID,
            "short",
            index,
            publish_date=day,
            destinations=["tiktok"],
            filename=f"{index}.mp4",
            channel=channel,
            metadata=metadata("short", f"T{index}"),
        )
    assert len(state.queue(since=date(2026, 9, 15))) == 2
    assert len(state.queue(channel="a")) == 2
    assert len(state.queue(since=date(2026, 9, 15), channel="b")) == 1


# --- the delivery, end to end in folder mode -------------------------------- #


@pytest.fixture
def packaged(tmp_path):
    """A workspace with three rendered and packaged shorts, and a fake Drive."""
    settings = _settings_with(tmp_path, DESTINATIONS)
    state = State(settings.state_db)
    state.upsert_video(
        SourceInfo(
            video_id=VIDEO_ID,
            title="Video de teste",
            channel="Canal",
            duration=600.0,
            url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
            upload_date="20260115",
            width=1920,
            height=1080,
        )
    )
    clips = []
    for index in (1, 2, 3):
        clip = Clip(
            index=index,
            slug=f"corte-{index}",
            profile="short",
            segments=[TimeRange(start=0.0, end=50.0)],
            status=ClipStatus.APPROVED,
        )
        clips.append(clip)
        clip_dir = settings.profile_dir(VIDEO_ID, "short") / "clips" / clip.dir_name
        clip_dir.mkdir(parents=True)
        (clip_dir / "clip.mp4").write_bytes(f"video {index}".encode())
        (clip_dir / "metadata.json").write_text(
            metadata("short", f"Corte {index}").model_dump_json(), encoding="utf-8"
        )
    pipeline.save_clips(settings, ClipSet(video_id=VIDEO_ID, profile="short", clips=clips))
    return settings


def _queue_root(settings):
    return settings.deliver.drive_path / "Clipper" / "publicar"


def test_delivery_fills_the_day_folders_by_destination(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    first = _queue_root(packaged) / TOMORROW.isoformat() / "youtube-e-tiktok"
    second = _queue_root(packaged) / (TOMORROW + timedelta(days=1)).isoformat()
    assert sorted(p.name for p in first.glob("*.mp4")) == ["Corte 1.mp4", "Corte 2.mp4"]
    assert sorted(p.name for p in (second / "youtube-e-tiktok").glob("*.mp4")) == ["Corte 3.mp4"]
    assert (first / "Corte 1.mp4").read_bytes() == b"video 1"


def test_the_day_carries_the_plan_of_that_day_only(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    plan = (_queue_root(packaged) / TOMORROW.isoformat() / "PUBLICAR.md").read_text(
        encoding="utf-8"
    )
    assert f"Publicar em {TOMORROW.isoformat()}" in plan
    assert "**destino:** youtube + tiktok" in plan
    assert "Corte 1" in plan and "Corte 2" in plan
    assert "Corte 3" not in plan, "o plano do dia so fala dos cortes do dia"


def test_no_video_reaches_the_archive_but_the_metadata_does(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    archive = packaged.deliver.drive_path / "Clipper" / "arquivo"
    assert list(archive.rglob("*.mp4")) == []
    assert len(list(archive.rglob("metadata.json"))) == 3
    assert (archive / "canal" / "2026-01-15_video-de-teste" / "INDEX.md").exists()


def test_delivering_twice_keeps_every_clip_on_its_day(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    before = [(c.filename, c.publish_date) for c in State(packaged.state_db).queue()]

    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    assert [(c.filename, c.publish_date) for c in State(packaged.state_db).queue()] == before


def test_the_staging_tree_is_not_left_behind(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    assert not (packaged.workspace.expanduser() / "queue-staging").exists()


def test_a_clip_with_no_destination_goes_to_a_visible_folder(packaged):
    """A missing rule must not silently lose a clip."""
    packaged.publish.destinations.clear()
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    assert list(_queue_root(packaged).rglob("sem-destino/*.mp4"))
    plan = (_queue_root(packaged) / TOMORROW.isoformat() / "PUBLICAR.md").read_text(
        encoding="utf-8"
    )
    assert "**destino:** nenhum (ajuste publish.destinations)" in plan


def test_publish_plan_is_written_from_the_queue(packaged, tmp_path):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    output = tmp_path / "PLANO.md"

    pipeline.publish_plan(packaged, output=output)

    plan = output.read_text(encoding="utf-8")
    assert f"publicar/{TOMORROW.isoformat()}/youtube-e-tiktok/Corte 1.mp4" in plan
    assert "Corte 3" in plan


def test_publish_plan_says_what_to_do_when_the_queue_is_empty(packaged, tmp_path):
    from clipper.utils.errors import ClipperError

    with pytest.raises(ClipperError, match="queue-migrate"):
        pipeline.publish_plan(packaged, output=tmp_path / "PLANO.md")


# --- adopting a delivery made before the queue existed ---------------------- #


@pytest.fixture
def old_delivery(tmp_path):
    """The Drive as `deliver` used to write it: one folder per clip, video inside."""
    settings = _settings_with(tmp_path, DESTINATIONS)
    video = settings.deliver.drive_path / "Clipper" / "canal" / "2026-01-15_video-de-teste"
    (video / "INDEX.md").parent.mkdir(parents=True)
    (video / "INDEX.md").write_text("# indice", encoding="utf-8")
    for profile_folder, folder, title, video_name in [
        ("shorts", "01_corte-1", "Corte 1", "Corte 1.mp4"),
        ("shorts", "02_corte-2", "Corte 2", "clip.mp4"),
        ("shorts", "03_corte-3", "Corte 3", None),
        ("longs", "01_longo", "Um longo", "Um longo.mp4"),
    ]:
        clip_dir = video / profile_folder / folder
        clip_dir.mkdir(parents=True)
        profile = "short" if profile_folder == "shorts" else "long"
        (clip_dir / "metadata.json").write_text(
            metadata(profile, title).model_dump_json(), encoding="utf-8"
        )
        if video_name is None:
            # pulled up into the profile folder by hand, which is the navigation
            # this change exists to undo
            (video / profile_folder / f"{title}.mp4").write_bytes(title.encode())
        else:
            (clip_dir / video_name).write_bytes(title.encode())
    return settings


def test_the_migration_shows_the_plan_and_moves_nothing(old_delivery):
    moves = pipeline.queue_migrate(old_delivery)

    assert len(moves) == 4
    assert not _queue_root(old_delivery).exists()
    assert State(old_delivery.state_db).queue() == []


def test_the_migration_moves_the_videos_into_the_day_folders(old_delivery):
    pipeline.queue_migrate(old_delivery, apply=True)

    day = _queue_root(old_delivery) / TOMORROW.isoformat()
    assert sorted(p.name for p in (day / "youtube-e-tiktok").glob("*.mp4")) == [
        "Corte 1.mp4",
        "Corte 2.mp4",
    ]
    # the long only goes to YouTube, and 16:9 has its own folder for it
    assert [p.name for p in (day / "so-youtube").glob("*.mp4")] == ["Um longo.mp4"]
    assert (day / "PUBLICAR.md").exists()
    # the clip whose video had been dragged up by hand came along too
    second = _queue_root(old_delivery) / (TOMORROW + timedelta(days=1)).isoformat()
    assert [p.name for p in (second / "youtube-e-tiktok").glob("*.mp4")] == ["Corte 3.mp4"]


def test_the_migration_renames_an_old_clip_mp4_to_its_title(old_delivery):
    pipeline.queue_migrate(old_delivery, apply=True)
    day = _queue_root(old_delivery) / TOMORROW.isoformat() / "youtube-e-tiktok"
    assert (day / "Corte 2.mp4").read_bytes() == b"Corte 2"


def test_the_migration_sends_the_rest_to_the_archive(old_delivery):
    pipeline.queue_migrate(old_delivery, apply=True)

    root = old_delivery.deliver.drive_path / "Clipper"
    assert not (root / "canal").exists(), "a arvore antiga nao fica na raiz"
    archived = root / "arquivo" / "canal" / "2026-01-15_video-de-teste"
    assert (archived / "INDEX.md").exists()
    assert len(list(archived.rglob("metadata.json"))) == 4
    assert list(archived.rglob("*.mp4")) == []


def test_the_migration_fills_the_queue_in_state(old_delivery):
    pipeline.queue_migrate(old_delivery, apply=True)

    queue = State(old_delivery.state_db).queue()
    # ordered the way the queue is read: by day, then format, then clip number
    assert [c.filename for c in queue] == [
        "Um longo.mp4",
        "Corte 1.mp4",
        "Corte 2.mp4",
        "Corte 3.mp4",
    ]
    assert {c.channel for c in queue} == {"canal"}
    assert queue[0].archive_path.startswith("Clipper/arquivo/canal/")


def test_the_migration_leaves_what_is_already_published_in_the_archive(old_delivery):
    pipeline.queue_migrate(old_delivery, skip=2, apply=True)

    queue = State(old_delivery.state_db).queue()
    assert [c.metadata.title for c in queue] == ["Um longo", "Corte 3"]
    archived = old_delivery.deliver.drive_path / "Clipper" / "arquivo" / "canal"
    assert sorted(p.name for p in archived.rglob("*.mp4")) == ["Corte 1.mp4", "clip.mp4"]


def test_the_migration_can_leave_a_whole_video_out(old_delivery):
    """The clips already published are not always the first ones of the plan."""
    pipeline.queue_migrate(old_delivery, skip_videos=["video-de-teste"], apply=True)

    assert State(old_delivery.state_db).queue() == []
    assert not _queue_root(old_delivery).exists()


def test_running_the_migration_again_finds_nothing_new(old_delivery):
    pipeline.queue_migrate(old_delivery, apply=True)
    before = State(old_delivery.state_db).queue()

    assert pipeline.queue_migrate(old_delivery, apply=True) == []
    assert State(old_delivery.state_db).queue() == before


# --- taking a clip out of the queue ---------------------------------------- #


def test_a_clip_can_be_taken_out_of_the_queue_by_video_id(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    removed = pipeline.queue_remove(packaged, [VIDEO_ID])

    assert len(removed) == 3
    assert State(packaged.state_db).queue() == []


def test_taking_one_out_by_part_of_its_title_leaves_the_rest(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])

    pipeline.queue_remove(packaged, ["Corte 2"])

    assert [c.metadata.title for c in State(packaged.state_db).queue()] == ["Corte 1", "Corte 3"]


def test_removing_does_not_touch_the_file_in_the_drive(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    pipeline.queue_remove(packaged, ["Corte 1"])

    day = _queue_root(packaged) / TOMORROW.isoformat() / "youtube-e-tiktok"
    assert (day / "Corte 1.mp4").exists(), "quem apaga do Drive e voce, nao o comando"


def test_removing_something_that_is_not_in_the_queue_says_so(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    assert pipeline.queue_remove(packaged, ["nao existe"]) == []
    assert len(State(packaged.state_db).queue()) == 3


def test_replan_rewrites_the_day_plan_after_a_removal(packaged):
    pipeline.deliver(packaged, VIDEO_ID, ["short"])
    pipeline.queue_remove(packaged, ["Corte 2"])

    pipeline.queue_replan(packaged)

    plan = (_queue_root(packaged) / TOMORROW.isoformat() / "PUBLICAR.md").read_text(
        encoding="utf-8"
    )
    assert "Corte 1" in plan
    assert "Corte 2" not in plan, "o plano do dia acompanha a fila"
