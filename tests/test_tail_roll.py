"""The runway of source footage kept after the chosen ending (SPEC 7.3)."""

from __future__ import annotations

import pytest

from clipper.models import Segment, TimeRange, Transcript, Word
from clipper.profiles import resolve_profile
from clipper.stages.render import runway_interval
from clipper.video import subtitles


def transcript_of(*lines: tuple[float, float, str], duration: float) -> Transcript:
    segments = []
    for index, (start, end, text) in enumerate(lines):
        tokens = text.split()
        step = (end - start) / len(tokens)
        segments.append(
            Segment(
                id=index,
                start=start,
                end=end,
                text=text,
                words=[
                    Word(
                        text=t,
                        start=round(start + i * step, 3),
                        end=round(start + (i + 1) * step, 3),
                    )
                    for i, t in enumerate(tokens)
                ],
            )
        )
    return Transcript(video_id="v", language="pt", duration=duration, segments=segments)


TRANSCRIPT = transcript_of(
    (0.0, 8.0, "Essa e a resposta que fecha o assunto."),
    (8.2, 12.0, "Nao, mas qual que e a sua?"),
    duration=60.0,
)


def test_the_runway_never_reaches_the_captions():
    """Speech inside the runway is ambience: captioning it would read as the clip."""
    profile = resolve_profile("short")
    doc = subtitles.build_ass(TRANSCRIPT, [TimeRange(start=0.0, end=8.0)], [0.0], profile)
    assert "Nao, mas qual" not in doc.replace("\\N", " ").upper().lower()
    assert "0:00:08" in doc or "0:00:07" in doc


def test_the_banner_covers_the_runway_too():
    """The call to action is worth the extra seconds of screen time."""
    profile = resolve_profile("short")
    doc = subtitles.build_ass(
        TRANSCRIPT,
        [TimeRange(start=0.0, end=8.0)],
        [0.0],
        profile,
        banner=subtitles.Banner(text="E voce?", y=328, end=11.0),
    )
    banner = next(line for line in doc.splitlines() if ",Banner," in line)
    assert "0:00:11.00" in banner


@pytest.mark.parametrize(
    ("roll", "expected"),
    [(0.0, 8.0), (3.0, 11.0), (60.0, 60.0)],
    ids=["off", "normal", "past the end of the video"],
)
def test_the_runway_is_clamped_to_the_source(roll, expected):
    """A runway may not run off the end of the video."""
    got = runway_interval(TimeRange(start=0.0, end=8.0), roll, TRANSCRIPT.duration)
    assert got.end == expected
    assert got.start == 0.0


def test_a_transcript_with_no_duration_still_gets_its_runway():
    """ffmpeg stops at the real end of the file; nothing here has to guess it."""
    got = runway_interval(TimeRange(start=0.0, end=8.0), 3.0, 0.0)
    assert got.end == 11.0
