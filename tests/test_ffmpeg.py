"""ffmpeg command construction - SPEC 7.3."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipper.profiles import resolve_profile
from clipper.stages.render import build_video_filters, output_size
from clipper.video import ffmpeg

SRC = Path("/tmp/source.mp4")
DST = Path("/tmp/out.mp4")


def media(width: int, height: int, duration: float = 60.0) -> ffmpeg.MediaInfo:
    return ffmpeg.MediaInfo(
        width=width, height=height, duration=duration, fps=30.0, has_audio=True, has_video=True
    )


# --- cutting --------------------------------------------------------------- #


def test_cut_re_encodes_rather_than_copying():
    args = ffmpeg.cut_args(SRC, DST, 10.0, 40.0)
    assert "-c" not in args or "copy" not in args
    assert "libx264" in args
    assert args[args.index("-ss") + 1] == "10.000"
    assert args[args.index("-to") + 1] == "40.000"


def test_cut_never_seeks_before_zero():
    args = ffmpeg.cut_args(SRC, DST, -5.0, 10.0)
    assert args[args.index("-ss") + 1] == "0.000"


# --- sizing ---------------------------------------------------------------- #


def test_fit_size_scales_down_to_the_profile():
    assert ffmpeg.fit_size(3840, 2160, 1920, 1080) == (1920, 1080)


def test_fit_size_never_upscales():
    assert ffmpeg.fit_size(1280, 720, 1920, 1080) == (1280, 720)


def test_fit_size_returns_even_dimensions():
    width, height = ffmpeg.fit_size(1919, 1079, 1920, 1080)
    assert width % 2 == 0 and height % 2 == 0


def test_long_profile_keeps_a_small_source_resolution():
    """SPEC 4.2: a long never upscales."""
    assert output_size(resolve_profile("long"), media(1280, 720)) == (1280, 720)


def test_long_profile_scales_a_big_source_down():
    assert output_size(resolve_profile("long"), media(3840, 2160)) == (1920, 1080)


def test_short_profile_always_uses_the_exact_profile_resolution():
    assert output_size(resolve_profile("short"), media(1280, 720)) == (1080, 1920)
    assert output_size(resolve_profile("short"), media(3840, 2160)) == (1080, 1920)


# --- filter order ---------------------------------------------------------- #


def test_subtitles_are_always_the_last_filter():
    filters = build_video_filters(
        resolve_profile("short"),
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="center",
        zoom="none",
        subtitles_path=Path("/tmp/s.ass"),
    )
    assert filters[-1].startswith("ass=")


def test_zoom_comes_after_the_crop_and_before_the_subtitles():
    profile = resolve_profile("short", {"short": {"zoom": "punch_in"}})
    filters = build_video_filters(
        profile,
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="center",
        zoom="punch_in",
        subtitles_path=Path("/tmp/s.ass"),
    )
    assert "crop" in filters[0]
    assert "eval=frame" in filters[1]
    assert filters[2].startswith("ass=")


def test_reframe_none_only_scales():
    filters = build_video_filters(
        resolve_profile("long"),
        media(1920, 1080),
        width=1920,
        height=1080,
        reframe="none",
        zoom="none",
        subtitles_path=None,
    )
    assert filters == ["scale=1920:1080,setsar=1"]


def test_blur_mode_builds_a_background():
    filters = build_video_filters(
        resolve_profile("short", {"short": {"reframe": "blur"}}),
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="blur",
        zoom="none",
        subtitles_path=None,
    )
    assert "boxblur" in filters[0]
    assert "overlay" in filters[0]


def test_face_filter_is_used_when_available():
    filters = build_video_filters(
        resolve_profile("short"),
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="face",
        zoom="none",
        subtitles_path=None,
        face_filter="crop=608:1080:'100':0",
    )
    assert filters[0] == "crop=608:1080:'100':0"
    assert filters[1] == "scale=1080:1920,setsar=1"


def test_face_falls_back_to_a_centre_crop_without_a_filter():
    filters = build_video_filters(
        resolve_profile("short"),
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="face",
        zoom="none",
        subtitles_path=None,
        face_filter=None,
    )
    assert filters[0].startswith("crop=")


# --- audio and encoding ---------------------------------------------------- #


def test_loudnorm_uses_the_profile_target():
    assert ffmpeg.loudnorm_filter(-14.0) == "loudnorm=I=-14:TP=-1.5:LRA=11"
    assert ffmpeg.loudnorm_filter(-16.5) == "loudnorm=I=-16.5:TP=-1.5:LRA=11"


def test_encode_args_carry_crf_preset_and_fps():
    args = ffmpeg.encode_args(
        SRC,
        DST,
        video_filters=["scale=2:2"],
        audio_filters=["anull"],
        crf=20,
        preset="fast",
        fps=60,
    )
    assert args[args.index("-crf") + 1] == "20"
    assert args[args.index("-preset") + 1] == "fast"
    assert args[args.index("-r") + 1] == "60"
    assert args[args.index("-vf") + 1] == "scale=2:2"
    assert "+faststart" in args


def test_subtitles_filter_escapes_the_path():
    got = ffmpeg.subtitles_filter(Path("/tmp/dir with:colon/s.ass"))
    assert got.startswith("ass='")
    assert r"\:" in got


def test_thumbnail_is_taken_at_the_configured_second():
    args = ffmpeg.thumbnail_args(SRC, Path("/tmp/t.jpg"), at=1.0)
    assert args[args.index("-ss") + 1] == "1.000"
    assert args[args.index("-frames:v") + 1] == "1"


@pytest.mark.parametrize(("value", "expected"), [(1080, 1080), (1081, 1080), (2, 2)])
def test_even_rounds_down(value, expected):
    assert ffmpeg.even(value) == expected


def test_cut_and_final_encode_use_different_quality():
    """Three encode passes at the delivery CRF compound the loss (SSIM 0.9946
    vs 0.9960 when the intermediates stay at 16)."""
    from clipper.config import RenderSettings

    render = RenderSettings(crf=21)
    assert render.intermediate_crf < render.crf

    cut = ffmpeg.cut_args(SRC, DST, 0.0, 10.0, crf=render.intermediate_crf)
    assert cut[cut.index("-crf") + 1] == "16"

    final = ffmpeg.encode_args(
        SRC, DST, video_filters=[], audio_filters=[], crf=render.crf, preset="medium", fps=30
    )
    assert final[final.index("-crf") + 1] == "21"


def test_letterbox_keeps_the_whole_frame():
    """The opposite trade to `center`: nothing cropped, bars instead."""
    filters = build_video_filters(
        resolve_profile("short", {"short": {"reframe": "letterbox"}}),
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="letterbox",
        zoom="none",
        subtitles_path=None,
    )
    assert "crop" not in filters[0]
    assert "force_original_aspect_ratio=decrease" in filters[0]
    assert "pad=1080:1920" in filters[0]


def test_letterbox_fills_the_profile_canvas():
    profile = resolve_profile("short", {"short": {"reframe": "letterbox"}})
    assert output_size(profile, media(1920, 1080)) == (1080, 1920)
    # even a smaller source still produces the full vertical canvas
    assert output_size(profile, media(1280, 720)) == (1080, 1920)


def test_the_bar_colour_is_configurable():
    from clipper.video.ffmpeg import letterbox_filter

    assert "color=0x101820" in letterbox_filter(1080, 1920, "#101820")


def test_zoom_is_rejected_on_letterbox():
    """punch_in over letterboxed video would upscale, as with reframe none."""
    from clipper.profiles import ProfileError

    with pytest.raises(ProfileError, match="punch_in"):
        resolve_profile("short", {"short": {"reframe": "letterbox", "zoom": "punch_in"}})


def test_blur_background_is_dimmed_by_default():
    """Burned captions sit on the blurred background and need the contrast."""
    from clipper.video.ffmpeg import blur_pad_filter

    assert "eq=brightness=-0.3" in blur_pad_filter(1080, 1920)


def test_blur_dimming_can_be_switched_off():
    from clipper.video.ffmpeg import blur_pad_filter

    plain = blur_pad_filter(1080, 1920, brightness=0.0, saturation=1.0)
    assert "eq=" not in plain
    assert "boxblur" in plain


def test_the_profile_drives_the_dimming():
    profile = resolve_profile("short", {"short": {"reframe": "blur", "blur_brightness": -0.5}})
    filters = build_video_filters(
        profile,
        media(1920, 1080),
        width=1080,
        height=1920,
        reframe="blur",
        zoom="none",
        subtitles_path=None,
    )
    assert "brightness=-0.5" in filters[0]


# --- loudness em duas passadas --------------------------------------------- #

LOUDNORM_JSON = """[Parsed_loudnorm_0 @ 0x5] 
{
	"input_i" : "-19.42",
	"input_tp" : "-3.20",
	"input_lra" : "7.40",
	"input_thresh" : "-29.68",
	"output_i" : "-14.02",
	"target_offset" : "0.19"
}
"""


def test_the_measuring_pass_is_read_back():
    stats = ffmpeg.parse_loudnorm_stats(LOUDNORM_JSON)
    assert stats is not None
    assert stats.input_i == "-19.42" and stats.target_offset == "0.19"


def test_a_pass_that_printed_nothing_usable_measures_nothing():
    assert ffmpeg.parse_loudnorm_stats("sem json aqui") is None
    assert ffmpeg.parse_loudnorm_stats('{"outra": "coisa"}') is None


def test_without_a_measurement_the_filter_is_the_one_pass_one():
    assert ffmpeg.loudnorm_filter(-14.0) == "loudnorm=I=-14:TP=-1.5:LRA=11"


def test_with_a_measurement_the_filter_carries_it():
    """One pass only approximates: measured over real clips it missed by up to 2.9 LU."""
    stats = ffmpeg.parse_loudnorm_stats(LOUDNORM_JSON)
    built = ffmpeg.loudnorm_filter(-14.0, stats)

    assert "measured_I=-19.42" in built
    assert "measured_thresh=-29.68" in built
    assert "offset=0.19" in built and "linear=true" in built


def test_the_measurement_covers_exactly_the_cut_that_will_be_encoded(tmp_path):
    args = ffmpeg.loudnorm_measure_args(tmp_path / "a.mp4", target_i=-14.0, trim=(12.0, 40.5))
    assert args[args.index("-ss") + 1] == "12.000"
    assert args[args.index("-to") + 1] == "40.500"
    assert "print_format=json" in args[args.index("-af") + 1]


def test_without_a_trim_the_whole_file_is_measured(tmp_path):
    args = ffmpeg.loudnorm_measure_args(tmp_path / "a.mp4", target_i=-14.0)
    assert "-ss" not in args and "-to" not in args
