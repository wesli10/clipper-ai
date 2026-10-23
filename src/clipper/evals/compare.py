"""Comparing one agent run against the baseline (SPEC-agent 11.3).

The overlap is the honest half of this: it says whether the agent found the same
moments, not whether it found better ones. That is what your mark in `notas.md`
is for, and why the report puts the two side by side.
"""

from __future__ import annotations

from clipper.config import Settings
from clipper.evals.models import (
    MATCH_THRESHOLD,
    BaselineClip,
    BaselineVideo,
    ClipMatch,
    VideoComparison,
)
from clipper.models import ClipSet
from clipper.monitor.agent import RunStore, run_paths
from clipper.state import State

Interval = tuple[float, float]


def overlap_ratio(left: list[Interval], right: list[Interval]) -> float:
    """Intersection over union of two sets of intervals.

    Union rather than the shorter side: a clip that contains the baseline one and
    twice as much again is not the same clip, and IoU says so.
    """
    if not left or not right:
        return 0.0
    shared = sum(
        max(0.0, min(a_end, b_end) - max(a_start, b_start))
        for a_start, a_end in left
        for b_start, b_end in right
    )
    if shared <= 0:
        return 0.0
    total = sum(e - s for s, e in left) + sum(e - s for s, e in right) - shared
    return shared / total if total > 0 else 0.0


def match_clips(
    agent: list[tuple[str, str, list[Interval]]], baseline: list[BaselineClip]
) -> tuple[list[ClipMatch], list[str]]:
    """Greedy pairing by overlap: the best pair first, each side used once."""
    pairs = sorted(
        (
            (overlap_ratio(segments, clip.segments), index, position)
            for index, (_, _, segments) in enumerate(agent)
            for position, clip in enumerate(baseline)
        ),
        reverse=True,
    )
    taken_agent: dict[int, int] = {}
    taken_baseline: set[int] = set()
    for score, index, position in pairs:
        if score < MATCH_THRESHOLD or index in taken_agent or position in taken_baseline:
            continue
        taken_agent[index] = position
        taken_baseline.add(position)

    matches: list[ClipMatch] = []
    for index, (clip_id, title, segments) in enumerate(agent):
        at = taken_agent.get(index)
        paired = baseline[at] if at is not None else None
        matches.append(
            ClipMatch(
                clip_id=clip_id,
                title=title,
                segments=segments,
                duration_s=round(sum(e - s for s, e in segments), 1),
                baseline_title=paired.title if paired else "",
                baseline_segments=paired.segments if paired else [],
                overlap=round(overlap_ratio(segments, paired.segments), 3) if paired else 0.0,
            )
        )
    missed = [
        clip.title or f"{clip.profile} {clip.segments}"
        for position, clip in enumerate(baseline)
        if position not in taken_baseline
    ]
    return matches, missed


def agent_clips(
    settings: Settings, video_id: str, profiles: list[str]
) -> list[tuple[str, str, list[Interval]]]:
    """What the run committed, as (clip_id, topic, intervals)."""
    found: list[tuple[str, str, list[Interval]]] = []
    for profile in profiles:
        path = settings.profile_dir(video_id, profile) / "clips.json"
        if not path.exists():
            continue
        clip_set = ClipSet.model_validate_json(path.read_text(encoding="utf-8"))
        for clip in clip_set.clips:
            found.append(
                (
                    f"{video_id}/{profile}/{clip.index}",
                    clip.topic or clip.slug,
                    [(round(s.start, 2), round(s.end, 2)) for s in clip.segments],
                )
            )
    return found


def compare(
    settings: Settings,
    baseline: BaselineVideo,
    *,
    profiles: list[str],
    run_id: str = "",
    built_at: str = "",
) -> VideoComparison:
    """One video: what the run did, against what was delivered before.

    ``built_at`` is when the baseline was read out of the Drive; a run older than
    that may be where those clips came from.
    """
    store = RunStore()
    paths = run_paths(settings.agent_dir(baseline.video_id))
    if run_id:
        paths = [path for path in paths if path.stem == run_id]
    view = store.read(paths[0]) if paths else None

    wanted = baseline.for_profile(profiles)
    matches, missed = match_clips(agent_clips(settings, baseline.video_id, profiles), wanted)

    by_clip = {clip.clip_id: clip for clip in (view.clips if view else [])}
    for match in matches:
        recorded = by_clip.get(match.clip_id)
        if recorded is not None:
            match.renders = recorded.renders
            match.verdict = recorded.verdict

    paired = [m for m in matches if m.overlap > 0]
    renders = sum(clip.renders for clip in (view.clips if view else []))
    delivered = len(view.delivered) if view and view.delivered else len(matches)
    checks = [check for clip in (view.clips if view else []) for check in clip.checks]

    # Once the agent has published a video, its clips are what the Drive holds -
    # so rebuilding the baseline from the Drive hands the agent its own work to
    # be compared against, and the overlap comes out at 100% saying nothing.
    #
    # "Before" is the whole point: the eval's own run starts after the baseline
    # was built, and counting that one would flag every video it ever measured.
    published_before = any(
        run.started_at < built_at
        for run in State(settings.state_db).list_runs(baseline.video_id)
        if built_at
    )

    return VideoComparison(
        video_id=baseline.video_id,
        title=baseline.title,
        run_id=view.run_id if view else "",
        state=view.state if view else "sem run",
        baseline_clips=len(wanted),
        agent_clips=len(matches),
        matched=len(paired),
        mean_overlap=round(sum(m.overlap for m in paired) / len(paired), 3) if paired else 0.0,
        renders=renders,
        renders_per_clip=round(renders / delivered, 2) if delivered else 0.0,
        turns=view.turns if view else 0,
        cost_usd=view.cost_usd if view else 0.0,
        elapsed_s=view.elapsed_s if view else 0.0,
        fails=sum(1 for check in checks if check.status == "fail"),
        warns=sum(1 for check in checks if check.status == "warn"),
        matches=matches,
        missed=missed,
        self_baseline=published_before and bool(paired),
    )
