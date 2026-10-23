"""The comment question above the frame (SPEC 7.3 step 5)."""

from __future__ import annotations

import pytest

from clipper.models import Clip, ClipCandidate, TimeRange
from clipper.profiles import resolve_profile
from clipper.stages import select
from clipper.stages.render import banner_position
from clipper.video import subtitles
from clipper.video.ffmpeg import MediaInfo


def media(width: int, height: int) -> MediaInfo:
    return MediaInfo(
        width=width, height=height, duration=60.0, fps=30.0, has_audio=True, has_video=True
    )


@pytest.fixture
def profile():
    return resolve_profile("short")


# --------------------------------------------------------------------------- #
# Where it goes
# --------------------------------------------------------------------------- #


def test_the_bar_of_a_16_9_source_puts_the_question_above_the_frame(profile):
    # 1080 wide fits 1920x1080 into 607px, leaving a 656px bar; centre is 328.
    assert banner_position(profile, media(1920, 1080), reframe="blur") == 328


def test_a_squarer_source_leaves_a_shorter_bar(profile):
    assert banner_position(profile, media(1440, 1080), reframe="blur") == 278


def test_letterbox_has_the_same_geometry_as_blur(profile):
    assert banner_position(profile, media(1920, 1080), reframe="letterbox") == banner_position(
        profile, media(1920, 1080), reframe="blur"
    )


def test_a_vertical_source_has_no_bar_to_write_in(profile):
    assert banner_position(profile, media(1080, 1920), reframe="blur") is None


def test_a_cropping_mode_never_gets_a_banner(profile):
    """The canvas is all image; the question would land on somebody's face."""
    for mode in ("center", "face", "none"):
        assert banner_position(profile, media(1920, 1080), reframe=mode) is None


def test_a_bar_too_short_to_use_is_refused(profile):
    """A nearly vertical source fills almost the whole canvas."""
    # 1080x1620 fits at full width, leaving 150px of bar - under the 192px
    # (10% of 1920) the profile asks for.
    assert banner_position(profile, media(1080, 1620), reframe="blur") is None


def test_the_minimum_bar_is_configurable(profile):
    relaxed = profile.model_copy(
        update={"banner": profile.banner.model_copy(update={"min_bar_pct": 0.05})}
    )
    assert banner_position(relaxed, media(1080, 1620), reframe="blur") == 75


# --------------------------------------------------------------------------- #
# What it draws
# --------------------------------------------------------------------------- #


def test_the_question_spans_the_whole_clip(profile):
    event = subtitles.banner_event(subtitles.Banner(text="E voce?", y=328, end=45.0), profile)
    assert event.startswith("Dialogue: 0,0:00:00.00,0:00:45.00,Banner,")
    assert "\\pos(540,328)" in event


def test_a_long_question_wraps(profile):
    event = subtitles.banner_event(
        subtitles.Banner(text="O que voce nao levaria na marmita?", y=328, end=45.0), profile
    )
    assert "\\N" in event


def test_build_ass_draws_the_banner_when_there_is_one(profile, tmp_path):
    from clipper.models import Segment, Transcript, Word

    transcript = Transcript(
        video_id="v",
        language="pt",
        duration=10.0,
        segments=[
            Segment(
                id=0,
                start=0.0,
                end=2.0,
                text="oi tudo bem",
                words=[
                    Word(text="oi", start=0.0, end=0.6),
                    Word(text="tudo", start=0.6, end=1.2),
                    Word(text="bem", start=1.2, end=2.0),
                ],
            )
        ],
    )
    doc = subtitles.build_ass(
        transcript,
        [TimeRange(start=0.0, end=10.0)],
        [0.0],
        profile,
        banner=subtitles.Banner(text="E voce?", y=328, end=10.0),
    )
    assert "Style: Banner," in doc
    assert ",Banner,," in doc


def test_an_empty_question_draws_nothing(profile):
    from clipper.models import Transcript

    transcript = Transcript(video_id="v", language="pt", duration=10.0, segments=[])
    doc = subtitles.build_ass(
        transcript,
        [TimeRange(start=0.0, end=10.0)],
        [0.0],
        profile,
        banner=subtitles.Banner(text="   ", y=328, end=10.0),
    )
    assert ",Banner,," not in doc


# --------------------------------------------------------------------------- #
# Where it comes from
# --------------------------------------------------------------------------- #


def test_the_question_reaches_the_clip_from_the_model(profile):
    candidate = ClipCandidate(
        segments=[TimeRange(start=0.0, end=30.0)],
        comment_prompt="O que voce nao levaria na marmita?",
    )
    clip = select.to_clips([candidate], profile)[0]
    assert clip.comment_prompt == "O que voce nao levaria na marmita?"


def test_a_clip_without_a_question_is_normal(profile):
    clip = Clip(index=1, slug="x", profile="short", segments=[TimeRange(start=0.0, end=30.0)])
    assert clip.comment_prompt == ""
