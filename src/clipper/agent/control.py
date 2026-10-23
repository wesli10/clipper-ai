"""The operator's hand on a running session (SPEC-agent 9.5).

The agent is autonomous up to the Drive, which is exactly why there has to be a
handbrake. It is a file, like the review handshake: the page writes it, the
runtime reads it before every call, and nothing holds a socket open.

Pausing **blocks** rather than refusing. A refusal would burn a turn and a bit of
context every couple of seconds, and a paused run would spend its budget standing
still.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["run", "pause", "stop"]

#: How often a paused run looks to see whether it may carry on.
POLL_INTERVAL_S = 2.0
#: A pause nobody lifts should not hold the machine for ever.
MAX_PAUSE_S = 60 * 60


class SessionControl(BaseModel):
    """``<run_id>.control.json`` - what you asked of a run that is still going."""

    model_config = ConfigDict(extra="forbid")

    action: Action = "run"
    #: Clips you refused. They are not rendered again and never published.
    vetoed: list[str] = Field(default_factory=list, max_length=50)
    #: On stop: ship what is already finished, or leave it in the workspace.
    publish_on_stop: bool = True
    #: Who asked, for the log. The page writes "monitor".
    by: str = "monitor"
    at: float = 0.0

    @property
    def paused(self) -> bool:
        return self.action == "pause"

    @property
    def stopping(self) -> bool:
        return self.action == "stop"


def control_path(agent_dir: Path, run_id: str) -> Path:
    return agent_dir / f"{run_id}.control.json"


def read_control(path: Path) -> SessionControl:
    """The current order, or the default when there is no file or it is broken."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return SessionControl()
    try:
        return SessionControl.model_validate_json(raw)
    except ValueError:
        # A half-written file is not an emergency: carry on as if unpaused and
        # read it again on the next call.
        return SessionControl()


def write_control(path: Path, control: SessionControl) -> SessionControl:
    stamped = control.model_copy(update={"at": time.time()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stamped.model_dump_json(indent=2), encoding="utf-8")
    return stamped


class ControlWatcher:
    """Reads the control file for one run, without re-reading it every second."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime = -1.0
        self._control = SessionControl()

    def current(self) -> SessionControl:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return SessionControl()
        if mtime != self._mtime:
            self._mtime = mtime
            self._control = read_control(self.path)
        return self._control

    def vetoed(self) -> set[str]:
        return set(self.current().vetoed)


def clip_id_of(tool: str, args: dict[str, object]) -> str:
    """The clip a call is about, when it is about one."""
    if tool in ("review_render", "set_metadata"):
        return str(args.get("clip_id", ""))
    if tool == "render_clip":
        return f"{args.get('video_id', '')}/{args.get('profile', '')}/{args.get('index', '')}"
    return ""


def json_error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)
