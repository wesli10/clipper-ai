"""What the page is allowed to write (SPEC-agent 9.5).

The monitor stopped being read-only, so each of the four writes gets its own
gate, writes one known file, and takes no path from the request.
"""

from __future__ import annotations

import json

import pytest
import yaml

from clipper.agent.channel import ChannelProfile, TitleStyle, load_learned, resolve
from clipper.agent.control import SessionControl, control_path, read_control, write_control
from clipper.config import load_settings
from clipper.monitor.writes import (
    ChannelEdit,
    ControlEdit,
    QueueEdit,
    channel_view,
    read_queue,
    save_channel,
    save_control,
    write_queue,
)
from clipper.utils.errors import InputError

VIDEO = "https://www.youtube.com/watch?v=RfCT-LJkfx8"
OTHER = "https://www.youtube.com/watch?v=qH2SB1A2h7U"


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "channels").mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\nchannels_dir: {tmp_path / 'channels'}\n", encoding="utf-8"
    )
    return load_settings(config)


# --- queue ----------------------------------------------------------------- #


def test_the_queue_can_be_rewritten_in_the_order_the_page_asks(tmp_path):
    path = tmp_path / "fila.txt"
    path.write_text(f"# minha fila\n\n{VIDEO}\n{OTHER}\n", encoding="utf-8")

    urls = write_queue(path, QueueEdit(urls=[OTHER, VIDEO]))

    assert urls == [OTHER, VIDEO]
    assert read_queue(path) == [OTHER, VIDEO]


def test_the_header_of_the_file_survives_a_reorder(tmp_path):
    """It explains what the file is; losing it to a drag-and-drop is a poor trade."""
    path = tmp_path / "fila.txt"
    path.write_text(f"# minha fila\n# segunda linha\n{VIDEO}\n", encoding="utf-8")

    write_queue(path, QueueEdit(urls=[OTHER]))

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# minha fila\n# segunda linha\n")
    assert VIDEO not in text and OTHER in text


def test_a_queue_that_never_existed_gets_the_default_header(tmp_path):
    path = tmp_path / "fila.txt"
    write_queue(path, QueueEdit(urls=[VIDEO]))
    assert path.read_text(encoding="utf-8").startswith("# Fila do clipper.")


def test_something_that_is_not_a_video_url_is_refused(tmp_path):
    path = tmp_path / "fila.txt"
    with pytest.raises(InputError, match="nenhuma fonte"):
        write_queue(path, QueueEdit(urls=["ftp://exemplo.com/x.mp4"]))
    assert not path.exists()


def test_the_same_video_twice_only_goes_once(tmp_path):
    path = tmp_path / "fila.txt"
    assert write_queue(path, QueueEdit(urls=[VIDEO, VIDEO, OTHER])) == [VIDEO, OTHER]


def test_emptying_the_queue_is_allowed(tmp_path):
    path = tmp_path / "fila.txt"
    path.write_text(f"{VIDEO}\n", encoding="utf-8")
    assert write_queue(path, QueueEdit(urls=[])) == []
    assert read_queue(path) == []


# --- channels -------------------------------------------------------------- #


def a_channel(**extra) -> ChannelProfile:
    fields = {
        "id": "meucanal",
        "name": "Meu Canal",
        "profiles": ["short"],
        "title": TitleStyle(tone="direto", hashtags=["#meucanal"]),
        **extra,
    }
    return ChannelProfile(**fields)


def test_a_channel_written_from_the_page_reads_back_as_a_channel(settings):
    save_channel(settings, "meucanal", ChannelEdit(profile=a_channel()))

    resolved = resolve(settings, "meucanal")
    assert resolved is not None
    assert resolved.profile.name == "Meu Canal"
    assert resolved.profile.title.hashtags == ["#meucanal"]


def test_the_file_says_that_editing_from_the_page_costs_the_comments(settings):
    path = save_channel(settings, "meucanal", ChannelEdit(profile=a_channel()))
    assert "comentarios" in path.read_text(encoding="utf-8").splitlines()[1]
    # And it is real YAML, not a blob the browser sent.
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["id"] == "meucanal"


def test_the_id_in_the_url_wins_over_whatever_the_form_carried(settings):
    save_channel(settings, "correto", ChannelEdit(profile=a_channel()))
    assert (settings.channels_dir / "correto.yaml").exists()
    assert resolve(settings, "correto") is not None


@pytest.mark.parametrize("bad", ["../fora", "COM MAIUSCULA", "com espaco", "", "x" * 60])
def test_an_id_that_is_not_a_filename_is_refused(settings, bad):
    with pytest.raises(InputError, match="id de canal invalido"):
        save_channel(settings, bad, ChannelEdit(profile=a_channel()))


def test_a_render_default_outside_the_whitelist_is_refused(settings):
    profile = a_channel(render_defaults={"short": {"output_width": 720}})
    with pytest.raises(ValueError, match="output_width"):
        save_channel(settings, "meucanal", ChannelEdit(profile=profile))


def test_the_editor_gets_the_configuration_and_the_notes_apart(settings):
    from clipper.agent.channel import LearnedNote, remember

    save_channel(settings, "meucanal", ChannelEdit(profile=a_channel()))
    channel = resolve(settings, "meucanal")
    assert channel is not None
    remember(settings, channel, [LearnedNote(note="algo que o agent observou")])

    view = channel_view(settings, "meucanal")
    assert view["profile"]["name"] == "Meu Canal"
    assert view["learned"]["entries"][0]["note"] == "algo que o agent observou"
    assert view["path"].endswith("meucanal.yaml")


def test_saving_a_channel_does_not_touch_what_the_agent_learned(settings):
    from clipper.agent.channel import LearnedNote, remember

    save_channel(settings, "meucanal", ChannelEdit(profile=a_channel()))
    channel = resolve(settings, "meucanal")
    assert channel is not None
    remember(settings, channel, [LearnedNote(note="anotacao antiga")])

    save_channel(settings, "meucanal", ChannelEdit(profile=a_channel(name="Outro nome")))

    assert load_learned(settings, "meucanal").entries[0].note == "anotacao antiga"


# --- session control ------------------------------------------------------- #


@pytest.fixture
def a_run(settings):
    directory = settings.agent_dir("vid0000001")
    directory.mkdir(parents=True)
    (directory / "20260911-120000-vid.jsonl").write_text("{}\n", encoding="utf-8")
    return "vid0000001", "20260911-120000-vid"


def test_pausing_a_run_writes_the_order_next_to_its_log(settings, a_run):
    video_id, run_id = a_run
    control = save_control(settings, ControlEdit(video_id=video_id, run_id=run_id, action="pause"))

    assert control.paused and control.at > 0
    path = control_path(settings.agent_dir(video_id), run_id)
    assert read_control(path).action == "pause"


def test_a_run_that_does_not_exist_cannot_be_ordered_about(settings, a_run):
    video_id, _ = a_run
    with pytest.raises(InputError, match="nao existe"):
        save_control(settings, ControlEdit(video_id=video_id, run_id="20260911-999999-x"))


def test_an_action_nobody_defined_is_refused(settings, a_run):
    video_id, run_id = a_run
    with pytest.raises(InputError, match="acao desconhecida"):
        save_control(settings, ControlEdit(video_id=video_id, run_id=run_id, action="explodir"))


def test_vetoes_pile_up_instead_of_replacing_each_other(settings, a_run):
    """A clip you refused stays refused; dropping one silently would be worse."""
    video_id, run_id = a_run
    save_control(
        settings, ControlEdit(video_id=video_id, run_id=run_id, vetoed=[f"{video_id}/short/1"])
    )
    control = save_control(
        settings, ControlEdit(video_id=video_id, run_id=run_id, vetoed=[f"{video_id}/short/2"])
    )

    assert control.vetoed == [f"{video_id}/short/1", f"{video_id}/short/2"]


def test_a_control_file_half_written_reads_as_no_order_at_all(tmp_path):
    path = tmp_path / "run.control.json"
    path.write_text('{"action": "pau', encoding="utf-8")
    assert read_control(path).action == "run"


def test_a_missing_control_file_is_simply_no_order(tmp_path):
    assert read_control(tmp_path / "nada.json").action == "run"


def test_the_written_order_round_trips(tmp_path):
    path = tmp_path / "run.control.json"
    written = write_control(path, SessionControl(action="stop", publish_on_stop=False))
    read = read_control(path)
    assert read.stopping and read.publish_on_stop is False
    assert json.loads(path.read_text(encoding="utf-8"))["at"] == written.at
