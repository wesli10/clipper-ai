"""A tiny read-only HTTP server for the monitor page.

Standard library only: the monitor must run next to a pipeline that is already
working, without adding dependencies or touching its files.
"""

from __future__ import annotations

import json
import re
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

from clipper.config import Settings
from clipper.models import ClipStatus, ReviewRequest, ReviewResponse
from clipper.monitor.snapshot import SnapshotSource
from clipper.stages.review import REQUEST_NAME as REVIEW_REQUEST_NAME
from clipper.stages.review import RESPONSE_NAME as REVIEW_RESPONSE_NAME
from clipper.utils.console import console, info
from clipper.utils.errors import ClipperError

STATIC_DIR = Path(__file__).parent / "static"
#: How often a connected page is offered a fresh snapshot.
PUSH_INTERVAL_S = 1.0
#: Comment line keeping proxies and the browser from dropping an idle stream.
HEARTBEAT_S = 15.0
#: A review decision is a few hundred bytes; anything larger is not one.
MAX_BODY_BYTES = 256 * 1024
#: `<data>-<hora>-<video>`: the only shape a run id may have in a URL.
RUN_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[A-Za-z0-9_.-]{1,16}")


def _safe_name(value: str) -> str:
    """A single path component, or empty when the value tries to escape."""
    cleaned = value.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned.startswith("."):
        return ""
    return cleaned


class MonitorHandler(BaseHTTPRequestHandler):
    """Serves the page, the snapshot stream and the clip files."""

    source: ClassVar[SnapshotSource]
    protocol_version = "HTTP/1.1"
    server_version = "clipper-monitor"

    # -- routing ---------------------------------------------------------- #

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                self._send_bytes(
                    (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8"
                )
            elif route == "/api/snapshot":
                payload = self.source.get().model_dump_json().encode("utf-8")
                self._send_bytes(payload, "application/json; charset=utf-8", cache=False)
            elif route == "/api/events":
                self._stream_events()
            elif route == "/api/thumbnail":
                self._send_clip_file(query, "thumbnail.jpg", "image/jpeg")
            elif route == "/api/clip":
                self._send_clip_file(query, "clip.mp4", "video/mp4", ranged=True)
            elif route == "/api/source":
                self._send_source(query)
            elif route == "/api/agent/runs":
                self._send_agent_runs(query)
            elif route == "/api/agent":
                self._send_agent_run(query)
            elif route == "/api/agent/clip":
                self._send_agent_clip(query)
            elif route == "/api/channels":
                self._send_channels(query)
            elif route == "/api/queue":
                self._send_queue()
            else:
                self._send_bytes(b"nao encontrado", "text/plain; charset=utf-8", status=404)
        except (BrokenPipeError, ConnectionResetError):
            return

    # -- endpoints -------------------------------------------------------- #

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last = ""
        last_beat = time.monotonic()
        while True:
            snapshot = self.source.get()
            # `generated_at` moves on every rebuild; only real changes are pushed.
            fingerprint = snapshot.model_dump_json(exclude={"generated_at"})
            now = time.monotonic()
            if fingerprint != last:
                body = snapshot.model_dump_json()
                self.wfile.write(f"data: {body}\n\n".encode())
                self.wfile.flush()
                last = fingerprint
                last_beat = now
            elif now - last_beat > HEARTBEAT_S:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_beat = now
            time.sleep(PUSH_INTERVAL_S)

    #: Every write the page may do, and the file each one lands in. Nothing here
    #: takes a path from the request (CLAUDE.md).
    WRITE_ROUTES: ClassVar[tuple[str, ...]] = (
        "/api/review",
        "/api/queue",
        "/api/channel",
        "/api/agent/control",
        "/api/evals",
    )

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in self.WRITE_ROUTES:
            self._send_bytes(b"nao encontrado", "text/plain; charset=utf-8", status=404)
            return
        body = self._read_body()
        if body is None:
            return
        try:
            if route == "/api/review":
                self._save_review(ReviewResponse.model_validate_json(body))
            elif route == "/api/queue":
                self._save_queue(body)
            elif route == "/api/channel":
                self._save_channel(body)
            elif route == "/api/evals":
                self._save_marks(body)
            else:
                self._save_control(body)
        except (ClipperError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "corpo invalido"}, status=400)
            return None
        return self.rfile.read(length)

    # -- everything the page may write (SPEC-agent 9.5, 11.3) --------------- #

    def _save_queue(self, body: bytes) -> None:
        from clipper.monitor.writes import QueueEdit, write_queue

        path = self.source.queue_path
        if path is None:
            self._send_json({"ok": False, "error": "esta sessao nao tem fila"}, status=400)
            return
        urls = write_queue(path, QueueEdit.model_validate_json(body))
        self.source.invalidate()
        info(f"fila atualizada pela pagina: {len(urls)} video(s)")
        self._send_json({"ok": True, "urls": urls})

    def _save_channel(self, body: bytes) -> None:
        from clipper.monitor.writes import ChannelEdit, save_channel

        edit = ChannelEdit.model_validate_json(body)
        path = save_channel(self.source.settings, edit.profile.id, edit)
        self.source.invalidate()
        info(f"canal '{edit.profile.id}' salvo pela pagina")
        self._send_json({"ok": True, "path": str(path)})

    def _save_marks(self, body: bytes) -> None:
        from clipper.monitor.writes import EvalEdit, save_marks

        edit = EvalEdit.model_validate_json(body)
        path, written = save_marks(self.source.settings, edit)
        self.source.invalidate()
        info(f"eval '{edit.round}': {written} nota(s) pela pagina")
        self._send_json({"ok": True, "path": str(path), "written": written})

    def _save_control(self, body: bytes) -> None:
        from clipper.monitor.writes import ControlEdit, save_control

        edit = ControlEdit.model_validate_json(body)
        control = save_control(self.source.settings, edit)
        self.source.invalidate()
        info(f"run {edit.run_id}: {control.action} pedido pela pagina")
        self._send_json({"ok": True, "control": control.model_dump(mode="json")})

    def _send_channels(self, query: dict[str, list[str]]) -> None:
        from clipper.agent.channel import available
        from clipper.monitor.writes import channel_view

        settings = self.source.settings
        wanted = _safe_name((query.get("id") or [""])[0])
        if wanted:
            self._send_bytes(
                json.dumps(channel_view(settings, wanted)).encode("utf-8"),
                "application/json; charset=utf-8",
                cache=False,
            )
            return
        self._send_bytes(
            json.dumps({"channels": available(settings)}).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _send_queue(self) -> None:
        from clipper.monitor.writes import read_queue

        path = self.source.queue_path
        urls = read_queue(path) if path is not None else []
        self._send_bytes(
            json.dumps({"path": str(path or ""), "urls": urls}).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _save_review(self, answer: ReviewResponse) -> None:
        video_id = _safe_name(answer.video_id)
        profile = _safe_name(answer.profile)
        if not (video_id and profile):
            self._send_json({"ok": False, "error": "video ou perfil invalido"}, status=400)
            return

        request_dir = self.source.settings.profile_dir(video_id, profile)
        request_path = request_dir / REVIEW_REQUEST_NAME
        if not request_path.exists():
            # The pipeline gave up, or someone else answered first.
            self._send_json(
                {"ok": False, "error": "essa revisao nao esta mais pendente"}, status=409
            )
            return
        try:
            request = ReviewRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=409)
            return

        known = {clip.index for clip in request.clips}
        unknown = [d.index for d in answer.decisions if d.index not in known]
        if unknown:
            self._send_json({"ok": False, "error": f"cortes inexistentes: {unknown}"}, status=400)
            return
        if any(d.status is ClipStatus.PENDING for d in answer.decisions):
            self._send_json({"ok": False, "error": "decida aprovar ou rejeitar"}, status=400)
            return

        decided = answer.model_copy(
            update={"video_id": video_id, "profile": profile, "decided_at": time.time()}
        )
        # One atomic write: the pipeline polls this file and must never read half.
        temporary = request_dir / (REVIEW_RESPONSE_NAME + ".tmp")
        temporary.write_text(decided.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(request_dir / REVIEW_RESPONSE_NAME)
        approved = sum(1 for d in decided.decisions if d.status is ClipStatus.APPROVED)
        self._send_json({"ok": True, "approved": approved, "total": len(decided.decisions)})

    def _send_source(self, query: dict[str, list[str]]) -> None:
        """The downloaded video, so the review page can play the chosen ranges."""
        video_id = _safe_name(query.get("video", [""])[0])
        if not video_id:
            self._send_bytes(b"parametros invalidos", "text/plain; charset=utf-8", status=400)
            return
        path = self.source.settings.video_dir(video_id) / "source.mp4"
        if not path.exists():
            self._send_bytes(
                b"video nao esta no workspace", "text/plain; charset=utf-8", status=404
            )
            return
        self._send_ranged(path, "video/mp4")

    # -- the agent session (SPEC-agent 9.3) -------------------------------- #

    def _agent_dir(self, query: dict[str, list[str]]) -> Path | None:
        video_id = _safe_name((query.get("video_id") or [""])[0])
        if not video_id:
            return None
        return self.source.settings.agent_dir(video_id)

    def _run_path(self, query: dict[str, list[str]]) -> Path | None:
        directory = self._agent_dir(query)
        run_id = (query.get("run_id") or [""])[0].strip()
        if directory is None or not RUN_ID_RE.fullmatch(run_id):
            return None
        path = directory / f"{run_id}.jsonl"
        return path if path.is_file() else None

    def _send_agent_runs(self, query: dict[str, list[str]]) -> None:
        directory = self._agent_dir(query)
        if directory is None:
            self._send_json({"ok": False, "error": "video_id invalido"}, status=400)
            return
        runs = self.source.runs.listing(directory)
        self._send_bytes(
            json.dumps({"runs": [run.model_dump(mode="json") for run in runs]}).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _send_agent_run(self, query: dict[str, list[str]]) -> None:
        path = self._run_path(query)
        if path is None:
            self._send_json({"ok": False, "error": "run nao encontrada"}, status=404)
            return
        run = self.source.runs.read(path)
        try:
            from_seq = int((query.get("from_seq") or ["0"])[0])
        except ValueError:
            from_seq = 0
        payload = run.model_dump(mode="json")
        payload["trail"] = [step for step in payload["trail"] if step["seq"] > from_seq]
        self._send_bytes(
            json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", cache=False
        )

    def _send_agent_clip(self, query: dict[str, list[str]]) -> None:
        path = self._run_path(query)
        clip_id = (query.get("clip_id") or [""])[0].strip()
        if path is None or not clip_id:
            self._send_json({"ok": False, "error": "run ou corte invalido"}, status=404)
            return
        run = self.source.runs.read(path)
        clip = next((item for item in run.clips if item.clip_id == clip_id), None)
        if clip is None:
            self._send_json({"ok": False, "error": "corte nao encontrado nesta run"}, status=404)
            return
        steps = [
            step.model_dump(mode="json")
            for step in run.trail
            if clip_id in step.summary or clip_id in step.detail
        ]
        self._send_bytes(
            json.dumps({"clip": clip.model_dump(mode="json"), "steps": steps}).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status, cache=False)

    def _send_clip_file(
        self, query: dict[str, list[str]], name: str, content_type: str, *, ranged: bool = False
    ) -> None:
        video_id = _safe_name(query.get("video", [""])[0])
        profile = _safe_name(query.get("profile", [""])[0])
        clip = _safe_name(query.get("clip", [""])[0])
        if not (video_id and profile and clip):
            self._send_bytes(b"parametros invalidos", "text/plain; charset=utf-8", status=400)
            return

        settings = self.source.settings
        path = settings.profile_dir(video_id, profile) / "clips" / clip / name
        if not path.exists() and name == "thumbnail.jpg":
            path = self.source.history.thumbnail_path(video_id, profile, clip)
        if not path.exists():
            self._send_bytes(b"arquivo ainda nao existe", "text/plain; charset=utf-8", status=404)
            return
        if ranged:
            self._send_ranged(path, content_type)
        else:
            self._send_bytes(path.read_bytes(), content_type, cache=False)

    # -- transport -------------------------------------------------------- #

    def _send_bytes(
        self, body: bytes, content_type: str, *, status: int = 200, cache: bool = True
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_ranged(self, path: Path, content_type: str) -> None:
        """Serve a file with byte ranges, so the browser can seek inside a clip."""
        size = path.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        partial = header.startswith("bytes=")
        if partial:
            raw = header.removeprefix("bytes=").split("-", 1)
            try:
                start = int(raw[0]) if raw[0] else 0
                if len(raw) > 1 and raw[1]:
                    end = min(int(raw[1]), size - 1)
            except ValueError:
                partial = False
        if start >= size:
            self._send_bytes(b"", content_type, status=416)
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format: str, *args: object) -> None:
        """Quiet by default: the page polls constantly and the log is noise."""
        return


def build_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    queue_path: Path | None = None,
) -> ThreadingHTTPServer:
    """The configured server, not yet serving - port 0 picks a free one."""
    source = SnapshotSource(settings, queue_path=queue_path)
    handler = type("BoundMonitorHandler", (MonitorHandler,), {"source": source})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    queue_path: Path | None = None,
    open_browser: bool = False,
) -> None:
    """Run the monitor until interrupted."""
    server = build_server(settings, host=host, port=port, queue_path=queue_path)

    url = f"http://{host}:{port}/"
    console.print(f"[bold green]monitor no ar:[/bold green] {url}")
    if queue_path:
        console.print(f"[dim]fila: {queue_path}[/dim]")
    console.print("[dim]Ctrl+C para encerrar[/dim]")
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("monitor encerrado")
    finally:
        server.server_close()


def snapshot_json(settings: Settings, queue_path: Path | None = None) -> str:
    """One snapshot as JSON - handy for debugging without a browser."""
    source = SnapshotSource(settings, queue_path=queue_path)
    return json.dumps(json.loads(source.get(max_age=0.0).model_dump_json()), ensure_ascii=False)
