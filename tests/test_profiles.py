from __future__ import annotations

import pytest

from clipper.profiles import (
    BUILTIN_PROFILES,
    ProfileError,
    available_profiles,
    resolve_profile,
)


def test_builtin_short_defaults():
    p = resolve_profile("short")
    assert (p.output_width, p.output_height) == (1080, 1920)
    assert p.is_vertical
    assert p.reframe == "face"
    assert p.boundary_mode == "sentence"
    assert p.multi_segment is False
    assert p.max_segments == 1
    assert p.subtitle_style == "karaoke"
    assert p.subtitle.max_words_per_line == 4
    assert p.select_prompt.name == "select_short.md"


def test_builtin_long_defaults():
    p = resolve_profile("long")
    assert (p.output_width, p.output_height) == (1920, 1080)
    assert not p.is_vertical
    assert p.reframe == "none"
    assert p.boundary_mode == "topic"
    assert p.multi_segment is True
    assert p.max_segments == 4
    assert p.subtitle_style == "classic"
    assert p.subtitle.max_lines == 2


def test_override_merges_nested_subtitle():
    p = resolve_profile("short", {"short": {"n_clips": 8, "subtitle": {"size": 90}}})
    assert p.n_clips == 8
    assert p.subtitle.size == 90
    # untouched nested fields keep the built-in value
    assert p.subtitle.max_words_per_line == 4
    assert p.subtitle.highlight_color == "#FFD400"


def test_extends_creates_custom_profile():
    p = resolve_profile(
        "long_vertical",
        {
            "long_vertical": {
                "extends": "long",
                "output_width": 1080,
                "output_height": 1920,
                "reframe": "blur",
            }
        },
    )
    assert p.name == "long_vertical"
    assert (p.output_width, p.output_height) == (1080, 1920)
    assert p.reframe == "blur"
    # inherited from long
    assert p.boundary_mode == "topic"
    assert p.max_segments == 4


def test_extends_chain():
    overrides = {
        "a": {"extends": "short", "n_clips": 9},
        "b": {"extends": "a", "fps": 60},
    }
    p = resolve_profile("b", overrides)
    assert p.n_clips == 9
    assert p.fps == 60
    assert p.reframe == "face"


def test_extends_cycle_is_rejected():
    with pytest.raises(ProfileError, match="ciclo"):
        resolve_profile("a", {"a": {"extends": "b"}, "b": {"extends": "a"}})


def test_unknown_profile_is_rejected():
    with pytest.raises(ProfileError, match="desconhecido"):
        resolve_profile("nope")


def test_punch_in_with_reframe_none_is_rejected():
    with pytest.raises(ProfileError, match="punch_in"):
        resolve_profile("long", {"long": {"zoom": "punch_in"}})


def test_punch_in_allowed_when_cropping():
    p = resolve_profile("short", {"short": {"zoom": "punch_in"}})
    assert p.zoom == "punch_in"


def test_duration_target_must_be_inside_range():
    with pytest.raises(ProfileError, match="duration_target"):
        resolve_profile("short", {"short": {"duration_target": 300}})


def test_max_segments_must_be_one_without_multi_segment():
    with pytest.raises(ProfileError, match="max_segments"):
        resolve_profile("short", {"short": {"max_segments": 3}})


def test_unknown_field_is_rejected():
    with pytest.raises(ProfileError):
        resolve_profile("short", {"short": {"reslution": 1080}})


def test_available_profiles_lists_builtins_first():
    assert available_profiles({"zzz": {"extends": "short"}}) == ["long", "quiz", "short", "zzz"]
    assert set(BUILTIN_PROFILES) == {"short", "long", "quiz"}


def test_yaml_reading_off_as_a_boolean_still_means_off():
    """YAML 1.1 turns a bare `off` into False before it ever reaches pydantic."""
    assert resolve_profile("long", {"long": {"subtitle_style": False}}).subtitle_style == "off"


def test_a_true_there_is_still_an_error():
    with pytest.raises(ProfileError):
        resolve_profile("long", {"long": {"subtitle_style": True}})
