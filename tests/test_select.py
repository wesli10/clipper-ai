"""Selection stage - prompt building and the mocked end-to-end pass (SPEC 6)."""

from __future__ import annotations

import json

import pytest

from clipper.llm.base import BaseLLMProvider
from clipper.models import Segment, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.stages.select import build_prompt, format_transcript, select_clips, to_clips
from clipper.utils.errors import InputError


def span(idx: int, start: float, end: float, text: str) -> Segment:
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
def transcript() -> Transcript:
    segments = [
        span(i, i * 30.0, i * 30.0 + 28.0, f"bloco numero {i} com uma ideia completa aqui.")
        for i in range(20)
    ]
    return Transcript(video_id="vid", language="pt", duration=600.0, segments=segments)


class CannedProvider(BaseLLMProvider):
    name = "canned"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return json.dumps(self.payload)


# --- prompt ---------------------------------------------------------------- #


def test_transcript_lines_carry_a_timestamp(transcript):
    text = format_transcript(transcript)
    first = text.splitlines()[0]
    assert first.startswith("[00:00:00.0 | 0.00s -> 28.00s] ")
    assert "bloco numero 0" in first
    assert len(text.splitlines()) == len(transcript.segments)


def test_markers_carry_the_seconds_the_schema_expects(transcript):
    """A small model will not convert hh:mm:ss to seconds reliably."""
    line = format_transcript(transcript).splitlines()[5]
    segment = transcript.segments[5]
    assert f"{segment.start:.2f}s -> {segment.end:.2f}s" in line
    assert "[00:02:30.0 |" in line


def test_short_prompt_has_every_placeholder_filled(transcript):
    profile = resolve_profile("short")
    prompt = build_prompt(profile, transcript)
    assert "{transcript}" not in prompt
    assert "{n_clips}" not in prompt
    assert "{duration_max}" not in prompt
    # n_clips + 3 candidates (SPEC 6.2)
    assert "Teto de cortes: 8" in prompt
    assert "teto, n\u00e3o uma meta" in prompt
    assert "mínimo 20s" in prompt
    assert "bloco numero 5" in prompt


def test_long_prompt_asks_for_multi_segment_clips(transcript):
    profile = resolve_profile("long")
    prompt = build_prompt(profile, transcript)
    assert "{max_segments}" not in prompt
    # n_clips + 2 candidates (SPEC 6.3)
    assert "Teto de cortes: 4" in prompt
    assert "1 a 4" in prompt


def test_prompt_honours_the_n_override(transcript):
    prompt = build_prompt(resolve_profile("short"), transcript, n_clips=2)
    assert "Teto de cortes: 5" in prompt


# --- end-to-end with a mocked provider ------------------------------------- #


def test_select_snaps_the_llm_timestamps(transcript):
    """The model proposes sloppy times; the output must land on segment edges."""
    provider = CannedProvider(
        {
            "clips": [
                {
                    "segments": [{"start": 91.3, "end": 133.7}],
                    "hook": "bloco numero 3",
                    "reason": "boa ideia",
                    "score": 88,
                    "self_contained": True,
                    "topic": "uma ideia completa",
                }
            ]
        }
    )
    result = select_clips(transcript, resolve_profile("short"), provider, pad_before=0, pad_after=0)
    assert len(result.clips) == 1
    interval = result.clips[0].segments[0]
    starts = {s.start for s in transcript.segments}
    ends = {s.end for s in transcript.segments}
    assert interval.start in starts
    assert interval.end in ends
    assert interval.start != 91.3


def test_select_numbers_and_slugs_the_clips(transcript):
    provider = CannedProvider(
        {
            "clips": [
                {"segments": [{"start": 0.0, "end": 58.0}], "score": 70, "topic": "Ação e Foco"},
                {"segments": [{"start": 300.0, "end": 358.0}], "score": 90, "topic": "Sono"},
            ]
        }
    )
    result = select_clips(transcript, resolve_profile("short"), provider)
    assert [c.index for c in result.clips] == [1, 2]
    # ranked by score, so "Sono" comes first
    assert result.clips[0].slug == "sono"
    assert result.clips[1].slug == "acao-e-foco"
    assert result.clips[0].dir_name == "01_sono"


def test_select_rejects_a_candidate_that_is_too_short(transcript):
    short_segments = Transcript(
        video_id="vid",
        language="pt",
        duration=100.0,
        segments=[span(i, i * 5.0, i * 5.0 + 4.5, f"frase curta numero {i}.") for i in range(20)],
    )
    provider = CannedProvider(
        {
            "clips": [
                {"segments": [{"start": 0.0, "end": 30.0}], "score": 90, "topic": "bom"},
                {"segments": [{"start": 60.0, "end": 62.0}], "score": 40, "topic": "curto demais"},
            ]
        }
    )
    result = select_clips(short_segments, resolve_profile("short"), provider)
    assert [c.topic for c in result.clips] == ["bom"]
    assert len(result.rejected) == 1
    assert "abaixo do minimo do perfil" in result.rejected[0].reason


def test_select_rejects_a_worse_overlapping_candidate(transcript):
    provider = CannedProvider(
        {
            "clips": [
                {"segments": [{"start": 0.0, "end": 58.0}], "score": 90, "topic": "bom"},
                {"segments": [{"start": 5.0, "end": 55.0}], "score": 40, "topic": "quase igual"},
            ]
        }
    )
    result = select_clips(transcript, resolve_profile("short"), provider)
    assert [c.topic for c in result.clips] == ["bom"]
    assert len(result.rejected) == 1
    assert "sobrepoe" in result.rejected[0].reason


def test_select_truncates_to_n_clips(transcript):
    provider = CannedProvider(
        {
            "clips": [
                {"segments": [{"start": i * 60.0, "end": i * 60.0 + 58.0}], "score": 50 + i}
                for i in range(9)
            ]
        }
    )
    result = select_clips(transcript, resolve_profile("short"), provider)
    assert len(result.clips) == 5
    assert len(result.raw) == 9


def test_select_on_an_empty_transcript_is_an_input_error():
    empty = Transcript(video_id="v", language="pt", duration=0.0, segments=[])
    with pytest.raises(InputError, match="vazia"):
        select_clips(empty, resolve_profile("short"), CannedProvider({"clips": []}))


def test_to_clips_falls_back_to_a_generic_slug():
    from clipper.models import ClipCandidate, TimeRange

    clips = to_clips(
        [ClipCandidate(segments=[TimeRange(start=0, end=30)], score=10)], resolve_profile("short")
    )
    assert clips[0].slug == "corte-1"


# --- retry when every candidate is rejected --------------------------------- #


class TwoAnswerProvider(BaseLLMProvider):
    """First answer breaks the rules, second one obeys them."""

    name = "two-answer"

    def __init__(self, first: dict, second: dict) -> None:
        self.answers = [first, second]
        self.prompts: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.prompts.append(user)
        index = min(len(self.prompts) - 1, len(self.answers) - 1)
        return json.dumps(self.answers[index])


TOO_SHORT = {
    "clips": [{"segments": [{"start": 0.0, "end": 40.0}], "score": 90, "topic": "curto demais"}]
}
LONG_ENOUGH = {
    "clips": [{"segments": [{"start": 0.0, "end": 178.0}], "score": 90, "topic": "agora serve"}]
}


def test_a_fully_rejected_answer_triggers_one_retry(transcript):
    profile = resolve_profile("long", {"long": {"duration_min": 120.0}})
    provider = TwoAnswerProvider(TOO_SHORT, LONG_ENOUGH)

    result = select_clips(transcript, profile, provider)

    assert len(provider.prompts) == 2
    assert [c.topic for c in result.clips] == ["agora serve"]


def test_the_retry_tells_the_model_what_was_wrong(transcript):
    profile = resolve_profile("long", {"long": {"duration_min": 120.0}})
    provider = TwoAnswerProvider(TOO_SHORT, LONG_ENOUGH)

    select_clips(transcript, profile, provider)

    retry_prompt = provider.prompts[1]
    assert "curto demais" in retry_prompt
    assert "abaixo do minimo do perfil" in retry_prompt
    assert "120s" in retry_prompt


def test_a_partly_valid_answer_does_not_retry(transcript):
    """Only a wholesale rejection is worth another call."""
    answer = {
        "clips": [
            {"segments": [{"start": 0.0, "end": 178.0}], "score": 90, "topic": "bom"},
            {"segments": [{"start": 300.0, "end": 320.0}], "score": 50, "topic": "curto"},
        ]
    }
    profile = resolve_profile("long", {"long": {"duration_min": 120.0}})
    provider = TwoAnswerProvider(answer, answer)

    result = select_clips(transcript, profile, provider)

    assert len(provider.prompts) == 1
    assert [c.topic for c in result.clips] == ["bom"]


def test_a_second_bad_answer_is_reported_not_raised(transcript):
    profile = resolve_profile("long", {"long": {"duration_min": 120.0}})
    provider = TwoAnswerProvider(TOO_SHORT, TOO_SHORT)

    result = select_clips(transcript, profile, provider)

    assert len(provider.prompts) == 2
    assert result.clips == []
    assert result.rejected


def test_an_empty_answer_does_not_retry(transcript):
    """Nothing was rejected, so there is nothing to correct."""
    provider = TwoAnswerProvider({"clips": []}, LONG_ENOUGH)
    result = select_clips(transcript, resolve_profile("short"), provider)
    assert len(provider.prompts) == 1
    assert result.clips == []
