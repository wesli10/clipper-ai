"""Stage 3a - human review of the selected clips (SPEC 7.1).

Shows the candidates in a table and lets the user approve, reject, edit an
interval, drop an interval of a multi-segment clip, or preview it. Any edit
re-runs the boundary snapping, so a hand-typed time is snapped just like the
LLM's (SPEC 7.1).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from rich.prompt import Prompt
from rich.table import Table

from clipper.models import (
    Clip,
    ClipSet,
    ClipStatus,
    ReviewClipView,
    ReviewRequest,
    ReviewResponse,
    TimeRange,
    Transcript,
)
from clipper.profiles import Profile
from clipper.stages.select import merge_intervals, snap_range
from clipper.utils.console import console, error, info, warn
from clipper.utils.errors import InputError
from clipper.utils.text import format_timestamp, parse_timecode

#: Handed to the monitor; answered by the review page (same shape as the `file`
#: LLM provider: one file out, one file in, nobody holding a socket open).
REQUEST_NAME = "review_request.json"
RESPONSE_NAME = "review_response.json"
#: How often the pipeline looks for the answer while it waits.
POLL_INTERVAL_S = 1.5
#: Transcript excerpt carried per clip - enough to judge, not the whole talk.
EXCERPT_CHARS = 1500

HELP = (
    "[bold]a[/bold] aprovar  [bold]r[/bold] rejeitar  [bold]e[/bold] editar intervalo  "
    "[bold]d[/bold] remover intervalo  [bold]p[/bold] preview  "
    "[bold]s[/bold] salvar e sair  [bold]q[/bold] sair sem salvar"
)


def render_table(clip_set: ClipSet, profile: Profile) -> Table:
    table = Table(title=f"{clip_set.video_id} - perfil {profile.name}")
    table.add_column("#", justify="right")
    table.add_column("status")
    table.add_column("intervalos")
    table.add_column("dur", justify="right")
    table.add_column("score", justify="right")
    table.add_column("tema")
    table.add_column("gancho")

    colors = {"approved": "green", "rejected": "red", "pending": "yellow"}
    for clip in clip_set.clips:
        ranges = "\n".join(
            f"{i + 1}. {format_timestamp(r.start)}-{format_timestamp(r.end)} ({r.duration:.0f}s)"
            for i, r in enumerate(clip.segments)
        )
        color = colors.get(str(clip.status), "white")
        table.add_row(
            str(clip.index),
            f"[{color}]{clip.status}[/{color}]",
            ranges,
            f"{clip.duration:.0f}s",
            f"{clip.score:.0f}",
            clip.topic,
            (clip.hook[:60] + "…") if len(clip.hook) > 60 else clip.hook,
        )
    return table


def auto_approve(clip_set: ClipSet, profile: Profile) -> ClipSet:
    """Approve the ``n_clips`` best and reject the rest (``--auto``, SPEC 7.1)."""
    ranked = sorted(clip_set.clips, key=lambda c: c.score, reverse=True)
    keep = {clip.index for clip in ranked[: profile.n_clips]}
    for clip in clip_set.clips:
        clip.status = ClipStatus.APPROVED if clip.index in keep else ClipStatus.REJECTED
    return clip_set


def player() -> str | None:
    for candidate in ("mpv", "ffplay"):
        if shutil.which(candidate):
            return candidate
    return None


#: Preview only has to be watchable - small and fast beats faithful.
PREVIEW_WIDTH = 640
PREVIEW_CRF = 32


def desktop_opener() -> str | None:
    """Name of a command that hands a file to the desktop's default player."""
    for candidate in ("wslview", "xdg-open", "open"):
        if shutil.which(candidate):
            return candidate
    explorer = shutil.which("explorer.exe") or "/mnt/c/Windows/explorer.exe"
    return explorer if Path(explorer).exists() else None


def open_with_desktop(path: Path, opener: str) -> None:
    """Open ``path`` with the system player.

    Under WSL the Windows side cannot read a Linux path, so it is converted with
    ``wslpath -w`` first. ``explorer.exe`` also exits 1 even when it worked, so
    its status says nothing and is ignored.
    """
    target = str(path)
    if opener.endswith("explorer.exe") and shutil.which("wslpath"):
        converted = subprocess.run(
            ["wslpath", "-w", str(path)], capture_output=True, text=True, check=False
        )
        if converted.returncode == 0 and converted.stdout.strip():
            target = converted.stdout.strip()
    subprocess.run([opener, target], check=False)


def extract_preview(source: Path, clip: Clip, dest: Path) -> Path:
    """Cut the clip's intervals into one small file, for a player that cannot seek.

    Scaled down and heavily compressed on purpose: this is for judging what is
    said and where the cut lands, and a full-size extract of a three-part clip ran
    to 77 MB.
    """
    from clipper.video import ffmpeg

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    args = ffmpeg.base_args()
    filters = []
    for index, interval in enumerate(clip.segments):
        args += ["-ss", f"{interval.start:.3f}", "-to", f"{interval.end:.3f}", "-i", str(source)]
        filters.append(
            f"[{index}:v]scale={PREVIEW_WIDTH}:-2,setsar=1[v{index}];[{index}:a]anull[a{index}]"
        )
    streams = "".join(f"[v{i}][a{i}]" for i in range(len(clip.segments)))
    graph = ";".join(filters) + f";{streams}concat=n={len(clip.segments)}:v=1:a=1[v][a]"

    out = dest / "preview.mp4"
    args += [
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-crf",
        str(PREVIEW_CRF),
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(out),
    ]
    ffmpeg.run(args, description="preview")
    return out


def preview(source: Path, clip: Clip) -> None:
    """Play each interval in sequence (SPEC 7.1)."""
    binary = player()
    if binary is not None:
        for interval in clip.segments:
            if binary == "mpv":
                command = [
                    "mpv",
                    f"--start={interval.start:.3f}",
                    f"--length={interval.duration:.3f}",
                    str(source),
                ]
            else:
                command = [
                    "ffplay",
                    "-autoexit",
                    "-ss",
                    f"{interval.start:.3f}",
                    "-t",
                    f"{interval.duration:.3f}",
                    str(source),
                ]
            subprocess.run(command, check=False)
        return

    opener = desktop_opener()
    if opener is None:
        warn(
            "sem mpv, ffplay ou um abridor do sistema; preview indisponivel. "
            "Instale mpv (apt install mpv) para o preview no terminal."
        )
        return

    info("[dim]extraindo o trecho e abrindo no player do sistema...[/dim]")
    path = extract_preview(source, clip, source.parent / "preview")
    open_with_desktop(path, opener)
    info(f"[dim]preview em {path}[/dim]")


def _find(clip_set: ClipSet, index: int) -> Clip | None:
    return next((c for c in clip_set.clips if c.index == index), None)


def edit_interval(
    clip: Clip, position: int, start: str, end: str, transcript: Transcript, profile: Profile
) -> None:
    """Replace one interval with hand-typed times, re-snapped (SPEC 7.1)."""
    if not 1 <= position <= len(clip.segments):
        raise ValueError(f"o corte {clip.index} nao tem intervalo {position}")
    raw = TimeRange(start=parse_timecode(start), end=parse_timecode(end))
    clip.segments[position - 1] = snap_range(transcript, raw, mode=profile.boundary_mode)
    # Step 4 of SPEC 6.1 in full: sort, then fuse intervals that ended up adjacent.
    merged = merge_intervals(clip.segments)
    if len(merged) < len(clip.segments):
        del clip.chapters[len(merged) :]
    clip.segments[:] = merged
    clip.offsets = []


def drop_interval(clip: Clip, position: int) -> None:
    if len(clip.segments) <= 1:
        raise ValueError("um corte precisa de pelo menos um intervalo")
    if not 1 <= position <= len(clip.segments):
        raise ValueError(f"o corte {clip.index} nao tem intervalo {position}")
    del clip.segments[position - 1]
    if position - 1 < len(clip.chapters):
        del clip.chapters[position - 1]
    clip.offsets = []


def apply_decisions(clip_set: ClipSet, approve: list[int], reject: list[int]) -> ClipSet:
    """Approve and reject by index, without a prompt.

    Lets the review run from a script or a non-interactive shell; anything not
    named keeps the status it had.
    """
    for index in approve:
        _require(clip_set, index).status = ClipStatus.APPROVED
    for index in reject:
        _require(clip_set, index).status = ClipStatus.REJECTED
    return clip_set


def build_request(
    clip_set: ClipSet,
    profile: Profile,
    transcript: Transcript,
    *,
    video_title: str = "",
    channel: str = "",
) -> ReviewRequest:
    """Everything the review page needs, so it never reads the transcript itself."""
    clips = [
        ReviewClipView(
            index=clip.index,
            topic=clip.topic,
            hook=clip.hook,
            reason=clip.reason,
            score=clip.score,
            duration=clip.duration,
            segments=list(clip.segments),
            text=_excerpt(transcript, clip),
            status=clip.status,
        )
        for clip in clip_set.clips
    ]
    return ReviewRequest(
        video_id=clip_set.video_id,
        profile=profile.name,
        video_title=video_title,
        channel=channel,
        n_clips=profile.n_clips,
        created_at=time.time(),
        clips=clips,
    )


def _excerpt(transcript: Transcript, clip: Clip) -> str:
    parts = [transcript.text_between(r.start, r.end) for r in clip.segments]
    text = " (...) ".join(p for p in parts if p)
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rstrip() + "…"


def apply_response(clip_set: ClipSet, response: ReviewResponse) -> ClipSet:
    """Write the decisions into the set; anything undecided is kept, like `s`."""
    decided = {d.index: d.status for d in response.decisions}
    for clip in clip_set.clips:
        status = decided.get(clip.index)
        if status is not None:
            clip.status = status
        elif clip.status is ClipStatus.PENDING:
            clip.status = ClipStatus.APPROVED
    return clip_set


def web_review(
    clip_set: ClipSet,
    profile: Profile,
    transcript: Transcript,
    request_dir: Path,
    *,
    video_title: str = "",
    channel: str = "",
    monitor_url: str = "",
    timeout_s: float = 3600.0,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> ClipSet:
    """Publish the candidates and wait for the monitor page to answer.

    The pipeline and the monitor are different processes, so the decision travels
    on disk: the request is written here and the page writes the response next to
    it. The request file is also what tells the monitor a review is pending.
    """
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / REQUEST_NAME
    response_path = request_dir / RESPONSE_NAME
    response_path.unlink(missing_ok=True)
    request = build_request(clip_set, profile, transcript, video_title=video_title, channel=channel)
    request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")

    where = monitor_url or "a pagina do monitor (clipper monitor)"
    console.print(
        f"\n[bold]Revisao pelo navegador[/bold]: {len(request.clips)} cortes de "
        f"{profile.name} esperando voce em\n  [bold]{where}[/bold]\n"
        "[dim]aguardando sua decisao... (ctrl+c para cancelar)[/dim]"
    )

    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            response = _read_response(response_path)
            if response is not None:
                reviewed = apply_response(clip_set, response)
                request_path.unlink(missing_ok=True)
                return reviewed
            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        request_path.unlink(missing_ok=True)
        raise
    request_path.unlink(missing_ok=True)
    raise InputError(
        f"ninguem revisou os cortes de {profile.name} em {timeout_s / 60:.0f} min. "
        "Rode de novo, ou usa --review auto para o Clipper escolher sozinho."
    )


def _read_response(path: Path) -> ReviewResponse | None:
    """The answer, once it is there and complete (it is written in one go)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return ReviewResponse.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def review(
    clip_set: ClipSet,
    profile: Profile,
    transcript: Transcript,
    source: Path,
    *,
    auto: bool = False,
    approve: list[int] | None = None,
    reject: list[int] | None = None,
) -> ClipSet | None:
    """Interactive loop. Returns the edited set, or ``None`` when the user quits."""
    if approve or reject:
        console.print(render_table(clip_set, profile))
        return apply_decisions(clip_set, approve or [], reject or [])
    if auto:
        return auto_approve(clip_set, profile)

    while True:
        console.print(render_table(clip_set, profile))
        console.print(HELP)
        try:
            answer = Prompt.ask("acao").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise InputError(
                "a revisao precisa de um terminal interativo.\n"
                "Rode 'clipper review' num terminal normal, ou decida sem "
                "interacao com --approve/--reject (por exemplo: "
                "clipper review <id> --profile short --approve 1,3)."
            ) from None
        if not answer:
            continue
        command, *rest = answer.split()

        try:
            if command == "q":
                return None
            if command == "s":
                for clip in clip_set.clips:
                    if clip.status is ClipStatus.PENDING:
                        clip.status = ClipStatus.APPROVED
                return clip_set
            if command in ("a", "r", "p"):
                targets = _targets(clip_set, rest)
                for clip in targets:
                    if command == "a":
                        clip.status = ClipStatus.APPROVED
                    elif command == "r":
                        clip.status = ClipStatus.REJECTED
                    else:
                        preview(source, clip)
                continue
            if command == "e":
                index, position, start, end = rest
                clip = _require(clip_set, int(index))
                edit_interval(clip, int(position), start, end, transcript, profile)
                info(f"corte {index} atualizado e reajustado as fronteiras")
                continue
            if command == "d":
                index, position = rest
                drop_interval(_require(clip_set, int(index)), int(position))
                continue
            error(f"acao desconhecida: {command}")
        except (ValueError, IndexError) as exc:
            error(str(exc))


def _targets(clip_set: ClipSet, rest: list[str]) -> list[Clip]:
    if not rest:
        raise ValueError("informe o numero do corte, por exemplo 'a 1' ou 'a 1 3'")
    return [_require(clip_set, int(token)) for token in rest]


def _require(clip_set: ClipSet, index: int) -> Clip:
    clip = _find(clip_set, index)
    if clip is None:
        raise ValueError(f"nao existe corte de indice {index}")
    return clip
