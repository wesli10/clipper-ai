"""Stage 4 - metadata for publication (SPEC 7.2).

The LLM writes the words; the code computes every number - chapter offsets come
from the concat offsets, never from the model (CLAUDE.md).
"""

from __future__ import annotations

import logging

from clipper.llm.base import LLMProvider
from clipper.models import (
    Chapter,
    Clip,
    ClipMetadata,
    MetadataBatchResponse,
    MetadataResponse,
    SourceInfo,
    Transcript,
)
from clipper.profiles import Profile
from clipper.utils.text import format_timestamp, unbacked_numbers

SYSTEM_PROMPT = (
    "Voce escreve metadados de publicacao para videos. Responde sempre e somente com um "
    "objeto JSON valido no schema pedido, sem texto em volta e sem cercas de markdown."
)

#: A short needs no chapters; a long gets one per interval (SPEC 7.2).
MIN_CHAPTERS_FOR_LONG = 2


def clip_text(transcript: Transcript, clip: Clip) -> str:
    """Transcript of the clip, with a marker between non-contiguous intervals."""
    parts = [transcript.text_between(r.start, r.end) for r in clip.segments]
    return "\n\n[...]\n\n".join(p for p in parts if p)


def describe_clip(clip: Clip, transcript: Transcript) -> str:
    """One clip's header and transcript, as the batch prompt presents it."""
    hints = [c.offset_hint for c in clip.chapters]
    header = [
        f"### Corte {clip.index}",
        f"- index: {clip.index}",
        f"- tema: {clip.topic or '(sem tema)'}",
        f"- duracao: {clip.duration / 60:.1f} min",
        f"- trechos: {len(clip.segments)}",
    ]
    if len(clip.segments) > 1:
        header.append(f"- temas dos trechos, em ordem: {'; '.join(hints) or '(sem dica)'}")
    return "\n".join(header) + "\n\n" + clip_text(transcript, clip)


def build_prompt(
    profile: Profile,
    clips: list[Clip],
    source: SourceInfo,
    transcript: Transcript,
) -> str:
    """Fill the profile's metadata template for every clip at once.

    One call per profile instead of one per clip: it cuts the per-call overhead
    N-fold, and letting the model see the whole set is what stops five clips from
    the same video getting five near-identical titles and hashtag lists.
    """
    template = profile.metadata_prompt.read_text(encoding="utf-8")
    values = {
        "language": transcript.language,
        "video_title": source.title,
        "channel": source.channel,
        "n_clips": str(len(clips)),
        "clips": "\n\n---\n\n".join(describe_clip(c, transcript) for c in clips),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def chapter_offsets(clip: Clip) -> list[float]:
    """Where each interval starts inside the rendered clip (SPEC 7.2)."""
    if clip.offsets and len(clip.offsets) == len(clip.segments):
        return list(clip.offsets)
    # No render yet: fall back to a plain sum of the interval durations.
    offsets, cursor = [], 0.0
    for interval in clip.segments:
        offsets.append(round(cursor, 3))
        cursor += interval.duration
    return offsets


def source_url_for(source: SourceInfo, clip: Clip) -> str:
    """Source URL pointing at the first interval."""
    separator = "&" if "?" in source.url else "?"
    return f"{source.url}{separator}t={int(clip.segments[0].start)}s"


def build_metadata_batch(
    clips: list[Clip],
    profile: Profile,
    source: SourceInfo,
    transcript: Transcript,
    provider: LLMProvider,
    *,
    logger: logging.Logger | None = None,
) -> dict[int, ClipMetadata]:
    """Metadata for every clip of a profile, in a single call, keyed by clip index."""
    if not clips:
        return {}

    prompt = build_prompt(profile, clips, source, transcript)
    if logger is not None:
        logger.info("metadata de %d cortes (%s) numa chamada", len(clips), profile.name)
    answer = provider.complete_json(SYSTEM_PROMPT, prompt, MetadataBatchResponse)

    by_index = {item.index: item for item in answer.items if item.index}
    ordered = [item for item in answer.items if not item.index]
    result: dict[int, ClipMetadata] = {}
    for position, clip in enumerate(clips):
        item = by_index.get(clip.index)
        if item is None and position < len(ordered):
            # The model dropped the index; fall back to answer order.
            item = ordered[position]
        if item is None:
            if logger is not None:
                logger.warning("sem metadados para o corte %02d", clip.index)
            continue
        # The agent's own `set_metadata` refuses a title promising a number the cut
        # never says; this is the same claim written by the batch, and it ships in
        # the fallback path, so at least it does not ship quietly (SPEC 1.2).
        unbacked = unbacked_numbers(item.title, clip_text(transcript, clip))
        if unbacked and logger is not None:
            logger.warning(
                "corte %02d: o titulo promete %s, que o corte nao diz - %r",
                clip.index,
                ", ".join(str(n) for n in unbacked),
                item.title,
            )
        result[clip.index] = assemble(clip, profile, source, transcript, item)
    return result


def assemble(
    clip: Clip,
    profile: Profile,
    source: SourceInfo,
    transcript: Transcript,
    answer: MetadataResponse,
) -> ClipMetadata:
    """Turn one model answer into a :class:`ClipMetadata`, computing every number."""
    offsets = chapter_offsets(clip)
    wants_chapters = profile.multi_segment and len(clip.segments) > 1

    chapters: list[Chapter] = []
    if wants_chapters:
        titles = list(answer.chapter_titles)
        for index, offset in enumerate(offsets):
            if index < len(titles) and titles[index].strip():
                title = titles[index].strip()
            elif index < len(clip.chapters):
                title = clip.chapters[index].offset_hint
            else:
                title = f"Parte {index + 1}"
            chapters.append(Chapter(offset=offset, title=title))
        if chapters:
            chapters[0] = Chapter(offset=0.0, title=chapters[0].title)

    return ClipMetadata(
        profile=profile.name,
        title=answer.title.strip(),
        hook_line=(answer.hook_line or clip.hook).strip(),
        description=answer.description.strip(),
        chapters=chapters,
        hashtags=[_clean_hashtag(h) for h in answer.hashtags if h.strip()],
        tags=[t.strip() for t in answer.tags if t.strip()],
        thumbnail_text=answer.thumbnail_text.strip(),
        thumbnail_concept=answer.thumbnail_concept.strip(),
        source_url=source_url_for(source, clip),
        source_segments=[(r.start, r.end) for r in clip.segments],
        language=transcript.language,
    )


def _clean_hashtag(value: str) -> str:
    text = value.strip().lstrip("#").replace(" ", "")
    return f"#{text.lower()}" if text else ""


def to_markdown(metadata: ClipMetadata) -> str:
    """Copy-and-paste friendly ``metadata.md`` (SPEC 7.2)."""
    blocks = [f"# {metadata.title}", "", metadata.description, ""]

    if metadata.chapters:
        blocks.extend([f"{format_timestamp(c.offset)} {c.title}" for c in metadata.chapters] + [""])
    if metadata.hashtags:
        blocks.extend([" ".join(metadata.hashtags), ""])

    ranges = ", ".join(
        f"{format_timestamp(start)} \u2013 {format_timestamp(end)}"
        for start, end in metadata.source_segments
    )
    blocks.append(f"Fonte: {metadata.source_url} ({ranges})")
    return "\n".join(blocks) + "\n"
