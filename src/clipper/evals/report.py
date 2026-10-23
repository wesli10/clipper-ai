"""The eval's two documents (SPEC-agent 11.3).

``report.md`` is what the machine measured; ``notas.md`` is the column only you
can fill. They are separate files because the first is rewritten on every run and
the second must survive that - your mark on a clip is the whole point of the
comparison, and the overlap number cannot stand in for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from clipper.evals.models import EvalReport, VideoComparison
from clipper.utils.errors import InputError
from clipper.utils.text import format_timestamp

NOTES_NAME = "notas.md"
REPORT_NAME = "report.md"

NOTES_HEADER = """# Notas desta rodada

Preencha a coluna **nota** de 1 a 5 e salve. `clipper eval report` le este arquivo de
volta e poe a media ao lado do que foi medido. Linhas em branco sao ignoradas.

| corte | titulo | nota | comentario |
|---|---|---|---|
"""

_ROW = re.compile(r"^\|([^|]+)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$")
#: A title cell may carry a markdown link; the page wants the words, not the link.
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One line of ``notas.md`` as the page shows it back to you."""

    clip_id: str
    title: str
    note: int = 0
    comment: str = ""


def read_rows(path: Path) -> list[NoteRow]:
    """Every clip of the round, marked or not, in the order they were written."""
    if not path.exists():
        return []
    rows: list[NoteRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        found = _ROW.match(line.strip())
        if found is None:
            continue
        clip_id = found.group(1).strip().strip("`")
        if "/" not in clip_id:
            continue
        raw = found.group(3).strip()
        title = _LINK.sub(r"\1", found.group(2)).strip()
        rows.append(
            NoteRow(
                clip_id=clip_id,
                title=title,
                note=int(raw) if raw.isdigit() and 1 <= int(raw) <= 5 else 0,
                comment=found.group(4).strip(),
            )
        )
    return rows


def update_rows(path: Path, marks: dict[str, tuple[int, str]]) -> int:
    """Write marks into the two columns that are yours, and touch nothing else.

    The file is the record, not a rendering of one: whatever you put in the title
    cell - a link to play the clip, a note to yourself - has to survive the page
    saving over it. So the rewrite is per line and per column, never a regenerated
    table. A mark of zero clears the row.
    """
    if not path.exists():
        raise InputError(f"esta rodada nao tem {NOTES_NAME}: {path}")
    written = 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        found = _ROW.match(line.strip())
        if found is None:
            continue
        clip_id = found.group(1).strip().strip("`")
        if clip_id not in marks:
            continue
        note, comment = marks[clip_id]
        lines[index] = (
            f"|{found.group(1)}|{found.group(2)}"
            f"| {note if note else ' '} | {_cell(comment)} |"
        )
        written += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def _cell(text: str) -> str:
    """A comment that cannot break the table it lives in."""
    flat = " ".join(text.split())
    return flat.replace("|", "/") or " "


def write_notes(path: Path, videos: list[VideoComparison]) -> Path:
    """One row per clip the agent produced, with the mark left blank."""
    rows = [
        f"| `{match.clip_id}` | {match.title or '-'} |  |  |"
        for video in videos
        for match in video.matches
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTES_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def read_notes(path: Path) -> dict[str, int]:
    """The marks you filled in, by clip id."""
    if not path.exists():
        return {}
    marks: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        found = _ROW.match(line.strip())
        if found is None:
            continue
        clip_id = found.group(1).strip().strip("`")
        raw = found.group(3).strip()
        if not raw.isdigit():
            continue
        value = int(raw)
        if 1 <= value <= 5:
            marks[clip_id] = value
    return marks


def render(report: EvalReport) -> str:
    """The comparison, video by video, with the totals on top."""
    comparable = report.comparable
    without_run = [v for v in report.videos if not v.matches]
    tainted = [v for v in report.videos if v.self_baseline]
    lines = [
        "# Eval do agent",
        "",
        f"- gerado em: {report.built_at}",
        f"- videos no baseline: {len(report.videos)}",
        (
            f"- sobreposicao media: {report.mean_overlap:.0%} "
            f"(sobre {len(comparable)} video(s) comparaveis)"
            if comparable
            else "- sobreposicao media: nenhum video comparavel ainda"
        ),
    ]
    if without_run:
        lines.append(f"- sem run do agent: {len(without_run)} video(s)")
    if tainted:
        lines.append(
            f"- baseline do proprio agent em {len(tainted)} video(s): ja foram publicados "
            "por ele, entao a sobreposicao ali compara o agent com ele mesmo"
        )
    if report.notes:
        lines.append(f"- sua nota media: {report.mean_note:.1f} ({len(report.notes)} cortes)")
    else:
        lines.append(f"- sua nota media: preencha {NOTES_NAME}")

    total_cost = sum(v.cost_usd for v in report.videos)
    total_time = sum(v.elapsed_s for v in report.videos)
    lines += [
        f"- custo estimado total: US$ {total_cost:.2f}",
        f"- tempo total de loop: {format_timestamp(total_time)}",
        "",
        "| video | baseline | agent | pareados | sobreposicao | renders/corte | fails | US$ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for video in report.videos:
        lines.append(
            f"| {video.title or video.video_id} | {video.baseline_clips} | {video.agent_clips} "
            f"| {video.matched} | {video.mean_overlap:.0%} | {video.renders_per_clip} "
            f"| {video.fails} | {video.cost_usd:.2f} |"
        )

    for video in report.videos:
        lines += ["", f"## {video.title or video.video_id}", ""]
        note = ""
        if video.self_baseline:
            note = " O baseline deste video veio do proprio agent: a sobreposicao nao mede nada."
        elif not video.matches:
            note = " O agent ainda nao rodou neste video."
        lines.append(f"Run `{video.run_id or '-'}` ({video.state}).{note}")
        header = "| corte do agent | par no baseline | sobreposicao | renders | laudo | nota |"
        lines += ["", header, "|---|---|---:|---:|---|---:|"]
        for match in video.matches:
            mark = report.notes.get(match.clip_id)
            lines.append(
                f"| {match.title or match.clip_id} ({match.duration_s:.0f}s) "
                f"| {match.baseline_title or '(sem par)'} | {match.overlap:.0%} "
                f"| {match.renders} | {match.verdict or '-'} | {mark or ''} |"
            )
        if video.missed:
            lines += ["", "Do baseline, o agent nao encontrou:"]
            lines += [f"- {title}" for title in video.missed]
    return "\n".join(lines) + "\n"


def build(directory: Path, videos: list[VideoComparison]) -> EvalReport:
    """Assemble the report, picking up any marks already written."""
    report = EvalReport(
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        videos=videos,
        notes=read_notes(directory / NOTES_NAME),
    )
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / NOTES_NAME).exists():
        write_notes(directory / NOTES_NAME, videos)
    (directory / REPORT_NAME).write_text(render(report), encoding="utf-8")
    return report
