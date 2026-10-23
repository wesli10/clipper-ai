"""Dynamic punch-in (SPEC 7.3 step 4).

Alternates between 100% and ~108% scale at sentence boundaries. Applied *after*
the reframe, over the already-cropped frame, so it never competes with face
tracking and never upscales a full-frame long (SPEC 4.2, enforced in
``profiles.py``).
"""

from __future__ import annotations

from clipper.models import TimeRange, Transcript, Word

#: Maximum zoom, as a multiplier. Kept small on purpose.
MAX_SCALE = 1.08
#: Seconds each transition takes.
TRANSITION_S = 0.15
#: A pause at least this long can start or end a punch-in.
SENTENCE_GAP_S = 0.4
#: How many sentences a zoom state lasts.
MIN_SENTENCES, MAX_SENTENCES = 2, 4


def sentence_boundaries(words: list[Word], *, gap: float = SENTENCE_GAP_S) -> list[float]:
    """Times where a sentence ends: punctuation or a pause."""
    boundaries: list[float] = []
    for index, word in enumerate(words[:-1]):
        pause = words[index + 1].start - word.end
        if pause > gap or word.text.rstrip().endswith((".", "?", "!")):
            boundaries.append(round(word.end, 3))
    return boundaries


#: Cadence, in sentences, cycled so the zoom does not feel metronomic.
CADENCE: tuple[int, ...] = (2, 3, 4, 3)


def zoom_windows(
    boundaries: list[float],
    duration: float,
    *,
    cadence: tuple[int, ...] = CADENCE,
) -> list[TimeRange]:
    """Intervals that should be shown zoomed in.

    Alternates between zoomed and not, spending 2 to 4 sentences in each state
    (SPEC 7.3 step 4).
    """
    if not boundaries:
        return []
    marks = [0.0, *boundaries, duration]
    windows: list[TimeRange] = []
    index = cadence[0]
    step = 1
    zoomed = True
    while index < len(marks) - 1:
        end_index = min(index + cadence[step % len(cadence)], len(marks) - 1)
        if zoomed and marks[end_index] > marks[index]:
            windows.append(TimeRange(start=marks[index], end=marks[end_index]))
        zoomed = not zoomed
        index = end_index
        step += 1
    return windows


def scale_expression(
    windows: list[TimeRange],
    *,
    max_scale: float = MAX_SCALE,
    transition: float = TRANSITION_S,
) -> str:
    """ffmpeg expression for the current zoom factor at time ``t``.

    Each window ramps in over ``transition`` seconds, holds, then ramps out.
    """
    if not windows:
        return "1"
    expression = "1"
    for window in reversed(windows):
        ramp_in_end = window.start + transition
        ramp_out_start = max(ramp_in_end, window.end - transition)
        rise = f"(1+{max_scale - 1:.4f}*(t-{window.start:.3f})/{transition:g})"
        fall = f"(1+{max_scale - 1:.4f}*({window.end:.3f}-t)/{transition:g})"
        inner = (
            f"if(lt(t,{ramp_in_end:.3f}),{rise},"
            f"if(lt(t,{ramp_out_start:.3f}),{max_scale:.4f},{fall}))"
        )
        expression = f"if(between(t,{window.start:.3f},{window.end:.3f}),{inner},{expression})"
    return expression


def punch_in_filter(
    width: int,
    height: int,
    *,
    transcript: Transcript | None = None,
    intervals: list[TimeRange] | None = None,
    offsets: list[float] | None = None,
    duration: float | None = None,
) -> str:
    """`scale`+`crop` pair that zooms without changing the output size.

    Scaling up and cropping back to ``width x height`` keeps the frame size
    constant, which is what the rest of the chain expects.
    """
    windows: list[TimeRange] = []
    if transcript is not None and intervals is not None and offsets is not None and duration:
        from clipper.video.subtitles import words_for_clip

        words = words_for_clip(transcript, intervals, offsets)
        windows = zoom_windows(sentence_boundaries(words), duration)

    factor = scale_expression(windows)
    return (
        f"scale=w='ceil({width}*({factor})/2)*2':h='ceil({height}*({factor})/2)*2':eval=frame,"
        f"crop={width}:{height}:'(iw-{width})/2':'(ih-{height})/2'"
    )
