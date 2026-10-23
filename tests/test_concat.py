"""Crossfade offsets - SPEC 7.3 step 2 and SPEC 11.1."""

from __future__ import annotations

import pytest

from clipper.video.concat import build_filter, offsets_for


def test_single_part_starts_at_zero():
    offsets, total = offsets_for([42.0], 0.3)
    assert offsets == [0.0]
    assert total == pytest.approx(42.0)


def test_each_crossfade_shortens_the_timeline():
    offsets, total = offsets_for([10.0, 20.0], 0.3)
    assert offsets == [0.0, pytest.approx(9.7)]
    assert total == pytest.approx(29.7)


def test_offsets_accumulate_across_many_parts():
    offsets, total = offsets_for([10.0, 10.0, 10.0, 10.0], 0.3)
    assert offsets == [0.0, pytest.approx(9.7), pytest.approx(19.4), pytest.approx(29.1)]
    assert total == pytest.approx(39.1)


def test_a_zero_transition_is_a_plain_concatenation():
    offsets, total = offsets_for([10.0, 20.0, 5.0], 0.0)
    assert offsets == [0.0, 10.0, 30.0]
    assert total == pytest.approx(35.0)


def test_filter_has_one_crossfade_per_junction():
    graph = build_filter([10.0, 20.0, 30.0], 0.3)
    assert graph.count("xfade") == 2
    assert graph.count("acrossfade") == 2
    assert "[vout]" in graph and "[aout]" in graph


def test_filter_offsets_match_the_computed_timeline():
    graph = build_filter([10.0, 20.0], 0.3)
    assert "offset=9.700" in graph
    assert "duration=0.3" in graph


def test_filter_needs_at_least_two_parts():
    with pytest.raises(ValueError, match="dois intervalos"):
        build_filter([10.0], 0.3)
