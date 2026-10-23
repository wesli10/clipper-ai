"""Data models shared across pipeline stages.

Nothing but pydantic models travels between modules - no loose dicts (CLAUDE.md).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Transcript (SPEC 5.4)
# --------------------------------------------------------------------------- #


class Word(BaseModel):
    """A single word with its timing, as produced by faster-whisper."""

    model_config = ConfigDict(frozen=True)

    text: str
    start: float
    end: float
    prob: float = 1.0


class Segment(BaseModel):
    """A whisper segment (roughly a sentence)."""

    id: int
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    video_id: str
    language: str
    duration: float
    segments: list[Segment] = Field(default_factory=list)

    def words(self) -> list[Word]:
        """All words in chronological order."""
        return [w for seg in self.segments for w in seg.words]

    def text_between(self, start: float, end: float) -> str:
        """Transcript text overlapping ``[start, end]``."""
        parts = [s.text.strip() for s in self.segments if s.end > start and s.start < end]
        return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Source metadata (SPEC 5.1 / 5.2)
# --------------------------------------------------------------------------- #


class SourceInfo(BaseModel):
    """Everything the pipeline needs to know about a downloaded video."""

    video_id: str
    title: str
    channel: str
    duration: float
    url: str
    language: str | None = None
    width: int = 0
    height: int = 0
    upload_date: str | None = None


# --------------------------------------------------------------------------- #
# Selection (SPEC 6)
# --------------------------------------------------------------------------- #


class TimeRange(BaseModel):
    """A half-open interval of the source video, in seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlap(self, other: TimeRange) -> float:
        """Overlapping duration in seconds (0 when disjoint)."""
        return max(0.0, min(self.end, other.end) - max(self.start, other.start))

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.end <= self.start:
            raise ValueError(f"intervalo invalido: end ({self.end}) <= start ({self.start})")
        return self


class ChapterHint(BaseModel):
    """LLM-provided title for one interval of a multi-segment clip (SPEC 6.3)."""

    offset_hint: str


class ClipCandidate(BaseModel):
    """Raw LLM proposal, before deterministic boundary snapping."""

    segments: list[TimeRange] = Field(min_length=1)
    hook: str = ""
    #: The clip's last sentence, quoted from the transcript. Checked against the
    #: clip, so the model has to look at where it is ending.
    closing: str = ""
    #: What the viewer takes away - forces the payoff to be stated, not assumed.
    payoff: str = ""
    #: Quiz format: where the answer starts, in source seconds. The card is held
    #: here, so the viewer sees the question and not yet the answer.
    reveal_at: float | None = None
    #: The question sentence, quoted from the transcript. Checked like ``closing``.
    question: str = ""
    #: What the card asks the viewer while the frame is held.
    card_text: str = ""
    #: A question for the viewer to answer in the comments, shown in the bar
    #: above the frame for the whole clip.
    comment_prompt: str = ""
    reason: str = ""
    score: float = 0.0
    self_contained: bool = True
    topic: str = ""
    chapters: list[ChapterHint] = Field(default_factory=list)

    @property
    def start(self) -> float:
        return min(s.start for s in self.segments)

    @property
    def end(self) -> float:
        return max(s.end for s in self.segments)

    @property
    def duration(self) -> float:
        """Total playable duration - the sum of the intervals, not end-start."""
        return sum(s.duration for s in self.segments)


class SelectionResponse(BaseModel):
    """Envelope the LLM must return (SPEC 6.2)."""

    clips: list[ClipCandidate] = Field(default_factory=list)


class ClipStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Clip(BaseModel):
    """A candidate after boundary snapping; the unit rendered and packaged."""

    index: int
    slug: str
    profile: str
    segments: list[TimeRange] = Field(min_length=1)
    hook: str = ""
    reason: str = ""
    score: float = 0.0
    topic: str = ""
    chapters: list[ChapterHint] = Field(default_factory=list)
    status: ClipStatus = ClipStatus.PENDING
    #: Start of each interval inside the rendered clip, filled in by concat (SPEC 7.3 step 2).
    offsets: list[float] = Field(default_factory=list)
    #: Quiz format: where inside the clip the frame is held for the card.
    reveal_at: float | None = None
    card_text: str = ""
    comment_prompt: str = ""

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def dir_name(self) -> str:
        return f"{self.index:02d}_{self.slug}"


class ClipSet(BaseModel):
    """`candidates.json` / `clips.json` payload."""

    video_id: str
    profile: str
    clips: list[Clip] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Review handed to a browser (SPEC 7.1)
# --------------------------------------------------------------------------- #


class ReviewClipView(BaseModel):
    """One candidate as the review page needs to show it.

    Everything the page draws travels in the request file: the monitor never
    opens the transcript, which is megabytes, to render a decision screen.
    """

    index: int
    topic: str = ""
    hook: str = ""
    reason: str = ""
    score: float = 0.0
    duration: float = 0.0
    segments: list[TimeRange] = Field(default_factory=list)
    #: What is said inside the intervals, trimmed for reading on screen.
    text: str = ""
    status: ClipStatus = ClipStatus.PENDING


class ReviewRequest(BaseModel):
    """`review_request.json`: the pipeline is stopped, waiting for a person."""

    video_id: str
    profile: str
    video_title: str = ""
    channel: str = ""
    #: How many clips the profile asks for - context for how picky to be.
    n_clips: int = 0
    created_at: float = 0.0
    clips: list[ReviewClipView] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    index: int
    status: ClipStatus


class ReviewResponse(BaseModel):
    """`review_response.json`: what the person decided."""

    video_id: str = ""
    profile: str = ""
    decisions: list[ReviewDecision] = Field(default_factory=list)
    decided_at: float = 0.0


# --------------------------------------------------------------------------- #
# Packaging (SPEC 7.2)
# --------------------------------------------------------------------------- #


class Chapter(BaseModel):
    offset: float
    title: str


class ClipMetadata(BaseModel):
    profile: str
    title: str
    hook_line: str
    description: str
    chapters: list[Chapter] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    #: Three to five words for the thumbnail. Image models still garble a long
    #: accented sentence, so the punchy phrase beats the full title.
    thumbnail_text: str = ""
    #: What the art should depict, in one sentence.
    thumbnail_concept: str = ""
    source_url: str = ""
    source_segments: list[tuple[float, float]] = Field(default_factory=list)
    language: str = ""


class MetadataResponse(BaseModel):
    """What the LLM returns for metadata; offsets are computed by us, never by it."""

    #: 1-based clip index, so a reordered answer can still be matched up.
    index: int = 0
    title: str
    hook_line: str = ""
    description: str = ""
    chapter_titles: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    thumbnail_text: str = ""
    thumbnail_concept: str = ""


class MetadataBatchResponse(BaseModel):
    """Metadata for every clip of a profile, in one answer (one call, not N)."""

    items: list[MetadataResponse] = Field(default_factory=list)
