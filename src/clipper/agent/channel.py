"""The channel the clips are published to (SPEC-agent 7).

"Channel" here is *your* publishing channel, not the source video's. The word
``profile`` is already taken by format, so this is a ``Channel`` throughout.

Two halves that never mix. ``channels/<id>.yaml`` is configuration: written by
you, versioned, and the agent cannot touch it. ``<id>.learned.json`` in the
workspace is what the agent noticed while working, capped and dated - you can
delete it whole without losing anything of yours.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from clipper.config import Settings
from clipper.utils.errors import InputError

#: Notes one run may add, and how many are kept before the oldest fall off.
MAX_NOTES_PER_RUN = 5
MAX_NOTES = 50
MAX_NOTE_CHARS = 300

NoteKind = Literal["editorial", "render", "titulo", "outro"]
#: Whether the run saw this happen or merely inferred it. The agent has no
#: audience data, and must not write as if it had.
Confidence = Literal["observed", "assumed"]


class TitleStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: str = ""
    max_chars: int = Field(default=70, ge=20, le=100)
    avoid: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    #: Tags that go on everything this channel publishes. Guaranteed by the code
    #: rather than asked of the prompt: a channel tag the model forgets once is a
    #: clip that falls out of the channel's own search.
    hashtags: list[str] = Field(default_factory=list, max_length=3)


class EditorialStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class ChannelProfile(BaseModel):
    """``channels/<id>.yaml`` - yours, and only yours."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    language: str = ""
    #: Formats this channel publishes. What is not here does not exist for the
    #: agent (SPEC-agent 2.1) - it is how `long` stays off without being deleted.
    profiles: list[str] = Field(default_factory=list)
    weekly_target: dict[str, int] = Field(default_factory=dict)
    title: TitleStyle = Field(default_factory=TitleStyle)
    editorial: EditorialStyle = Field(default_factory=EditorialStyle)
    #: Per-profile overrides applied to every run of this channel, from the same
    #: whitelist the agent may use. Anything else belongs in `config.yaml`.
    render_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)


class LearnedNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(max_length=MAX_NOTE_CHARS)
    kind: NoteKind = "outro"
    confidence: Confidence = "observed"
    run_id: str = ""
    video_id: str = ""
    at: str = ""


class LearnedNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_at: str = ""
    entries: list[LearnedNote] = Field(default_factory=list)


class Channel(BaseModel):
    """What a run works with: the configuration plus what was learned."""

    model_config = ConfigDict(extra="forbid")

    profile: ChannelProfile
    learned: LearnedNotes = Field(default_factory=LearnedNotes)

    @property
    def id(self) -> str:
        return self.profile.id

    def overrides_for(self, profile_name: str) -> dict[str, Any]:
        return dict(self.profile.render_defaults.get(profile_name) or {})


# --------------------------------------------------------------------------- #
# Where things live
# --------------------------------------------------------------------------- #


def channel_path(settings: Settings, channel_id: str) -> Path:
    return settings.channels_dir / f"{channel_id}.yaml"


def learned_path(settings: Settings, channel_id: str) -> Path:
    return settings.workspace.expanduser() / "channels" / f"{channel_id}.learned.json"


def available(settings: Settings) -> list[str]:
    directory = settings.channels_dir
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.yaml"))


def load_profile(settings: Settings, channel_id: str) -> ChannelProfile:
    path = channel_path(settings, channel_id)
    if not path.exists():
        known = ", ".join(available(settings)) or "nenhum"
        raise InputError(f"canal '{channel_id}' nao encontrado em {path} (existem: {known})")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise InputError(f"{path}: o arquivo deve conter um mapa no nivel raiz")
    declared = raw.get("id")
    if declared and declared != channel_id:
        # The filename is what `--channel` addresses and what the notes file is
        # named after; a different id inside would split the two silently.
        raise InputError(
            f"{path}: o campo 'id' diz '{declared}' mas o arquivo se chama "
            f"'{channel_id}.yaml'. Use o mesmo nos dois."
        )
    raw["id"] = channel_id
    try:
        return ChannelProfile.model_validate(raw)
    except ValueError as exc:
        raise InputError(f"canal '{channel_id}' invalido ({path}): {exc}") from exc


def load_learned(settings: Settings, channel_id: str) -> LearnedNotes:
    path = learned_path(settings, channel_id)
    if not path.exists():
        return LearnedNotes()
    try:
        return LearnedNotes.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        # Corrupt notes are the agent's own scratch pad: start a new one rather
        # than fail a run over it.
        return LearnedNotes()


def resolve(settings: Settings, channel_id: str | None = None) -> Channel | None:
    """The channel of this run, or ``None`` when there is none to use.

    With one channel configured it is used without being named - asking for
    ``--channel`` when there is only one answer is ceremony.
    """
    if channel_id:
        return Channel(
            profile=load_profile(settings, channel_id),
            learned=load_learned(settings, channel_id),
        )
    names = available(settings)
    if len(names) != 1:
        return None
    only = names[0]
    return Channel(profile=load_profile(settings, only), learned=load_learned(settings, only))


def remember(
    settings: Settings,
    channel: Channel,
    notes: list[LearnedNote],
    *,
    run_id: str = "",
    video_id: str = "",
) -> int:
    """Append what a run learned, newest last, oldest dropped past the cap."""
    if not notes:
        return 0
    stamped = [
        note.model_copy(
            update={
                "run_id": note.run_id or run_id,
                "video_id": note.video_id or video_id,
                "at": note.at or datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        for note in notes[:MAX_NOTES_PER_RUN]
    ]
    kept = [*channel.learned.entries, *stamped][-MAX_NOTES:]
    channel.learned = LearnedNotes(
        updated_at=datetime.now(UTC).isoformat(timespec="seconds"), entries=kept
    )

    path = learned_path(settings, channel.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(channel.learned.model_dump_json(indent=2), encoding="utf-8")
    return len(stamped)


# --------------------------------------------------------------------------- #
# What the model reads
# --------------------------------------------------------------------------- #


def as_prompt(channel: Channel | None) -> str:
    """The channel block of the opening message, in Portuguese, or nothing."""
    if channel is None:
        return ""
    profile = channel.profile
    lines = [f"## Canal: {profile.name or profile.id}"]
    if profile.language:
        lines.append(f"- idioma: {profile.language}")
    if profile.profiles:
        lines.append(f"- formatos: {', '.join(profile.profiles)}")
    if profile.weekly_target:
        wanted = ", ".join(f"{count} {name}" for name, count in profile.weekly_target.items())
        lines.append(f"- meta semanal: {wanted}")
    if profile.title.tone:
        lines.append(f"- tom do titulo: {profile.title.tone} (ate {profile.title.max_chars} chars)")
    if profile.title.avoid:
        lines.append(f"- no titulo, evite: {'; '.join(profile.title.avoid)}")
    if profile.title.examples:
        lines.append(f"- titulos que funcionaram: {'; '.join(profile.title.examples)}")
    if profile.title.hashtags:
        lines.append(
            f"- hashtags fixas do canal (o codigo ja as inclui): "
            f"{' '.join(profile.title.hashtags)} - escreva so as do tema"
        )
    if profile.editorial.prefer:
        lines.append(f"- rende neste canal: {'; '.join(profile.editorial.prefer)}")
    if profile.editorial.avoid:
        lines.append(f"- nao rende: {'; '.join(profile.editorial.avoid)}")

    if channel.learned.entries:
        lines.append("")
        lines.append("Anotado em runs anteriores (suas, nao do dono do canal):")
        for note in channel.learned.entries[-10:]:
            mark = "" if note.confidence == "observed" else " (suposicao)"
            lines.append(f"- [{note.kind}] {note.note}{mark}")
    return "\n".join(lines)
