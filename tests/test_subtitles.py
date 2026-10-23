"""ASS generation - SPEC 7.3 step 5 and SPEC 11.1."""

from __future__ import annotations

import pytest

from clipper.models import Segment, TimeRange, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.video.subtitles import (
    break_lines,
    build_ass,
    escape_text,
    installed_font_families,
    resolve_font,
    to_ass_colour,
    words_for_clip,
    wrap_for_display,
)


def make_transcript(text: str, start: float = 0.0, step: float = 0.5) -> Transcript:
    words, t = [], start
    for token in text.split():
        words.append(Word(text=token, start=round(t, 3), end=round(t + step - 0.05, 3)))
        t += step
    segment = Segment(id=0, start=start, end=words[-1].end, text=text, words=words)
    return Transcript(video_id="v", language="pt", duration=words[-1].end + 1, segments=[segment])


# --- colours and escaping -------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#FFFFFF", "&H00FFFFFF"),
        ("#FFD400", "&H0000D4FF"),
        ("#000000", "&H00000000"),
        ("#00000099", "&H66000000"),
        ("#000000FF", "&H00000000"),
    ],
)
def test_colour_conversion_inverts_alpha_and_swaps_channels(value, expected):
    assert to_ass_colour(value) == expected


def test_invalid_colour_is_rejected():
    with pytest.raises(ValueError, match="cor invalida"):
        to_ass_colour("vermelho")


def test_braces_and_backslashes_are_escaped():
    assert escape_text("a {b} c\\d") == "a \\{b\\} c\\\\d"


def test_accents_and_emoji_survive_escaping():
    assert escape_text("ação é 🚀") == "ação é 🚀"


# --- word collection and offsets ------------------------------------------- #


def test_words_are_shifted_to_the_rendered_timeline():
    transcript = make_transcript("uma duas tres quatro cinco", start=100.0)
    words = words_for_clip(transcript, [TimeRange(start=100.0, end=102.5)], [0.0])
    assert words[0].start == pytest.approx(0.0)
    assert all(w.start >= 0 for w in words)


def test_words_of_a_second_interval_use_its_concat_offset():
    words = [Word(text=f"w{i}", start=i, end=i + 0.9) for i in range(40)]
    segment = Segment(id=0, start=0, end=40, text=" ".join(w.text for w in words), words=words)
    transcript = Transcript(video_id="v", language="pt", duration=40, segments=[segment])

    collected = words_for_clip(
        transcript,
        [TimeRange(start=0, end=10), TimeRange(start=30, end=40)],
        [0.0, 9.7],  # 10 s minus a 0.3 s crossfade
    )
    late = [w for w in collected if w.text == "w30"]
    assert late and late[0].start == pytest.approx(9.7)


def test_offsets_must_match_the_intervals():
    transcript = make_transcript("uma duas tres")
    with pytest.raises(ValueError, match="offset"):
        words_for_clip(transcript, [TimeRange(start=0, end=1)], [0.0, 1.0])


# --- line breaking --------------------------------------------------------- #


def test_karaoke_lines_respect_the_word_limit():
    transcript = make_transcript("uma duas tres quatro cinco seis sete oito nove dez")
    profile = resolve_profile("short")
    lines = break_lines(transcript.words(), profile.subtitle, kind="karaoke")
    assert lines
    assert all(len(line.words) <= profile.subtitle.max_words_per_line for line in lines)


def test_karaoke_lines_respect_the_character_limit():
    transcript = make_transcript("palavrasgrandes exageradamente compridas demais aqui")
    profile = resolve_profile("short")
    lines = break_lines(transcript.words(), profile.subtitle, kind="karaoke")
    assert all(
        len(line.text) <= profile.subtitle.max_chars_per_line or len(line.words) == 1
        for line in lines
    )


def test_a_pause_forces_a_new_line():
    words = [
        Word(text="antes", start=0.0, end=0.4),
        Word(text="da", start=0.4, end=0.6),
        Word(text="pausa", start=0.6, end=1.0),
        Word(text="depois", start=2.5, end=3.0),
    ]
    profile = resolve_profile("short")
    lines = break_lines(words, profile.subtitle, kind="karaoke")
    assert lines[-1].text == "depois"


def test_punctuation_ends_a_line():
    words = [
        Word(text="fim.", start=0.0, end=0.4),
        Word(text="comeco", start=0.5, end=0.9),
    ]
    lines = break_lines(words, resolve_profile("short").subtitle, kind="karaoke")
    assert lines[0].text == "fim."


def test_classic_wrapping_respects_max_lines():
    style = resolve_profile("long").subtitle
    text = "uma frase bastante longa que precisa ser quebrada em duas linhas no maximo mesmo"
    wrapped = wrap_for_display(text, style)
    assert wrapped.count("\\N") <= style.max_lines - 1


# --- documents ------------------------------------------------------------- #


def test_karaoke_document_has_the_expected_style_and_k_tags():
    transcript = make_transcript("uma duas tres quatro cinco seis")
    profile = resolve_profile("short")
    ass = build_ass(transcript, [TimeRange(start=0.0, end=3.0)], [0.0], profile)

    assert "[V4+ Styles]" in ass
    assert profile.subtitle.font in ass or profile.subtitle.fallback_font in ass
    assert ",72," in ass  # font size
    assert "\\k" in ass
    assert "Dialogue:" in ass
    # BorderStyle 1 (outline) for karaoke, outline width 4
    assert ",1,4,0,2," in ass


def test_classic_document_uses_a_box_and_no_karaoke():
    transcript = make_transcript("uma duas tres quatro cinco seis sete oito")
    profile = resolve_profile("long")
    ass = build_ass(transcript, [TimeRange(start=0.0, end=4.0)], [0.0], profile)

    assert "\\k" not in ass
    assert ",3,1,0,2," in ass  # BorderStyle 3 = opaque box
    assert "&H66000000" in ass  # box_color #00000099
    assert ",44," in ass


def test_uppercase_only_applies_to_the_karaoke_profile():
    transcript = make_transcript("palavra minuscula aqui")
    short_ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("short"))
    long_ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("long"))
    assert "PALAVRA" in short_ass
    assert "palavra" in long_ass


def test_playres_matches_the_profile_resolution():
    transcript = make_transcript("uma duas tres")
    ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("short"))
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass


def test_vertical_position_becomes_a_margin_in_pixels():
    transcript = make_transcript("uma duas tres")
    ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("short"))
    # 70% height on a 1920 tall frame leaves a 576 px bottom margin
    assert ",576,1" in ass


def test_subtitle_style_off_raises():
    transcript = make_transcript("uma duas tres")
    profile = resolve_profile("short", {"short": {"subtitle_style": "off"}})
    with pytest.raises(ValueError, match="off"):
        build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], profile)


def test_special_characters_do_not_break_the_document():
    transcript = make_transcript("cor {vermelha} e 50% \\ ação")
    ass = build_ass(transcript, [TimeRange(start=0, end=4)], [0.0], resolve_profile("long"))
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert dialogue
    assert "\\{" in ass or "{vermelha}" not in ass


# --- font resolution ------------------------------------------------------- #


def test_an_installed_font_is_used_as_is(monkeypatch):
    monkeypatch.setattr(
        "clipper.video.subtitles.installed_font_families",
        lambda: frozenset({"montserrat extrabold", "dejavu sans"}),
    )
    assert resolve_font(resolve_profile("short").subtitle) == "Montserrat ExtraBold"


def test_a_missing_font_falls_back(monkeypatch):
    monkeypatch.setattr(
        "clipper.video.subtitles.installed_font_families",
        lambda: frozenset({"dejavu sans", "liberation sans"}),
    )
    style = resolve_profile("short").subtitle
    assert resolve_font(style) == style.fallback_font


def test_without_fontconfig_the_configured_font_is_kept(monkeypatch):
    monkeypatch.setattr("clipper.video.subtitles.installed_font_families", frozenset)
    assert resolve_font(resolve_profile("short").subtitle) == "Montserrat ExtraBold"


def test_font_family_lookup_never_raises():
    assert isinstance(installed_font_families(), frozenset)


def test_the_document_names_a_font_that_exists_or_the_fallback():
    transcript = make_transcript("uma duas tres")
    profile = resolve_profile("short")
    ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], profile)
    assert resolve_font(profile.subtitle) in ass


# --- ASS structure --------------------------------------------------------- #


def test_dialogue_fields_match_the_declared_format():
    """A field-count mismatch leaks a comma into the rendered caption."""
    transcript = make_transcript("uma duas tres")
    ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("short"))

    format_line = next(line for line in ass.splitlines() if line.startswith("Format: Layer"))
    names = [n.strip() for n in format_line.removeprefix("Format:").split(",")]
    assert names[-1] == "Text"
    assert "MarginV" in names

    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    body = dialogue.removeprefix("Dialogue:")
    # Text is the last field and may itself contain commas, so split at most n-1 times.
    fields = body.split(",", len(names) - 1)
    assert len(fields) == len(names)
    text = fields[-1]
    assert not text.startswith(",")
    assert text.lstrip().startswith("{\\k")


def test_classic_dialogue_text_does_not_start_with_a_comma():
    transcript = make_transcript("uma duas tres quatro")
    ass = build_ass(transcript, [TimeRange(start=0, end=2)], [0.0], resolve_profile("long"))
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    text = dialogue.split(",", 9)[-1]
    assert not text.startswith(",")
    assert text.strip()
