"""LLM response handling - SPEC 6.4 and 11.1. No real API is ever called."""

from __future__ import annotations

import pytest

from clipper.llm.base import BaseLLMProvider, extract_json, parse_json_response
from clipper.models import SelectionResponse
from clipper.utils.errors import ExternalError

GOOD = """
{"clips": [{"segments": [{"start": 10.0, "end": 40.0}], "hook": "oi", "score": 80,
 "reason": "r", "self_contained": true, "topic": "t"}]}
"""


class ScriptedProvider(BaseLLMProvider):
    """Returns canned answers in order; records how many calls it received."""

    name = "scripted"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


def test_plain_json_parses():
    parsed = parse_json_response(GOOD, SelectionResponse)
    assert len(parsed.clips) == 1
    assert parsed.clips[0].segments[0].start == 10.0


def test_markdown_fence_is_stripped():
    parsed = parse_json_response(f"```json\n{GOOD}\n```", SelectionResponse)
    assert len(parsed.clips) == 1


def test_bare_fence_is_stripped():
    parsed = parse_json_response(f"```\n{GOOD}\n```", SelectionResponse)
    assert len(parsed.clips) == 1


def test_chatter_around_the_json_is_ignored():
    text = f"Claro! Aqui estao os cortes:\n{GOOD}\nEspero que ajude."
    assert len(parse_json_response(text, SelectionResponse).clips) == 1


def test_segments_out_of_order_are_accepted_and_kept():
    text = """{"clips": [{"segments": [{"start": 300, "end": 340}, {"start": 10, "end": 50}],
               "score": 70}]}"""
    parsed = parse_json_response(text, SelectionResponse)
    assert [s.start for s in parsed.clips[0].segments] == [300.0, 10.0]


def test_inverted_interval_is_rejected():
    text = '{"clips": [{"segments": [{"start": 50, "end": 10}], "score": 70}]}'
    with pytest.raises(ValueError, match="schema"):
        parse_json_response(text, SelectionResponse)


def test_clip_without_segments_is_rejected():
    with pytest.raises(ValueError, match="schema"):
        parse_json_response('{"clips": [{"segments": [], "score": 70}]}', SelectionResponse)


@pytest.mark.parametrize("text", ["", "sem json aqui", "[1, 2, 3]"])
def test_answers_without_an_object_are_rejected(text):
    with pytest.raises(ValueError):
        extract_json(text)


def test_a_bad_answer_triggers_exactly_one_retry():
    provider = ScriptedProvider("desculpe, nao consegui", GOOD)
    parsed = provider.complete_json("sys", "user", SelectionResponse)
    assert len(parsed.clips) == 1
    assert len(provider.calls) == 2
    assert "SOMENTE" in provider.calls[1][1]


def test_two_bad_answers_raise_an_external_error():
    provider = ScriptedProvider("nada", "nada de novo")
    with pytest.raises(ExternalError, match="duas tentativas"):
        provider.complete_json("sys", "user", SelectionResponse)
    assert len(provider.calls) == 2


def test_a_good_answer_never_retries():
    provider = ScriptedProvider(GOOD)
    provider.complete_json("sys", "user", SelectionResponse)
    assert len(provider.calls) == 1


# --- Gemini quota handling ------------------------------------------------- #


def test_a_zero_quota_429_is_not_retryable():
    """`limit: 0` means the key has no access at all; waiting cannot help."""
    from clipper.llm.gemini import has_no_quota

    exc = Exception(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded for "
        "metric: generate_content_free_tier_requests, limit: 0, model: gemini-3.1-pro'}}"
    )
    assert has_no_quota(exc)


def test_an_ordinary_rate_limit_is_retryable():
    from clipper.llm.gemini import _is_retryable, has_no_quota

    exc = Exception("429 RESOURCE_EXHAUSTED. limit: 60, please slow down")
    assert not has_no_quota(exc)
    assert _is_retryable(exc)


def test_the_api_suggested_delay_is_honoured():
    from clipper.llm.gemini import retry_after

    assert retry_after(Exception("Please retry in 38.695296834s.")) == pytest.approx(
        38.695, abs=1e-3
    )
    assert retry_after(Exception("sem sugestao")) is None


def test_a_suggested_delay_is_capped():
    from clipper.llm.gemini import MAX_RETRY_AFTER_S, retry_after

    assert retry_after(Exception("Please retry in 99999s.")) == MAX_RETRY_AFTER_S
