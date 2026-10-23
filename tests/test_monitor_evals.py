"""Marking an eval round from the page (SPEC-agent 11.3).

The fifth write of the monitor, held to the same shape as the other four: its own
gate, one known file, and no path from the request. What is different here is that
the file is also yours to edit by hand, so the page may only touch two columns.
"""

from __future__ import annotations

import pytest

from clipper.config import load_settings
from clipper.evals.report import NOTES_NAME, read_notes, read_rows, update_rows
from clipper.models import Clip, ClipSet, ClipStatus, TimeRange
from clipper.monitor.snapshot import eval_rounds
from clipper.monitor.writes import EvalEdit, EvalMark, rounds_with_notes, save_marks
from clipper.utils.errors import InputError

VIDEO_ID = "vid0000003"

NOTES = """# Notas desta rodada

Escreva a nota de 1 a 5.

| corte | titulo | nota | comentario |
|---|---|---|---|
| `vid0000003/short/1` | [o primeiro](http://127.0.0.1:8787/api/clip?x=1) |  |  |
| `vid0000003/short/2` | o segundo | 3 | ja tinha nota |
"""


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\nevals_dir: {tmp_path / 'evals'}\n", encoding="utf-8"
    )
    return load_settings(config)


@pytest.fixture
def round_dir(settings):
    directory = settings.evals_dir / "rodada1"
    directory.mkdir(parents=True)
    (directory / NOTES_NAME).write_text(NOTES, encoding="utf-8")
    return directory


@pytest.fixture
def workspace(settings):
    """Two clips, the second one dropped and never rendered."""
    profile_dir = settings.profile_dir(VIDEO_ID, "short")
    profile_dir.mkdir(parents=True)
    clips = [
        Clip(
            index=1,
            slug="primeiro",
            profile="short",
            segments=[TimeRange(start=10.0, end=55.0)],
        ),
        Clip(
            index=2,
            slug="segundo",
            profile="short",
            segments=[TimeRange(start=80.0, end=110.0)],
            status=ClipStatus.REJECTED,
        ),
    ]
    (profile_dir / "clips.json").write_text(
        ClipSet(video_id=VIDEO_ID, profile="short", clips=clips).model_dump_json(),
        encoding="utf-8",
    )
    rendered = profile_dir / "clips" / "01_primeiro"
    rendered.mkdir(parents=True)
    (rendered / "clip.mp4").write_bytes(b"x")
    return profile_dir


# --- the file -------------------------------------------------------------- #


def test_a_row_is_read_back_with_the_link_stripped_off(round_dir):
    rows = read_rows(round_dir / NOTES_NAME)

    assert [r.clip_id for r in rows] == [f"{VIDEO_ID}/short/1", f"{VIDEO_ID}/short/2"]
    assert rows[0].title == "o primeiro"
    assert (rows[1].note, rows[1].comment) == (3, "ja tinha nota")


def test_writing_a_mark_leaves_the_rest_of_the_line_alone(round_dir):
    """The title cell is yours - a link you put there has to survive a save."""
    path = round_dir / NOTES_NAME

    update_rows(path, {f"{VIDEO_ID}/short/1": (4, "boa abertura")})

    text = path.read_text(encoding="utf-8")
    assert "[o primeiro](http://127.0.0.1:8787/api/clip?x=1)" in text
    assert "Escreva a nota de 1 a 5." in text
    assert read_notes(path) == {f"{VIDEO_ID}/short/1": 4, f"{VIDEO_ID}/short/2": 3}


def test_a_zero_takes_the_mark_back(round_dir):
    path = round_dir / NOTES_NAME

    update_rows(path, {f"{VIDEO_ID}/short/2": (0, "")})

    assert read_notes(path) == {}
    assert read_rows(path)[1].comment == ""


def test_a_comment_cannot_break_the_table(round_dir):
    path = round_dir / NOTES_NAME

    update_rows(path, {f"{VIDEO_ID}/short/1": (5, "achei | otimo\nmesmo")})

    assert read_rows(path)[0].comment == "achei / otimo mesmo"
    assert read_notes(path)[f"{VIDEO_ID}/short/1"] == 5


# --- the gate -------------------------------------------------------------- #


def test_only_a_round_that_exists_can_be_written(settings, round_dir):
    assert rounds_with_notes(settings) == ["rodada1"]

    path, written = save_marks(
        settings,
        EvalEdit(round="rodada1", marks=[EvalMark(clip_id=f"{VIDEO_ID}/short/1", note=5)]),
    )

    assert path == round_dir / NOTES_NAME and written == 1


@pytest.mark.parametrize("name", ["outra", "../../etc", "rodada1/..", ".", "evals"])
def test_a_round_name_is_matched_against_disk_not_joined(settings, round_dir, name):
    with pytest.raises(InputError, match="rodada desconhecida"):
        save_marks(settings, EvalEdit(round=name, marks=[]))


def test_a_clip_outside_the_round_is_refused(settings, round_dir):
    with pytest.raises(InputError, match="nao estao em"):
        save_marks(
            settings, EvalEdit(round="rodada1", marks=[EvalMark(clip_id="outro/short/9", note=1)])
        )


@pytest.mark.parametrize("note", [-1, 6, 99])
def test_a_mark_outside_one_to_five_is_refused(note):
    with pytest.raises(ValueError, match="note"):
        EvalMark(clip_id=f"{VIDEO_ID}/short/1", note=note)


def test_no_rounds_means_nothing_to_write(settings):
    assert rounds_with_notes(settings) == []
    with pytest.raises(InputError, match="nenhuma"):
        save_marks(settings, EvalEdit(round="rodada1", marks=[]))


# --- what the page is given ------------------------------------------------ #


def test_the_round_reaches_the_page_with_the_clips_resolved(settings, round_dir, workspace):
    rounds = eval_rounds(settings)

    assert [r.name for r in rounds] == ["rodada1"]
    first, second = rounds[0].clips
    assert (first.dir_name, first.playable, first.dropped) == ("01_primeiro", True, False)
    assert round(first.duration) == 45
    # Rendered once and then taken out: marking it says whether that was right.
    assert (second.dir_name, second.playable, second.dropped) == ("02_segundo", False, True)
    assert (rounds[0].marked, rounds[0].mean_note) == (1, 3.0)


def test_a_clip_whose_workspace_was_cleaned_still_shows_up(settings, round_dir):
    """No clips.json: the row is still yours to mark, it just cannot be played."""
    clip = eval_rounds(settings)[0].clips[0]

    assert clip.clip_id == f"{VIDEO_ID}/short/1"
    assert clip.title == "o primeiro"
    assert clip.dir_name == "" and clip.playable is False


def test_a_directory_without_notes_is_not_a_round(settings, round_dir):
    (settings.evals_dir / "so-um-relatorio").mkdir()

    assert [r.name for r in eval_rounds(settings)] == ["rodada1"]


def test_the_route_is_declared_as_a_write(settings):
    from clipper.monitor.server import build_server

    server = build_server(settings, host="127.0.0.1", port=0)
    try:
        assert "/api/evals" in server.RequestHandlerClass.WRITE_ROUTES
    finally:
        server.server_close()
