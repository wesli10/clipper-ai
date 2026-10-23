"""What the agent's tools give back (SPEC-agent 5).

Every tool answers with one of these models, serialized as compact JSON and
truncated to the tool's declared ceiling. The model needs enough to decide, never
the raw data - no transcript, no whole ``clips.json``, no ffmpeg stdout.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clipper.profiles import Profile, ProfileError, SubtitleKind

# --------------------------------------------------------------------------- #
# Profile overrides - the only render knobs the model may turn
# --------------------------------------------------------------------------- #


class ProfileOverride(BaseModel):
    """The whitelist of SPEC-agent 5.8.

    Resolution, fonts, colours and prompts are configuration and stay out of the
    model's reach (CLAUDE.md). ``reframe`` and ``zoom`` are out too while face
    tracking is off: the whole frame is a decision of ``config.yaml``, and there
    is no cropped frame for a zoom to live on (SPEC-agent 2.1).
    """

    model_config = ConfigDict(extra="forbid")

    n_clips: int | None = Field(default=None, ge=1, le=30)
    tail_roll_s: float | None = Field(default=None, ge=0.0, le=15.0)
    subtitle_style: SubtitleKind | None = None
    banner_enabled: bool | None = None
    card_seconds: float | None = Field(default=None, ge=0.0, le=10.0)
    duration_min: float | None = Field(default=None, gt=0)
    duration_target: float | None = Field(default=None, gt=0)
    duration_max: float | None = Field(default=None, gt=0)
    min_score: float | None = Field(default=None, ge=0.0, le=100.0)

    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)

    def apply(self, profile: Profile) -> Profile:
        """Effective profile for this run.

        The durations may only move *inside* the configured range: the profile is
        what says a short is a short, and an agent widening its own bounds would
        make the format meaningless.
        """
        values = self.model_dump(exclude_none=True)
        for key in ("duration_min", "duration_target", "duration_max"):
            wanted = values.get(key)
            if wanted is None:
                continue
            if not profile.duration_min <= wanted <= profile.duration_max:
                raise ProfileError(
                    f"{key}={wanted:g}s fora da faixa do perfil '{profile.name}' "
                    f"({profile.duration_min:g}s a {profile.duration_max:g}s); "
                    "a faixa e configuracao, nao decisao da run"
                )

        raw: dict[str, Any] = profile.model_dump()
        banner_enabled = values.pop("banner_enabled", None)
        if banner_enabled is not None:
            raw["banner"] = {**raw["banner"], "enabled": banner_enabled}
        raw.update(values)
        # Back through the validator: card without subtitles, zoom without a crop
        # and an inconsistent duration range stay impossible. Pydantic wraps the
        # ProfileError, so it is unwrapped here - the agent reads this message.
        try:
            return Profile.model_validate(raw)
        except ValidationError as exc:
            reasons = "; ".join(
                error["msg"].removeprefix("Value error, ") for error in exc.errors()
            )
            raise ProfileError(f"override invalido para '{profile.name}': {reasons}") from exc


# --------------------------------------------------------------------------- #
# Tool results
# --------------------------------------------------------------------------- #


class ProfileSummary(BaseModel):
    name: str
    duration_min: float
    duration_target: float
    duration_max: float
    n_clips: int
    min_score: float
    multi_segment: bool
    max_segments: int
    boundary_mode: str
    reframe: str
    subtitle_style: str
    card_seconds: float
    output_width: int
    output_height: int


class ProfilesResult(BaseModel):
    """Only what the channel has enabled; a disabled format is not an option."""

    profiles: list[ProfileSummary] = Field(default_factory=list)
    overridable: list[str] = Field(default_factory=list)


class StageState(BaseModel):
    stage: str
    profile: str = ""
    status: str


class VideoResult(BaseModel):
    video_id: str
    title: str = ""
    channel: str = ""
    url: str = ""
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    language: str = ""
    upload_date: str = ""
    reused: bool = False
    stages_done: list[StageState] = Field(default_factory=list)


class TranscribeResult(BaseModel):
    video_id: str
    language: str
    duration_s: float
    n_segments: int
    n_words: int
    transcript_path: str
    reused: bool = False
    elapsed_s: float = 0.0


class OutlineBlock(BaseModel):
    start: float
    end: float
    n_sentences: int
    first_sentence: str


class OutlineResult(BaseModel):
    video_id: str
    duration_s: float
    n_sentences: int
    blocks: list[OutlineBlock] = Field(default_factory=list)


class BoundaryReport(BaseModel):
    """Whether an interval's edges hold up, by the same rules the pipeline uses."""

    opens_well: bool
    opening_sentence: str = ""
    is_closing: bool
    closing_sentence: str = ""
    ends_on_question: bool
    gap_before_s: float = 0.0
    gap_after_s: float = 0.0
    suggested_start: float
    suggested_end: float


class InspectResult(BaseModel):
    video_id: str
    start: float
    end: float
    duration_s: float
    before: str = ""
    text: str = ""
    after: str = ""
    boundaries: BoundaryReport


class CandidateView(BaseModel):
    id: str
    segments: list[tuple[float, float]] = Field(default_factory=list)
    duration_s: float
    score: float
    topic: str = ""
    hook: str = ""
    reason: str = ""
    self_contained: bool = True
    reveal_at: float | None = None
    #: The question drawn in the bar above the frame. It travels with the
    #: candidate because the scan already wrote one, and a field the agent never
    #: sees is a field it never passes on - which is how shorts shipped with an
    #: empty bar.
    comment_prompt: str = ""
    card_text: str = ""


class RejectedView(BaseModel):
    topic: str = ""
    segments: list[tuple[float, float]] = Field(default_factory=list)
    reason: str


class CandidatesResult(BaseModel):
    """Rejected candidates travel with the reason: it is what makes a fix possible."""

    video_id: str
    profile: str
    accepted: list[CandidateView] = Field(default_factory=list)
    rejected: list[RejectedView] = Field(default_factory=list)
    provider: str = ""
    n_prompt_chars: int = 0
    reused: bool = False


class SnapResult(BaseModel):
    accepted: bool
    segments: list[tuple[float, float]] = Field(default_factory=list)
    duration_s: float = 0.0
    reason: str = ""
    changed: bool = False
    hook: str = ""
    closing: str = ""
    reveal_at: float | None = None


class CommittedClip(BaseModel):
    clip_id: str
    index: int
    slug: str
    topic: str = ""
    duration_s: float
    segments: list[tuple[float, float]] = Field(default_factory=list)


class CommitResult(BaseModel):
    video_id: str
    profile: str
    clips: list[CommittedClip] = Field(default_factory=list)
    rejected: list[RejectedView] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)


class RenderResultView(BaseModel):
    clip_id: str
    path: str
    duration_s: float
    width: int
    height: int
    degraded: bool = False
    reused: bool = False
    elapsed_s: float = 0.0
    renders_left: int | None = None


class DropResult(BaseModel):
    """A clip taken out of the run, and what is left of the profile after it."""

    clip_id: str
    reason: str
    #: Clips of this profile still in play, and how far they got.
    remaining: int
    rendered: int
    packaged: int
    reused: bool = False


CheckStatus = Literal["ok", "warn", "fail"]


class CheckView(BaseModel):
    check: str
    status: CheckStatus
    value: str = ""
    expected: str = ""
    fix: str = ""


class ReviewResult(BaseModel):
    """The verdict decides whether spending another render is worth it."""

    clip_id: str
    verdict: CheckStatus
    checks: list[CheckView] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    renders_left: int | None = None

    @property
    def failed(self) -> list[CheckView]:
        return [c for c in self.checks if c.status == "fail"]


class MetadataResult(BaseModel):
    clip_id: str
    title: str
    filename: str = ""
    chapters: list[tuple[float, str]] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PublishedClip(BaseModel):
    clip_id: str
    title: str = ""
    size_bytes: int = 0


class PublishResult(BaseModel):
    video_id: str
    destination: str = ""
    dry_run: bool = False
    published: list[PublishedClip] = Field(default_factory=list)
    skipped: list[RejectedView] = Field(default_factory=list)
    #: Things the tool had to do on the agent's behalf, e.g. write missing metadata.
    notes: list[str] = Field(default_factory=list)


class FinishResult(BaseModel):
    run_id: str
    report_path: str
    delivered: int = 0
    skipped: int = 0
