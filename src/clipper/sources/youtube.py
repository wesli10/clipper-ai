"""YouTube implementation of :class:`~clipper.sources.base.VideoSource`."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from clipper.models import SourceInfo
from clipper.utils.errors import ExternalError, InputError

#: `bestvideo<=1080p mp4 + bestaudio m4a`, falling back to a single mp4 (SPEC 5.1).
#:
#: H.264 (``avc1``) is preferred over whatever "mp4" happens to mean today.
#: YouTube now serves AV1 in mp4 containers, and while ffmpeg decodes it, the
#: OpenCV wheel does not - which left face detection reading zero frames and
#: every short silently falling back to a centre crop.
FORMAT_SELECTOR = (
    "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "best[ext=mp4]/best"
)

_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


class YouTubeSource:
    """Downloads with yt-dlp; ignores playlist parameters entirely."""

    name = "youtube"

    def matches(self, url: str) -> bool:
        try:
            return self._video_id_or_none(url) is not None
        except ValueError:
            return False

    def video_id(self, url: str) -> str:
        video_id = self._video_id_or_none(url)
        if video_id is None:
            raise InputError(f"nao consegui extrair o id de video desta URL: {url}")
        return video_id

    @staticmethod
    def _video_id_or_none(url: str) -> str | None:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.netloc or "").lower()
        if host not in _HOSTS:
            return None

        if host.endswith("youtu.be"):
            candidate = parsed.path.lstrip("/").split("/")[0]
            return candidate if _ID_RE.match(candidate) else None

        if parsed.path in ("/watch", "/watch/"):
            values = parse_qs(parsed.query).get("v", [])
            candidate = values[0] if values else ""
            return candidate if _ID_RE.match(candidate) else None

        for prefix in _PATH_PREFIXES:
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix) :].split("/")[0]
                return candidate if _ID_RE.match(candidate) else None
        return None

    def canonical_url(self, url: str) -> str:
        """URL without playlist/timestamp noise."""
        return f"https://www.youtube.com/watch?v={self.video_id(url)}"

    # -- metadata ---------------------------------------------------------- #

    def probe(self, url: str) -> SourceInfo:
        info = self._extract(url, download=False)
        return self._to_source_info(info, url)

    def fetch(self, url: str, dest: Path, *, logger: logging.Logger | None = None) -> SourceInfo:
        dest.mkdir(parents=True, exist_ok=True)
        info = self._extract(
            url,
            download=True,
            outtmpl=str(dest / "source.%(ext)s"),
            logger=logger,
        )
        source_info = self._to_source_info(info, url)
        (dest / "source.info.json").write_text(
            json.dumps(_strip_info(info), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return source_info

    # -- internals --------------------------------------------------------- #

    def _extract(
        self,
        url: str,
        *,
        download: bool,
        outtmpl: str | None = None,
        logger: logging.Logger | None = None,
    ) -> dict[str, Any]:
        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.utils import DownloadError
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ExternalError(f"yt-dlp nao esta instalado: {exc}") from exc

        options: dict[str, Any] = {
            "format": FORMAT_SELECTOR,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
        }
        if outtmpl:
            options["outtmpl"] = outtmpl
        if logger is not None:
            options["logger"] = logger

        canonical = self.canonical_url(url)
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(canonical, download=download)
        except DownloadError as exc:
            raise _translate_error(exc, canonical) from exc
        if info is None:
            raise ExternalError(f"yt-dlp nao retornou metadados para {canonical}")
        return dict(info)

    @staticmethod
    def _to_source_info(info: dict[str, Any], original_url: str) -> SourceInfo:
        video_id = str(info.get("id") or "")
        return SourceInfo(
            video_id=video_id,
            title=str(info.get("title") or ""),
            channel=str(info.get("uploader") or info.get("channel") or ""),
            duration=float(info.get("duration") or 0.0),
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else original_url,
            language=(info.get("language") or None),
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            upload_date=(info.get("upload_date") or None),
        )


_INFO_KEYS = (
    "id",
    "title",
    "description",
    "uploader",
    "channel",
    "channel_id",
    "channel_url",
    "duration",
    "upload_date",
    "language",
    "width",
    "height",
    "fps",
    "view_count",
    "like_count",
    "tags",
    "categories",
    "webpage_url",
    "thumbnail",
)


def _strip_info(info: dict[str, Any]) -> dict[str, Any]:
    """Keep the useful metadata; drop the megabytes of format listings."""
    return {key: info[key] for key in _INFO_KEYS if key in info}


def _translate_error(exc: Exception, url: str) -> ExternalError:
    message = str(exc).lower()
    if "private" in message:
        return ExternalError(f"video privado: {url}")
    if "age" in message and ("restrict" in message or "confirm" in message):
        return ExternalError(f"video com restricao de idade, nao e possivel baixar: {url}")
    if "members-only" in message or "join this channel" in message:
        return ExternalError(f"video exclusivo para membros do canal: {url}")
    if "unavailable" in message or "not available" in message:
        return ExternalError(f"video indisponivel: {url}")
    if "sign in" in message:
        return ExternalError(f"o YouTube exigiu login para este video: {url}")
    return ExternalError(f"yt-dlp falhou em {url}: {exc}")


#: Registry consulted by the pipeline; order matters when sources overlap.
SOURCES: tuple[object, ...] = (YouTubeSource(),)
