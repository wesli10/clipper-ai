"""The channel tools (SPEC-agent 5.13).

Reading is free and the agent should start from it. Writing only ever appends to
the notes the agent itself keeps - your `channels/<id>.yaml` is not reachable
from here, by design.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from clipper.agent.channel import (
    MAX_NOTES_PER_RUN,
    Channel,
    Confidence,
    LearnedNote,
    NoteKind,
    as_prompt,
    remember,
)
from clipper.agent.tools.base import ToolContext, ToolSpec
from clipper.utils.errors import InputError


class ChannelView(BaseModel):
    """The merge, labelled: what you configured and what the agent noticed."""

    channel: str = ""
    name: str = ""
    language: str = ""
    profiles: list[str] = Field(default_factory=list)
    weekly_target: dict[str, int] = Field(default_factory=dict)
    title_tone: str = ""
    title_max_chars: int = 0
    title_avoid: list[str] = Field(default_factory=list)
    title_examples: list[str] = Field(default_factory=list)
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    render_defaults: dict[str, dict[str, object]] = Field(default_factory=dict)
    learned: list[str] = Field(default_factory=list)


class ChannelNotesResult(BaseModel):
    channel: str
    added: int
    kept: int


class LoadChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require(ctx: ToolContext) -> Channel:
    if ctx.channel is None:
        raise InputError(
            "esta run nao tem canal configurado; trabalhe pelo perfil de formato "
            "(list_profiles) e nao invente um estilo de canal"
        )
    return ctx.channel


def load_channel_profile(ctx: ToolContext, args: LoadChannelInput) -> ChannelView:
    del args
    channel = _require(ctx)
    profile = channel.profile
    return ChannelView(
        channel=profile.id,
        name=profile.name,
        language=profile.language,
        profiles=list(profile.profiles),
        weekly_target=dict(profile.weekly_target),
        title_tone=profile.title.tone,
        title_max_chars=profile.title.max_chars,
        title_avoid=list(profile.title.avoid),
        title_examples=list(profile.title.examples),
        prefer=list(profile.editorial.prefer),
        avoid=list(profile.editorial.avoid),
        render_defaults={k: dict(v) for k, v in profile.render_defaults.items()},
        learned=[
            f"[{note.kind}] {note.note}" + ("" if note.confidence == "observed" else " (suposicao)")
            for note in channel.learned.entries[-15:]
        ],
    )


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=10, max_length=300)
    kind: NoteKind = "outro"
    confidence: Confidence = Field(
        default="observed",
        description="'observed' so para o que aconteceu nesta run; o resto e 'assumed'.",
    )


class UpdateChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = ""
    notes: list[Note] = Field(min_length=1, max_length=MAX_NOTES_PER_RUN)


def update_channel_profile(ctx: ToolContext, args: UpdateChannelInput) -> ChannelNotesResult:
    channel = _require(ctx)
    added = remember(
        ctx.settings,
        channel,
        [LearnedNote(note=n.note, kind=n.kind, confidence=n.confidence) for n in args.notes],
        run_id=ctx.run_id,
        video_id=args.video_id,
    )
    return ChannelNotesResult(channel=channel.id, added=added, kept=len(channel.learned.entries))


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="load_channel_profile",
        description=(
            "Use logo no comeco, antes de planejar: o canal diz que formatos publica, "
            "em que tom escreve titulo, o que rende e o que nao rende, e o que runs "
            "anteriores anotaram. Titulo e escolha editorial saem daqui."
        ),
        input_model=LoadChannelInput,
        handler=load_channel_profile,
        output_limit=3000,
    ),
    ToolSpec(
        name="update_channel_profile",
        description=(
            "Use no fim da run, e so para o que voce observou nesta run: um padrao que "
            "se repetiu, um parametro que resolveu um problema, um tipo de trecho que "
            "nao funciona neste canal. Nao anote palpite como se fosse observacao, e "
            "nao anote o obvio."
        ),
        input_model=UpdateChannelInput,
        handler=update_channel_profile,
        output_limit=400,
        read_only=False,
    ),
)


def channel_prompt(ctx: ToolContext) -> str:
    return as_prompt(ctx.channel)
