"""Face-following crop - SPEC 7.3 step 3."""

from __future__ import annotations

from itertools import pairwise

import pytest

from clipper.video.faces import FaceBox, FaceSample
from clipper.video.reframe import (
    DEAD_ZONE_PCT,
    MIN_FACE_COVERAGE,
    coverage,
    crop_expression,
    pick_face_center,
    smooth_centers,
    to_scenes,
    to_steps,
)

WIDE = 1920 / 1080
VERTICAL = 1080 / 1920


def sample(t: float, *faces: tuple[float, float]) -> FaceSample:
    return FaceSample(
        t=t, faces=[FaceBox(cx=cx, cy=0.4, width=size, height=size) for cx, size in faces]
    )


# --- choosing a centre ----------------------------------------------------- #


def test_no_face_gives_no_centre():
    assert pick_face_center(sample(0.0), VERTICAL, WIDE) is None


def test_single_face_is_followed():
    assert pick_face_center(sample(0.0, (0.7, 0.1)), VERTICAL, WIDE) == pytest.approx(0.7)


def test_two_close_faces_are_centred_between_them():
    got = pick_face_center(sample(0.0, (0.45, 0.1), (0.55, 0.1)), VERTICAL, WIDE)
    assert got == pytest.approx(0.5)


def test_two_distant_faces_frame_the_largest():
    got = pick_face_center(sample(0.0, (0.1, 0.2), (0.9, 0.05)), VERTICAL, WIDE)
    assert got == pytest.approx(0.1)


# --- smoothing ------------------------------------------------------------- #


def test_small_jitter_is_absorbed_by_the_dead_zone():
    samples = [sample(i * 0.5, (0.5 + (0.01 if i % 2 else -0.01), 0.1)) for i in range(20)]
    centers = smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)
    values = {round(c, 4) for _, c in centers}
    assert len(values) == 1


def test_a_real_move_is_followed():
    samples = [sample(i * 0.5, (0.2, 0.1)) for i in range(10)]
    samples += [sample(5.0 + i * 0.5, (0.8, 0.1)) for i in range(20)]
    centers = smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)
    assert centers[-1][1] > 0.6


def test_smoothing_lags_behind_a_jump():
    """EMA with alpha 0.2 must not teleport on the first frame after a cut."""
    samples = [sample(0.0, (0.2, 0.1)), sample(0.5, (0.9, 0.1))]
    centers = smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)
    assert centers[1][1] < 0.5


def test_frames_without_a_face_hold_the_last_centre():
    samples = [sample(0.0, (0.3, 0.1)), sample(0.5), sample(1.0)]
    centers = smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)
    assert len(centers) == 3
    assert centers[-1][1] == pytest.approx(centers[0][1])


def test_dead_zone_is_five_percent():
    assert DEAD_ZONE_PCT == 0.05


# --- scenes ---------------------------------------------------------------- #


def test_a_stable_track_is_one_scene():
    centers = [(i * 0.5, 0.5) for i in range(20)]
    assert len(to_scenes(centers)) == 1


def test_a_large_jump_starts_a_new_scene():
    centers = [(i * 0.5, 0.2) for i in range(10)] + [(5.0 + i * 0.5, 0.8) for i in range(10)]
    scenes = to_scenes(centers)
    assert len(scenes) == 2
    assert scenes[0].center_x < scenes[1].center_x


# --- coverage and fallback ------------------------------------------------- #


def test_coverage_counts_samples_with_a_face():
    samples = [sample(0.0, (0.5, 0.1)), sample(0.5), sample(1.0, (0.5, 0.1)), sample(1.5)]
    assert coverage(samples) == pytest.approx(0.5)


def test_coverage_threshold_is_forty_percent():
    assert MIN_FACE_COVERAGE == 0.40


def test_empty_sample_list_has_zero_coverage():
    assert coverage([]) == 0.0


# --- the ffmpeg expression -------------------------------------------------- #


def test_crop_keeps_the_target_aspect_and_stays_inside_the_frame():
    scenes = to_scenes([(0.0, 0.5)])
    expression = crop_expression(scenes, 1920, 1080, VERTICAL)
    assert expression.startswith("crop=")
    width, height = expression.split("=")[1].split(":")[0:2]
    assert int(width) in (606, 608)  # 1080 * 9/16, rounded to even
    assert int(height) == 1080


def test_multiple_scenes_become_a_time_conditional():
    centers = [(i * 0.5, 0.2) for i in range(6)] + [(3.0 + i * 0.5, 0.8) for i in range(6)]
    scenes = to_scenes(centers)
    expression = crop_expression(scenes, 1920, 1080, VERTICAL)
    assert "if(lt(t," in expression


def test_crop_x_is_clamped_at_the_edges():
    expression = crop_expression(to_scenes([(0.0, 0.99)]), 1920, 1080, VERTICAL)
    x_value = int(expression.split(":'")[1].split("'")[0])
    assert 0 <= x_value <= 1920 - 606


# --- following a pan -------------------------------------------------------- #


def panning_centers() -> list[tuple[float, float]]:
    """A face crossing the frame, as smoothing actually reports it."""
    samples = [sample(i * 0.5, (0.20 + 0.52 * i / 23, 0.11)) for i in range(24)]
    return smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)


def test_a_pan_is_one_scene_but_many_framing_steps():
    """A slow pan is not a camera cut, yet the crop still has to follow it."""
    centers = panning_centers()
    assert len(to_scenes(centers)) == 1
    assert len(to_steps(centers)) > 3


def test_steps_follow_the_subject_across_the_frame():
    steps = to_steps(panning_centers())
    assert steps[0].center_x < steps[-1].center_x
    assert steps[-1].center_x - steps[0].center_x > 0.25


def test_steps_cover_the_whole_timeline_without_gaps():
    steps = to_steps(panning_centers(), end=12.0)
    assert steps[0].start == pytest.approx(0.0)
    assert steps[-1].end == pytest.approx(12.0)
    for first, second in pairwise(steps):
        assert first.end == pytest.approx(second.start)


def test_a_still_subject_produces_a_single_step():
    samples = [sample(i * 0.5, (0.5, 0.11)) for i in range(20)]
    centers = smooth_centers(samples, target_aspect=VERTICAL, frame_aspect=WIDE)
    assert len(to_steps(centers)) == 1


def test_the_crop_keeps_the_subject_inside_the_window_at_every_step():
    """The regression: averaging a pan into one fixed crop loses the face."""
    steps = to_steps(panning_centers())
    source_w, source_h = 1280, 720
    crop_w = int(min(source_w, source_h * VERTICAL)) // 2 * 2
    for step in steps:
        x = int(min(source_w - crop_w, max(0, round(step.center_x * source_w - crop_w / 2))))
        face_px = step.center_x * source_w
        assert x <= face_px <= x + crop_w, f"rosto fora do crop no passo {step.start}s"


def test_the_expression_switches_at_every_step():
    steps = to_steps(panning_centers())
    expression = crop_expression(steps, 1280, 720, VERTICAL)
    assert expression.count("if(lt(t,") == len(steps) - 1


def test_zero_samples_is_reported_as_a_decode_failure(tmp_path, monkeypatch):
    """ "No frames" must not look the same as "no faces" - one is a bug."""
    import clipper.video.faces as faces_mod
    from clipper.utils.errors import ExternalError

    class DeadCapture:
        def isOpened(self):  # noqa: N802 - cv2 API
            return True

        def get(self, _prop):
            return 0.0

        def read(self):
            return False, None

        def set(self, *_args):
            return True

        def release(self):
            return None

    fake_cv2 = type(
        "cv2",
        (),
        {
            "VideoCapture": staticmethod(lambda _p: DeadCapture()),
            "CAP_PROP_FPS": 0,
            "CAP_PROP_FRAME_COUNT": 1,
            "CAP_PROP_FRAME_WIDTH": 2,
            "CAP_PROP_FRAME_HEIGHT": 3,
            "CAP_PROP_POS_MSEC": 4,
            "COLOR_BGR2RGB": 5,
            "cvtColor": staticmethod(lambda f, _c: f),
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "cv2", fake_cv2)
    monkeypatch.setattr(faces_mod, "model_path", lambda *a, **k: tmp_path / "m.tflite")

    with pytest.raises(ExternalError, match="AV1"):
        faces_mod.detect_faces(tmp_path / "v.mp4", "vid")
