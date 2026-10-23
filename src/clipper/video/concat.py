"""Joining non-contiguous intervals of a long clip (SPEC 7.3 step 2).

Never a hard cut: video crossfades with ``xfade`` and audio with ``acrossfade``,
so a jump between two parts of the video is not perceived as a splice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from clipper.video import ffmpeg


@dataclass(frozen=True, slots=True)
class ConcatResult:
    """Where each input part starts inside the joined file."""

    path: Path
    offsets: list[float]
    duration: float


def offsets_for(durations: list[float], transition: float) -> tuple[list[float], float]:
    """Start offset of each part in the joined timeline, and the total duration.

    Each crossfade overlaps the two parts by ``transition`` seconds, so every
    part after the first starts ``transition`` earlier than a plain concatenation
    would put it. Chapter offsets and subtitle shifts both come from here
    (SPEC 7.2 / 7.3 step 5).
    """
    offsets: list[float] = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        offsets.append(round(cursor, 3))
        cursor += duration - (transition if index < len(durations) - 1 else 0.0)
    return offsets, round(cursor, 3)


def build_filter(durations: list[float], transition: float) -> str:
    """`xfade`/`acrossfade` chain for ``len(durations)`` inputs."""
    if len(durations) < 2:
        raise ValueError("concat precisa de pelo menos dois intervalos")

    parts: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"
    elapsed = durations[0]
    for index in range(1, len(durations)):
        out_v = f"v{index}"
        out_a = f"a{index}"
        offset = max(0.0, elapsed - transition)
        parts.append(
            f"[{video_label}][{index}:v]"
            f"xfade=transition=fade:duration={transition:g}:offset={offset:.3f}[{out_v}]"
        )
        parts.append(
            f"[{audio_label}][{index}:a]acrossfade=d={transition:g}:c1=tri:c2=tri[{out_a}]"
        )
        video_label, audio_label = out_v, out_a
        elapsed = offset + durations[index]
    parts.append(f"[{video_label}]null[vout]")
    parts.append(f"[{audio_label}]anull[aout]")
    return ";".join(parts)


def concat(
    parts: list[Path],
    dest: Path,
    *,
    transition: float = 0.3,
    crf: int = 18,
    preset: str = "medium",
    logger: logging.Logger | None = None,
) -> ConcatResult:
    """Join ``parts`` with crossfades and report where each one landed."""
    if not parts:
        raise ValueError("nada para concatenar")

    durations = [ffmpeg.probe(part, logger=logger).duration for part in parts]
    offsets, total = offsets_for(durations, transition)

    if len(parts) == 1:
        return ConcatResult(path=parts[0], offsets=[0.0], duration=durations[0])

    args = ffmpeg.base_args()
    for part in parts:
        args += ["-i", str(part)]
    args += [
        "-filter_complex",
        build_filter(durations, transition),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(dest),
    ]
    ffmpeg.run(args, logger=logger, description="concat")
    return ConcatResult(path=dest, offsets=offsets, duration=total)
