"""The agent session, read back from its JSONL (SPEC-agent 9.2).

A projection, not a second source of truth: if the log exists the session exists,
including after the run ended and including when the process died mid-way. The
file only ever grows, so it is read from where the last read stopped - rebuilding
a 2.000-line trail every 0,7 s is the obvious way to make the monitor the slowest
thing on the machine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from clipper.agent.budget import BudgetLine
from clipper.agent.control import control_path, read_control
from clipper.agent.models import CheckView
from clipper.monitor.models import (
    AgentClipView,
    AgentNow,
    AgentRunView,
    AgentStepView,
    BoundaryChangeView,
)
from clipper.utils.text import format_timestamp

#: Run states, in the words the page shows.
STATE_LABELS = {
    "done": "concluida",
    "incomplete": "incompleta",
    "failed": "falhou",
    "cancelled": "cancelada",
    "running": "rodando",
}

STAGE_ORDER = ("escolhido", "renderizado", "revisado", "com metadados", "publicado")


def _parse_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _loads(text: str) -> dict[str, Any]:
    """A tool answer as data, or an empty dict when it was truncated or plain."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def describe(tool: str, args: dict[str, Any]) -> str:
    """The call's arguments in one readable line."""
    if tool == "get_video":
        return str(args.get("url") or args.get("video_id") or "")
    if tool in ("transcribe_video", "get_outline"):
        return str(args.get("video_id", ""))
    if tool == "inspect_range":
        start, end = args.get("start", 0.0), args.get("end", 0.0)
        return f"{format_timestamp(float(start))} - {format_timestamp(float(end))}"
    if tool == "find_candidates":
        focus = f", foco: {args['focus']}" if args.get("focus") else ""
        return f"perfil {args.get('profile', '?')}{focus}"
    if tool == "snap_clip":
        segments = (args.get("clip") or {}).get("segments") or []
        return ", ".join(
            f"{format_timestamp(float(s))} - {format_timestamp(float(e))}" for s, e in segments
        )
    if tool == "commit_clips":
        return f"{args.get('profile', '?')}: {len(args.get('clips') or [])} cortes"
    if tool == "render_clip":
        force = " (refazendo)" if args.get("force") else ""
        return f"{args.get('profile', '?')} #{args.get('index', '?')}{force}"
    if tool == "review_render":
        return str(args.get("clip_id", ""))
    if tool == "set_metadata":
        return str(args.get("title", ""))
    if tool == "publish_clips":
        mode = " (ensaio)" if args.get("dry_run") else ""
        return f"{', '.join(args.get('profiles') or [])}{mode}"
    if tool == "finish_run":
        return str(args.get("summary", ""))[:80]
    return ""


@dataclass
class RunReader:
    """Incremental reader for one run's log."""

    path: Path
    offset: int = 0
    view: AgentRunView | None = None
    _clips: dict[str, AgentClipView] = field(default_factory=dict)
    _pending: tuple[str, str, float, dict[str, Any]] | None = None
    _last_ts: float = 0.0

    def refresh(self) -> AgentRunView:
        """Read whatever was appended since last time and return the session."""
        if self.view is None:
            self.view = AgentRunView(run_id=self.path.stem.removesuffix(".jsonl"))
        try:
            size = self.path.stat().st_size
        except OSError:
            return self._finish()
        if size < self.offset:
            # The file shrank: it is not the same run any more, start over.
            self.offset = 0
            self._clips.clear()
            self.view = AgentRunView(run_id=self.view.run_id)

        if size > self.offset:
            with self.path.open("r", encoding="utf-8") as handle:
                handle.seek(self.offset)
                for line in handle:
                    if not line.endswith("\n"):
                        # A line still being written; pick it up next time.
                        break
                    self.offset += len(line.encode("utf-8"))
                    self._apply(line)
        return self._finish()

    # -- one event ---------------------------------------------------------- #

    def _apply(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        view = self.view
        if view is None:
            return

        kind = str(event.get("type", ""))
        ts = str(event.get("ts", ""))
        self._last_ts = _parse_ts(ts) or self._last_ts

        if kind == "run_start":
            view.video_id = str(event.get("video_id", ""))
            view.url = str(event.get("url", ""))
            view.model = str(event.get("model", ""))
            view.started_at = ts
            view.budget = _budget_lines(event.get("budget"))
        elif kind == "turn":
            view.turns += 1
            view.now = AgentNow(text=str(event.get("text", "")))
        elif kind == "tool_start":
            tool = str(event.get("tool", ""))
            args = dict(event.get("input") or {})
            self._pending = (tool, ts, _parse_ts(ts), args)
        elif kind in ("tool_call", "tool_error"):
            self._pending = None
            self._step(event, kind)
        elif kind == "budget_deny":
            self._pending = None
            step = AgentStepView(
                seq=int(event.get("seq", 0)),
                ts=ts,
                kind="budget_deny",
                tool=str(event.get("tool", "")),
                summary=describe(str(event.get("tool", "")), dict(event.get("input") or {})),
                error=str(event.get("reason", "")),
            )
            view.trail.append(step)
            view.errors.append(step)
        elif kind == "run_end":
            view.state = STATE_LABELS.get(str(event.get("status", "")), str(event.get("status")))
            view.reason = str(event.get("reason", ""))
            view.ended_at = ts
            view.cost_usd = float(event.get("cost_usd") or 0.0)
            view.budget = _budget_lines(event.get("budget")) or view.budget
            view.elapsed_s = float(event.get("elapsed_s") or 0.0)
            view.now = AgentNow()
        elif kind == "run_error":
            view.state = "falhou"
            view.reason = str(event.get("error", ""))

    def _step(self, event: dict[str, Any], kind: str) -> None:
        view = self.view
        if view is None:
            return
        tool = str(event.get("tool", ""))
        args = dict(event.get("input") or {})
        output = str(event.get("output", "") or "")
        step = AgentStepView(
            seq=int(event.get("seq", 0)),
            ts=str(event.get("ts", "")),
            kind=kind,
            tool=tool,
            summary=describe(tool, args),
            duration_ms=int(event.get("duration_ms") or 0),
            error=str(event.get("error") or "") if kind == "tool_error" else "",
            detail=json.dumps(args, ensure_ascii=False)[:600],
            output=output[:1200],
            truncated=bool(event.get("output_truncated")),
        )
        view.trail.append(step)
        if kind == "tool_error":
            view.errors.append(step)
            return
        self._absorb(tool, args, _loads(output))

    def _absorb(self, tool: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
        """Turn a successful answer into what the page shows per clip."""
        view = self.view
        if view is None:
            return

        if tool == "commit_clips":
            for clip in payload.get("clips") or []:
                clip_id = str(clip.get("clip_id", ""))
                if not clip_id:
                    continue
                # Merged, never replaced: an agent that re-commits the list after
                # fixing one clip would otherwise wipe what the other clips cost
                # and how they were judged (seen on the first real run).
                known = self._clips.get(clip_id)
                if known is None:
                    self._clips[clip_id] = AgentClipView(
                        clip_id=clip_id,
                        profile=str(payload.get("profile", "")),
                        index=int(clip.get("index") or 0),
                        topic=str(clip.get("topic", "")),
                    )
                else:
                    known.topic = str(clip.get("topic", "")) or known.topic
        elif tool == "render_clip":
            clip = self._clip_for(str(payload.get("clip_id", "")), args)
            if clip is not None:
                clip.stage = _later(clip.stage, "renderizado")
                if not payload.get("reused"):
                    clip.renders += 1
        elif tool == "review_render":
            clip = self._clip_for(str(payload.get("clip_id", "")), args)
            if clip is not None:
                clip.stage = _later(clip.stage, "revisado")
                clip.verdict = str(payload.get("verdict", ""))
                clip.checks = [
                    CheckView.model_validate(check)
                    for check in payload.get("checks") or []
                    if isinstance(check, dict) and check.get("status") != "ok"
                ]
        elif tool == "set_metadata":
            clip = self._clip_for(str(payload.get("clip_id", "")), args)
            if clip is not None:
                clip.stage = _later(clip.stage, "com metadados")
                clip.title = str(payload.get("title", ""))
        elif tool == "publish_clips":
            view.destination = str(payload.get("destination", "")) or view.destination
            for item in payload.get("published") or []:
                clip = self._clip_for(str(item.get("clip_id", "")), {})
                if clip is not None and not payload.get("dry_run"):
                    clip.stage = "publicado"
        elif tool == "snap_clip":
            before = [
                (float(s), float(e)) for s, e in (args.get("clip") or {}).get("segments") or []
            ]
            after = [(float(s), float(e)) for s, e in payload.get("segments") or []]
            if before and (after != before or not payload.get("accepted", True)):
                view.boundary_changes.append(
                    BoundaryChangeView(
                        before=before,
                        after=after,
                        accepted=bool(payload.get("accepted", False)),
                        reason=str(payload.get("reason", "")),
                    )
                )
        elif tool == "finish_run":
            view.delivered = [str(item) for item in args.get("delivered") or []]
            view.skipped = [
                f"{item.get('what', '')}: {item.get('reason', '')}"
                for item in args.get("skipped") or []
                if isinstance(item, dict)
            ]

    def _clip_for(self, clip_id: str, args: dict[str, Any]) -> AgentClipView | None:
        if not clip_id and args:
            clip_id = str(args.get("clip_id", ""))
        if not clip_id:
            return None
        found = self._clips.get(clip_id)
        if found is None:
            profile, _, index = clip_id.rpartition("/")
            found = AgentClipView(
                clip_id=clip_id,
                profile=profile.rpartition("/")[2],
                index=int(index) if index.isdigit() else 0,
            )
            self._clips[clip_id] = found
        return found

    # -- assembling the answer ---------------------------------------------- #

    def _finish(self) -> AgentRunView:
        view = self.view
        if view is None:  # pragma: no cover - refresh always builds one
            return AgentRunView(run_id=self.path.stem)
        view.clips = sorted(self._clips.values(), key=lambda c: (c.profile, c.index))
        view.trail_total = len(view.trail)
        now = time.time()
        view.idle_s = round(max(0.0, now - self._last_ts), 1) if self._last_ts else 0.0
        if not view.ended_at and view.started_at:
            view.elapsed_s = round(max(0.0, now - _parse_ts(view.started_at)), 1)
        if self._pending is not None:
            tool, _, started, args = self._pending
            view.now = AgentNow(
                text=view.now.text,
                tool=tool,
                summary=describe(tool, args),
                since_s=round(max(0.0, now - started), 1),
            )
        return view


def _budget_lines(raw: Any) -> list[BudgetLine]:
    if not isinstance(raw, dict):
        return []
    return [
        BudgetLine.model_validate(line) for line in raw.get("lines") or [] if isinstance(line, dict)
    ]


def _later(current: str, candidate: str) -> str:
    """Stages only move forward: a re-render does not undo the review."""
    order = {name: index for index, name in enumerate(STAGE_ORDER)}
    return candidate if order.get(candidate, 0) >= order.get(current, 0) else current


# --------------------------------------------------------------------------- #
# Finding the runs of a video
# --------------------------------------------------------------------------- #


def run_paths(agent_dir: Path) -> list[Path]:
    """Every run log of a video, newest first."""
    if not agent_dir.is_dir():
        return []
    return sorted(agent_dir.glob("*.jsonl"), reverse=True)


class RunStore:
    """Keeps one reader per log, so each refresh only reads what is new."""

    def __init__(self) -> None:
        self._readers: dict[Path, RunReader] = {}

    def read(self, path: Path) -> AgentRunView:
        reader = self._readers.get(path)
        if reader is None:
            reader = RunReader(path=path)
            self._readers[path] = reader
        view = reader.refresh()
        # The control file is the operator's, not the run's: it is read here so
        # the page can show what it asked for, even before the run reacts.
        control = read_control(control_path(path.parent, view.run_id))
        view.control = control.action
        view.vetoed = list(control.vetoed)
        return view

    def latest(self, agent_dir: Path) -> AgentRunView | None:
        paths = run_paths(agent_dir)
        return self.read(paths[0]) if paths else None

    def listing(self, agent_dir: Path) -> list[AgentRunView]:
        return [self.read(path).summary(steps=0) for path in run_paths(agent_dir)]
