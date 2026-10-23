"""What an eval compares (SPEC-agent 11).

The baseline is what the linear pipeline already delivered; it is rebuilt from
the Drive rather than kept by hand, because the clips are the record and a list
maintained beside them would drift.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Below this, two clips are not the same moment of the video.
MATCH_THRESHOLD = 0.30


class BaselineClip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    title: str = ""
    segments: list[tuple[float, float]] = Field(default_factory=list)
    folder: str = ""

    @property
    def duration(self) -> float:
        return sum(end - start for start, end in self.segments)


class BaselineVideo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    url: str = ""
    title: str = ""
    channel: str = ""
    clips: list[BaselineClip] = Field(default_factory=list)

    def for_profile(self, names: list[str]) -> list[BaselineClip]:
        return [clip for clip in self.clips if clip.profile in names]


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    built_at: str = ""
    source: str = ""
    videos: list[BaselineVideo] = Field(default_factory=list)

    def video(self, video_id: str) -> BaselineVideo | None:
        return next((v for v in self.videos if v.video_id == video_id), None)


class ClipMatch(BaseModel):
    """One clip of the agent's, and the baseline clip it lines up with."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    title: str = ""
    segments: list[tuple[float, float]] = Field(default_factory=list)
    duration_s: float = 0.0
    baseline_title: str = ""
    baseline_segments: list[tuple[float, float]] = Field(default_factory=list)
    overlap: float = 0.0
    renders: int = 0
    verdict: str = ""


class VideoComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    title: str = ""
    run_id: str = ""
    state: str = ""
    baseline_clips: int = 0
    agent_clips: int = 0
    matched: int = 0
    mean_overlap: float = 0.0
    renders: int = 0
    renders_per_clip: float = 0.0
    turns: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    fails: int = 0
    warns: int = 0
    matches: list[ClipMatch] = Field(default_factory=list)
    #: Baseline clips the agent did not find. The interesting half of the report.
    missed: list[str] = Field(default_factory=list)
    #: The agent has published this video before, so part of "the baseline" is its
    #: own work and the overlap compares it to itself. Says so instead of scoring.
    self_baseline: bool = False

    @property
    def comparable(self) -> bool:
        return bool(self.matches) and not self.self_baseline


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    built_at: str = ""
    videos: list[VideoComparison] = Field(default_factory=list)
    #: Your marks, read back from notas.md when you have filled it in.
    notes: dict[str, int] = Field(default_factory=dict)

    @property
    def comparable(self) -> list[VideoComparison]:
        """Videos where the comparison means something."""
        return [v for v in self.videos if v.comparable]

    @property
    def mean_overlap(self) -> float:
        matched = [v for v in self.comparable if v.matched]
        return sum(v.mean_overlap for v in matched) / len(matched) if matched else 0.0

    @property
    def mean_note(self) -> float:
        marks = [value for value in self.notes.values() if value]
        return sum(marks) / len(marks) if marks else 0.0
