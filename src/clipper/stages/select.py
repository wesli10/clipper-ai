"""Stage 2 - clip selection (SPEC 6).

The LLM proposes *what* to cut; this module decides *exactly where*. Timestamps
coming from the model are never used directly - they always go through
:func:`snap_boundaries` (CLAUDE.md).
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from itertools import pairwise

from clipper.llm.base import LLMProvider
from clipper.llm.ollama import CHARS_PER_TOKEN
from clipper.models import (
    Clip,
    ClipCandidate,
    Segment,
    SelectionResponse,
    TimeRange,
    Transcript,
)
from clipper.profiles import Profile
from clipper.utils.errors import ExternalError, InputError
from clipper.utils.text import format_timestamp, slugify

# --- tuning constants, all from SPEC 6.1 step 4 ---------------------------- #

#: A pause longer than this, just before the proposed start, is a natural entry point.
SILENCE_GAP_S = 0.3
#: How far back to look for that pause.
SILENCE_LOOKBACK_S = 1.5
#: A pause longer than this ends a sentence even without punctuation.
SENTENCE_END_GAP_S = 0.5
#: How far forward we may travel looking for the end of a sentence.
SENTENCE_FORWARD_S = 15.0
#: A pause longer than this is a topic boundary.
TOPIC_GAP_S = 1.5
#: Search radius around the proposed instant in ``topic`` mode.
TOPIC_SEARCH_S = 8.0
#: Intervals of the same clip closer than this are merged.
MERGE_GAP_S = 2.0
#: A clip is dropped if any of its intervals is shorter than this.
MIN_INTERVAL_S = 8.0
#: A candidate is dropped when it overlaps a better one by more than this fraction.
MAX_OVERLAP_RATIO = 0.30

SENTENCE_PUNCT = ".?!…"
#: A gap this long ends a sentence even when whisper wrote no punctuation.
#:
#: Calibrated on two real transcripts: at 0.6 s only 89% of the resulting units
#: ended in punctuation - the rest were sentences split by an ordinary speaking
#: pause - and at 1.2 s that reaches 99% without making the units meaningfully
#: longer (3.8 s -> 4.3 s average). A well-punctuated transcript is unaffected.
SENTENCE_PAUSE_S = 1.2

#: Tag questions. In Portuguese these close a thought instead of opening one -
#: "o repertorio conta, né?" and "sao o 7, o 11 e o 13, sacou?" are conclusions,
#: not questions waiting for an answer, and rejecting them threw away good clips.
RHETORICAL_TAGS: tuple[str, ...] = (
    "ne",
    "nao e",
    "sacou",
    "entendeu",
    "entende",
    "certo",
    "sabe",
    "ta",
    "viu",
    "ok",
    "hein",
    "concorda",
    "percebe",
    "saca",
)
#: Trailing marks that mean the speaker trailed off, not that they finished.
TRAILING_OFF = ("...", "\u2026", "..", "-", "\u2013")

#: Starting a segment with one of these means the previous one closed nothing -
#: the speaker is still going.
CONTINUATION_STARTERS: tuple[str, ...] = (
    "mas ",
    "porque ",
    "por que ",
    "e ",
    "ai ",
    "entao ",
    "que ",
    "so que ",
    "tipo ",
    "ou seja",
    "sendo que",
    "alem disso",
    "inclusive",
    "dai ",
)

#: Preamble: costs the viewer nothing when what follows has substance, and makes
#: the clip open on nothing when it does not ("Cara, ta la, aprovado.").
SOFT_OPENERS: frozenset[str] = frozenset(
    {
        "é",
        "e",
        "ó",
        "o",
        "ô",
        "ah",
        "ahn",
        "eh",
        "hum",
        "uhum",
        "aham",
        "pô",
        "po",
        "poxa",
        "nossa",
        "caramba",
        "eita",
        "putz",
        "opa",
        "uau",
        "cara",
        "mano",
        "velho",
        "gente",
        "olha",
        "olhe",
        "veja",
        "assim",
    }
)

#: Openers that always mean the clip starts in the middle of something. Each one
#: was the real first line of a clip that shipped - while the model reported a
#: hook that sounded nothing like it.
HARD_OPENERS: tuple[str, ...] = (
    # answering a question the viewer never heard
    "sim ",
    "nao mas",
    "nao nao",
    "nao sei se",
    "exato",
    "exatamente",
    "com certeza",
    "pois e",
    "pois entao",
    "claro que",
    # carrying on a list or an earlier answer
    "segundo ",
    "terceiro ",
    "quarto ",
    "quinto ",
    "tambem ",
    "depois disso",
    # hedging instead of saying something
    "e que ",
    "ta bom",
    "ta certo",
    "ta legal",
    "sei la",
    "deixa eu ver",
    "voce ve",
    "vamos supor",
)

#: Words pointing back at something the viewer never saw. Near the start of a
#: sentence they are the clearest sign the clip began too late ("Tem tudo isso.").
BACK_REFERENCES: frozenset[str] = frozenset(
    {
        "isso",
        "isto",
        "aquilo",
        "esse",
        "essa",
        "esses",
        "essas",
        "disso",
        "dessa",
        "desse",
        "daquilo",
        "nisso",
        "ele",
        "ela",
        "eles",
        "elas",
        "dele",
        "dela",
    }
)
#: Markers of a story already under way. In the middle of a sentence they are
#: ordinary speech; in its first words they mean the clip walked in late
#: ("Pegou o carro, ai voce desmirilhou o carro").
MID_NARRATIVE_MARKERS: frozenset[str] = frozenset({"ai", "entao", "dai", "tipo"})
#: How far into the opening a back-reference or a marker still disqualifies it.
BACK_REFERENCE_WORDS = 4
#: What is left after the preamble has to be a sentence, not a fragment.
MIN_OPENING_WORDS = 6
#: How far the start may travel forward looking for a sentence that opens.
HOOK_ADVANCE_S = 12.0
#: How much of the clip's head the quoted hook may appear in. Tighter than
#: CLOSING_TAIL_S on purpose: a hook is the opening line, not "somewhere early".
HOOK_HEAD_S = 10.0

#: Words that, at the start of a segment, signal a change of subject (SPEC 6.1).
TOPIC_CONNECTORS: tuple[str, ...] = (
    "entao",
    "agora",
    "outra coisa",
    "mudando",
    "voltando",
    "aliais",
    "alias",
    "proximo",
    "proxima",
    "outro ponto",
    "outro tema",
    "outra pergunta",
    "deixa eu te perguntar",
    "vamos falar",
    "falando em",
    "bom",
    "beleza",
    "enfim",
    "so",
    "now",
    "another thing",
    "moving on",
    "next",
    "anyway",
    "let's talk",
)

_ACCENTS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
    "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z' ]+", " ", text.translate(_ACCENTS).lower()).strip()


def _normalize_quote(text: str) -> str:
    """Like :func:`_normalize` but keeps digits.

    Comparing quotes without them makes "sao o 7, o 11 e o 13" indistinguishable
    from any other sentence of the same shape.
    """
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", text.translate(_ACCENTS).lower())
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass(frozen=True, slots=True)
class RejectedClip:
    """A candidate dropped by the deterministic rules, with the reason."""

    candidate: ClipCandidate
    reason: str


# --------------------------------------------------------------------------- #
# Transcript navigation helpers
# --------------------------------------------------------------------------- #


def sentences(transcript: Transcript) -> list[Segment]:
    """Group whisper segments into sentences.

    A segment is not a sentence. How finely whisper splits speech varies a lot
    with the audio: one real video came back 98% of segments ending in
    punctuation, another only 31% - the latter arriving as sub-sentence
    fragments ("Então eu nunca consigo" / "Mapear uma esfera" / "Num plano").
    Every boundary rule here assumes it is looking at a sentence, so build them
    first: merge segments until real punctuation or a real pause.
    """
    raw = transcript.segments
    if not raw:
        return []

    merged: list[Segment] = []
    current: list[Segment] = []
    for index, segment in enumerate(raw):
        current.append(segment)
        gap = raw[index + 1].start - segment.end if index + 1 < len(raw) else float("inf")
        if _ends_sentence(segment) or gap >= SENTENCE_PAUSE_S:
            merged.append(_merge(current, len(merged)))
            current = []
    if current:
        merged.append(_merge(current, len(merged)))
    return merged


def _merge(group: list[Segment], index: int) -> Segment:
    return Segment(
        id=index,
        start=group[0].start,
        end=group[-1].end,
        text=" ".join(s.text.strip() for s in group if s.text.strip()),
        words=[w for s in group for w in s.words],
    )


def _segment_index_at(segments: list[Segment], t: float) -> int:
    """Index of the segment containing ``t``; the following segment when in a gap."""
    starts = [s.start for s in segments]
    idx = bisect_right(starts, t) - 1
    if idx < 0:
        return 0
    if t >= segments[idx].end and idx + 1 < len(segments):
        return idx + 1
    return idx


def _gap_before(segments: list[Segment], index: int) -> float:
    if index <= 0:
        return float("inf")
    return segments[index].start - segments[index - 1].end


def _gap_after(segments: list[Segment], index: int) -> float:
    if index >= len(segments) - 1:
        return float("inf")
    return segments[index + 1].start - segments[index].end


def _starts_with_connector(segment: Segment) -> bool:
    text = _normalize(segment.text)
    return any(text.startswith(c) for c in TOPIC_CONNECTORS)


def _ends_sentence(segment: Segment) -> bool:
    text = segment.text.rstrip()
    if text.endswith(TRAILING_OFF):
        # "Nossa, mas..." ends with a period but is the opposite of an ending.
        return False
    return text.endswith(tuple(SENTENCE_PUNCT))


def _starts_continuation(segment: Segment) -> bool:
    """True when this segment continues the previous thought rather than opening one.

    Accents are kept on purpose: stripping them turns the interjection "É" into
    the conjunction "e", and a perfectly good ending before "É, eu ia te
    perguntar isso" gets rejected as a continuation.
    """
    text = re.sub(r"[^a-zà-ÿ' ]+", " ", segment.text.lower()).strip()
    if not text:
        return False
    return text.startswith(CONTINUATION_STARTERS)


def _words_of(segment: Segment) -> list[str]:
    """The sentence's words, lowercased and free of punctuation, accents kept."""
    return re.sub(r"[^a-zà-ÿ' ]+", " ", segment.text.lower()).split()


def opens_well(segments: list[Segment], index: int, *, mode: str = "sentence") -> bool:
    """Whether ``segments[index]`` can open a clip - the mirror of :func:`is_closing`.

    The end of a clip has three deterministic checks and the start had none, so
    clips shipped opening on "Po, nao, nao sabia disso ai, gente" while the model
    reported a hook that read nothing like it. A short lives on its first
    sentence (SPEC 1.2), so the same kind of rule belongs here.

    In ``topic`` mode the connector *is* the opening - a long starts where the
    subject does, and "Agora, falando de..." is exactly that boundary.
    """
    segment = segments[index]
    words = _words_of(segment)
    if not words:
        return False
    if _starts_continuation(segment):
        return False
    if mode != "topic" and _starts_with_connector(segment):
        return False

    body = list(words)
    while body and body[0] in SOFT_OPENERS:
        body.pop(0)
    if not body:
        return False
    if " ".join(word.translate(_ACCENTS) for word in body).startswith(HARD_OPENERS):
        return False
    head = [word.translate(_ACCENTS) for word in body[:BACK_REFERENCE_WORDS]]
    if any(word in BACK_REFERENCES or word in MID_NARRATIVE_MARKERS for word in head):
        return False
    # A preamble is fine in front of a real sentence and fatal in front of a
    # fragment: "Cara, eu perdi duzentos mil" opens, "Cara, ta la, aprovado" does not.
    return not (len(body) < len(words) and len(body) < MIN_OPENING_WORDS)


def is_closing(segments: list[Segment], index: int) -> bool:
    """Whether the end of ``segments[index]`` closes the subject.

    A sentence boundary is not a subject boundary. Three things disqualify one,
    and each was observed ending a real clip mid-thought: trailing off
    ("Nossa, mas..."), a question - which opens a beat instead of closing it -
    and a following segment that simply carries on ("Mas como que foi...").
    """
    text = segments[index].text.rstrip()
    # `sentences()` already guarantees each unit ends at punctuation or a real
    # pause, so there is nothing more to check about completeness here.
    if text.endswith(TRAILING_OFF):
        return False
    if text.endswith("?") and not _is_rhetorical(text):
        return False
    return not (index + 1 < len(segments) and _starts_continuation(segments[index + 1]))


# --------------------------------------------------------------------------- #
# Boundary snapping
# --------------------------------------------------------------------------- #


def snap_start(transcript: Transcript, t: float, mode: str = "sentence") -> float:
    """Move ``t`` back to a natural start of speech (SPEC 6.1 step 4)."""
    segments = sentences(transcript)
    if not segments:
        return max(0.0, t)

    if mode == "topic":
        strong = _topic_start(transcript, t)
        if strong is not None:
            return strong

    index = _segment_index_at(segments, t)
    candidate = segments[index].start
    refined = _end_of_silence_before(transcript, candidate, segments)
    return refined if refined is not None else candidate


def _end_of_silence_before(
    transcript: Transcript, candidate: float, segments: list[Segment]
) -> float | None:
    """Start of the first word after a real pause, within the look-back window.

    The pause has to line up with the start of a segment. An intra-sentence
    hesitation can easily exceed :data:`SILENCE_GAP_S` - real speech is full of
    them - and starting there would open the clip mid-sentence, which SPEC 1.2
    forbids outright.
    """
    starts = {round(segment.start, 3) for segment in segments}
    best: float | None = None
    window_start = candidate - SILENCE_LOOKBACK_S
    for previous, current in pairwise(transcript.words()):
        if current.start > candidate + 1e-6:
            break
        if current.start < window_start:
            continue
        if current.start - previous.end > SILENCE_GAP_S and round(current.start, 3) in starts:
            best = current.start
    return best


def snap_end(transcript: Transcript, t: float, mode: str = "sentence") -> float:
    """Move ``t`` forward to a point that does not break the sentence (SPEC 6.1).

    This guarantees the cut is not mid-word, mid-sentence, on a trailing "...",
    on a question, or immediately before a continuation - the failures seen in
    real clips. It does **not** decide where the subject ends: that is semantic,
    it belongs to the model, and searching far for it makes clips drift into
    unrelated material.
    """
    segments = sentences(transcript)
    if not segments:
        return max(0.0, t)

    if mode == "topic":
        strong = _topic_end(transcript, t)
        if strong is not None:
            return strong

    # The segment that ends at or after `t`. Using _segment_index_at here would
    # skip the model's own segment whenever its end coincides with `t`, which is
    # exactly what the prompt asks the model to do.
    index = next((i for i, seg in enumerate(segments) if seg.end >= t - 1e-3), len(segments) - 1)

    # The model picked this end deliberately, and deciding where a subject ends
    # is its job. If its choice already closes cleanly, keep it: searching on
    # from here is how good endings got pushed into the next speaker's line.
    if abs(segments[index].end - t) <= 1e-3 and is_closing(segments, index):
        return segments[index].end

    limit = t + SENTENCE_FORWARD_S

    # The nearest closing, not the best-looking one. Preferring a distant strong
    # pause was measurably worse: it walked clips 20-30 s past what the model
    # asked for, into unrelated banter. Whether a subject is finished is a
    # semantic question; the snapper only refuses to stop somewhere broken.
    for i in range(index, len(segments)):
        segment = segments[i]
        if segment.end > limit:
            break
        if i > index and _starts_with_connector(segment):
            # A new subject starts here; going past it changes what the clip is about.
            break
        if is_closing(segments, i):
            return segment.end

    # Nothing closed cleanly in the window.
    for i in range(index, len(segments)):
        segment = segments[i]
        if segment.start > limit:
            break
        if segment.end > limit:
            continue
        if _ends_sentence(segment) or _gap_after(segments, i) > SENTENCE_END_GAP_S:
            return segment.end

    end = segments[index].end
    if end <= limit:
        return end
    # One sentence runs past the whole window - someone talking without pause or
    # punctuation. There is no textual boundary to use, so fall back to the only
    # signal left: the widest gap between words inside the window.
    return _widest_word_gap(transcript, t, limit) or end


def _widest_word_gap(transcript: Transcript, start: float, limit: float) -> float | None:
    """End of the word before the biggest silence in ``[start, limit]``."""
    best: tuple[float, float] | None = None
    for previous, current in pairwise(transcript.words()):
        if previous.end < start:
            continue
        if previous.end > limit:
            break
        gap = current.start - previous.end
        if best is None or gap > best[0]:
            best = (gap, previous.end)
    return None if best is None else best[1]


def _topic_start(transcript: Transcript, t: float) -> float | None:
    """Closest strong boundary to ``t``: a long pause or a topic connector."""
    segments = sentences(transcript)
    best: tuple[float, float] | None = None
    for i, segment in enumerate(segments):
        distance = abs(segment.start - t)
        if distance > TOPIC_SEARCH_S:
            continue
        strong = _gap_before(segments, i) > TOPIC_GAP_S or _starts_with_connector(segment)
        if strong and (best is None or distance < best[0]):
            best = (distance, segment.start)
    return None if best is None else best[1]


def _topic_end(transcript: Transcript, t: float) -> float | None:
    segments = sentences(transcript)
    best: tuple[float, float] | None = None
    for i, segment in enumerate(segments):
        distance = abs(segment.end - t)
        if distance > TOPIC_SEARCH_S:
            continue
        follows_connector = i + 1 < len(segments) and _starts_with_connector(segments[i + 1])
        strong = _gap_after(segments, i) > TOPIC_GAP_S or (
            _ends_sentence(segment) and follows_connector
        )
        if strong and (best is None or distance < best[0]):
            best = (distance, segment.end)
    return None if best is None else best[1]


def snap_range(
    transcript: Transcript,
    interval: TimeRange,
    *,
    mode: str = "sentence",
    pad_before: float = 0.25,
    pad_after: float = 0.4,
) -> TimeRange:
    """Snap one interval to speech boundaries and apply the padding of SPEC 6.1."""
    start = snap_start(transcript, interval.start, mode)
    end = snap_end(transcript, max(interval.end, start + 1e-3), mode)
    if end <= start:
        end = snap_end(transcript, start + 1e-3, "sentence")
    start = _pad_into_silence_before(transcript, start, pad_before)
    end = _pad_into_silence_after(transcript, end, pad_after)
    if end <= start:
        end = start + 0.5
    return TimeRange(start=round(start, 3), end=round(end, 3))


def _pad_into_silence_before(transcript: Transcript, start: float, pad: float) -> float:
    """Extend the start backwards, but never over the previous word.

    Padding is there to avoid clipping the first syllable; letting it run into
    the neighbouring sentence would undo the snapping we just did.
    """
    floor = 0.0
    for word in transcript.words():
        if word.end <= start + 1e-6:
            floor = max(floor, word.end)
        else:
            break
    return max(0.0, floor, start - pad)


def _pad_into_silence_after(transcript: Transcript, end: float, pad: float) -> float:
    """Extend the end forwards, but never into the next word."""
    ceiling = transcript.duration if transcript.duration else end + pad
    for word in transcript.words():
        if word.start >= end - 1e-6:
            ceiling = min(ceiling, word.start)
            break
    return min(ceiling, end + pad)


def merge_intervals(intervals: list[TimeRange], *, gap: float = MERGE_GAP_S) -> list[TimeRange]:
    """Sort chronologically and fuse intervals closer than ``gap`` (SPEC 6.1)."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda r: (r.start, r.end))
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start - last.end < gap:
            merged[-1] = TimeRange(start=last.start, end=max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def _is_rhetorical(text: str) -> bool:
    """Whether a trailing "?" is a tag question rather than a real one."""
    words = _normalize(text.rstrip().rstrip("?")).split()
    if not words:
        return False
    return words[-1] in RHETORICAL_TAGS or " ".join(words[-2:]) in RHETORICAL_TAGS


def ends_on_question(transcript: Transcript, end: float, tolerance: float = 1.0) -> bool:
    """Whether the clip stops on a question whose answer is not in it.

    The single most damaging way a clip can end: the viewer is left holding the
    question. No heuristic can find the answer for us, so the candidate is
    rejected and the model gets to choose again (SPEC 6.1 step 3).
    """
    for segment in sentences(transcript):
        if abs(segment.end - end) <= tolerance:
            text = segment.text.rstrip()
            return text.endswith("?") and not _is_rhetorical(text)
    return False


#: How much of the clip's tail the quoted closing may appear in.
CLOSING_TAIL_S = 25.0


def closing_is_present(transcript: Transcript, candidate: ClipCandidate) -> bool:
    """Whether the model's quoted closing line really is near the clip's end.

    The model is asked to quote the clip's last sentence. Checking it turns "does
    this clip finish its subject" from something we have to trust into something
    we can verify: a quote that is not there means it never looked at the ending.
    """
    quoted = _normalize_quote(candidate.closing)
    if len(quoted) < 8:
        return True  # nothing usable to check against
    last = candidate.segments[-1]
    tail_start = max(last.start, last.end - CLOSING_TAIL_S)
    return quoted in _normalize_quote(transcript.text_between(tail_start, last.end))


def hook_is_present(transcript: Transcript, candidate: ClipCandidate) -> bool:
    """Whether the model's quoted hook really is at the clip's start.

    The mirror of :func:`closing_is_present`. Without it the hook was a promise
    nobody checked: the model quoted a line that prends and the clip opened
    somewhere else entirely.
    """
    quoted = _normalize_quote(candidate.hook)
    if len(quoted) < 8:
        return True  # nothing usable to check against
    first = candidate.segments[0]
    head_end = min(first.end, first.start + HOOK_HEAD_S)
    return quoted in _normalize_quote(transcript.text_between(first.start, head_end))


def opening_sentence(transcript: Transcript, interval: TimeRange) -> Segment | None:
    """The sentence the clip opens on."""
    for segment in sentences(transcript):
        if segment.end > interval.start + 0.05:
            return segment
    return None


def advance_to_opening(
    transcript: Transcript,
    interval: TimeRange,
    *,
    mode: str = "sentence",
    floor_s: float,
    pad_before: float = 0.25,
) -> TimeRange | None:
    """Move the start forward to the first sentence that can open a clip.

    Returns the interval untouched when it already opens, a shorter one when a
    real opening is close enough, and ``None`` when there is none within
    :data:`HOOK_ADVANCE_S` - a rejection whose reason travels back to the model
    on the retry, which is the only way it learns to pick a better start.
    """
    segments = sentences(transcript)
    if not segments:
        return interval
    index = next((i for i, s in enumerate(segments) if s.end > interval.start + 0.05), None)
    if index is None or opens_well(segments, index, mode=mode):
        return interval

    limit = interval.start + HOOK_ADVANCE_S
    for following in range(index + 1, len(segments)):
        start = segments[following].start
        if start > limit or interval.end - start < floor_s:
            break
        if opens_well(segments, following, mode=mode):
            padded = _pad_into_silence_before(transcript, start, pad_before)
            return TimeRange(start=round(padded, 3), end=interval.end)
    return None


def snap_reveal(
    transcript: Transcript,
    interval: TimeRange,
    reveal_at: float,
    profile: Profile,
) -> float | str:
    """Move the reveal to the start of a sentence, or say why it cannot be held.

    Returns the snapped timestamp, or the reason it was refused.

    The hold only works if what precedes it is a real question: holding on a
    statement gives the viewer nothing to guess. That is checkable, so it is
    checked here rather than trusted to the prompt - the same reasoning as
    :func:`closing_is_present`.
    """
    segments = sentences(transcript)
    if not segments:
        return "transcricao sem frases para ancorar a revelacao"

    inside = [s for s in segments if interval.start < s.start < interval.end]
    if not inside:
        return "nao ha comeco de frase dentro do corte para segurar o card"
    snapped = min(inside, key=lambda s: abs(s.start - reveal_at)).start

    lead = snapped - interval.start
    tail = interval.end - snapped
    minimum = profile.card_min_part_s
    if lead < minimum:
        return f"pergunta de {lead:.1f}s antes da revelacao (minimo {minimum:g}s)"
    if tail < minimum:
        return f"resposta de {tail:.1f}s apos a revelacao (minimo {minimum:g}s)"
    if not ends_on_question(transcript, snapped, tolerance=0.25):
        return (
            "a frase logo antes de reveal_at nao e uma pergunta; "
            "o card so funciona segurando uma pergunta de verdade"
        )
    return snapped


def snap_candidate(
    transcript: Transcript,
    candidate: ClipCandidate,
    profile: Profile,
    *,
    pad_before: float = 0.25,
    pad_after: float = 0.4,
) -> ClipCandidate | RejectedClip:
    """Apply step 4 of SPEC 6.1 to a whole candidate."""
    raw = candidate.segments[: profile.max_segments]
    snapped = [
        snap_range(
            transcript,
            interval,
            mode=profile.boundary_mode,
            pad_before=pad_before,
            pad_after=pad_after,
        )
        for interval in raw
    ]
    merged = merge_intervals(snapped)

    if profile.multi_segment and len(merged) > 1:
        short = [r for r in merged if r.duration < MIN_INTERVAL_S]
        if short:
            return RejectedClip(
                candidate,
                f"intervalo de {short[0].duration:.1f}s abaixo do minimo de {MIN_INTERVAL_S:g}s",
            )
    if len(merged) > profile.max_segments:
        merged = merged[: profile.max_segments]

    if candidate.score < profile.min_score:
        return RejectedClip(
            candidate,
            f"score {candidate.score:.0f} abaixo do minimo do perfil "
            f"({profile.min_score:.0f}); melhor devolver menos cortes",
        )

    if not closing_is_present(transcript, candidate):
        return RejectedClip(
            candidate,
            f'a frase de fechamento citada ("{candidate.closing[:50]}") nao esta no fim do corte',
        )

    if merged and ends_on_question(transcript, merged[-1].end):
        return RejectedClip(
            candidate,
            "termina numa pergunta e a resposta fica de fora; estenda ate a resposta",
        )

    if not hook_is_present(transcript, candidate):
        return RejectedClip(
            candidate,
            f'a frase de gancho citada ("{candidate.hook[:50]}") nao esta no inicio do corte',
        )

    updates: dict[str, object] = {}
    if merged:
        # The first sentence is the hook (SPEC 1.2). Nothing checked it, and
        # clips opened on "Segundo, o que ja estao falando" or "Po, nao sabia
        # disso ai". When the opening is somebody else's sentence, start at the
        # next one that stands on its own - and refuse the clip when there is none.
        rest = sum(r.duration for r in merged[1:])
        opened = advance_to_opening(
            transcript,
            merged[0],
            mode=profile.boundary_mode,
            floor_s=max(MIN_INTERVAL_S, profile.duration_min - rest),
            pad_before=pad_before,
        )
        if opened is None:
            said = opening_sentence(transcript, merged[0])
            quoted = (said.text.strip()[:60] + "…") if said else "?"
            return RejectedClip(
                candidate,
                f'abre no meio de outra fala ("{quoted}"); comece numa frase que prenda sozinha',
            )
        if opened.start != merged[0].start:
            merged[0] = opened
            moved = opening_sentence(transcript, opened)
            if moved is not None:
                updates["hook"] = moved.text.strip()

    updates["segments"] = merged
    if profile.card_seconds > 0:
        if candidate.reveal_at is None:
            return RejectedClip(candidate, "formato quiz exige 'reveal_at'")
        reveal = snap_reveal(transcript, merged[0], candidate.reveal_at, profile)
        if isinstance(reveal, str):
            return RejectedClip(candidate, reveal)
        updates["reveal_at"] = reveal

    total = sum(r.duration for r in merged)
    if total < profile.duration_min:
        return RejectedClip(
            candidate,
            f"duracao {total:.1f}s abaixo do minimo do perfil ({profile.duration_min:g}s)",
        )
    if total > profile.duration_max:
        return RejectedClip(
            candidate,
            f"duracao {total:.1f}s acima do maximo do perfil ({profile.duration_max:g}s)",
        )

    updates["chapters"] = candidate.chapters[: len(merged)]
    return candidate.model_copy(update=updates)


def overlap_ratio(a: ClipCandidate, b: ClipCandidate) -> float:
    """Overlapping share of the smaller of the two candidates."""
    overlap = sum(x.overlap(y) for x in a.segments for y in b.segments)
    smallest = min(a.duration, b.duration)
    return overlap / smallest if smallest > 0 else 0.0


def drop_overlapping(
    candidates: list[ClipCandidate],
) -> tuple[list[ClipCandidate], list[RejectedClip]]:
    """Keep the highest-scoring candidate of each overlapping group (SPEC 6.1)."""
    kept: list[ClipCandidate] = []
    rejected: list[RejectedClip] = []
    for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
        clash = next((k for k in kept if overlap_ratio(candidate, k) > MAX_OVERLAP_RATIO), None)
        if clash is None:
            kept.append(candidate)
        else:
            rejected.append(
                RejectedClip(
                    candidate,
                    f"sobrepoe mais de {MAX_OVERLAP_RATIO:.0%} do corte "
                    f"'{clash.topic or format_timestamp(clash.start)}' que tem score maior",
                )
            )
    return kept, rejected


def snap_boundaries(
    transcript: Transcript,
    candidates: list[ClipCandidate],
    profile: Profile,
    *,
    pad_before: float = 0.25,
    pad_after: float = 0.4,
) -> tuple[list[ClipCandidate], list[RejectedClip]]:
    """Full step 4 of SPEC 6.1: snap, merge, validate, de-overlap, rank, truncate."""
    snapped: list[ClipCandidate] = []
    rejected: list[RejectedClip] = []
    for candidate in candidates:
        result = snap_candidate(
            transcript, candidate, profile, pad_before=pad_before, pad_after=pad_after
        )
        if isinstance(result, RejectedClip):
            rejected.append(result)
        else:
            snapped.append(result)

    kept, overlapping = drop_overlapping(snapped)
    rejected.extend(overlapping)
    kept.sort(key=lambda c: c.score, reverse=True)
    return kept[: profile.n_clips], rejected


# --------------------------------------------------------------------------- #
# Prompt construction and the stage entry point (SPEC 6.1 steps 1-3, 5)
# --------------------------------------------------------------------------- #

#: Fed back to the model when every candidate broke a rule (SPEC 6.1 step 3).
RETRY_TEMPLATE = """A sua resposta anterior foi descartada por inteiro: nenhum corte
respeitou as regras. Motivos, corte a corte:

{reasons}

Corrija e responda de novo, no mesmo formato JSON. Preste atencao em especial a
duracao: **some voce mesmo as duracoes dos intervalos de cada corte** e confirme que o
total fica entre {duration_min}s e {duration_max}s antes de responder. Se um corte ficar
curto demais, estenda os intervalos ou junte mais trechos do mesmo tema; nao devolva
cortes fora da faixa."""


SYSTEM_PROMPT = (
    "Voce e um editor de video profissional. Voce responde sempre e somente com um objeto "
    "JSON valido no schema pedido, sem texto em volta e sem cercas de markdown."
)


def format_transcript(transcript: Transcript, *, window: TimeRange | None = None) -> str:
    """One line per sentence, prefixed with its timestamp (SPEC 6.1 step 1).

    Sentences, not raw whisper segments. The model is told to copy the marker
    values verbatim, so the markers *are* the only ends it can choose from - and
    on a transcript whisper split into 1.2 s fragments, every one of them fell
    mid-sentence. No prompt wording fixes that; the choices have to be whole
    sentences to begin with.

    The marker carries both the readable ``hh:mm:ss.s`` of the spec and the raw
    seconds the JSON schema expects. Asking the model to convert between the two
    is a conversion the prompt never spelled out, and a small model gets it
    wrong: a real run returned ``0.02`` for a segment at ``[00:00:02.0]``.
    """
    lines = []
    for segment in sentences(transcript):
        if window is not None and (segment.end <= window.start or segment.start >= window.end):
            continue
        stamp = format_timestamp(segment.start, always_hours=True)
        fraction = f"{segment.start - int(segment.start):.1f}"[1:]
        lines.append(
            f"[{stamp}{fraction} | {segment.start:.2f}s -> {segment.end:.2f}s] "
            f"{segment.text.strip()}"
        )
    return "\n".join(lines)


def build_prompt(
    profile: Profile,
    transcript: Transcript,
    *,
    n_clips: int | None = None,
    transcript_text: str | None = None,
    window: TimeRange | None = None,
    extra: str | None = None,
) -> str:
    """Fill the profile's selection template.

    Uses explicit replacement rather than ``str.format`` because the templates
    contain literal JSON braces.
    """
    template = profile.select_prompt.read_text(encoding="utf-8")
    wanted = n_clips or profile.n_clips
    values = {
        "n_clips": str(wanted),
        "n_candidates": str(wanted + profile.extra_candidates),
        "duration_min": f"{profile.duration_min:g}",
        "duration_max": f"{profile.duration_max:g}",
        "duration_target": f"{profile.duration_target:g}",
        "max_segments": str(profile.max_segments),
        "language": transcript.language,
        "transcript": (
            transcript_text
            if transcript_text is not None
            else format_transcript(transcript, window=window)
        ),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    # The caller's extra instruction is appended, never merged into the template:
    # the profile's prompt is calibrated and a run-specific note has no business
    # rewriting it.
    if extra and extra.strip():
        template += f"\n\n---\nInstrucao adicional desta chamada:\n{extra.strip()}\n"
    return template


def to_clips(candidates: list[ClipCandidate], profile: Profile) -> list[Clip]:
    """Turn ranked candidates into numbered :class:`Clip` objects."""
    clips: list[Clip] = []
    for index, candidate in enumerate(candidates, start=1):
        label = candidate.topic or candidate.hook or f"corte-{index}"
        clips.append(
            Clip(
                index=index,
                slug=slugify(label),
                profile=profile.name,
                segments=list(candidate.segments),
                hook=candidate.hook,
                reason=candidate.reason,
                score=candidate.score,
                topic=candidate.topic,
                chapters=list(candidate.chapters),
                reveal_at=candidate.reveal_at,
                card_text=candidate.card_text,
                comment_prompt=candidate.comment_prompt,
            )
        )
    return clips


def select_in_windows(
    transcript: Transcript,
    profile: Profile,
    provider: LLMProvider,
    *,
    n_clips: int | None = None,
    logger: logging.Logger | None = None,
    window: TimeRange | None = None,
    extra: str | None = None,
) -> list[ClipCandidate]:
    """Windowed selection for small-context providers (SPEC 6.4).

    Each overlapping window is asked for candidates, then one merge call
    deduplicates them and - for multi-segment profiles - may combine intervals
    that came from different windows into a single clip.
    """
    from clipper.llm.ollama import merge_prompt, split_windows

    wanted = n_clips or profile.n_clips
    windows = split_windows(
        format_transcript(transcript, window=window),
        window_tokens=provider.max_context_tokens,
        overlap=0.10,
    )
    if logger is not None:
        logger.info("select %s: %d janelas de transcricao", profile.name, len(windows))

    collected: list[ClipCandidate] = []
    for chunk in windows:
        prompt = build_prompt(
            profile, transcript, n_clips=wanted, transcript_text=chunk.text, extra=extra
        )
        try:
            answer = provider.complete_json(SYSTEM_PROMPT, prompt, SelectionResponse)
        except ExternalError as exc:
            if logger is not None:
                logger.warning("janela %d falhou (%s); seguindo", chunk.index, exc)
            continue
        collected.extend(answer.clips)

    if len(windows) <= 1 or not collected:
        return collected

    template_path = profile.select_prompt.parent / "merge_windows.md"
    prompt = merge_prompt(
        template_path.read_text(encoding="utf-8"),
        [c.model_dump(mode="json") for c in collected],
        profile_name=profile.name,
        n_clips=wanted,
        duration_min=profile.duration_min,
        duration_target=profile.duration_target,
        duration_max=profile.duration_max,
        max_segments=profile.max_segments,
    )
    try:
        merged = provider.complete_json(SYSTEM_PROMPT, prompt, SelectionResponse)
    except ExternalError as exc:
        if logger is not None:
            logger.warning("merge das janelas falhou (%s); usando os candidatos brutos", exc)
        return collected
    return list(merged.clips)


def _retry_note(rejected: list[RejectedClip], profile: Profile) -> str:
    reasons = "\n".join(
        f'- "{item.candidate.topic or item.candidate.hook or "sem titulo"}": {item.reason}'
        for item in rejected[:10]
    )
    return RETRY_TEMPLATE.format(
        reasons=reasons,
        duration_min=f"{profile.duration_min:g}",
        duration_max=f"{profile.duration_max:g}",
    )


@dataclass(frozen=True, slots=True)
class SelectionResult:
    clips: list[Clip]
    rejected: list[RejectedClip]
    raw: list[ClipCandidate]
    #: Size of the prompt the provider was given, for the run's cost report.
    prompt_chars: int = 0


def select_clips(
    transcript: Transcript,
    profile: Profile,
    provider: LLMProvider,
    *,
    n_clips: int | None = None,
    pad_before: float = 0.25,
    pad_after: float = 0.4,
    logger: logging.Logger | None = None,
    window: TimeRange | None = None,
    extra: str | None = None,
) -> SelectionResult:
    """Ask the provider for candidates, then snap them deterministically.

    ``window`` narrows the stretch of transcript the model reads; the snapping
    still sees the whole thing, because a boundary is decided by the sentences
    around it and those may sit outside the window. ``extra`` is a run-specific
    instruction appended after the profile's template (SPEC-agent 5.6).
    """
    if not transcript.segments:
        raise InputError("a transcricao esta vazia; nada para selecionar")

    prompt = build_prompt(profile, transcript, n_clips=n_clips, window=window, extra=extra)
    if logger is not None:
        logger.info(
            "select %s: prompt de %d chars sobre %d segmentos",
            profile.name,
            len(prompt),
            len(transcript.segments),
        )

    if len(prompt) // CHARS_PER_TOKEN > provider.max_context_tokens:
        raw = select_in_windows(
            transcript,
            profile,
            provider,
            n_clips=n_clips,
            logger=logger,
            window=window,
            extra=extra,
        )
    else:
        raw = list(provider.complete_json(SYSTEM_PROMPT, prompt, SelectionResponse).clips)
    if logger is not None:
        logger.info("select %s: %d candidatos brutos", profile.name, len(raw))

    effective = profile.model_copy(update={"n_clips": n_clips or profile.n_clips})
    kept, rejected = snap_boundaries(
        transcript, raw, effective, pad_before=pad_before, pad_after=pad_after
    )

    # Everything broke a rule: tell the model what went wrong and ask once more.
    # Same principle as the invalid-JSON retry of SPEC 6.1 step 3.
    if not kept and rejected:
        if logger is not None:
            logger.info(
                "select %s: todos os %d candidatos foram descartados; pedindo correcao",
                profile.name,
                len(rejected),
            )
        retry = f"{prompt}\n\n---\n{_retry_note(rejected, effective)}"
        try:
            retried = list(provider.complete_json(SYSTEM_PROMPT, retry, SelectionResponse).clips)
        except ExternalError as exc:
            if logger is not None:
                logger.warning("a segunda tentativa falhou (%s)", exc)
            retried = []
        if retried:
            raw = retried
            kept, rejected = snap_boundaries(
                transcript, raw, effective, pad_before=pad_before, pad_after=pad_after
            )

    if logger is not None:
        for item in rejected:
            label = item.candidate.topic or item.candidate.hook
            logger.info("descartado (%s): %s", item.reason, label)
    return SelectionResult(
        clips=to_clips(kept, profile),
        rejected=rejected,
        raw=raw,
        prompt_chars=len(prompt),
    )
