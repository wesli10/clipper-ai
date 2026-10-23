"""The end card appended to a rendered clip (SPEC 7.3 step 7).

The outro is a finished video, not footage: it must never pass through the
reframe, zoom or subtitle chain, so it is joined *after* the final filtered
encode rather than alongside the source intervals.

Whatever a generator produces - odd frame rate, no audio track, the wrong
resolution - is normalised once into the profile's exact output parameters, so
the crossfade that follows has nothing to reconcile.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from clipper.profiles import Profile
from clipper.utils.errors import RenderError
from clipper.video import ffmpeg

#: Sample rate and layout every normalised outro is given, so ``acrossfade``
#: never meets two different audio formats.
AUDIO_RATE = 48_000
AUDIO_CHANNELS = 2


def normalise_args(
    source: Path,
    dest: Path,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    has_audio: bool,
    audio_bitrate: str = "192k",
) -> list[str]:
    """Re-encode an outro to exactly the profile's output parameters.

    The whole frame is kept and padded rather than cropped: an end card is text
    and icons, and cropping one cuts the message in half.
    """
    args = [*ffmpeg.base_args(), "-i", str(source)]
    if not has_audio:
        # A silent track of the right shape, so the join sees two audio streams.
        args += [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
            "-shortest",
        ]
    args += [
        "-vf",
        ffmpeg.letterbox_filter(width, height),
        "-r",
        str(fps),
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
        audio_bitrate,
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        str(dest),
    ]
    return args


def cache_key(source: Path, width: int, height: int, fps: int) -> str:
    """Identity of a normalised outro: the file's content and the target shape."""
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{width}x{height}@{fps}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def prepare(
    profile: Profile,
    cache_dir: Path,
    *,
    crf: int,
    preset: str,
    audio_bitrate: str = "192k",
    logger: logging.Logger | None = None,
) -> Path | None:
    """The profile's outro, normalised and cached; ``None`` when it has none.

    Normalising once per (file, shape) instead of once per clip matters on a
    batch: the work is identical for every clip of the same profile.
    """
    if profile.outro is None:
        return None
    source = profile.outro.expanduser()
    if not source.exists():
        raise RenderError(
            f"outro do perfil '{profile.name}' nao encontrado: {source}\n"
            "Ajuste profiles.<perfil>.outro no config.yaml ou remova a chave."
        )

    width = ffmpeg.even(profile.output_width)
    height = ffmpeg.even(profile.output_height)
    dest = cache_dir / f"outro_{profile.name}_{cache_key(source, width, height, profile.fps)}.mp4"
    if dest.exists():
        return dest

    media = ffmpeg.probe(source, logger=logger)
    if not media.has_video:
        raise RenderError(f"o outro '{source}' nao tem faixa de video")
    cache_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        normalise_args(
            source,
            dest,
            width=width,
            height=height,
            fps=profile.fps,
            crf=crf,
            preset=preset,
            has_audio=media.has_audio,
            audio_bitrate=audio_bitrate,
        ),
        logger=logger,
        description="outro",
    )
    return dest
