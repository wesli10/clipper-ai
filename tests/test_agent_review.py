"""Deterministic render review (SPEC-agent 6).

Everything here runs without ffmpeg: the measurements are handed to the checks,
which is the whole point of gathering them in one place.
"""

from __future__ import annotations

import pytest

from clipper.agent import review
from clipper.agent.review import AssEvent, RenderFacts
from clipper.models import Clip, Segment, TimeRange, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.video.ffmpeg import MediaInfo

VIDEO_ID = "vid0000001"


def spoken(seg_id: int, start: float, text: str, length: float) -> Segment:
    tokens = text.split()
    step = length / len(tokens)
    words = [
        Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step - 0.02, 3))
        for i, t in enumerate(tokens)
    ]
    return Segment(id=seg_id, start=start, end=round(start + length, 3), text=text, words=words)


@pytest.fixture
def transcript():
    segments = [
        spoken(i, i * 30.0, f"assunto numero {i} explicado com calma ate o fim.", 28.0)
        for i in range(10)
    ]
    return Transcript(video_id=VIDEO_ID, language="pt", duration=300.0, segments=segments)


@pytest.fixture
def short(prompts_dir):
    return resolve_profile("short", prompts_dir=prompts_dir)


@pytest.fixture
def clip():
    return Clip(
        index=1,
        slug="corte",
        profile="short",
        segments=[TimeRange(start=60.0, end=88.0)],
        offsets=[0.0],
    )


def facts(**overrides) -> RenderFacts:
    base = {
        "clip_id": f"{VIDEO_ID}/short/1",
        "media": MediaInfo(
            width=1080, height=1920, duration=28.0, fps=30.0, has_audio=True, has_video=True
        ),
        "expected_duration": 28.0,
        "speech_duration": 28.0,
        "loudness": -14.0,
    }
    base.update(overrides)
    return RenderFacts(**base)  # type: ignore[arg-type]


def by_name(checks, name):
    return next(c for c in checks if c.check == name)


# --- reading the .ass back -------------------------------------------------- #


def test_events_come_back_from_a_real_ass_document(transcript, short, clip):
    from clipper.video import subtitles

    document = subtitles.build_ass(transcript, clip.segments, [0.0], short)
    events = review.parse_ass_events(document)

    assert events and all(e.style == "Default" for e in events)
    assert events[0].start >= 0.0 and events[0].end > events[0].start
    # Karaoke tags are not text: what the viewer reads is what gets measured.
    assert "\\k" not in " ".join(events[0].lines)
    assert "assunto".upper() in " ".join(events[0].lines).upper()


def test_a_line_that_is_not_a_dialogue_is_ignored():
    assert review.parse_ass_events("[Events]\nFormat: Layer, Start\nComment: 0,x") == []


# --- duration --------------------------------------------------------------- #


def test_expected_duration_follows_the_crossfade_arithmetic(transcript, short, clip):
    plain = review.expected_duration(clip, short, transcript, transition=0.3)
    assert plain == pytest.approx(28.0)

    two = clip.model_copy(
        update={
            "segments": [TimeRange(start=60.0, end=88.0), TimeRange(start=150.0, end=178.0)],
            "offsets": [0.0, 27.7],
        }
    )
    joined = review.expected_duration(two, short, transcript, transition=0.3)
    assert joined == pytest.approx(28.0 + 28.0 - 0.3)


def test_the_runway_and_the_end_card_are_part_of_the_expected_length(transcript, short, clip):
    rolled = short.model_copy(update={"tail_roll_s": 3.0})
    assert review.expected_duration(clip, rolled, transcript, transition=0.3) == pytest.approx(31.0)
    with_card = review.expected_duration(
        clip, rolled, transcript, transition=0.3, outro_duration=4.0
    )
    assert with_card == pytest.approx(31.0 + 4.0 - 0.3)


def test_a_file_that_is_not_the_clip_fails_on_duration(short):
    truncated = facts(
        media=MediaInfo(
            width=1080, height=1920, duration=12.0, fps=30.0, has_audio=True, has_video=True
        )
    )
    check = by_name(review.check_output(truncated, short), "duration")
    assert check.status == "fail" and "12.00s" in check.value


# --- output ----------------------------------------------------------------- #


def test_a_good_render_passes_every_output_check(short):
    assert all(c.status == "ok" for c in review.check_output(facts(), short))


def test_the_long_may_come_out_smaller_but_never_bigger(prompts_dir):
    long_profile = resolve_profile("long", prompts_dir=prompts_dir)
    smaller = facts(
        media=MediaInfo(
            width=1280, height=720, duration=28.0, fps=30.0, has_audio=True, has_video=True
        )
    )
    assert by_name(review.check_output(smaller, long_profile), "resolution").status == "ok"

    upscaled = facts(
        media=MediaInfo(
            width=2560, height=1440, duration=28.0, fps=30.0, has_audio=True, has_video=True
        )
    )
    assert by_name(review.check_output(upscaled, long_profile), "resolution").status == "fail"


def test_a_silent_file_is_a_failure(short):
    mute = facts(
        media=MediaInfo(
            width=1080, height=1920, duration=28.0, fps=30.0, has_audio=False, has_video=True
        )
    )
    assert by_name(review.check_output(mute, short), "audio_track").status == "fail"


def test_normalisation_that_did_not_take_is_caught(short):
    assert by_name(review.check_output(facts(loudness=-23.0), short), "loudness").status == "fail"
    assert by_name(review.check_output(facts(loudness=-14.6), short), "loudness").status == "ok"


def test_loudness_that_could_not_be_measured_only_warns(short):
    assert by_name(review.check_output(facts(loudness=None), short), "loudness").status == "warn"


# --- captions --------------------------------------------------------------- #


def caption(start: float, end: float, text: str = "UMA LINHA CURTA") -> AssEvent:
    return AssEvent(start=start, end=end, style="Default", text=text)


def test_captions_that_never_appeared_are_a_failure(short):
    checks = review.check_captions(facts(events=[]), short)
    assert by_name(checks, "caption_sync").status == "fail"


def test_a_profile_without_captions_is_not_checked_for_them(short):
    silent = short.model_copy(update={"subtitle_style": "off"})
    assert review.check_captions(facts(events=[]), silent) == []


def test_captions_starting_late_are_a_failure(short):
    late = [caption(3.0, 5.0), caption(6.0, 9.0)]
    checks = review.check_captions(facts(events=late), short)
    assert by_name(checks, "caption_sync").status == "fail"


def test_captions_running_past_the_chosen_end_are_a_failure(short):
    spilling = [caption(0.2, 2.0), caption(2.0, 40.0)]
    checks = review.check_captions(facts(events=spilling), short)
    assert by_name(checks, "caption_sync").status == "fail"


def test_captions_dropping_out_are_a_failure(short):
    sparse = [caption(0.2, 2.0), caption(20.0, 22.0)]
    assert (
        by_name(review.check_captions(facts(events=sparse), short), "caption_density").status
        == "fail"
    )


def test_a_line_wider_than_the_style_allows_is_a_warning(short):
    dense = [caption(i * 0.9, i * 0.9 + 0.8) for i in range(30)]
    dense[5] = caption(4.5, 5.3, "UMA LINHA MUITO MAIS LONGA DO QUE O ESTILO PERMITE DE VERDADE")
    checks = review.check_captions(facts(events=dense), short)
    assert by_name(checks, "caption_fit").status == "warn"
    assert by_name(checks, "caption_density").status == "ok"


def test_the_comment_question_must_not_sit_on_the_captions(short):
    events = [caption(i * 0.9, i * 0.9 + 0.8) for i in range(30)]
    high = AssEvent(start=0.0, end=28.0, style="Banner", text=r"{\pos(540,100)}PERGUNTA")
    assert (
        by_name(
            review.check_captions(facts(events=[*events, high]), short), "caption_collision"
        ).status
        == "ok"
    )

    low = AssEvent(start=0.0, end=28.0, style="Banner", text=r"{\pos(540,1400)}PERGUNTA")
    assert (
        by_name(
            review.check_captions(facts(events=[*events, low]), short), "caption_collision"
        ).status
        == "fail"
    )


# --- boundaries ------------------------------------------------------------- #


def test_a_clean_cut_passes_the_boundary_checks(transcript, short, clip):
    checks = review.check_boundaries(clip, short, transcript)
    assert {c.check for c in checks} == {"opening", "closing", "cut_in_pause"}
    assert all(c.status == "ok" for c in checks)


def test_a_clip_that_ends_on_a_question_is_refused(transcript, short):
    asking = transcript.model_copy(
        update={
            "segments": [
                *transcript.segments[:2],
                spoken(2, 60.0, "e por que isso acontece com todo mundo?", 28.0),
                *transcript.segments[3:],
            ]
        }
    )
    clip = Clip(
        index=1,
        slug="c",
        profile="short",
        segments=[TimeRange(start=30.0, end=88.0)],
    )
    assert by_name(review.check_boundaries(clip, short, asking), "closing").status == "fail"


def test_a_cut_inside_a_word_is_caught(transcript, short):
    inside = Clip(
        index=1,
        slug="c",
        profile="short",
        segments=[TimeRange(start=61.0, end=87.0)],
    )
    check = by_name(review.check_boundaries(inside, short, transcript), "cut_in_pause")
    assert check.status == "fail" and check.fix == "snap_clip"


# --- first frame, quiz ------------------------------------------------------ #


def test_a_black_first_frame_is_a_failure():
    assert review.check_first_frame(facts(thumbnail_luma=2.0))[0].status == "fail"
    assert review.check_first_frame(facts(thumbnail_luma=120.0))[0].status == "ok"
    assert review.check_first_frame(facts()) == []


def test_the_quiz_needs_a_question_and_an_answer_around_the_hold(prompts_dir, clip):
    quiz = resolve_profile("quiz", prompts_dir=prompts_dir)
    assert review.check_quiz(clip, quiz)[0].status == "fail"  # sem reveal_at

    balanced = clip.model_copy(update={"reveal_at": 74.0})
    assert review.check_quiz(balanced, quiz)[0].status == "ok"

    lopsided = clip.model_copy(update={"reveal_at": 61.0})
    assert review.check_quiz(lopsided, quiz)[0].status == "fail"


def test_the_quiz_check_is_silent_for_a_profile_without_a_card(short, clip):
    assert review.check_quiz(clip, short) == []


# --- faces (dormant while the crop is off) ---------------------------------- #


def face_track(centres: list[float]) -> object:
    from clipper.video.faces import FaceBox, FaceSample, FaceTrack

    return FaceTrack(
        video_id=VIDEO_ID,
        width=1920,
        height=1080,
        samples=[
            FaceSample(
                t=60.0 + index * 0.5,
                faces=[FaceBox(cx=cx, cy=0.5, width=0.1, height=0.2, score=0.9)],
            )
            for index, cx in enumerate(centres)
        ],
    )


def test_without_a_cropping_reframe_nothing_can_leave_the_frame(short, clip):
    """Dormant by design: with `blur` the whole frame is kept (SPEC-agent 2.1)."""
    blurred = short.model_copy(update={"reframe": "blur"})
    assert review.check_faces(facts(face_track=face_track([0.5] * 5)), clip, blurred) == []


def test_a_face_held_in_the_middle_passes(short, clip):
    cropping = short.model_copy(update={"reframe": "center"})
    checks = review.check_faces(facts(face_track=face_track([0.5] * 6)), clip, cropping)
    assert by_name(checks, "face_in_frame").status == "ok"
    assert by_name(checks, "face_coverage").status == "ok"


def test_a_face_the_crop_could_not_follow_is_caught(short, clip):
    cropping = short.model_copy(update={"reframe": "center"})
    checks = review.check_faces(
        facts(face_track=face_track([0.5, 0.5, 0.5, 0.5, 0.95])), clip, cropping
    )
    assert by_name(checks, "face_in_frame").status == "fail"


def test_samples_without_a_face_lower_the_coverage(short, clip):
    from clipper.video.faces import FaceSample, FaceTrack

    cropping = short.model_copy(update={"reframe": "center"})
    track = FaceTrack(
        video_id=VIDEO_ID,
        width=1920,
        height=1080,
        samples=[FaceSample(t=60.0 + i * 0.5, faces=[]) for i in range(6)],
    )
    checks = review.check_faces(facts(face_track=track), clip, cropping)
    assert by_name(checks, "face_coverage").status == "fail"


# --- verdict ---------------------------------------------------------------- #


def test_the_verdict_is_the_worst_status_present():
    assert review.verdict_of([review.ok("a"), review.ok("b")]) == "ok"
    assert review.verdict_of([review.ok("a"), review.warn("b", "x", "y")]) == "warn"
    assert review.verdict_of([review.warn("a", "x", "y"), review.fail("b", "x", "y")]) == "fail"


# --- the runway (found by the first real run) ------------------------------- #


def test_a_runway_that_runs_into_the_next_subject_is_flagged(transcript, short, clip):
    """Every other check passed and the short still ended inside the next story."""
    rolling = short.model_copy(update={"tail_roll_s": 5.0})
    check = review.check_tail_roll(clip, rolling, transcript)[0]

    assert check.status == "warn"
    assert "assunto numero 3" in check.value
    assert check.fix.startswith("tail_roll_s abaixo de")


def test_a_runway_that_lands_in_silence_passes(transcript, short, clip):
    quiet = short.model_copy(update={"tail_roll_s": 1.5})
    check = review.check_tail_roll(clip, quiet, transcript)[0]

    assert check.status == "ok" and "sem fala nova" in check.value


def test_a_profile_without_a_runway_is_not_checked_for_one(transcript, short, clip):
    assert review.check_tail_roll(clip, short, transcript) == []


def test_the_runway_never_runs_off_the_end_of_the_video(transcript, short):
    ending = Clip(
        index=1,
        slug="c",
        profile="short",
        segments=[TimeRange(start=240.0, end=transcript.duration)],
    )
    rolling = short.model_copy(update={"tail_roll_s": 10.0})
    assert review.check_tail_roll(ending, rolling, transcript) == []
