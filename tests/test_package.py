"""Metadata assembly - SPEC 7.2 and SPEC 11.1."""

from __future__ import annotations

import json

import pytest

from clipper.llm.base import BaseLLMProvider
from clipper.models import (
    Chapter,
    ChapterHint,
    Clip,
    ClipMetadata,
    Segment,
    TimeRange,
    Transcript,
    Word,
)
from clipper.profiles import resolve_profile
from clipper.stages.package import (
    build_metadata_batch,
    build_prompt,
    chapter_offsets,
    clip_text,
    describe_clip,
    source_url_for,
    to_markdown,
)


def segment(idx: int, start: float, end: float, text: str) -> Segment:
    tokens = text.split()
    step = (end - start) / len(tokens)
    words = [
        Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
        for i, t in enumerate(tokens)
    ]
    return Segment(id=idx, start=start, end=end, text=text, words=words)


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        video_id="vid",
        language="pt",
        duration=600.0,
        segments=[
            segment(i, i * 30.0, i * 30.0 + 28.0, f"assunto {i} explicado aqui") for i in range(20)
        ],
    )


@pytest.fixture
def source():
    from clipper.models import SourceInfo

    return SourceInfo(
        video_id="vid",
        title="Podcast completo",
        channel="Canal X",
        duration=600.0,
        url="https://www.youtube.com/watch?v=vid",
        upload_date="20260115",
    )


class CannedProvider(BaseLLMProvider):
    name = "canned"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return json.dumps(self.payload)


SHORT_ITEM = {
    "index": 1,
    "title": "O erro que todo mundo comete",
    "hook_line": "Voce esta fazendo isso errado",
    "description": "Uma explicacao curta. E o que fazer no lugar.",
    "chapter_titles": [],
    "hashtags": ["Produtividade", "#foco", "habitos"],
    "tags": ["produtividade", "foco"],
}
SHORT_ANSWER = {"items": [SHORT_ITEM]}

LONG_ITEM = {
    "index": 1,
    "title": "Produtividade e sono: o que realmente importa",
    "hook_line": "Dois habitos que mudam o resto",
    "description": "Um paragrafo explicando o conteudo do corte.",
    "chapter_titles": ["O foco", "O sono"],
    "hashtags": ["produtividade", "sono"],
    "tags": ["produtividade", "sono", "habitos"],
}
LONG_ANSWER = {"items": [LONG_ITEM]}


# --- offsets --------------------------------------------------------------- #


def test_chapter_offsets_come_from_the_render():
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=100), TimeRange(start=300, end=400)],
        offsets=[0.0, 99.7],
    )
    assert chapter_offsets(clip) == [0.0, 99.7]


def test_chapter_offsets_fall_back_to_plain_durations_before_rendering():
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=100), TimeRange(start=300, end=400)],
    )
    assert chapter_offsets(clip) == [0.0, 100.0]


def test_source_url_points_at_the_first_interval(source):
    clip = Clip(index=1, slug="s", profile="short", segments=[TimeRange(start=754.2, end=812.9)])
    assert source_url_for(source, clip) == "https://www.youtube.com/watch?v=vid&t=754s"


# --- prompts --------------------------------------------------------------- #


def test_long_prompt_carries_the_chapter_hints(transcript, source):
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=150), TimeRange(start=300, end=450)],
        chapters=[ChapterHint(offset_hint="o foco"), ChapterHint(offset_hint="o sono")],
    )
    prompt = build_prompt(resolve_profile("long"), [clip], source, transcript)
    assert "{clips}" not in prompt
    assert "{n_clips}" not in prompt
    assert "o foco; o sono" in prompt
    assert "Canal X" in prompt


def test_clip_text_marks_the_gap_between_intervals(transcript):
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=60), TimeRange(start=300, end=360)],
    )
    assert "[...]" in clip_text(transcript, clip)


# --- metadata -------------------------------------------------------------- #


def one(clip, profile, source, transcript, answer):
    """Run the batch path for a single clip and return its metadata."""
    produced = build_metadata_batch([clip], profile, source, transcript, CannedProvider(answer))
    return produced[clip.index]


def test_short_metadata_has_no_chapters(transcript, source):
    clip = Clip(index=1, slug="s", profile="short", segments=[TimeRange(start=0, end=45)])
    metadata = one(clip, resolve_profile("short"), source, transcript, SHORT_ANSWER)
    assert metadata.chapters == []
    assert metadata.title == SHORT_ITEM["title"]
    assert metadata.profile == "short"


def test_hashtags_are_normalised(transcript, source):
    clip = Clip(index=1, slug="s", profile="short", segments=[TimeRange(start=0, end=45)])
    metadata = one(clip, resolve_profile("short"), source, transcript, SHORT_ANSWER)
    assert metadata.hashtags == ["#produtividade", "#foco", "#habitos"]


def test_long_metadata_chapters_use_computed_offsets(transcript, source):
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=150), TimeRange(start=300, end=450)],
        chapters=[ChapterHint(offset_hint="o foco"), ChapterHint(offset_hint="o sono")],
        offsets=[0.0, 149.7],
    )
    metadata = one(clip, resolve_profile("long"), source, transcript, LONG_ANSWER)
    assert [c.offset for c in metadata.chapters] == [0.0, 149.7]
    assert [c.title for c in metadata.chapters] == ["O foco", "O sono"]


def test_first_chapter_always_starts_at_zero(transcript, source):
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=150), TimeRange(start=300, end=450)],
        offsets=[0.4, 149.7],
    )
    metadata = one(clip, resolve_profile("long"), source, transcript, LONG_ANSWER)
    assert metadata.chapters[0].offset == 0.0


def test_missing_chapter_titles_fall_back_to_the_hints(transcript, source):
    answer = {"items": [dict(LONG_ITEM, chapter_titles=["Só o primeiro"])]}
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=150), TimeRange(start=300, end=450)],
        chapters=[ChapterHint(offset_hint="o foco"), ChapterHint(offset_hint="o sono")],
        offsets=[0.0, 149.7],
    )
    metadata = one(clip, resolve_profile("long"), source, transcript, answer)
    assert metadata.chapters[1].title == "o sono"


def test_a_single_interval_long_gets_no_chapters(transcript, source):
    clip = Clip(index=1, slug="s", profile="long", segments=[TimeRange(start=0, end=200)])
    metadata = one(clip, resolve_profile("long"), source, transcript, LONG_ANSWER)
    assert metadata.chapters == []


# --- markdown -------------------------------------------------------------- #


def test_markdown_matches_the_spec_layout():
    metadata = ClipMetadata(
        profile="long",
        title="Titulo",
        hook_line="gancho",
        description="Descricao do corte.",
        chapters=[Chapter(offset=0.0, title="Abertura"), Chapter(offset=90.0, title="Sono")],
        hashtags=["#a", "#b"],
        tags=["x"],
        source_url="https://www.youtube.com/watch?v=vid&t=754s",
        source_segments=[(754.0, 809.0), (1270.0, 1360.0)],
        language="pt",
    )
    markdown = to_markdown(metadata)
    assert markdown.startswith("# Titulo\n")
    assert "00:00 Abertura" in markdown
    assert "01:30 Sono" in markdown
    assert "#a #b" in markdown
    dash = "\u2013"
    expected = (
        f"Fonte: https://www.youtube.com/watch?v=vid&t=754s "
        f"(12:34 {dash} 13:29, 21:10 {dash} 22:40)"
    )
    assert expected in markdown


def test_markdown_of_a_short_has_no_chapter_block():
    metadata = ClipMetadata(
        profile="short",
        title="T",
        hook_line="h",
        description="d",
        hashtags=["#a"],
        source_url="u",
        source_segments=[(10.0, 40.0)],
        language="pt",
    )
    markdown = to_markdown(metadata)
    assert "00:00" not in markdown


# --- batching (one call per profile, not one per clip) ---------------------- #


def clips_for(n: int) -> list[Clip]:
    return [
        Clip(
            index=i,
            slug=f"c{i}",
            profile="short",
            segments=[TimeRange(start=i * 60.0, end=i * 60.0 + 45.0)],
            topic=f"tema {i}",
        )
        for i in range(1, n + 1)
    ]


def batch_answer(n: int) -> dict:
    return {
        "items": [
            {
                "index": i,
                "title": f"Titulo {i}",
                "hook_line": f"gancho {i}",
                "description": f"descricao {i}",
                "chapter_titles": [],
                "hashtags": [f"tema{i}"],
                "tags": [f"tag{i}"],
            }
            for i in range(1, n + 1)
        ]
    }


def test_five_clips_take_one_call(transcript, source):
    provider = CannedProvider(batch_answer(5))
    produced = build_metadata_batch(
        clips_for(5), resolve_profile("short"), source, transcript, provider
    )
    assert len(provider.prompts) == 1
    assert sorted(produced) == [1, 2, 3, 4, 5]
    assert produced[3].title == "Titulo 3"


def test_the_prompt_carries_every_clip(transcript, source):
    provider = CannedProvider(batch_answer(3))
    build_metadata_batch(clips_for(3), resolve_profile("short"), source, transcript, provider)
    prompt = provider.prompts[0]
    for i in (1, 2, 3):
        assert f"### Corte {i}" in prompt
        assert f"tema {i}" in prompt
    assert "3 cortes" in prompt


def test_answers_are_matched_by_index_not_position(transcript, source):
    """A reordered answer must still land on the right clip."""
    answer = batch_answer(3)
    answer["items"].reverse()
    produced = build_metadata_batch(
        clips_for(3), resolve_profile("short"), source, transcript, CannedProvider(answer)
    )
    assert produced[1].title == "Titulo 1"
    assert produced[3].title == "Titulo 3"


def test_answers_without_an_index_fall_back_to_order(transcript, source):
    answer = {
        "items": [
            {k: v for k, v in item.items() if k != "index"} for item in batch_answer(3)["items"]
        ]
    }
    produced = build_metadata_batch(
        clips_for(3), resolve_profile("short"), source, transcript, CannedProvider(answer)
    )
    assert produced[1].title == "Titulo 1"
    assert produced[2].title == "Titulo 2"


def test_a_missing_clip_is_skipped_not_fatal(transcript, source):
    answer = batch_answer(3)
    del answer["items"][1]  # the model forgot clip 2
    produced = build_metadata_batch(
        clips_for(3), resolve_profile("short"), source, transcript, CannedProvider(answer)
    )
    assert sorted(produced) == [1, 3]


def test_no_clips_means_no_call(transcript, source):
    provider = CannedProvider(batch_answer(0))
    assert build_metadata_batch([], resolve_profile("short"), source, transcript, provider) == {}
    assert provider.prompts == []


def test_clip_header_announces_multi_segment_chapters(transcript):
    clip = Clip(
        index=1,
        slug="s",
        profile="long",
        segments=[TimeRange(start=0, end=150), TimeRange(start=300, end=450)],
        chapters=[ChapterHint(offset_hint="o foco"), ChapterHint(offset_hint="o sono")],
    )
    header = describe_clip(clip, transcript)
    assert "- trechos: 2" in header
    assert "o foco; o sono" in header
