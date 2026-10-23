"""The page itself, run against a real snapshot (SPEC 15.4).

Everything else here tests what the server sends; nothing tested what the browser
does with it, and that is where the page broke: `renderAgent` read a `const`
declared eight lines below it, so the moment a video had an agent run with clips
the function threw - and `render` calls it third, so the journey, the gallery, the
videos, the queue, the channels, the marks and the log never drew. A blank page
and a silent console.

So: boot the script under node with a DOM stub, feed it a snapshot, and check the
sections filled in. It skips where node is missing rather than failing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.config import Settings
from clipper.models import Clip, ClipSet, SourceInfo, TimeRange
from clipper.monitor.server import STATIC_DIR
from clipper.monitor.snapshot import build_snapshot
from clipper.state import StageStatus, State

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node e necessario")

VIDEO_ID = "vid0000004"

#: Enough DOM for the script to load and draw; anything it touches answers.
BOOT = """
import fs from "node:fs";
const make = () => ({
  classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
  style: {}, dataset: {}, hidden: false, innerHTML: "", textContent: "", value: "",
  children: [], addEventListener(){}, contains: () => false, closest: () => null,
  appendChild(){}, remove(){}, focus(){}, blur(){},
});
const seen = {};
const el = (id) => (seen[id] = seen[id] || make());
globalThis.document = {
  getElementById: el, addEventListener(){}, querySelector: () => make(),
  querySelectorAll: () => [], createElement: () => make(),
  body: make(), documentElement: make(), activeElement: null, title: "",
};
globalThis.window = { addEventListener(){} };
globalThis.matchMedia = () => ({ matches: false, addEventListener(){} });
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.EventSource = class { addEventListener(){} close(){} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;

const source = fs.readFileSync(process.argv[2], "utf8");
const api = new Function("return (function(){" + source + "; return { render }; })()")();
api.render(JSON.parse(fs.readFileSync(process.argv[3], "utf8")));
const filled = {};
for (const [id, node] of Object.entries(seen)) {
  if (node.innerHTML) filled[id] = node.innerHTML.length;
}
console.log(JSON.stringify({ filled, hidden: { evals: seen["ev-wrap"].hidden } }));
"""


def page_script() -> str:
    """The page's inline JavaScript, as the browser would run it."""
    import re

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def draw(tmp_path: Path, snapshot_json: str) -> dict:
    """Run the page over one snapshot; raises if anything throws."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "page.js").write_text(page_script(), encoding="utf-8")
    (tmp_path / "boot.mjs").write_text(BOOT, encoding="utf-8")
    (tmp_path / "snap.json").write_text(snapshot_json, encoding="utf-8")
    done = subprocess.run(
        [
            "node",
            str(tmp_path / "boot.mjs"),
            str(tmp_path / "page.js"),
            str(tmp_path / "snap.json"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace=tmp_path / "workspace",
        default_profiles=["short"],
        evals_dir=tmp_path / "evals",
    )


@pytest.fixture
def a_video_with_a_run(settings: Settings) -> None:
    """A delivered video whose agent session carries clips - what broke the page."""
    state = State(settings.state_db)
    state.upsert_video(
        SourceInfo(video_id=VIDEO_ID, title="Video", channel="Canal", duration=600.0, url="u")
    )
    clip = Clip(index=1, slug="corte", profile="short", segments=[TimeRange(start=10.0, end=55.0)])
    profile_dir = settings.profile_dir(VIDEO_ID, "short")
    profile_dir.mkdir(parents=True)
    (profile_dir / "clips.json").write_text(
        ClipSet(video_id=VIDEO_ID, profile="short", clips=[clip]).model_dump_json(),
        encoding="utf-8",
    )
    clip_dir = profile_dir / "clips" / clip.dir_name
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip.mp4").write_bytes(b"0" * 2048)
    for stage in ("ingest", "transcribe"):
        state.set_stage(VIDEO_ID, stage, StageStatus.DONE)
    for stage in ("select", "review", "render", "package", "deliver"):
        state.set_stage(VIDEO_ID, stage, StageStatus.DONE, profile="short")

    run_id = "20260911-120000-vid00000"
    agent_dir = settings.agent_dir(VIDEO_ID)
    agent_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "run_start", "run_id": run_id, "seq": 1, "video_id": VIDEO_ID, "url": "u"},
        {
            "type": "tool_call",
            "run_id": run_id,
            "seq": 2,
            "tool": "render_clip",
            "input": {"video_id": VIDEO_ID, "profile": "short", "index": 1},
            # The clip of the session is read out of the answer, not the call.
            "output": json.dumps({"clip_id": f"{VIDEO_ID}/short/1", "duration_s": 45.0}),
        },
        {"type": "run_end", "run_id": run_id, "seq": 3, "status": "done", "reason": ""},
    ]
    (agent_dir / f"{run_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


@needs_node
def test_the_page_draws_every_section_of_a_real_snapshot(
    tmp_path: Path, settings: Settings, a_video_with_a_run: None
) -> None:
    from clipper.monitor.agent import RunStore

    snapshot = build_snapshot(settings, runs=RunStore())
    session = snapshot.videos[0].agent
    # The clips are the point: the page only read the broken name while drawing them.
    assert session is not None and session.clips, "a sessao precisa chegar com cortes"

    drawn = draw(tmp_path / "run", snapshot.model_dump_json())

    # Everything render() touches, in the order it touches them: a throw in one
    # takes out all the rest, which is exactly how this went unnoticed.
    for section in ("stops", "videos", "grid"):
        assert drawn["filled"].get(section), f"'{section}' nao foi desenhado"


@needs_node
def test_the_marks_section_draws_when_a_round_exists(tmp_path: Path, settings: Settings) -> None:
    from clipper.monitor.models import EvalClipView, EvalRoundView

    snapshot = build_snapshot(settings)
    snapshot = snapshot.model_copy(
        update={
            "evals": [
                EvalRoundView(
                    name="rodada1",
                    path="evals/rodada1/notas.md",
                    clips=[
                        EvalClipView(
                            clip_id=f"{VIDEO_ID}/short/1",
                            video_id=VIDEO_ID,
                            profile="short",
                            index=1,
                            title="um corte",
                            dir_name="01_corte",
                            duration=45.0,
                            playable=True,
                        )
                    ],
                )
            ]
        }
    )

    drawn = draw(tmp_path / "marks", snapshot.model_dump_json())

    assert drawn["hidden"]["evals"] is False
    assert drawn["filled"].get("ev-list")
    assert drawn["filled"].get("ev-tabs")


@needs_node
def test_an_empty_workspace_still_draws(tmp_path: Path, settings: Settings) -> None:
    """The first thing anyone sees; it must not depend on there being data."""
    drawn = draw(tmp_path / "empty", build_snapshot(settings).model_dump_json())

    assert drawn["hidden"]["evals"] is True
    assert drawn["filled"].get("videos")
