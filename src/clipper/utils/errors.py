"""Error taxonomy mapped to the CLI exit codes of SPEC 9."""

from __future__ import annotations

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_EXTERNAL = 3
EXIT_RENDER = 4


class ClipperError(Exception):
    """Base error; ``exit_code`` decides the process status."""

    exit_code: int = EXIT_INPUT


class InputError(ClipperError):
    """Bad URL, unknown profile, missing video, invalid config."""

    exit_code = EXIT_INPUT


class ExternalError(ClipperError):
    """Network, yt-dlp, LLM API, rclone."""

    exit_code = EXIT_EXTERNAL


class RenderError(ClipperError):
    """ffmpeg/ffprobe failure or output that failed verification."""

    exit_code = EXIT_RENDER
