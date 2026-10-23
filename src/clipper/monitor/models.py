"""Views the monitor page renders. Nothing here is written back to the pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field

from clipper.agent.budget import BudgetLine
from clipper.agent.models import CheckView
from clipper.models import ReviewRequest

#: Where an item sits when it has finished the whole pipeline.
DELIVERED = "deliver"

#: A `running` row with no pipeline process behind it. Not a status the database
#: has: it is what the page calls a stage whose run ended without closing it.
STALLED = "parado"


class StageView(BaseModel):
    """One node of the flow diagram, for one video or one (video, profile)."""

    name: str
    status: str = "pending"
    detail: str = ""
    updated_at: str = ""


class ClipView(BaseModel):
    """A clip travelling through the profile stages."""

    video_id: str
    profile: str
    index: int
    slug: str
    dir_name: str
    title: str = ""
    topic: str = ""
    hook: str = ""
    score: float = 0.0
    duration: float = 0.0
    status: str = "pending"
    #: Stage the clip is sitting at right now.
    node: str = "select"
    #: True while that stage is actually working on it.
    active: bool = False
    rendered: bool = False
    packaged: bool = False
    delivered: bool = False
    size_bytes: int = 0
    has_thumbnail: bool = False
    segments: list[tuple[float, float]] = Field(default_factory=list)
    updated_at: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.video_id}/{self.profile}/{self.dir_name}"


class ProfileView(BaseModel):
    """One lane of the diagram: everything that happens per (video, profile)."""

    name: str
    stages: list[StageView] = Field(default_factory=list)
    clips: list[ClipView] = Field(default_factory=list)
    #: True while the pipeline is stopped waiting for a decision on this page.
    awaiting_review: bool = False


class VideoView(BaseModel):
    video_id: str
    title: str = ""
    channel: str = ""
    url: str = ""
    duration: float = 0.0
    stages: list[StageView] = Field(default_factory=list)
    profiles: list[ProfileView] = Field(default_factory=list)
    #: Stage of the video itself (ingest/transcribe) or "profiles" once fanned out.
    node: str = "ingest"
    active: bool = False
    failed: bool = False
    delivered: bool = False
    #: Name of the log file the tail came from, e.g. ``render_short``.
    log_name: str = ""
    log: list[str] = Field(default_factory=list)
    #: True when the workspace was already cleaned and this is remembered state.
    from_history: bool = False
    updated_at: float = 0.0
    #: The agent session of this video, when there is one (SPEC-agent 9).
    agent: AgentRunView | None = None


# --------------------------------------------------------------------------- #
# The agent session (SPEC-agent 9)
# --------------------------------------------------------------------------- #


class AgentStepView(BaseModel):
    """One line of the trail: a call, a failure or a refusal."""

    seq: int
    ts: str = ""
    #: tool_call | tool_error | budget_deny
    kind: str = "tool_call"
    tool: str = ""
    #: The arguments in Portuguese, short enough to read in a list.
    summary: str = ""
    duration_ms: int = 0
    error: str = ""
    detail: str = ""
    output: str = ""
    truncated: bool = False


class AgentClipView(BaseModel):
    """A clip as this run treated it: what it cost and how it ended."""

    clip_id: str
    profile: str = ""
    index: int = 0
    topic: str = ""
    title: str = ""
    #: escolhido | renderizado | revisado | com metadados | publicado
    stage: str = "escolhido"
    renders: int = 0
    verdict: str = ""
    checks: list[CheckView] = Field(default_factory=list)


class BoundaryChangeView(BaseModel):
    """A `snap_clip` that moved an interval, with both sides of the move."""

    ts: str = ""
    before: list[tuple[float, float]] = Field(default_factory=list)
    after: list[tuple[float, float]] = Field(default_factory=list)
    accepted: bool = True
    reason: str = ""


class AgentNow(BaseModel):
    """What is happening this second - the answer to "is it stuck?"."""

    text: str = ""
    tool: str = ""
    summary: str = ""
    since_s: float = 0.0


class AgentRunView(BaseModel):
    """A whole session, projected from its JSONL (SPEC-agent 9.2)."""

    run_id: str
    video_id: str = ""
    url: str = ""
    model: str = ""
    #: rodando | concluida | incompleta | falhou | cancelada
    state: str = "rodando"
    reason: str = ""
    started_at: str = ""
    ended_at: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    #: Seconds since the last line was written; a big number means it is stuck.
    idle_s: float = 0.0
    budget: list[BudgetLine] = Field(default_factory=list)
    now: AgentNow = Field(default_factory=AgentNow)
    clips: list[AgentClipView] = Field(default_factory=list)
    trail: list[AgentStepView] = Field(default_factory=list)
    errors: list[AgentStepView] = Field(default_factory=list)
    boundary_changes: list[BoundaryChangeView] = Field(default_factory=list)
    delivered: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    destination: str = ""
    #: How many trail steps exist in the log, when only a tail is carried.
    trail_total: int = 0
    #: What you asked of this run from the page: run | pause | stop.
    control: str = "run"
    vetoed: list[str] = Field(default_factory=list)

    def summary(self, *, steps: int = 5) -> AgentRunView:
        """The small version the snapshot carries, so the SSE stays light."""
        return self.model_copy(
            update={
                "trail": self.trail[-steps:],
                "trail_total": len(self.trail),
                "boundary_changes": [],
                "errors": self.errors[-3:],
            }
        )


# --------------------------------------------------------------------------- #
# The eval round (SPEC-agent 11)
# --------------------------------------------------------------------------- #


class EvalClipView(BaseModel):
    """One clip of a round, with your mark and enough to play it back."""

    clip_id: str
    video_id: str = ""
    profile: str = ""
    index: int = 0
    title: str = ""
    #: Directory under the profile's `clips/`; empty once the workspace is wiped.
    dir_name: str = ""
    duration: float = 0.0
    #: The agent rendered it and then took it out - marking it says whether it was right.
    dropped: bool = False
    playable: bool = False
    note: int = 0
    comment: str = ""


class EvalRoundView(BaseModel):
    """A directory under `evals/` holding a `notas.md`."""

    name: str
    path: str
    clips: list[EvalClipView] = Field(default_factory=list)
    marked: int = 0
    mean_note: float = 0.0


class QueueItem(BaseModel):
    position: int
    url: str
    video_id: str = ""
    title: str = ""
    #: fila | processando | parado | entregue | falhou | invalido
    status: str = "fila"


class Snapshot(BaseModel):
    generated_at: float
    running: bool = False
    workspace: str = ""
    queue_path: str = ""
    queue: list[QueueItem] = Field(default_factory=list)
    videos: list[VideoView] = Field(default_factory=list)
    video_stages: list[str] = Field(default_factory=list)
    profile_stages: list[str] = Field(default_factory=list)
    clips_total: int = 0
    clips_delivered: int = 0
    #: Command lines of the clipper processes currently running.
    processes: list[str] = Field(default_factory=list)
    #: Reviews waiting for a person, in the order they started waiting.
    pending_reviews: list[ReviewRequest] = Field(default_factory=list)
    #: Channels configured, for the editor on the page (SPEC-agent 7).
    channels: list[str] = Field(default_factory=list)
    #: Eval rounds waiting for your marks (SPEC-agent 11.3).
    evals: list[EvalRoundView] = Field(default_factory=list)
    error: str = ""
