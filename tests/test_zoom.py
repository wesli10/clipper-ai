"""Dynamic punch-in - SPEC 7.3 step 4."""

from __future__ import annotations

from itertools import pairwise

import pytest

from clipper.models import Word
from clipper.profiles import ProfileError, resolve_profile
from clipper.video.zoom import (
    MAX_SCALE,
    punch_in_filter,
    scale_expression,
    sentence_boundaries,
    zoom_windows,
)


def words(spec: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


def test_punctuation_is_a_sentence_boundary():
    got = sentence_boundaries(words([("uma.", 0, 1), ("duas", 1.1, 2), ("tres", 2.1, 3)]))
    assert got == [1.0]


def test_a_long_pause_is_a_sentence_boundary():
    got = sentence_boundaries(words([("uma", 0, 1), ("duas", 2.0, 3), ("tres", 3.1, 4)]))
    assert 1.0 in got


def test_a_short_pause_is_not_a_boundary():
    assert sentence_boundaries(words([("uma", 0, 1), ("duas", 1.2, 2)])) == []


def test_windows_alternate_and_stay_inside_the_clip():
    boundaries = [float(i) for i in range(1, 12)]
    windows = zoom_windows(boundaries, 12.0)
    assert windows
    assert all(w.end <= 12.0 for w in windows)
    # zoomed and non-zoomed stretches alternate, so windows never touch
    for first, second in pairwise(windows):
        assert second.start > first.end


def test_no_boundaries_means_no_zoom():
    assert zoom_windows([], 60.0) == []
    assert scale_expression([]) == "1"


def test_expression_holds_the_maximum_scale_inside_a_window():
    windows = zoom_windows([1.0, 2.0, 3.0, 4.0, 5.0], 6.0)
    expression = scale_expression(windows)
    assert f"{MAX_SCALE:.4f}" in expression
    assert "between(t," in expression


def test_zoom_amplitude_stays_within_eight_percent():
    assert MAX_SCALE <= 1.08


def test_filter_keeps_the_output_size_constant():
    filter_str = punch_in_filter(1080, 1920)
    assert "crop=1080:1920" in filter_str
    assert "eval=frame" in filter_str


def test_zoom_is_rejected_on_a_profile_without_a_crop():
    with pytest.raises(ProfileError, match="punch_in"):
        resolve_profile("long", {"long": {"zoom": "punch_in"}})


def test_zoom_is_allowed_on_a_cropping_profile():
    assert resolve_profile("short", {"short": {"zoom": "punch_in"}}).zoom == "punch_in"
