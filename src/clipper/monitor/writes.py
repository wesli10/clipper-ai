"""What the page is allowed to write (SPEC-agent 9.5).

The monitor used to be read-only but for the review handshake. It now writes five
things, and this module is every one of them: the review decision stays where it
was, and the queue, the channels, a running session's controls and the marks of an
eval round come through here. Each has its own pydantic gate, writes exactly one
known file, and nothing takes a path from the request.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from clipper.agent.channel import ChannelProfile, channel_path, learned_path, load_learned
from clipper.agent.control import SessionControl, control_path, read_control, write_control
from clipper.config import Settings
from clipper.utils.errors import InputError

#: A channel id is a filename; keep it to what a filename should be.
CHANNEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
#: The header of `fila.txt`, kept when the page rewrites the list.
QUEUE_HEADER = (
    "# Fila do clipper. Uma URL por linha; '#' e linhas vazias sao ignoradas.\n"
    "# Pode acrescentar URLs com a fila rodando - o arquivo e relido a cada video.\n"
)


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #


class QueueEdit(BaseModel):
    """The queue as the page wants it, in order."""

    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(default_factory=list, max_length=200)


def validate_urls(urls: list[str]) -> list[str]:
    """Every line must be a URL some source recognises."""
    from clipper.stages.ingest import pick_source

    cleaned: list[str] = []
    for raw in urls:
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        pick_source(url)  # raises InputError with the reason
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def read_queue(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def write_queue(path: Path, edit: QueueEdit) -> list[str]:
    """Rewrite the queue, keeping whatever comments headed the file.

    The header explains what the file is; losing it because somebody reordered
    two lines from a web page would be a poor trade.
    """
    urls = validate_urls(edit.urls)
    header = QUEUE_HEADER
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()
        leading = []
        for line in existing:
            if line.strip() and not line.strip().startswith("#"):
                break
            leading.append(line)
        if any(line.strip() for line in leading):
            header = "\n".join(leading).rstrip("\n") + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    return urls


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #


class ChannelEdit(BaseModel):
    """A channel as the form sends it - the fields, never raw YAML."""

    model_config = ConfigDict(extra="forbid")

    profile: ChannelProfile


def channel_view(settings: Settings, channel_id: str) -> dict[str, Any]:
    """Everything the editor needs: the configuration and what was learned."""
    from clipper.agent.channel import load_profile

    profile = load_profile(settings, channel_id)
    learned = load_learned(settings, channel_id)
    return {
        "profile": profile.model_dump(mode="json"),
        "learned": learned.model_dump(mode="json"),
        "path": str(channel_path(settings, channel_id)),
        "learned_path": str(learned_path(settings, channel_id)),
    }


def save_channel(settings: Settings, channel_id: str, edit: ChannelEdit) -> Path:
    """Write ``channels/<id>.yaml`` from validated fields.

    Only the fields go through - never a blob of YAML from the browser - so the
    file cannot end up holding something a channel is not. The cost is that hand
    written comments do not survive an edit from the page, and the header says so.
    """
    if CHANNEL_ID_RE.fullmatch(channel_id) is None:
        raise InputError(
            f"id de canal invalido: '{channel_id}' (minusculas, numeros, - e _, ate 40)"
        )
    profile = edit.profile.model_copy(update={"id": channel_id})
    for name in profile.render_defaults:
        # The same whitelist the agent is held to; a channel cannot reach further.
        from clipper.agent.models import ProfileOverride

        ProfileOverride.model_validate(profile.render_defaults[name])

    path = channel_path(settings, channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        profile.model_dump(mode="json", exclude_defaults=False),
        allow_unicode=True,
        sort_keys=False,
    )
    path.write_text(
        "# Canal de publicacao (SPEC-agent 7). Escrito pela pagina do monitor:\n"
        "# comentarios que voce acrescentar aqui a mao somem na proxima edicao pela\n"
        "# pagina. O agent le este arquivo e nunca escreve nele.\n" + body,
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# The marks of an eval round (SPEC-agent 11.3)
# --------------------------------------------------------------------------- #


class EvalMark(BaseModel):
    """One clip's mark. Zero clears it, which is how you take a mark back."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(min_length=3, max_length=120)
    note: int = Field(default=0, ge=0, le=5)
    comment: str = Field(default="", max_length=300)


class EvalEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round: str = Field(min_length=1, max_length=60)
    marks: list[EvalMark] = Field(default_factory=list, max_length=200)


def rounds_with_notes(settings: Settings) -> list[str]:
    """Names of the rounds that exist and have a notes file to write into."""
    from clipper.evals.report import NOTES_NAME

    root = settings.evals_dir.expanduser()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / NOTES_NAME).is_file())


def save_marks(settings: Settings, edit: EvalEdit) -> tuple[Path, int]:
    """Write the marks into that round's `notas.md`, and only into its two columns.

    The round is not a path from the request: it is matched against the rounds
    that already exist on disk, so the name can only ever select one of them.
    """
    from clipper.evals.report import NOTES_NAME, update_rows

    known = rounds_with_notes(settings)
    if edit.round not in known:
        raise InputError(
            f"rodada desconhecida: '{edit.round}' "
            f"(existem: {', '.join(known) if known else 'nenhuma'})"
        )
    path = settings.evals_dir.expanduser() / edit.round / NOTES_NAME
    marks = {mark.clip_id: (mark.note, mark.comment) for mark in edit.marks}
    written = update_rows(path, marks)
    if written != len(marks):
        raise InputError(
            f"{len(marks) - written} corte(s) da sua lista nao estao em {edit.round}/{NOTES_NAME}"
        )
    return path, written


# --------------------------------------------------------------------------- #
# A running session
# --------------------------------------------------------------------------- #


class ControlEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    run_id: str
    action: str = "run"
    vetoed: list[str] = Field(default_factory=list, max_length=50)
    publish_on_stop: bool = True


def save_control(settings: Settings, edit: ControlEdit) -> SessionControl:
    """Write the order for one run, refusing to invent a run that is not there."""
    agent_dir = settings.agent_dir(edit.video_id)
    if not (agent_dir / f"{edit.run_id}.jsonl").is_file():
        raise InputError(f"run '{edit.run_id}' nao existe para o video '{edit.video_id}'")
    if edit.action not in ("run", "pause", "stop"):
        raise InputError(f"acao desconhecida: '{edit.action}' (use run, pause ou stop)")

    path = control_path(agent_dir, edit.run_id)
    current = read_control(path)
    return write_control(
        path,
        SessionControl(
            action=edit.action,  # type: ignore[arg-type]
            # Vetoes accumulate: un-vetoing a clip is not something a run can undo
            # halfway through, and losing one silently would be worse.
            vetoed=sorted({*current.vetoed, *edit.vetoed}),
            publish_on_stop=edit.publish_on_stop,
        ),
    )
