"""The quiz format: cut at the question, hold a card, then reveal (SPEC 6.1/7.3)."""

from __future__ import annotations

import pytest

from clipper.models import Clip, ClipCandidate, Segment, TimeRange, Transcript, Word
from clipper.profiles import ProfileError, resolve_profile
from clipper.stages import select
from clipper.stages.render import DEFAULT_CARD_TEXT, card_overlay
from clipper.video import ffmpeg, subtitles


def transcript_of(*sentences: tuple[float, float, str]) -> Transcript:
    segments = []
    for index, (start, end, text) in enumerate(sentences):
        tokens = text.split()
        step = (end - start) / len(tokens)
        words = [
            Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
            for i, t in enumerate(tokens)
        ]
        segments.append(Segment(id=index, start=start, end=end, text=text, words=words))
    return Transcript(video_id="v", language="pt", duration=sentences[-1][1] + 1, segments=segments)


QUIZ = transcript_of(
    (0.0, 6.0, "A gente estava conversando sobre marmita ontem."),
    (6.0, 12.0, "Qual voce acha que foi o pior alimento da lista?"),
    (12.0, 20.0, "Foi peixe, e o cara esquentou no microondas da empresa inteira."),
    (20.0, 28.0, "Depois disso ninguem mais falou com ele por uma semana."),
)


@pytest.fixture
def profile():
    return resolve_profile("quiz")


# --------------------------------------------------------------------------- #
# Choosing where to hold
# --------------------------------------------------------------------------- #


def test_reveal_snaps_to_the_sentence_that_answers(profile):
    got = select.snap_reveal(QUIZ, TimeRange(start=0.0, end=28.0), 12.8, profile)
    assert got == 12.0


def test_reveal_is_refused_when_the_line_before_is_not_a_question(profile):
    got = select.snap_reveal(QUIZ, TimeRange(start=0.0, end=28.0), 6.0, profile)
    assert isinstance(got, str)
    assert "pergunta" in got


def test_reveal_is_refused_when_the_question_side_is_too_short(profile):
    got = select.snap_reveal(QUIZ, TimeRange(start=10.5, end=28.0), 12.0, profile)
    assert isinstance(got, str)
    assert "pergunta de 1.5s" in got


def test_reveal_is_refused_when_the_answer_side_is_too_short(profile):
    got = select.snap_reveal(QUIZ, TimeRange(start=0.0, end=14.0), 12.0, profile)
    assert isinstance(got, str)
    assert "resposta de 2.0s" in got


def test_a_rhetorical_tag_is_not_a_question_to_hold_on(profile):
    rhetorical = transcript_of(
        (0.0, 8.0, "Esse cara levava marmita todo dia, ne?"),
        (8.0, 18.0, "E um dia ele esqueceu o peixe no microondas."),
    )
    got = select.snap_reveal(rhetorical, TimeRange(start=0.0, end=18.0), 8.0, profile)
    assert isinstance(got, str)


# --------------------------------------------------------------------------- #
# The candidate as a whole
# --------------------------------------------------------------------------- #


def candidate(**fields) -> ClipCandidate:
    base = dict(
        segments=[TimeRange(start=0.0, end=28.0)],
        reveal_at=12.0,
        score=90.0,
        closing="Depois disso ninguem mais falou com ele por uma semana.",
    )
    return ClipCandidate(**{**base, **fields})


def test_a_quiz_candidate_keeps_one_segment_and_gains_a_reveal(profile):
    got = select.snap_candidate(QUIZ, candidate(), profile)
    assert isinstance(got, ClipCandidate)
    assert len(got.segments) == 1
    assert got.reveal_at == 12.0


def test_a_quiz_candidate_without_a_reveal_is_rejected(profile):
    got = select.snap_candidate(QUIZ, candidate(reveal_at=None), profile)
    assert isinstance(got, select.RejectedClip)
    assert "reveal_at" in got.reason


def test_the_reveal_reason_reaches_the_retry_note(profile):
    got = select.snap_candidate(QUIZ, candidate(reveal_at=6.0), profile)
    assert isinstance(got, select.RejectedClip)
    assert "card" in got.reason


def test_the_short_profile_ignores_a_reveal_it_did_not_ask_for():
    short = resolve_profile("short")
    got = select.snap_candidate(QUIZ, candidate(reveal_at=99.0), short)
    assert isinstance(got, ClipCandidate)
    assert got.reveal_at == 99.0  # carried, but never used: card_seconds is 0


def test_to_clips_carries_the_card_across(profile):
    clips = select.to_clips([candidate(card_text="Chuta o alimento.")], profile)
    assert clips[0].reveal_at == 12.0
    assert clips[0].card_text == "Chuta o alimento."


# --------------------------------------------------------------------------- #
# Rendering the hold
# --------------------------------------------------------------------------- #


def test_cut_args_holds_the_last_frame_and_pads_the_silence():
    args = ffmpeg.cut_args("s.mp4", "d.mp4", 1.0, 5.0, hold=2.4)  # type: ignore[arg-type]
    assert "tpad=stop_mode=clone:stop_duration=2.4" in args
    assert "apad=pad_dur=2.4" in args


def test_cut_args_without_a_hold_adds_no_filter():
    args = ffmpeg.cut_args("s.mp4", "d.mp4", 1.0, 5.0)  # type: ignore[arg-type]
    assert "-vf" not in args and "-af" not in args


def test_the_card_covers_the_gap_between_the_two_parts():
    clip = Clip(index=1, slug="q", profile="quiz", segments=[TimeRange(start=0.0, end=28.0)])
    intervals = [TimeRange(start=0.0, end=12.0), TimeRange(start=12.0, end=28.0)]
    # part one is 12s of footage plus a 2.4s hold, crossfaded by 0.3s
    card = card_overlay(clip, "pt", intervals, offsets=[0.0, 14.1])
    assert (card.start, card.end) == (12.0, 14.1)
    assert card.text == DEFAULT_CARD_TEXT["pt"]


def test_the_card_uses_the_models_own_words_when_it_wrote_some():
    clip = Clip(
        index=1,
        slug="q",
        profile="quiz",
        segments=[TimeRange(start=0.0, end=28.0)],
        card_text="Chuta o alimento.",
    )
    card = card_overlay(clip, "pt", [TimeRange(start=0.0, end=12.0)], offsets=[0.0, 14.1])
    assert card.text == "Chuta o alimento."


# --------------------------------------------------------------------------- #
# The card as ASS
# --------------------------------------------------------------------------- #


def test_the_card_is_drawn_over_the_whole_output_frame(profile):
    events = subtitles.card_events(subtitles.Card(start=5.0, end=7.4, text="E ai?"), profile)
    dim = next(e for e in events if ",Dim," in e)
    assert f"l {profile.output_width} 0" in dim
    assert f"l 0 {profile.output_height}" in dim


def test_the_countdown_lands_on_the_reveal(profile):
    events = subtitles.card_events(subtitles.Card(start=5.0, end=7.4, text="E ai?"), profile)
    digits = [e for e in events if e.rstrip().endswith(("1", "2")) and ",Card," in e]
    assert len(digits) == 2
    assert "0:00:06.40" in digits[0] and "0:00:07.40" in digits[1]


def test_the_card_text_wraps_for_the_big_font(profile):
    events = subtitles.card_events(
        subtitles.Card(start=0.0, end=2.4, text="O que voce acha que ele respondeu"), profile
    )
    text = next(e for e in events if ",Card," in e)
    assert "\\N" in text


def test_build_ass_places_the_card_after_the_captions(profile):
    doc = subtitles.build_ass(
        QUIZ,
        [TimeRange(start=0.0, end=12.0), TimeRange(start=12.0, end=28.0)],
        [0.0, 14.1],
        profile,
        card=subtitles.Card(start=12.0, end=14.1, text="Chuta."),
    )
    assert "Style: Card," in doc and "Style: Dim," in doc
    assert doc.index("Dialogue: 0,0:00:00") < doc.index(",Dim,")


def test_build_ass_without_a_card_declares_no_card_events(profile):
    doc = subtitles.build_ass(QUIZ, [TimeRange(start=0.0, end=12.0)], [0.0], profile)
    assert ",Dim," not in doc and ",Card," not in doc


# --------------------------------------------------------------------------- #
# Profile consistency
# --------------------------------------------------------------------------- #


def test_a_card_without_subtitles_is_a_configuration_error():
    with pytest.raises(ProfileError):
        resolve_profile("quiz", {"quiz": {"subtitle_style": "off"}})
