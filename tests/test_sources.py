from __future__ import annotations

import pytest

from clipper.sources.youtube import YouTubeSource
from clipper.stages.ingest import pick_source
from clipper.utils.errors import InputError

source = YouTubeSource()

VALID = [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("http://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    # playlist params are ignored (SPEC 5.1)
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90s", "dQw4w9WgXcQ"),
    ("  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
]

INVALID = [
    "https://vimeo.com/12345",
    "https://www.instagram.com/reel/Cabcdef/",
    "https://www.tiktok.com/@user/video/12345",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/",
    "https://www.youtube.com/playlist?list=PL123",
    "not a url",
    "",
    "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
]


@pytest.mark.parametrize(("url", "expected"), VALID)
def test_matches_and_extracts_id(url, expected):
    assert source.matches(url)
    assert source.video_id(url) == expected


@pytest.mark.parametrize("url", INVALID)
def test_rejects_other_urls(url):
    assert not source.matches(url)
    with pytest.raises(InputError):
        source.video_id(url)


def test_canonical_url_drops_playlist_and_timestamp():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=90s"
    assert source.canonical_url(url) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize(("url", "_expected"), VALID)
def test_pick_source_finds_youtube(url, _expected):
    assert pick_source(url) is not None


def test_pick_source_rejects_unsupported():
    with pytest.raises(InputError, match="nenhuma fonte"):
        pick_source("https://vimeo.com/12345")


def test_format_selector_prefers_h264():
    """AV1 in an mp4 container decodes in ffmpeg but not in the OpenCV wheel."""
    from clipper.sources.youtube import FORMAT_SELECTOR

    first = FORMAT_SELECTOR.split("/")[0]
    assert "vcodec^=avc1" in first
    assert "height<=1080" in first
    # still degrades to any mp4, then anything at all
    assert FORMAT_SELECTOR.endswith("/best")
