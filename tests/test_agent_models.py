"""Profile overrides - the only render knobs the agent may turn (SPEC-agent 5.8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clipper.agent.models import ProfileOverride
from clipper.profiles import ProfileError, resolve_profile


@pytest.fixture
def short(prompts_dir):
    return resolve_profile("short", prompts_dir=prompts_dir)


def test_an_empty_override_leaves_the_profile_alone(short):
    override = ProfileOverride()
    assert override.is_empty()
    assert override.apply(short) == short


def test_whitelisted_fields_reach_the_profile(short):
    effective = ProfileOverride(n_clips=3, tail_roll_s=2.5, subtitle_style="off").apply(short)
    assert (effective.n_clips, effective.tail_roll_s) == (3, 2.5)
    assert effective.subtitle_style == "off"
    # Everything else is untouched: the override is a patch, not a new profile.
    assert (effective.output_width, effective.output_height) == (1080, 1920)
    assert effective.name == "short"


def test_banner_is_nested_but_addressed_flat(short):
    assert ProfileOverride(banner_enabled=False).apply(short).banner.enabled is False
    assert short.banner.enabled is True


@pytest.mark.parametrize("field", ["reframe", "zoom", "output_width", "subtitle", "outro"])
def test_everything_outside_the_whitelist_is_refused(field):
    with pytest.raises(ValidationError):
        ProfileOverride.model_validate({field: "x"})


def test_durations_may_move_inside_the_configured_range(short):
    effective = ProfileOverride(duration_min=30.0, duration_target=50.0).apply(short)
    assert (effective.duration_min, effective.duration_target) == (30.0, 50.0)


def test_durations_may_not_widen_the_range(short):
    with pytest.raises(ProfileError, match="fora da faixa"):
        ProfileOverride(duration_max=600.0).apply(short)


def test_the_profile_validator_still_runs(short):
    """A card without subtitles is impossible, override or not."""
    with pytest.raises(ProfileError, match="card_seconds exige legenda"):
        ProfileOverride(card_seconds=2.0, subtitle_style="off").apply(short)


def test_an_inconsistent_range_is_refused(short):
    with pytest.raises(ProfileError, match="duration_target"):
        ProfileOverride(duration_min=80.0).apply(short)
