"""Video source abstraction (SPEC 5.2).

Only ``YouTubeSource`` is implemented, but nothing outside ``sources/`` may know
that the input is a YouTube URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from clipper.models import SourceInfo


@runtime_checkable
class VideoSource(Protocol):
    """A place videos can be fetched from."""

    name: str

    def matches(self, url: str) -> bool:
        """True when this source can handle ``url``."""
        ...

    def video_id(self, url: str) -> str:
        """Stable identifier used as the workspace directory name."""
        ...

    def probe(self, url: str) -> SourceInfo:
        """Read metadata without downloading."""
        ...

    def fetch(self, url: str, dest: Path, *, logger: logging.Logger | None = None) -> SourceInfo:
        """Download to ``dest`` (a directory) and return the final metadata."""
        ...
