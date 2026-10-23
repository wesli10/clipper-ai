"""The end card joined after a clip (SPEC 7.3 step 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipper.profiles import resolve_profile
from clipper.utils.errors import RenderError
from clipper.video import outro


@pytest.fixture
def profile(tmp_path):
    return resolve_profile("short").model_copy(update={"outro": tmp_path / "outro.mp4"})


def test_a_profile_without_an_outro_prepares_nothing(tmp_path):
    assert outro.prepare(resolve_profile("short"), tmp_path, crf=18, preset="fast") is None


def test_a_missing_outro_says_where_to_fix_it(profile, tmp_path):
    with pytest.raises(RenderError) as exc:
        outro.prepare(profile, tmp_path / "cache", crf=18, preset="fast")
    assert "outro.mp4" in str(exc.value)
    assert "config.yaml" in str(exc.value)


# --------------------------------------------------------------------------- #
# Normalising whatever the generator produced
# --------------------------------------------------------------------------- #


def test_the_whole_card_is_kept_and_padded_never_cropped():
    """Cropping an end card cuts the message in half."""
    args = outro.normalise_args(
        Path("in.mp4"),
        Path("out.mp4"),
        width=1080,
        height=1920,
        fps=30,
        crf=18,
        preset="fast",
        has_audio=True,
    )
    chain = args[args.index("-vf") + 1]
    assert "force_original_aspect_ratio=decrease" in chain
    assert "pad=1080:1920" in chain
    assert "crop=" not in chain


def test_a_silent_card_gets_an_audio_track_of_the_right_shape():
    """acrossfade needs two audio streams, and matching ones."""
    args = outro.normalise_args(
        Path("in.mp4"),
        Path("out.mp4"),
        width=1080,
        height=1920,
        fps=30,
        crf=18,
        preset="fast",
        has_audio=False,
    )
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in args
    assert "-shortest" in args
    assert args[args.index("-ar") + 1] == "48000"


def test_a_card_that_already_has_audio_gets_no_second_input():
    args = outro.normalise_args(
        Path("in.mp4"),
        Path("out.mp4"),
        width=1080,
        height=1920,
        fps=30,
        crf=18,
        preset="fast",
        has_audio=True,
    )
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" not in args
    assert args.count("-i") == 1


def test_the_frame_rate_is_forced_to_the_profile():
    args = outro.normalise_args(
        Path("in.mp4"),
        Path("out.mp4"),
        width=1080,
        height=1920,
        fps=30,
        crf=18,
        preset="fast",
        has_audio=True,
    )
    assert args[args.index("-r") + 1] == "30"


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def test_the_same_file_and_shape_reuse_one_normalised_copy(tmp_path):
    source = tmp_path / "outro.mp4"
    source.write_bytes(b"card")
    assert outro.cache_key(source, 1080, 1920, 30) == outro.cache_key(source, 1080, 1920, 30)


def test_a_different_shape_is_a_different_copy(tmp_path):
    source = tmp_path / "outro.mp4"
    source.write_bytes(b"card")
    assert outro.cache_key(source, 1080, 1920, 30) != outro.cache_key(source, 1920, 1080, 30)


def test_editing_the_card_invalidates_the_copy(tmp_path):
    source = tmp_path / "outro.mp4"
    source.write_bytes(b"card")
    before = outro.cache_key(source, 1080, 1920, 30)
    source.write_bytes(b"a different card")
    assert outro.cache_key(source, 1080, 1920, 30) != before
