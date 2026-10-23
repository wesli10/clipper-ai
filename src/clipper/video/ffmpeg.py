"""ffmpeg/ffprobe command builders and runner.

Every ffmpeg invocation in the project goes through here and is logged before it
runs (CLAUDE.md). Filter order is always: reframe -> zoom -> scale -> ass.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clipper.utils.errors import RenderError


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What ffprobe tells us about a file."""

    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool
    has_video: bool


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RenderError(
            "ffmpeg nao encontrado no PATH. Instale-o (apt install ffmpeg) e tente de novo."
        )
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise RenderError(
            "ffprobe nao encontrado no PATH. Ele acompanha o ffmpeg na maioria das distros; "
            "se voce usa um build estatico apenas com o binario ffmpeg, baixe tambem o ffprobe "
            "(a verificacao da saida do render depende dele)."
        )
    return path


def run(
    args: list[str],
    *,
    logger: logging.Logger | None = None,
    description: str = "ffmpeg",
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, logging it first; raise :class:`RenderError` on failure."""
    printable = shlex.join(args)
    if logger is not None:
        logger.info("%s: %s", description, printable)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as exc:
        raise RenderError(f"falha ao executar {description}: {exc}") from exc

    if logger is not None and result.stderr:
        logger.debug("%s stderr:\n%s", description, result.stderr[-8000:])
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-12:])
        raise RenderError(f"{description} falhou (codigo {result.returncode}):\n{tail}")
    return result


def probe(path: Path, *, logger: logging.Logger | None = None) -> MediaInfo:
    """Read stream properties with ffprobe."""
    args = [
        ffprobe_bin(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run(args, logger=logger, description="ffprobe")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"saida do ffprobe ilegivel para {path}: {exc}") from exc

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for source in (data.get("format", {}), video or {}):
        raw = source.get("duration")
        if raw is not None:
            try:
                duration = float(raw)
                break
            except (TypeError, ValueError):
                continue

    fps = 0.0
    if video is not None:
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        num, _, den = str(rate).partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0.0

    return MediaInfo(
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        duration=duration,
        fps=fps,
        has_audio=audio is not None,
        has_video=video is not None,
    )


def extract_audio(source: Path, dest: Path, *, logger: logging.Logger | None = None) -> None:
    """16 kHz mono PCM wav for whisper (SPEC 5.1)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    run(args, logger=logger, description="extract_audio")


def base_args() -> list[str]:
    return [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]


# --------------------------------------------------------------------------- #
# Command builders (SPEC 7.3)
# --------------------------------------------------------------------------- #


def cut_args(
    source: Path,
    dest: Path,
    start: float,
    end: float,
    *,
    crf: int = 18,
    preset: str = "medium",
    fps: int | None = None,
    hold: float = 0.0,
) -> list[str]:
    """Frame-exact extraction of one interval (SPEC 7.3 step 1).

    Re-encodes on purpose: ``-c copy`` snaps to keyframes and would move the cut
    away from the boundary that :mod:`clipper.stages.select` computed.

    ``hold`` freezes the last frame for that many seconds and pads the audio with
    silence, which is how the quiz card gets its still: extending the part itself
    keeps one file with one set of stream parameters, so the crossfade that
    follows has nothing to reconcile.
    """
    args = [
        *base_args(),
        "-ss",
        f"{max(0.0, start):.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
    ]
    if hold > 0:
        args += [
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={hold:g}",
            "-af",
            f"apad=pad_dur={hold:g}",
        ]
    if fps:
        args += ["-r", str(fps)]
    args += ["-c:a", "aac", "-b:a", "192k", str(dest)]
    return args


def even(value: int) -> int:
    """libx264 with yuv420p needs even dimensions."""
    return value if value % 2 == 0 else value - 1


def fit_size(source_w: int, source_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Largest size within ``max_w x max_h`` keeping the source aspect, never upscaling.

    This is what ``reframe: none`` uses: a source below the profile resolution
    keeps its own size instead of being blown up (SPEC 4.2 / 7.3 step 3).
    """
    if source_w <= 0 or source_h <= 0:
        return even(max_w), even(max_h)
    ratio = min(max_w / source_w, max_h / source_h, 1.0)
    return max(2, even(int(source_w * ratio))), max(2, even(int(source_h * ratio)))


def scale_filter(width: int, height: int) -> str:
    """Plain scale to an exact size, square pixels, no padding."""
    return f"scale={width}:{height},setsar=1"


def center_crop_filter(width: int, height: int) -> str:
    """Fixed centre crop to the profile aspect, then scale (SPEC 7.3 step 3)."""
    aspect = f"{width}/{height}"
    crop = f"crop='min(iw,ih*{aspect})':'min(ih,iw*{height}/{width})':'(iw-out_w)/2':'(ih-out_h)/2'"
    return f"{crop},scale={width}:{height},setsar=1"


def letterbox_filter(width: int, height: int, color: str = "#000000") -> str:
    """Whole frame scaled to fit, centred, with plain bars filling the rest.

    Nothing is cropped - the opposite trade to ``center``, which fills the canvas
    but throws away ~68% of a 16:9 frame's width.
    """
    bar = "0x" + color.lstrip("#")
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bar},setsar=1"
    )


def blur_pad_filter(
    width: int, height: int, brightness: float = -0.30, saturation: float = 0.6
) -> str:
    """Whole frame over a blurred, zoomed copy of itself (SPEC 7.3 step 3).

    The background is dimmed by default: burned captions sit on it, and at full
    brightness white text on a busy blur is hard to read. ``brightness=0`` and
    ``saturation=1`` give the plain blur.
    """
    dim = ""
    if brightness or saturation != 1.0:
        dim = f",eq=brightness={brightness:g}:saturation={saturation:g}"
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=luma_radius=40:luma_power=2{dim}[bgb];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


@dataclass(frozen=True, slots=True)
class LoudnessStats:
    """What the measuring pass found, in loudnorm's own vocabulary."""

    input_i: str
    input_lra: str
    input_tp: str
    input_thresh: str
    target_offset: str


def loudnorm_measure_args(
    source: Path, *, target_i: float, trim: tuple[float, float] | None = None
) -> list[str]:
    """Measure the audio the encode is about to normalise.

    ``trim`` has to match the encode's: measuring the whole source and applying
    the result to a 45-second cut of it would normalise against the wrong audio.
    """
    args = [ffmpeg_bin(), "-hide_banner", "-nostats"]
    if trim is not None:
        args += ["-ss", f"{max(0.0, trim[0]):.3f}", "-to", f"{trim[1]:.3f}"]
    args += [
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        f"loudnorm=I={target_i:g}:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    return args


def parse_loudnorm_stats(stderr: str) -> LoudnessStats | None:
    """The JSON block loudnorm prints at the end of a measuring pass."""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    try:
        return LoudnessStats(
            input_i=str(data["input_i"]),
            input_lra=str(data["input_lra"]),
            input_tp=str(data["input_tp"]),
            input_thresh=str(data["input_thresh"]),
            target_offset=str(data["target_offset"]),
        )
    except KeyError:
        return None


def loudnorm_filter(target_i: float, stats: LoudnessStats | None = None) -> str:
    """EBU R128 normalisation (SPEC 7.3 step 6).

    With ``stats`` this is loudnorm's two-pass mode, which actually lands on the
    target. One pass is dynamic and only approximates it: measured over 23 real
    clips it had a median of -14.3 and a worst case of -16.9 against a target of
    -14, and no amount of re-rendering moved it.
    """
    base = f"loudnorm=I={target_i:g}:TP=-1.5:LRA=11"
    if stats is None:
        return base
    return (
        f"{base}:measured_I={stats.input_i}:measured_LRA={stats.input_lra}"
        f":measured_TP={stats.input_tp}:measured_thresh={stats.input_thresh}"
        f":offset={stats.target_offset}:linear=true:print_format=summary"
    )


def subtitles_filter(ass_path: Path, *, fonts_dir: Path | None = None) -> str:
    """Burn an .ass file; always the last video filter (CLAUDE.md)."""
    escaped = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    if fonts_dir is not None:
        fonts = str(fonts_dir).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        return f"ass='{escaped}':fontsdir='{fonts}'"
    return f"ass='{escaped}'"


def encode_args(
    source: Path,
    dest: Path,
    *,
    video_filters: list[str],
    audio_filters: list[str],
    crf: int,
    preset: str,
    fps: int,
    audio_bitrate: str = "192k",
    trim: tuple[float, float] | None = None,
) -> list[str]:
    """Final encode (SPEC 7.3 step 7).

    ``trim`` cuts the interval in this same pass. A clip made of one interval
    needs no separate extraction: doing both at once halves the encoding work and
    removes a generation of loss.
    """
    args = [*base_args()]
    if trim is not None:
        args += ["-ss", f"{max(0.0, trim[0]):.3f}", "-to", f"{trim[1]:.3f}"]
    args += ["-i", str(source)]
    if video_filters:
        args += ["-vf", ",".join(video_filters)]
    if audio_filters:
        args += ["-af", ",".join(audio_filters)]
    args += [
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        str(dest),
    ]
    return args


def lavfi_path(path: Path) -> str:
    """Escape a filename for use inside a lavfi graph (``movie=...``)."""
    text = str(path)
    for char in ("\\", ":", ",", "'", "[", "]", ";"):
        text = text.replace(char, "\\" + char)
    return text


def loudness_args(source: Path) -> list[str]:
    """Measure the integrated loudness of a finished file (SPEC-agent 6).

    The render normalises with ``loudnorm``; this reads back what actually came
    out, which is the only way to notice a normalisation that did not take.
    """
    return [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


#: The summary block prints `I:` at the start of its own line; the per-frame
#: lines carry the same key mid-line, so anchoring is what tells them apart.
_LOUDNESS_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)


def parse_loudness(stderr: str) -> float | None:
    """Integrated loudness in LUFS, or ``None`` when ebur128 printed no summary."""
    found = _LOUDNESS_RE.findall(stderr)
    return float(found[-1]) if found else None


def frame_luma_args(image: Path) -> list[str]:
    """Average luma of a still image, through ffprobe's lavfi input."""
    return [
        ffprobe_bin(),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"movie={lavfi_path(image)},signalstats",
        "-show_entries",
        "frame_tags=lavfi.signalstats.YAVG",
        "-print_format",
        "json",
    ]


def parse_frame_luma(stdout: str) -> float | None:
    """The ``YAVG`` of the first frame, 0-255, or ``None`` when absent."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    for frame in data.get("frames", []):
        raw = (frame.get("tags") or {}).get("lavfi.signalstats.YAVG")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def thumbnail_args(source: Path, dest: Path, *, at: float = 1.0) -> list[str]:
    return [
        *base_args(),
        "-ss",
        f"{at:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
