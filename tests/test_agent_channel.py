"""The publishing channel (SPEC-agent 7).

Two halves that never mix: the YAML is yours and the agent cannot reach it; the
notes are the agent's and you can delete them whole.
"""

from __future__ import annotations

import json

import pytest

from clipper.agent import tools
from clipper.agent.channel import (
    MAX_NOTES,
    MAX_NOTES_PER_RUN,
    LearnedNote,
    as_prompt,
    available,
    learned_path,
    load_learned,
    remember,
    resolve,
)
from clipper.agent.tools.base import ToolContext
from clipper.config import load_settings
from clipper.utils.errors import InputError

CHANNEL = """
id: meucanal
name: "Meu Canal"
language: pt-BR
profiles: [short]
weekly_target: {short: 5}
title:
  tone: "direto, em minusculas"
  max_chars: 60
  avoid: ["VOCE NAO VAI ACREDITAR"]
  examples: ["o que ninguem te conta sobre ser CLT"]
editorial:
  prefer: ["historia pessoal com desfecho"]
  avoid: ["jaba"]
render_defaults:
  short:
    tail_roll_s: 2.0
"""


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "channels").mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\nchannels_dir: {tmp_path / 'channels'}\n", encoding="utf-8"
    )
    return load_settings(config)


@pytest.fixture
def channel(settings):
    (settings.channels_dir / "meucanal.yaml").write_text(CHANNEL, encoding="utf-8")
    resolved = resolve(settings, "meucanal")
    assert resolved is not None
    return resolved


def call(ctx, name, **payload):
    return tools.get(name).call(ctx, payload)


def answer(ctx, name, **payload):
    result = call(ctx, name, **payload)
    assert not result.is_error, result.text
    return json.loads(result.text)


# --- loading --------------------------------------------------------------- #


def test_a_channel_reads_back_as_configured(channel):
    assert channel.id == "meucanal"
    assert channel.profile.profiles == ["short"]
    assert channel.profile.title.max_chars == 60
    assert channel.overrides_for("short") == {"tail_roll_s": 2.0}
    assert channel.overrides_for("quiz") == {}


def test_one_channel_is_used_without_being_named(settings, channel):
    assert available(settings) == ["meucanal"]
    assert resolve(settings) is not None


def test_with_several_channels_the_run_must_say_which(settings, channel):
    (settings.channels_dir / "outro.yaml").write_text("id: outro\n", encoding="utf-8")
    assert resolve(settings) is None
    assert resolve(settings, "outro") is not None


def test_no_channel_at_all_is_a_valid_run(settings):
    assert resolve(settings) is None


def test_a_channel_that_does_not_exist_says_which_ones_do(settings, channel):
    with pytest.raises(InputError, match="meucanal"):
        resolve(settings, "naoexiste")


def test_a_field_the_channel_may_not_set_is_refused(settings):
    (settings.channels_dir / "mau.yaml").write_text(
        "id: mau\noutput_width: 720\n", encoding="utf-8"
    )
    with pytest.raises(InputError, match="invalido"):
        resolve(settings, "mau")


# --- learned notes --------------------------------------------------------- #


def test_notes_are_stamped_with_the_run_that_saw_them(settings, channel):
    remember(
        settings,
        channel,
        [LearnedNote(note="tail_roll de 2s evita invadir o assunto seguinte", kind="render")],
        run_id="20260911-120000-vid",
        video_id="vid",
    )
    stored = load_learned(settings, "meucanal")
    assert stored.entries[0].run_id == "20260911-120000-vid"
    assert stored.entries[0].video_id == "vid" and stored.entries[0].at
    assert stored.updated_at


def test_a_run_may_only_add_so_many_notes(settings, channel):
    added = remember(
        settings,
        channel,
        [LearnedNote(note=f"nota numero {i}") for i in range(MAX_NOTES_PER_RUN + 4)],
    )
    assert added == MAX_NOTES_PER_RUN


def test_the_oldest_notes_fall_off_the_end(settings, channel):
    for index in range(MAX_NOTES // MAX_NOTES_PER_RUN + 3):
        remember(
            settings,
            channel,
            [LearnedNote(note=f"lote {index} nota {n}") for n in range(MAX_NOTES_PER_RUN)],
        )
    entries = load_learned(settings, "meucanal").entries
    assert len(entries) == MAX_NOTES
    assert entries[-1].note.startswith("lote 12")


def test_notes_the_agent_scribbled_over_do_not_fail_a_run(settings, channel):
    learned_path(settings, "meucanal").parent.mkdir(parents=True, exist_ok=True)
    learned_path(settings, "meucanal").write_text("{nao e json", encoding="utf-8")
    assert load_learned(settings, "meucanal").entries == []


def test_deleting_the_notes_loses_nothing_of_yours(settings, channel):
    remember(settings, channel, [LearnedNote(note="uma observacao qualquer")])
    learned_path(settings, "meucanal").unlink()

    again = resolve(settings, "meucanal")
    assert again is not None
    assert again.learned.entries == []
    assert again.profile.title.tone == "direto, em minusculas"


# --- what the model reads -------------------------------------------------- #


def test_the_prompt_block_carries_tone_and_what_was_learned(settings, channel):
    remember(settings, channel, [LearnedNote(note="cortes de mesa emendam assunto", kind="render")])
    reloaded = resolve(settings, "meucanal")
    text = as_prompt(reloaded)

    assert "Meu Canal" in text and "direto, em minusculas" in text
    assert "historia pessoal com desfecho" in text
    assert "[render] cortes de mesa emendam assunto" in text


def test_a_guess_is_labelled_as_one(settings, channel):
    remember(
        settings,
        channel,
        [LearnedNote(note="talvez funcione melhor a noite", confidence="assumed")],
    )
    assert "(suposicao)" in as_prompt(resolve(settings, "meucanal"))


def test_no_channel_means_no_block():
    assert as_prompt(None) == ""


# --- the tools ------------------------------------------------------------- #


def test_the_agent_reads_the_channel_it_was_given(settings, channel):
    ctx = ToolContext(settings=settings, channel=channel, enabled_profiles=("short",))
    payload = answer(ctx, "load_channel_profile")

    assert payload["channel"] == "meucanal"
    assert payload["title_max_chars"] == 60
    assert payload["render_defaults"] == {"short": {"tail_roll_s": 2.0}}


def test_without_a_channel_the_tool_says_so_instead_of_inventing_one(settings):
    ctx = ToolContext(settings=settings)
    result = call(ctx, "load_channel_profile")
    assert result.is_error and "nao invente" in result.text


def test_the_agent_can_append_notes_but_not_touch_the_configuration(settings, channel):
    ctx = ToolContext(settings=settings, run_id="20260911-120000-vid", channel=channel)
    payload = answer(
        ctx,
        "update_channel_profile",
        video_id="vid",
        notes=[{"note": "muita piada boa dura 8 a 20s neste canal", "kind": "editorial"}],
    )

    assert payload["added"] == 1
    assert load_learned(settings, "meucanal").entries[0].kind == "editorial"
    # The YAML is untouched: the agent has no way to reach it.
    assert "muita piada" not in (settings.channels_dir / "meucanal.yaml").read_text(
        encoding="utf-8"
    )


def test_a_note_longer_than_the_cap_is_refused(settings, channel):
    ctx = ToolContext(settings=settings, channel=channel)
    result = call(ctx, "update_channel_profile", notes=[{"note": "x" * 400}])
    assert result.is_error and "note" in result.text


# --- the channel's render defaults ----------------------------------------- #


def test_the_channel_default_applies_and_the_run_may_override_it(settings, channel, tmp_path):
    """A standing decision of the channel; what the agent decides for this video wins."""
    from clipper.agent.models import ProfileOverride

    base = ProfileOverride.model_validate(channel.overrides_for("short"))
    assert base.tail_roll_s == 2.0

    merged = dict(channel.overrides_for("short"))
    merged.update(ProfileOverride(tail_roll_s=0.0).model_dump(exclude_none=True))
    assert ProfileOverride.model_validate(merged).tail_roll_s == 0.0


def test_the_filename_and_the_id_inside_must_agree(settings):
    """`--channel` addresses the file; the notes are named after the id."""
    (settings.channels_dir / "meucanal.yaml").write_text("id: outro\n", encoding="utf-8")
    with pytest.raises(InputError, match="o mesmo nos dois"):
        resolve(settings, "meucanal")


def test_a_file_without_an_id_takes_the_one_from_its_name(settings):
    (settings.channels_dir / "meucanal.yaml").write_text("name: Sem id\n", encoding="utf-8")
    resolved = resolve(settings, "meucanal")
    assert resolved is not None and resolved.id == "meucanal"


# --- fixed hashtags -------------------------------------------------------- #


def test_the_channel_tags_come_first_and_the_clip_completes_the_list():
    from clipper.agent.tools.publish import merge_hashtags

    merged, dropped = merge_hashtags(["#seucanal", "#humor"], ["#marmita", "#clt"])
    assert merged == ["#seucanal", "#humor", "#marmita", "#clt"]
    assert dropped == []


def test_the_same_tag_written_twice_only_goes_once():
    from clipper.agent.tools.publish import merge_hashtags

    merged, _ = merge_hashtags(["#humor"], ["Humor", "#HUMOR", "humor", "#skate"])
    assert merged == ["#humor", "#skate"]


def test_tags_beyond_the_cap_are_cut_and_reported():
    from clipper.agent.tools.publish import MAX_HASHTAGS, merge_hashtags

    merged, dropped = merge_hashtags(["#a", "#b", "#c"], ["#d", "#e", "#sobra1", "#sobra2"])
    assert len(merged) == MAX_HASHTAGS
    assert dropped == ["#sobra1", "#sobra2"]


@pytest.fixture
def rendered_clip(settings):
    """A workspace with one clip whose file exists - enough for set_metadata."""
    from clipper.models import Clip, ClipSet, Segment, SourceInfo, TimeRange, Transcript, Word
    from clipper.state import State

    video_id = "vid0000001"
    video_dir = settings.video_dir(video_id)
    video_dir.mkdir(parents=True)
    words = [
        Word(text=w, start=60.0 + i, end=60.9 + i) for i, w in enumerate(["um", "dois", "tres"])
    ]
    transcript = Transcript(
        video_id=video_id,
        language="pt",
        duration=600.0,
        segments=[Segment(id=0, start=60.0, end=63.0, text="um dois tres.", words=words)],
    )
    (video_dir / "transcript.json").write_text(transcript.model_dump_json(), encoding="utf-8")
    State(settings.state_db).upsert_video(
        SourceInfo(video_id=video_id, title="T", channel="C", duration=600.0, url="u")
    )

    clip = Clip(index=1, slug="corte", profile="short", segments=[TimeRange(start=60.0, end=63.0)])
    profile_dir = settings.profile_dir(video_id, "short")
    profile_dir.mkdir(parents=True)
    (profile_dir / "clips.json").write_text(
        ClipSet(video_id=video_id, profile="short", clips=[clip]).model_dump_json(),
        encoding="utf-8",
    )
    clip_dir = profile_dir / "clips" / clip.dir_name
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip.mp4").write_bytes(b"x")
    return video_id, clip_dir


def test_the_channel_tags_reach_the_written_metadata(settings, channel, rendered_clip):
    """Guaranteed by the code: a channel tag the model forgets is a clip lost to search."""
    video_id, clip_dir = rendered_clip
    channel.profile.title.hashtags = ["#seucanal", "#humor"]
    ctx = ToolContext(settings=settings, enabled_profiles=("short",), channel=channel)

    payload = answer(
        ctx,
        "set_metadata",
        clip_id=f"{video_id}/short/1",
        title="eu perdi o jogo por causa da marmita",
        hashtags=["#marmita"],
    )

    assert payload["hashtags"][:2] == ["#seucanal", "#humor"]
    assert "#marmita" in payload["hashtags"]
    written = json.loads((clip_dir / "metadata.json").read_text(encoding="utf-8"))
    assert written["hashtags"] == payload["hashtags"]


def test_a_title_longer_than_the_channel_asks_for_is_flagged(settings, channel, rendered_clip):
    video_id, _ = rendered_clip
    ctx = ToolContext(settings=settings, enabled_profiles=("short",), channel=channel)

    payload = answer(
        ctx,
        "set_metadata",
        clip_id=f"{video_id}/short/1",
        title="um titulo deliberadamente longo demais para o que este canal costuma usar",
        hashtags=[],
    )

    assert any("o canal pede ate 60" in warning for warning in payload["warnings"])
