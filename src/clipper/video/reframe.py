"""Face-following vertical crop (SPEC 7.3 step 3).

The crop centre is smoothed with an EMA and only moves once it leaves a dead
zone, so the frame does not jitter. Camera cuts break the smoothing into scenes,
each of which gets one fixed crop - simple to express in ffmpeg and stable to
watch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from clipper.config import Settings
from clipper.models import Clip
from clipper.video.faces import FaceSample, load_or_detect
from clipper.video.ffmpeg import MediaInfo

#: Exponential moving average factor for the crop centre.
EMA_ALPHA = 0.2
#: The centre only moves after drifting this fraction of the frame width.
DEAD_ZONE_PCT = 0.05
#: Below this share of samples with a face, fall back to a centre crop.
MIN_FACE_COVERAGE = 0.40
#: A jump larger than this (fraction of width) is treated as a camera cut.
SCENE_JUMP_PCT = 0.25


@dataclass(frozen=True, slots=True)
class Scene:
    """A stretch of the clip that shares one fixed crop."""

    start: float
    end: float
    center_x: float  # normalised 0-1


def pick_face_center(sample: FaceSample, target_aspect: float, frame_aspect: float) -> float | None:
    """Horizontal centre to keep, in normalised coordinates.

    With two faces: frame the largest, unless both fit inside one crop, in which
    case centre between them (SPEC 7.3 step 3).
    """
    if not sample.faces:
        return None
    if len(sample.faces) == 1:
        return sample.faces[0].cx

    ordered = sorted(sample.faces, key=lambda f: f.area, reverse=True)
    first, second = ordered[0], ordered[1]
    # Width of the crop window as a fraction of the source width.
    crop_width = min(1.0, target_aspect / frame_aspect)
    span = abs(first.cx - second.cx) + (first.width + second.width) / 2
    if span <= crop_width:
        return (first.cx + second.cx) / 2
    return first.cx


def smooth_centers(
    samples: list[FaceSample],
    *,
    target_aspect: float,
    frame_aspect: float,
    alpha: float = EMA_ALPHA,
    dead_zone: float = DEAD_ZONE_PCT,
) -> list[tuple[float, float]]:
    """``(time, centre_x)`` pairs after EMA smoothing and the dead zone."""
    output: list[tuple[float, float]] = []
    current: float | None = None
    smoothed: float | None = None

    for sample in samples:
        observed = pick_face_center(sample, target_aspect, frame_aspect)
        if observed is None:
            if current is not None:
                output.append((sample.t, current))
            continue
        smoothed = observed if smoothed is None else alpha * observed + (1 - alpha) * smoothed
        if current is None or abs(smoothed - current) > dead_zone:
            current = smoothed
        output.append((sample.t, current))
    return output


def to_steps(centers: list[tuple[float, float]], *, end: float | None = None) -> list[Scene]:
    """One entry per held centre.

    ``smooth_centers`` already applies the dead zone, so the track only changes
    when the subject really moved. Turning each held value into its own step
    keeps the crop following the speaker; averaging a whole scene into a single
    fixed crop loses them at both ends of any pan.
    """
    if not centers:
        return []
    steps: list[Scene] = []
    start_t, current = centers[0]
    for time, center in centers[1:]:
        if abs(center - current) > 1e-9:
            steps.append(Scene(start=start_t, end=time, center_x=current))
            start_t, current = time, center
    last_end = end if end is not None else centers[-1][0]
    steps.append(Scene(start=start_t, end=max(last_end, start_t), center_x=current))
    return steps


def to_scenes(
    centers: list[tuple[float, float]], *, jump: float = SCENE_JUMP_PCT, end: float | None = None
) -> list[Scene]:
    """Collapse the centre track into scenes, splitting at camera cuts.

    Used for diagnostics: a scene boundary is a hard cut, not a pan.
    """
    if not centers:
        return []
    scenes: list[Scene] = []
    start_t, start_c = centers[0]
    accumulated = [start_c]
    for time, center in centers[1:]:
        if abs(center - accumulated[-1]) > jump:
            scenes.append(
                Scene(start=start_t, end=time, center_x=sum(accumulated) / len(accumulated))
            )
            start_t, accumulated = time, [center]
        else:
            accumulated.append(center)
    last_end = end if end is not None else centers[-1][0]
    scenes.append(
        Scene(
            start=start_t,
            end=max(last_end, start_t),
            center_x=sum(accumulated) / len(accumulated),
        )
    )
    return scenes


def coverage(samples: list[FaceSample]) -> float:
    """Share of samples that found at least one face."""
    if not samples:
        return 0.0
    return sum(1 for s in samples if s.faces) / len(samples)


def crop_expression(
    scenes: list[Scene], source_width: int, source_height: int, target_aspect: float
) -> str:
    """A single ``crop`` filter whose x position switches per step.

    ffmpeg evaluates ``x`` per frame, so nested ``if(lt(t,...))`` gives each step
    its own fixed offset - the frame holds still, then moves, and never drifts.
    """
    crop_w = max(2, int(min(source_width, source_height * target_aspect)) // 2 * 2)
    crop_h = max(2, int(min(source_height, source_width / target_aspect)) // 2 * 2)
    max_x = max(0, source_width - crop_w)

    def x_for(scene: Scene) -> int:
        return int(min(max_x, max(0, round(scene.center_x * source_width - crop_w / 2))))

    if not scenes:
        expression = str(max_x // 2)
    else:
        expression = str(x_for(scenes[-1]))
        for scene in reversed(scenes[:-1]):
            expression = f"if(lt(t,{scene.end:.3f}),{x_for(scene)},{expression})"

    y = max(0, (source_height - crop_h) // 2)
    return f"crop={crop_w}:{crop_h}:'{expression}':{y}"


def face_crop_filter(
    settings: Settings,
    *,
    source_video_id: str,
    clip: Clip,
    offsets: list[float],
    media: MediaInfo,
    target_aspect: float,
    logger: logging.Logger | None = None,
) -> str | None:
    """Build the crop filter for ``clip``, or ``None`` to fall back to centre.

    The filter is expressed in the *rendered* timeline, so the samples of each
    source interval are shifted by that interval's concat offset.
    """
    video_dir = settings.video_dir(source_video_id)
    track = load_or_detect(
        video_dir / "source.mp4", video_dir / "faces.json", source_video_id, logger=logger
    )

    shifted: list[FaceSample] = []
    for interval, offset in zip(clip.segments, offsets, strict=True):
        for sample in track.between(interval.start, interval.end):
            moved = round(sample.t - interval.start + offset, 3)
            shifted.append(sample.model_copy(update={"t": moved}))
    shifted.sort(key=lambda s: s.t)

    if coverage(shifted) < MIN_FACE_COVERAGE:
        if logger is not None:
            logger.info(
                "rosto presente em apenas %.0f%% das amostras (minimo %.0f%%)",
                100 * coverage(shifted),
                100 * MIN_FACE_COVERAGE,
            )
        return None

    source_width = track.width or media.width
    source_height = track.height or media.height
    if not source_width or not source_height:
        return None

    centers = smooth_centers(
        shifted, target_aspect=target_aspect, frame_aspect=source_width / source_height
    )
    end = shifted[-1].t if shifted else None
    steps = to_steps(centers, end=end)
    if logger is not None:
        logger.info(
            "reframe por rosto: %d cenas, %d posicoes de enquadramento",
            len(to_scenes(centers, end=end)),
            len(steps),
        )
    return crop_expression(steps, source_width, source_height, target_aspect)
