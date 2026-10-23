"""Per-stage log files under ``workspace/<video_id>/logs/`` (SPEC 3.4)."""

from __future__ import annotations

import logging
from pathlib import Path

_HANDLERS: dict[Path, logging.FileHandler] = {}


def stage_logger(logs_dir: Path, stage: str, profile: str | None = None) -> logging.Logger:
    """Logger writing to ``logs/<stage>[_<profile>].log``.

    The logger is named after the log file, not just the stage, so processing two
    videos in one process does not send the second one's lines to the first one's
    file.
    """
    suffix = f"_{profile}" if profile else ""
    path = (logs_dir / f"{stage}{suffix}.log").resolve()
    logger = logging.getLogger(f"clipper.{path}")

    if path not in _HANDLERS:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        _HANDLERS[path] = handler
    return logger
