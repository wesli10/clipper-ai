"""The opening of a clip: the mirror of the rules that guard its ending.

The examples marked "real" are the actual first lines of clips this pipeline
published - every one of them scored 76-88 on hook quality by the model itself.
"""

from __future__ import annotations

import pytest

from clipper.models import ClipCandidate, Segment, TimeRange, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.stages.select import (
    HOOK_ADVANCE_S,
    advance_to_opening,
    hook_is_present,
    opens_well,
    snap_candidate,
)
from tests.conftest import make_segment

BAD_OPENINGS = [
    "Pegou o carro, aí você desmirilhou o carro.",  # real
    "Não, mas se eu sou um cara da treta, eu tenho uma.",  # real
    "Ô, você vê assim, ó, hoje a música no Spotify dá dinheiro.",  # real
    "Tem tudo isso.",  # real
    "Pô, não, não sabia disso aí, gente.",  # real
    "Cara, tá lá, aprovado.",  # real
    "Segundo, o que já estão falando, é que eles estão.",  # real
    "Então, voltando ao que eu falava do imposto.",
    "Mas aí ele me mandou a mensagem.",
    "Isso é o que eu falei pra ele ontem.",
]

GOOD_OPENINGS = [
    "Se você coloca um relógio falso, mano, isso diz mais sobre você.",  # real
    "Estou descendo a serra com o meu contador, o meu arquiteto.",  # real
    "Você usaria uma réplica no pulso?",
    "Cara, eu perdi duzentos mil reais numa aposta besta.",
    "Existe uma peça minúscula dentro do Rolex que nenhum falso copia.",
    "Não existe nada pior do que comprar um carro batido.",
    "Zero quilômetro não quer dizer zero quilômetro.",
    "Era falso.",
]


def one(text: str) -> list[Segment]:
    words = [
        Word(text=token, start=i * 0.4, end=i * 0.4 + 0.3) for i, token in enumerate(text.split())
    ]
    return [Segment(id=0, start=0.0, end=words[-1].end, text=text, words=words)]


@pytest.mark.parametrize("text", BAD_OPENINGS)
def test_a_clip_may_not_open_in_the_middle_of_something(text: str) -> None:
    assert not opens_well(one(text), 0)


@pytest.mark.parametrize("text", GOOD_OPENINGS)
def test_an_opening_that_stands_on_its_own_is_kept(text: str) -> None:
    assert opens_well(one(text), 0)


def test_a_long_may_open_on_a_topic_connector() -> None:
    """`topic` mode starts where the subject does, and that is what a connector marks."""
    segments = one("Agora, falando de importação, o que ninguém conta é o imposto.")
    assert opens_well(segments, 0, mode="topic")
    assert not opens_well(segments, 0, mode="sentence")


# --------------------------------------------------------------------------- #
# The quoted hook
# --------------------------------------------------------------------------- #


def build_transcript() -> Transcript:
    specs = [
        (0.0, "Pô, não, não sabia disso aí, gente."),
        (3.0, "Zero quilômetro não quer dizer zero quilômetro de verdade."),
        (9.0, "O carro pode ter cem quilômetros rodados e ainda ser vendido como zero."),
        (16.0, "Por isso eu sempre peço o laudo antes de assinar qualquer coisa."),
        (24.0, "É o único jeito de não levar gato por lebre nessa história."),
    ]
    segments = [make_segment(i, start, text) for i, (start, text) in enumerate(specs)]
    return Transcript(video_id="t", language="pt", duration=segments[-1].end + 2, segments=segments)


def candidate(start: float, end: float, hook: str, closing: str = "") -> ClipCandidate:
    return ClipCandidate(
        segments=[TimeRange(start=start, end=end)],
        hook=hook,
        closing=closing,
        score=90.0,
        topic="zero quilometro",
    )


def test_a_hook_quoted_from_the_middle_is_refused() -> None:
    transcript = build_transcript()
    inside = candidate(0.0, 30.0, "É o único jeito de não levar gato por lebre")
    assert not hook_is_present(transcript, inside)

    at_the_start = candidate(0.0, 30.0, "Pô, não, não sabia disso aí, gente")
    assert hook_is_present(transcript, at_the_start)


def test_a_hook_too_short_to_check_is_let_through() -> None:
    assert hook_is_present(build_transcript(), candidate(0.0, 30.0, "sim"))


# --------------------------------------------------------------------------- #
# Moving the start forward
# --------------------------------------------------------------------------- #


def test_the_start_moves_to_the_next_sentence_that_opens() -> None:
    transcript = build_transcript()
    moved = advance_to_opening(transcript, TimeRange(start=0.0, end=30.0), floor_s=5.0)

    assert moved is not None
    assert moved.start == pytest.approx(3.0, abs=0.3), "deve abrir no 'Zero quilômetro...'"
    assert moved.end == 30.0


def test_an_opening_that_already_works_is_left_alone() -> None:
    transcript = build_transcript()
    interval = TimeRange(start=3.0, end=30.0)
    assert advance_to_opening(transcript, interval, floor_s=5.0) == interval


def test_no_opening_within_reach_is_a_rejection() -> None:
    """Nothing is moved silently past the duration floor: the clip is refused."""
    transcript = build_transcript()
    # só 4s de folga: avançar uma frase deixaria o corte abaixo do piso
    assert advance_to_opening(transcript, TimeRange(start=0.0, end=30.0), floor_s=28.0) is None


def test_the_search_window_is_bounded() -> None:
    specs = [(0.0, "Pô, não, não sabia disso aí, gente.")]
    filler = "Aí então ele falou isso também pra mim naquele dia."
    at = 4.0
    while at < HOOK_ADVANCE_S + 20:
        specs.append((at, filler))
        at += 4.0
    specs.append((at, "Existe um detalhe que nenhum falso consegue copiar direito."))
    segments = [make_segment(i, start, text) for i, (start, text) in enumerate(specs)]
    transcript = Transcript(
        video_id="t", language="pt", duration=segments[-1].end + 2, segments=segments
    )
    assert advance_to_opening(transcript, TimeRange(start=0.0, end=at + 8), floor_s=5.0) is None


# --------------------------------------------------------------------------- #
# In the pipeline
# --------------------------------------------------------------------------- #


@pytest.fixture
def profile(prompts_dir):
    return resolve_profile(
        "short",
        {"short": {"duration_min": 8, "duration_target": 20, "duration_max": 90}},
        prompts_dir,
    )


def test_snap_candidate_fixes_the_opening_and_says_so_in_the_hook(profile) -> None:
    transcript = build_transcript()
    snapped = snap_candidate(
        transcript,
        candidate(0.0, 30.0, "Pô, não, não sabia disso aí, gente"),
        profile,
    )
    assert isinstance(snapped, ClipCandidate)
    assert snapped.segments[0].start == pytest.approx(3.0, abs=0.4)
    assert snapped.hook.startswith("Zero quilômetro"), "o gancho tem que descrever o corte real"


def test_snap_candidate_refuses_a_clip_that_cannot_open(prompts_dir) -> None:
    from clipper.stages.select import RejectedClip

    transcript = build_transcript()
    tight = resolve_profile(
        "short",
        {"short": {"duration_min": 28, "duration_target": 29, "duration_max": 90}},
        prompts_dir,
    )
    rejected = snap_candidate(
        transcript, candidate(0.0, 30.0, "Pô, não, não sabia disso aí, gente"), tight
    )
    assert isinstance(rejected, RejectedClip)
    assert "abre no meio de outra fala" in rejected.reason
