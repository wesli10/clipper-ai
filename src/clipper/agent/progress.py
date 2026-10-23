"""How far a profile got, and what that means for its stage row.

Three callers have to agree on this count or the monitor lies: `render_clip` and
`set_metadata` write it after every clip they finish, and the runtime writes it
once more when the run ends. The last one is the point - a row left at `running`
by a run that is over keeps the page showing a pipeline that is not there, and
nothing else ever comes back to close it (SPEC-agent 8.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from clipper.agent.tools.base import ToolContext
from clipper.profiles import ProfileError
from clipper.state import NO_PROFILE, StageRecord, StageStatus
from clipper.utils.errors import ClipperError

#: Appended to the detail of a stage the run left unfinished.
STALLED_NOTE = "run encerrada"

#: The two stages counted clip by clip, and so the two that can be left half-written.
CountedStage = Literal["render", "package"]


@dataclass(frozen=True, slots=True)
class Counted:
    """Clips of one profile, by how far each one got."""

    rendered: int
    packaged: int
    total: int

    @property
    def render_detail(self) -> str:
        return f"{self.rendered}/{self.total} cortes"

    @property
    def package_detail(self) -> str:
        # A profile with nothing rendered has nothing to package, but a 0/0 row
        # would read as done; the floor of 1 keeps it honest.
        return f"{self.packaged}/{max(1, self.rendered)} cortes"


def count(ctx: ToolContext, video_id: str, profile_name: str) -> Counted:
    """Walk the profile's clips and see what is on disk."""
    from clipper import pipeline
    from clipper.agent.tools.select import effective_profile
    from clipper.stages.render import clip_dir_for

    profile = effective_profile(ctx, video_id, profile_name)
    clip_set = pipeline.load_clips(ctx.settings, video_id, profile_name)
    dirs = [
        clip_dir_for(ctx.settings, video_id, profile, clip)
        for clip in pipeline.renderable(clip_set)
    ]
    rendered = [d for d in dirs if (d / "clip.mp4").exists()]
    packaged = [d for d in rendered if (d / "metadata.json").exists()]
    return Counted(rendered=len(rendered), packaged=len(packaged), total=len(dirs))


def mark(
    ctx: ToolContext,
    video_id: str,
    profile_name: str,
    stage: CountedStage,
    *,
    stalled: bool = False,
) -> Counted:
    """Write the stage row from what is on disk, and say what it found.

    With ``stalled`` the run is over: a stage still short of its total goes back to
    `pending` - there is work left and nobody doing it - instead of `running`.
    """
    counted = count(ctx, video_id, profile_name)
    if stage == "render":
        done, total, detail = counted.rendered, counted.total, counted.render_detail
    else:
        done, total, detail = counted.packaged, max(1, counted.rendered), counted.package_detail

    if done >= total:
        status = StageStatus.DONE
    elif stalled:
        status = StageStatus.PENDING
        detail = f"{detail}; {STALLED_NOTE}"
    else:
        status = StageStatus.RUNNING

    ctx.state().set_stage(video_id, stage, status, profile=profile_name, detail=detail)
    return counted


def reconcile(ctx: ToolContext, video_id: str) -> list[str]:
    """Close every stage this video left at `running`, and say which ones those were.

    Called once the run is over, whatever ended it. Either the work is on disk and
    the row should say done, or it is not and the row should say pending.
    """
    notes: list[str] = []
    for record in ctx.state().stages_for(video_id):
        if record.status is not StageStatus.RUNNING:
            continue
        stage = _counted_stage(record)
        parked = True
        if stage is not None:
            try:
                mark(ctx, video_id, record.profile, stage, stalled=True)
                parked = False
            except (ClipperError, ProfileError):
                # The clips of that profile are gone; park the row like any other.
                pass
        if parked:
            _park(ctx, video_id, record.stage, record.profile, record.detail)
        notes.append(f"{record.stage} ({record.profile}) estava em running e foi fechado")
    return notes


def _counted_stage(record: StageRecord) -> CountedStage | None:
    """The stage of this row, when it is one :func:`mark` knows how to count."""
    if record.profile == NO_PROFILE:
        return None
    if record.stage == "render":
        return "render"
    if record.stage == "package":
        return "package"
    return None


def _park(ctx: ToolContext, video_id: str, stage: str, profile: str, detail: str) -> None:
    """Back to pending, keeping whatever the row already said it had done."""
    ctx.state().set_stage(
        video_id,
        stage,
        StageStatus.PENDING,
        profile=profile,
        detail=f"{detail}; {STALLED_NOTE}" if detail else STALLED_NOTE,
    )
