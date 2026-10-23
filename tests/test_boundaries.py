"""Boundary snapping - SPEC 6.1 step 4 and SPEC 11.1."""

from __future__ import annotations

import pytest

from clipper.models import ClipCandidate, Segment, TimeRange, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.stages.select import (
    MAX_OVERLAP_RATIO,
    SENTENCE_FORWARD_S,
    RejectedClip,
    drop_overlapping,
    merge_intervals,
    snap_boundaries,
    snap_candidate,
    snap_end,
    snap_range,
    snap_start,
)


def words(text: str, start: float, step: float = 0.4) -> list[Word]:
    out, t = [], start
    for token in text.split():
        out.append(Word(text=token, start=round(t, 3), end=round(t + step * 0.9, 3), prob=0.9))
        t += step
    return out


def seg(idx: int, start: float, text: str, step: float = 0.4) -> Segment:
    ws = words(text, start, step)
    return Segment(id=idx, start=start, end=ws[-1].end, text=text, words=ws)


def span(idx: int, start: float, end: float, text: str) -> Segment:
    """A segment with exact start/end, words spread evenly - for precise fixtures."""
    tokens = text.split()
    step = (end - start) / len(tokens)
    ws = [
        Word(
            text=tok,
            start=round(start + i * step, 3),
            end=round(start + (i + 1) * step - 0.01, 3),
        )
        for i, tok in enumerate(tokens)
    ]
    return Segment(id=idx, start=start, end=end, text=text, words=ws)


@pytest.fixture
def tr() -> Transcript:
    """Segments at 0, 3, 7.5, 14, 18 (after a 1.9 s pause), 25, 31."""
    specs = [
        (0.0, "Bem vindos ao programa de hoje."),
        (3.0, "Hoje vamos falar sobre produtividade."),
        (7.5, "Entao a primeira coisa que voce precisa entender e o foco"),
        (14.0, "Foco nao e fazer mais coisas."),
        (18.0, "Agora mudando de assunto vamos falar de sono."),
        (25.0, "Dormir menos de seis horas destroi sua memoria."),
        (31.0, "E isso encerra a nossa conversa de hoje."),
    ]
    segments = [seg(i, s, t) for i, (s, t) in enumerate(specs)]
    return Transcript(video_id="t", language="pt", duration=40.0, segments=segments)


# --- start ----------------------------------------------------------------- #


def test_start_in_the_middle_of_a_sentence_rewinds_to_its_beginning(tr):
    assert snap_start(tr, 8.9) == pytest.approx(7.5)


def test_start_already_on_a_boundary_stays(tr):
    assert snap_start(tr, 14.0) == pytest.approx(14.0)


def test_start_before_the_first_segment_clamps(tr):
    assert snap_start(tr, -5.0) == pytest.approx(0.0)


def test_start_inside_a_silence_moves_to_the_next_speech(tr):
    # 16.x is the pause between segment 3 (ends ~16.16) and segment 4 (starts 18.0)
    assert snap_start(tr, 17.0) == pytest.approx(18.0)


# --- end ------------------------------------------------------------------- #


def test_end_in_the_middle_of_a_word_advances_to_the_end_of_the_sentence(tr):
    end = snap_end(tr, 4.2)
    assert end == pytest.approx(tr.segments[1].end)
    assert end > 4.2


def test_end_takes_the_nearest_closing(tr):
    """The nearest valid closing wins; chasing a better-looking one drifts."""
    assert snap_end(tr, 9.0) == pytest.approx(tr.segments[2].end)


def test_end_stops_before_a_change_of_subject(tr):
    """Segment 4 opens with "Agora mudando de assunto" - going past it drifts."""
    assert snap_end(tr, 9.0) < tr.segments[4].start


def test_end_advances_across_segments_with_neither_punctuation_nor_pause():
    packed = Transcript(
        video_id="t",
        language="pt",
        duration=20.0,
        segments=[
            span(0, 0.0, 5.0, "uma frase sem ponto final"),
            span(1, 5.1, 10.0, "outra frase que continua a ideia"),
            span(2, 10.1, 15.0, "e a frase que finalmente termina."),
        ],
    )
    assert snap_end(packed, 2.0) == pytest.approx(15.0)


def test_a_sentence_longer_than_the_window_cuts_at_the_widest_pause():
    """Continuous speech with no punctuation leaves only word gaps to go on."""
    endless = Transcript(
        video_id="t",
        language="pt",
        duration=200.0,
        segments=[
            span(i, i * 5.0, i * 5.0 + 4.9, "frase sem ponto e sem pausa") for i in range(30)
        ],
    )
    end = snap_end(endless, 2.0)
    assert end <= 2.0 + SENTENCE_FORWARD_S
    assert end > 2.0


def test_end_never_lands_mid_word(tr):
    for probe in (1.0, 5.5, 12.0, 20.0, 27.0):
        end = snap_end(tr, probe)
        assert any(abs(end - s.end) < 1e-6 for s in tr.segments)


# --- padding --------------------------------------------------------------- #


def test_padding_is_applied_around_the_snapped_interval(tr):
    snapped = snap_range(tr, TimeRange(start=8.0, end=15.0), pad_before=0.25, pad_after=0.4)
    assert snapped.start == pytest.approx(7.5 - 0.25)
    assert snapped.end == pytest.approx(snap_end(tr, 15.0) + 0.4)


def test_padding_never_goes_below_zero_or_past_the_video(tr):
    snapped = snap_range(tr, TimeRange(start=0.1, end=39.9))
    assert snapped.start >= 0.0
    assert snapped.end <= tr.duration


# --- topic mode ------------------------------------------------------------ #


def packed_with_pause() -> Transcript:
    """Tightly packed segments with one real pause before the segment at 22 s."""
    return Transcript(
        video_id="t",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 4.9, "primeira frase da conversa."),
            span(1, 5.0, 9.9, "continuando o mesmo raciocinio aqui."),
            span(2, 10.0, 14.9, "ainda no mesmo assunto de antes."),
            span(3, 15.0, 19.5, "fechando esse ponto por enquanto."),
            span(4, 22.0, 26.9, "vamos ver o proximo exemplo pratico."),
            span(5, 27.0, 31.9, "e ele mostra bem a ideia."),
        ],
    )


def test_topic_mode_prefers_a_long_pause_over_the_nearer_sentence_start():
    transcript = packed_with_pause()
    assert snap_start(transcript, 17.0, "sentence") == pytest.approx(15.0)
    assert snap_start(transcript, 17.0, "topic") == pytest.approx(22.0)


def test_topic_mode_uses_a_connector_when_there_is_no_long_pause():
    transcript = Transcript(
        video_id="t",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 4.9, "primeira frase da conversa."),
            span(1, 5.0, 9.9, "continuando o mesmo raciocinio aqui."),
            span(2, 10.0, 14.9, "agora mudando de assunto vamos falar de sono."),
            span(3, 15.0, 19.9, "dormir pouco destroi a memoria."),
        ],
    )
    # sentence mode would stop at 5.0; the connector at 10.0 is the topic boundary
    assert snap_start(transcript, 7.0, "sentence") == pytest.approx(5.0)
    assert snap_start(transcript, 7.0, "topic") == pytest.approx(10.0)


def test_topic_mode_picks_the_boundary_closest_to_the_proposal():
    transcript = packed_with_pause()
    # 21 s is nearer to the pause boundary at 22 than to anything else
    assert snap_start(transcript, 21.0, "topic") == pytest.approx(22.0)


def test_topic_mode_falls_back_to_sentence_when_nothing_strong_is_near(tr):
    far = Transcript(
        video_id="t",
        language="pt",
        duration=100.0,
        segments=[seg(i, i * 4.0, "uma frase qualquer sem conector") for i in range(20)],
    )
    assert snap_start(far, 41.0, "topic") == pytest.approx(40.0)


def test_topic_mode_only_searches_eight_seconds_around():
    transcript = Transcript(
        video_id="t",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 0.0, 9.9, "abertura do video sem nada de especial."),
            span(1, 10.0, 60.0, "um bloco muito longo de fala continua sem pausas."),
            span(2, 80.0, 90.0, "depois de uma pausa enorme comeca outro tema."),
        ],
    )
    # the strong boundary at 80 s is 30 s away from a proposal at 50 s: too far
    assert snap_start(transcript, 50.0, "topic") == pytest.approx(10.0)


# --- merging --------------------------------------------------------------- #


def test_intervals_closer_than_two_seconds_are_merged():
    merged = merge_intervals([TimeRange(start=0, end=10), TimeRange(start=11.5, end=20)], gap=2.0)
    assert len(merged) == 1
    assert merged[0].start == 0 and merged[0].end == 20


def test_intervals_farther_than_two_seconds_stay_separate():
    merged = merge_intervals([TimeRange(start=0, end=10), TimeRange(start=13, end=20)], gap=2.0)
    assert len(merged) == 2


def test_merging_sorts_out_of_order_intervals():
    merged = merge_intervals([TimeRange(start=60, end=80), TimeRange(start=0, end=10)])
    assert [r.start for r in merged] == [0, 60]


def test_overlapping_intervals_are_merged():
    merged = merge_intervals([TimeRange(start=0, end=30), TimeRange(start=20, end=50)])
    assert len(merged) == 1 and merged[0].end == 50


# --- candidate level ------------------------------------------------------- #


def long_transcript() -> Transcript:
    segments = [
        seg(i, i * 10.0, f"frase numero {i} com bastante conteudo aqui.") for i in range(60)
    ]
    return Transcript(video_id="t", language="pt", duration=600.0, segments=segments)


def test_clip_shorter_than_the_profile_minimum_is_rejected(tr):
    profile = resolve_profile("short")
    candidate = ClipCandidate(segments=[TimeRange(start=0.0, end=2.0)], score=90)
    assert isinstance(snap_candidate(tr, candidate, profile), RejectedClip)


def test_clip_longer_than_the_profile_maximum_is_rejected():
    profile = resolve_profile("short")
    candidate = ClipCandidate(segments=[TimeRange(start=0.0, end=400.0)], score=90)
    assert isinstance(snap_candidate(long_transcript(), candidate, profile), RejectedClip)


def test_long_profile_rejects_a_clip_with_a_very_short_interval():
    profile = resolve_profile("long")
    candidate = ClipCandidate(
        segments=[
            TimeRange(start=0.0, end=150.0),
            TimeRange(start=300.0, end=303.0),
        ],
        score=90,
    )
    result = snap_candidate(long_transcript(), candidate, profile)
    assert isinstance(result, RejectedClip)
    assert "abaixo do minimo" in result.reason


def test_long_profile_keeps_a_valid_multi_segment_clip():
    profile = resolve_profile("long")
    candidate = ClipCandidate(
        segments=[TimeRange(start=0.0, end=100.0), TimeRange(start=300.0, end=400.0)],
        score=90,
        chapters=[],
    )
    result = snap_candidate(long_transcript(), candidate, profile)
    assert isinstance(result, ClipCandidate)
    assert len(result.segments) == 2
    assert profile.duration_min <= result.duration <= profile.duration_max


def test_candidate_is_truncated_to_max_segments():
    profile = resolve_profile("long")
    candidate = ClipCandidate(
        segments=[TimeRange(start=i * 100.0, end=i * 100.0 + 40.0) for i in range(6)],
        score=90,
    )
    result = snap_candidate(long_transcript(), candidate, profile)
    assert isinstance(result, ClipCandidate)
    assert len(result.segments) <= profile.max_segments


# --- overlap --------------------------------------------------------------- #


def test_lower_scoring_overlapping_candidate_is_dropped():
    a = ClipCandidate(segments=[TimeRange(start=0, end=60)], score=90, topic="a")
    b = ClipCandidate(segments=[TimeRange(start=10, end=70)], score=50, topic="b")
    kept, rejected = drop_overlapping([a, b])
    assert [c.topic for c in kept] == ["a"]
    assert len(rejected) == 1 and "sobrepoe" in rejected[0].reason


def test_slightly_overlapping_candidates_both_survive():
    a = ClipCandidate(segments=[TimeRange(start=0, end=100)], score=90)
    b = ClipCandidate(segments=[TimeRange(start=90, end=190)], score=80)
    kept, rejected = drop_overlapping([a, b])
    assert len(kept) == 2 and not rejected
    assert MAX_OVERLAP_RATIO > 10 / 100


def test_a_clip_contained_in_a_better_one_is_dropped():
    big = ClipCandidate(segments=[TimeRange(start=0, end=300)], score=90)
    small = ClipCandidate(segments=[TimeRange(start=100, end=140)], score=80)
    kept, _ = drop_overlapping([big, small])
    assert len(kept) == 1 and kept[0].score == 90


# --- full pass ------------------------------------------------------------- #


def test_snap_boundaries_ranks_and_truncates_to_n_clips():
    profile = resolve_profile("short", {"short": {"n_clips": 2}})
    transcript = long_transcript()
    candidates = [
        ClipCandidate(segments=[TimeRange(start=i * 100.0, end=i * 100.0 + 40.0)], score=50 + i)
        for i in range(5)
    ]
    kept, _ = snap_boundaries(transcript, candidates, profile)
    assert len(kept) == 2
    assert kept[0].score >= kept[1].score


def test_snap_boundaries_never_returns_an_interval_starting_mid_segment():
    profile = resolve_profile("short")
    transcript = long_transcript()
    candidates = [
        ClipCandidate(segments=[TimeRange(start=103.7, end=147.2)], score=80),
        ClipCandidate(segments=[TimeRange(start=311.1, end=352.9)], score=70),
    ]
    kept, _ = snap_boundaries(transcript, candidates, profile, pad_before=0.0, pad_after=0.0)
    starts = {round(s.start, 3) for s in transcript.segments}
    for clip in kept:
        for interval in clip.segments:
            assert round(interval.start, 3) in starts


# --- shapes seen in a real transcript --------------------------------------- #


def hesitation_transcript() -> Transcript:
    """A sentence containing an intra-sentence pause longer than SILENCE_GAP_S.

    Taken from a real PT-BR interview: "...a mata, enfim, [0.38 s] estavam em
    cima". Whisper keeps it inside one segment, so the pause is a hesitation,
    not a sentence boundary.
    """
    words = [
        Word(text="e", start=500.240, end=500.900),
        Word(text="que", start=500.900, end=501.240),
        Word(text="o", start=501.240, end=502.220),
        Word(text="meio", start=502.220, end=502.940),
        Word(text="ambiente,", start=502.940, end=503.360),
        Word(text="a", start=503.500, end=504.020),
        Word(text="mata,", start=504.020, end=504.520),
        Word(text="enfim,", start=504.520, end=505.100),
        Word(text="estavam", start=505.480, end=506.140),
        Word(text="em", start=506.140, end=506.440),
        Word(text="cima", start=506.440, end=506.840),
    ]
    following = [
        Word(text="e", start=506.840, end=507.260),
        Word(text="queriam", start=507.260, end=507.980),
        Word(text="regularizar", start=507.980, end=508.720),
        Word(text="essa", start=508.720, end=509.000),
        Word(text="parte.", start=509.000, end=509.260),
    ]
    return Transcript(
        video_id="real",
        language="pt",
        duration=520.0,
        segments=[
            Segment(
                id=0,
                start=500.240,
                end=506.840,
                text="e que o meio ambiente, a mata, enfim, estavam em cima",
                words=words,
            ),
            Segment(
                id=1,
                start=506.840,
                end=509.260,
                text="e queriam regularizar essa parte.",
                words=following,
            ),
        ],
    )


def test_an_intra_sentence_pause_does_not_become_a_start():
    """SPEC 1.2: a clip never starts mid-sentence, whatever the pause rule says.

    The two whisper segments here are one sentence - the first has no final
    punctuation - so the start is the sentence's, never the hesitation inside it.
    """
    transcript = hesitation_transcript()
    start = snap_start(transcript, 507.0)
    assert start == pytest.approx(500.240)
    assert start != pytest.approx(505.480)  # where the hesitation is


def test_a_pause_at_a_segment_start_is_still_honoured():
    """The rule keeps working when the pause really is between sentences."""
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 5.0, "primeira frase completa aqui."),
            span(1, 7.0, 12.0, "segunda frase depois da pausa."),
        ],
    )
    # 2 s of silence before the segment at 7 s; a proposal inside it snaps there
    assert snap_start(transcript, 8.5) == pytest.approx(7.0)


def test_padding_never_runs_into_the_previous_word():
    transcript = hesitation_transcript()
    snapped = snap_range(
        transcript, TimeRange(start=507.0, end=509.0), pad_before=0.25, pad_after=0.4
    )
    for word in transcript.words():
        assert not (word.start < snapped.start < word.end), "start caiu dentro de uma palavra"


def test_padding_never_runs_into_the_next_word():
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 5.0, "primeira frase completa aqui."),
            span(1, 5.1, 10.0, "segunda frase quase colada na primeira."),
        ],
    )
    snapped = snap_range(transcript, TimeRange(start=0.0, end=4.0), pad_before=0.0, pad_after=0.4)
    # the sentence ends at 5.0 and the next word starts at 5.1,
    # so the 0.4 s tail must be clipped
    assert snapped.end == pytest.approx(5.1)


def test_padding_still_applies_when_there_is_room():
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 5.0, "primeira frase completa aqui."),
            span(1, 12.0, 17.0, "segunda frase bem depois."),
        ],
    )
    snapped = snap_range(transcript, TimeRange(start=0.0, end=4.0), pad_before=0.0, pad_after=0.4)
    assert snapped.end == pytest.approx(5.4)


# --- closing the subject, not just the sentence (SPEC 1.2) ------------------ #


def conversation() -> Transcript:
    """Shapes taken from a real interview where clips ended mid-thought."""
    return Transcript(
        video_id="real",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 100.0, 104.0, "Foi muito bom estar lá por dois, três dias."),
            span(1, 104.1, 107.6, "Mais uma vez, obrigado a todos os brasileiros."),
            span(
                2, 107.7, 111.7, "É, eu ia te perguntar isso, porque no Brasil você ainda não foi."
            ),
            span(3, 111.8, 113.4, "Mas como que foi a volta pra casa?"),
            span(4, 113.5, 114.3, "Indescritível."),
            span(5, 114.4, 119.0, "Algo muito bom, mágico, mas tem aquela outra parte."),
        ],
    )


def test_the_models_end_is_kept_when_it_already_closes():
    """The model decides where the subject ends; the snapper must not overrule it."""
    transcript = conversation()
    assert snap_end(transcript, 107.6) == pytest.approx(107.6)


def test_an_interjection_is_not_a_continuation():
    """ "É," is not the conjunction "e" - stripping accents confused the two."""
    transcript = conversation()
    # segment 2 starts with "É,"; that must not disqualify the ending before it
    assert snap_end(transcript, 107.6) == pytest.approx(107.6)


def test_a_real_conjunction_still_blocks_an_ending():
    transcript = conversation()
    # segment 3 starts with "Mas", so segment 2 does not close anything
    assert snap_end(transcript, 111.7) != pytest.approx(111.7)


def test_an_ending_on_a_question_is_rejected():
    transcript = conversation()
    # segment 3 is a question: a question opens a beat, it does not close one
    assert snap_end(transcript, 113.4) != pytest.approx(113.4)


def test_trailing_off_is_not_a_sentence_end():
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=40.0,
        segments=[
            span(0, 0.0, 3.0, "Nossa, mas..."),
            span(1, 5.5, 9.0, "A culpa foi do gramado."),
        ],
    )
    # "Nossa, mas..." ends with a period yet finishes nothing
    assert snap_end(transcript, 3.0) == pytest.approx(9.0)


def test_a_nearby_closing_is_not_skipped_for_a_later_one():
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=60.0,
        segments=[
            span(0, 0.0, 4.0, "Primeira frase que fecha."),
            span(1, 4.5, 8.0, "Segunda frase que fecha."),
            span(2, 20.0, 24.0, "Terceira frase depois de uma pausa longa."),
        ],
    )
    # the 12 s pause after segment 1 looks tempting, but segment 0 already closes
    assert snap_end(transcript, 2.0) == pytest.approx(4.0)


def test_the_search_stops_at_a_change_of_subject():
    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=60.0,
        segments=[
            span(0, 0.0, 4.0, "Uma frase que fecha o assunto."),
            span(1, 5.5, 10.0, "Agora mudando de assunto, vamos falar de outra coisa."),
            span(2, 30.0, 34.0, "E isso encerra com uma pausa enorme antes."),
        ],
    )
    assert snap_end(transcript, 2.0) == pytest.approx(4.0)


def test_a_clip_ending_on_a_question_is_rejected():
    """The worst possible ending: the viewer is left holding the question."""
    from clipper.models import ClipCandidate
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 0.0, 30.0, "Uma explicacao longa que ocupa bastante tempo do corte."),
            span(1, 30.5, 40.0, "Entao como e que chama esse teorema?"),
            span(2, 60.0, 70.0, "Teorema Egregium de Gauss."),
        ],
    )
    candidate = ClipCandidate(segments=[TimeRange(start=0.0, end=40.0)], score=90)
    result = snap_candidate(transcript, candidate, resolve_profile("short"))
    assert isinstance(result, RejectedClip)
    assert "pergunta" in result.reason


def test_a_clip_ending_on_a_statement_is_kept():
    from clipper.models import ClipCandidate
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 0.0, 30.0, "Uma explicacao longa que ocupa bastante tempo do corte."),
            span(1, 30.5, 40.0, "E a resposta e o teorema egregium de Gauss."),
        ],
    )
    candidate = ClipCandidate(segments=[TimeRange(start=0.0, end=40.0)], score=90)
    assert isinstance(
        snap_candidate(transcript, candidate, resolve_profile("short")), ClipCandidate
    )


def test_a_rhetorical_tag_is_not_a_real_question():
    """ "né?" and "sacou?" close a thought; treating them as openers threw
    away good clips on a real video."""
    from clipper.stages.select import _is_rhetorical

    for closing in ("Mas o repertório conta, né?", "São o 7, o 11 e o 13, sacou?"):
        assert _is_rhetorical(closing)
    for opening in ("Cadê a ressonância?", "Como é que chama esse teorema?"):
        assert not _is_rhetorical(opening)


def test_a_clip_may_end_on_a_rhetorical_tag():
    from clipper.models import ClipCandidate
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    transcript = Transcript(
        video_id="real",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 0.0, 30.0, "Uma explicacao longa que ocupa bastante tempo do corte."),
            span(1, 31.5, 40.0, "E por isso o repertorio conta, ne?"),
        ],
    )
    candidate = ClipCandidate(segments=[TimeRange(start=0.0, end=40.0)], score=90)
    assert isinstance(
        snap_candidate(transcript, candidate, resolve_profile("short")), ClipCandidate
    )


# --- quality over quantity --------------------------------------------------- #


def quality_transcript() -> Transcript:
    return Transcript(
        video_id="q",
        language="pt",
        duration=200.0,
        segments=[
            span(0, 0.0, 20.0, "Uma explicacao longa que ocupa bastante tempo do corte."),
            span(1, 21.5, 40.0, "E a conclusao disso tudo e que vale a pena tentar."),
        ],
    )


def candidate(**kwargs):
    from clipper.models import ClipCandidate

    base = {"segments": [TimeRange(start=0.0, end=40.0)], "score": 90.0}
    return ClipCandidate(**{**base, **kwargs})


def test_a_candidate_below_the_score_floor_is_dropped():
    """The model's own score is the best signal that it is padding the list."""
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    profile = resolve_profile("short", {"short": {"min_score": 80}})
    result = snap_candidate(quality_transcript(), candidate(score=72.0), profile)
    assert isinstance(result, RejectedClip)
    assert "abaixo do minimo" in result.reason


def test_the_floor_is_off_by_default():
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    profile = resolve_profile("short")
    assert profile.min_score == 0.0
    assert isinstance(
        snap_candidate(quality_transcript(), candidate(score=10.0), profile), ClipCandidate
    )


def test_a_quoted_closing_that_is_not_there_is_rejected():
    """A quote the model invented means it never looked at the ending."""
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    result = snap_candidate(
        quality_transcript(),
        candidate(closing="e por isso o teorema de Gauss se aplica aqui"),
        resolve_profile("short"),
    )
    assert isinstance(result, RejectedClip)
    assert "fechamento" in result.reason


def test_a_real_closing_quote_passes():
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    result = snap_candidate(
        quality_transcript(),
        candidate(closing="E a conclusao disso tudo e que vale a pena tentar."),
        resolve_profile("short"),
    )
    assert isinstance(result, ClipCandidate)


def test_an_absent_closing_field_is_not_penalised():
    """Older answers, and providers that omit it, must still work."""
    from clipper.profiles import resolve_profile
    from clipper.stages.select import snap_candidate

    result = snap_candidate(quality_transcript(), candidate(), resolve_profile("short"))
    assert isinstance(result, ClipCandidate)


def test_a_closing_from_the_start_of_the_clip_is_rejected():
    """It has to be at the end, not merely somewhere in the clip."""
    from clipper.models import ClipCandidate
    from clipper.profiles import resolve_profile
    from clipper.stages.select import closing_is_present

    long_transcript = Transcript(
        video_id="q",
        language="pt",
        duration=300.0,
        segments=[
            span(i, i * 30.0, i * 30.0 + 28.0, f"frase numero {i} do assunto.") for i in range(8)
        ],
    )
    del resolve_profile
    early = ClipCandidate(
        segments=[TimeRange(start=0.0, end=240.0)], score=90, closing="frase numero 0 do assunto"
    )
    late = ClipCandidate(
        segments=[TimeRange(start=0.0, end=240.0)], score=90, closing="frase numero 7 do assunto"
    )
    assert not closing_is_present(long_transcript, early)
    assert closing_is_present(long_transcript, late)
