"""Tools for choosing what becomes a clip (SPEC-agent 5.1, 5.6, 5.7, 5.8).

The model proposes; ``snap_candidate`` decides. Every interval that reaches
``commit_clips`` has been through the same deterministic boundary rules as the
linear pipeline's - a timestamp from a model never reaches a render (CLAUDE.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipper.agent.models import (
    CandidatesResult,
    CandidateView,
    CommitResult,
    CommittedClip,
    ProfileOverride,
    ProfilesResult,
    ProfileSummary,
    RejectedView,
    SnapResult,
)
from clipper.agent.tools.base import ToolContext, ToolSpec
from clipper.models import (
    ChapterHint,
    ClipCandidate,
    ClipSet,
    ClipStatus,
    SelectionResponse,
    Transcript,
)
from clipper.models import TimeRange as Range
from clipper.profiles import Profile
from clipper.stages.select import RejectedClip, snap_boundaries, snap_candidate, to_clips
from clipper.state import StageStatus
from clipper.utils.errors import InputError
from clipper.utils.text import unbacked_numbers

#: Where the effective overrides of a (video, profile) live, next to clips.json.
OVERRIDES_NAME = "overrides.json"


def _view(candidate: ClipCandidate, prefix: str, position: int) -> CandidateView:
    return CandidateView(
        id=f"{prefix}#{position}",
        segments=[(round(s.start, 2), round(s.end, 2)) for s in candidate.segments],
        duration_s=round(candidate.duration, 1),
        score=candidate.score,
        topic=candidate.topic,
        hook=candidate.hook,
        reason=candidate.reason,
        self_contained=candidate.self_contained,
        reveal_at=candidate.reveal_at,
        comment_prompt=candidate.comment_prompt,
        card_text=candidate.card_text,
    )


def _rejected(item: RejectedClip) -> RejectedView:
    return RejectedView(
        topic=item.candidate.topic or item.candidate.hook,
        segments=[(round(s.start, 2), round(s.end, 2)) for s in item.candidate.segments],
        reason=item.reason,
    )


# --------------------------------------------------------------------------- #
# list_profiles
# --------------------------------------------------------------------------- #


class ProfilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def list_profiles(ctx: ToolContext, args: ProfilesInput) -> ProfilesResult:
    del args
    names = list(ctx.enabled_profiles) or list(ctx.settings.default_profiles)
    summaries = []
    for name in names:
        profile = ctx.profile(name)
        summaries.append(
            ProfileSummary(
                name=profile.name,
                duration_min=profile.duration_min,
                duration_target=profile.duration_target,
                duration_max=profile.duration_max,
                n_clips=profile.n_clips,
                min_score=profile.min_score,
                multi_segment=profile.multi_segment,
                max_segments=profile.max_segments,
                boundary_mode=profile.boundary_mode,
                reframe=profile.reframe,
                subtitle_style=profile.subtitle_style,
                card_seconds=profile.card_seconds,
                output_width=profile.output_width,
                output_height=profile.output_height,
            )
        )
    return ProfilesResult(
        profiles=summaries,
        overridable=sorted(ProfileOverride.model_fields),
    )


# --------------------------------------------------------------------------- #
# find_candidates
# --------------------------------------------------------------------------- #


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)


class CandidatesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    profile: str = Field(description="Nome de um perfil devolvido por list_profiles.")
    n_candidates: int | None = Field(default=None, ge=1, le=20)
    focus: str | None = Field(
        default=None, description="Instrucao extra, ex.: 'so trechos sobre CLT'."
    )
    time_window: TimeWindow | None = Field(
        default=None, description="Restringe a varredura a esse pedaco do video."
    )
    force: bool = Field(
        default=False, description="Ignora a varredura ja feita e chama o modelo de novo."
    )


def find_candidates(ctx: ToolContext, args: CandidatesInput) -> CandidatesResult:
    from clipper import pipeline
    from clipper.llm import build_provider
    from clipper.stages.select import select_clips

    settings = ctx.settings
    profile = ctx.profile(args.profile)
    transcript = pipeline.load_transcript(settings, args.video_id)
    out_dir = settings.profile_dir(args.video_id, args.profile)
    raw_path = out_dir / "candidates.json"

    wanted = profile.model_copy(update={"n_clips": args.n_candidates or profile.n_clips})
    window = (
        Range(start=args.time_window.start, end=args.time_window.end)
        if args.time_window is not None
        else None
    )

    if raw_path.exists() and not args.force:
        # The expensive half is the model reading the transcript; the snapping is
        # deterministic, so a repeat call re-derives it instead of paying again.
        stored = SelectionResponse.model_validate_json(raw_path.read_text(encoding="utf-8"))
        kept, rejected = snap_boundaries(
            transcript,
            list(stored.clips),
            wanted,
            pad_before=settings.render.pad_before_s,
            pad_after=settings.render.pad_after_s,
        )
        return CandidatesResult(
            video_id=args.video_id,
            profile=args.profile,
            accepted=[_view(c, args.profile, i) for i, c in enumerate(kept, start=1)],
            rejected=[_rejected(r) for r in rejected],
            reused=True,
        )

    logger = ctx.logger
    provider = build_provider(
        settings, stage="select", logger=logger, request_dir=out_dir, wait=True
    )
    state = ctx.state()
    state.set_stage(args.video_id, "select", StageStatus.RUNNING, profile=args.profile)
    try:
        result = select_clips(
            transcript,
            wanted,
            provider,
            n_clips=args.n_candidates,
            pad_before=settings.render.pad_before_s,
            pad_after=settings.render.pad_after_s,
            logger=logger,
            window=window,
            extra=args.focus,
        )
    except Exception as exc:
        state.set_stage(
            args.video_id,
            "select",
            StageStatus.FAILED,
            profile=args.profile,
            detail=str(exc)[:200],
        )
        raise

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        SelectionResponse(clips=result.raw).model_dump_json(indent=2), encoding="utf-8"
    )
    return CandidatesResult(
        video_id=args.video_id,
        profile=args.profile,
        accepted=[
            CandidateView(
                id=f"{args.profile}#{clip.index}",
                segments=[(round(s.start, 2), round(s.end, 2)) for s in clip.segments],
                duration_s=round(clip.duration, 1),
                score=clip.score,
                topic=clip.topic,
                hook=clip.hook,
                reason=clip.reason,
                reveal_at=clip.reveal_at,
                comment_prompt=clip.comment_prompt,
                card_text=clip.card_text,
            )
            for clip in result.clips
        ],
        rejected=[_rejected(r) for r in result.rejected],
        provider=getattr(provider, "name", ""),
        n_prompt_chars=result.prompt_chars,
    )


# --------------------------------------------------------------------------- #
# snap_clip
# --------------------------------------------------------------------------- #


class ClipProposal(BaseModel):
    """One clip as the model proposes it, before the boundary rules run."""

    model_config = ConfigDict(extra="forbid")

    segments: list[tuple[float, float]] = Field(min_length=1, max_length=8)
    topic: str = ""
    hook: str = Field(default="", description="A primeira frase do corte, citada da transcricao.")
    closing: str = Field(default="", description="A ultima frase do corte, citada da transcricao.")
    reason: str = ""
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    reveal_at: float | None = Field(
        default=None, description="Formato quiz: onde comeca a resposta, em segundos do video."
    )
    card_text: str = ""
    comment_prompt: str = Field(
        default="",
        description=(
            "Pergunta curta desenhada na barra acima do quadro, o corte inteiro. "
            "Carregue a que veio do candidato, ou escreva uma melhor."
        ),
    )
    chapter_titles: list[str] = Field(default_factory=list)

    def to_candidate(self) -> ClipCandidate:
        return ClipCandidate(
            segments=[Range(start=s, end=e) for s, e in self.segments],
            hook=self.hook,
            closing=self.closing,
            reason=self.reason,
            score=self.score,
            topic=self.topic,
            reveal_at=self.reveal_at,
            card_text=self.card_text,
            comment_prompt=self.comment_prompt,
            chapters=[ChapterHint(offset_hint=t) for t in self.chapter_titles],
        )


class SnapInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    profile: str
    clip: ClipProposal


def _check_questions(clip_set: ClipSet, transcript: Transcript) -> None:
    """The question on the bar has to be answerable from the clip that carries it.

    It is read by someone who saw only the cut, so a number it asks about and the
    cut never says is a question about a video the viewer did not watch. Checked
    here because the boundaries are final at commit and the fix is free - one more
    call, no render (CLAUDE.md: what can be verified, is verified in code).
    """
    from clipper.stages.package import clip_text

    for clip in clip_set.clips:
        if not clip.comment_prompt:
            continue
        missing = unbacked_numbers(clip.comment_prompt, clip_text(transcript, clip))
        if missing:
            raise InputError(
                f"o corte {clip.index:02d} pergunta por "
                f"{', '.join(str(n) for n in missing)}, que o proprio corte nao diz: "
                f"'{clip.comment_prompt}'. Reescreva a pergunta com o que esta dentro "
                "do corte, ou estenda o corte ate a fala que traz esse numero."
            )


def _snap(
    ctx: ToolContext, video_id: str, profile: Profile, proposal: ClipProposal
) -> ClipCandidate | RejectedClip:
    from clipper import pipeline

    transcript = pipeline.load_transcript(ctx.settings, video_id)
    return snap_candidate(
        transcript,
        proposal.to_candidate(),
        profile,
        pad_before=ctx.settings.render.pad_before_s,
        pad_after=ctx.settings.render.pad_after_s,
    )


def snap_clip(ctx: ToolContext, args: SnapInput) -> SnapResult:
    profile = ctx.profile(args.profile)
    # This tool answers about boundaries. The score gate belongs to choosing what
    # to keep - refusing a boundary test for "score 0 abaixo do minimo" answers a
    # question nobody asked, and costs a call to find that out (seen in a run).
    proposal = (
        args.clip if args.clip.score else args.clip.model_copy(update={"score": profile.min_score})
    )
    result = _snap(ctx, args.video_id, profile, proposal)
    if isinstance(result, RejectedClip):
        return SnapResult(accepted=False, reason=result.reason)

    segments = [(round(s.start, 2), round(s.end, 2)) for s in result.segments]
    return SnapResult(
        accepted=True,
        segments=segments,
        duration_s=round(result.duration, 1),
        changed=segments != [(round(s, 2), round(e, 2)) for s, e in args.clip.segments],
        hook=result.hook,
        closing=result.closing,
        reveal_at=result.reveal_at,
    )


# --------------------------------------------------------------------------- #
# commit_clips
# --------------------------------------------------------------------------- #


class CommitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    profile: str
    clips: list[ClipProposal] = Field(min_length=1, max_length=20)
    overrides: ProfileOverride | None = None

    @model_validator(mode="after")
    def _unique(self) -> CommitInput:
        starts = [clip.segments[0][0] for clip in self.clips]
        if len(set(starts)) != len(starts):
            raise ValueError("dois cortes comecam no mesmo instante")
        return self


def load_overrides(settings: Any, video_id: str, profile_name: str) -> ProfileOverride:
    """Overrides committed for this (video, profile), or an empty set."""
    path = Path(settings.profile_dir(video_id, profile_name)) / OVERRIDES_NAME
    if not path.exists():
        return ProfileOverride()
    return ProfileOverride.model_validate_json(path.read_text(encoding="utf-8"))


def effective_profile(ctx: ToolContext, video_id: str, profile_name: str) -> Profile:
    """The profile a render must use: config plus whatever the run committed."""
    return load_overrides(ctx.settings, video_id, profile_name).apply(ctx.profile(profile_name))


def commit_clips(ctx: ToolContext, args: CommitInput) -> CommitResult:
    from clipper import pipeline

    settings = ctx.settings
    # The channel's defaults sit under the call's overrides: a standing decision
    # of the channel ("tail_roll de 2s neste formato de mesa") applies to every
    # run, and what the agent decides for this video wins over it.
    merged = dict(ctx.channel.overrides_for(args.profile)) if ctx.channel else {}
    merged.update((args.overrides or ProfileOverride()).model_dump(exclude_none=True))
    try:
        overrides = ProfileOverride.model_validate(merged)
    except ValueError as exc:
        raise InputError(
            f"padroes de render do canal invalidos para '{args.profile}': {exc}"
        ) from exc
    profile = overrides.apply(ctx.profile(args.profile))
    transcript = pipeline.load_transcript(settings, args.video_id)

    kept: list[ClipCandidate] = []
    rejected: list[RejectedView] = []
    for proposal in args.clips:
        result = snap_candidate(
            transcript,
            proposal.to_candidate(),
            profile,
            pad_before=settings.render.pad_before_s,
            pad_after=settings.render.pad_after_s,
        )
        if isinstance(result, RejectedClip):
            rejected.append(_rejected(result))
        else:
            kept.append(result)

    if not kept:
        raise InputError(
            "nenhum corte sobreviveu as regras de fronteira: "
            + "; ".join(item.reason for item in rejected)
        )

    clip_set = ClipSet(video_id=args.video_id, profile=args.profile, clips=to_clips(kept, profile))
    _check_questions(clip_set, transcript)
    for clip in clip_set.clips:
        # There is no human review in agent mode (SPEC-agent 2, decision 2), so a
        # committed clip is an approved one - which is what `renderable()` reads.
        clip.status = ClipStatus.APPROVED

    out_dir = settings.profile_dir(args.video_id, args.profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    pipeline.save_clips(settings, clip_set)
    (out_dir / OVERRIDES_NAME).write_text(
        overrides.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    pipeline.prune_stale_clips(settings, args.video_id, args.profile, clip_set)

    state = ctx.state()
    state.set_stage(
        args.video_id,
        "select",
        StageStatus.DONE,
        profile=args.profile,
        detail=f"agent: {len(clip_set.clips)} cortes",
    )
    state.set_stage(
        args.video_id,
        "review",
        StageStatus.DONE,
        profile=args.profile,
        detail="decidido pelo agent",
    )

    return CommitResult(
        video_id=args.video_id,
        profile=args.profile,
        clips=[
            CommittedClip(
                clip_id=f"{args.video_id}/{args.profile}/{clip.index}",
                index=clip.index,
                slug=clip.slug,
                topic=clip.topic,
                duration_s=round(clip.duration, 1),
                segments=[(round(s.start, 2), round(s.end, 2)) for s in clip.segments],
            )
            for clip in clip_set.clips
        ],
        rejected=rejected,
        overrides=json.loads(overrides.model_dump_json(exclude_none=True)),
    )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_profiles",
        description=(
            "Use antes de planejar a run, para saber quais formatos este canal publica, "
            "quanto cada um dura e quantos cortes costuma render. O que nao aparece aqui "
            "esta desabilitado e nao e opcao."
        ),
        input_model=ProfilesInput,
        handler=list_profiles,
        output_limit=1500,
    ),
    ToolSpec(
        name="find_candidates",
        description=(
            "Use para pedir uma varredura da transcricao inteira em busca de trechos de um "
            "perfil. E a chamada mais cara da run. Leia tambem os rejeitados: o motivo diz "
            "o que corrigir, e corrigir custa menos que varrer de novo."
        ),
        input_model=CandidatesInput,
        handler=find_candidates,
        output_limit=4000,
        read_only=False,
    ),
    ToolSpec(
        name="snap_clip",
        description=(
            "Use sempre que quiser mudar um intervalo - estender para pegar o contexto que "
            "falta, encurtar, juntar pedacos. Nao renderiza nada e nao grava nada: e a "
            "forma barata de testar uma fronteira antes de gastar um render."
        ),
        input_model=SnapInput,
        handler=snap_clip,
        output_limit=1200,
    ),
    ToolSpec(
        name="commit_clips",
        description=(
            "Use quando tiver decidido os cortes de um perfil. Fixa a lista que o render e "
            "a entrega vao usar, e substitui qualquer lista anterior desse perfil. Leve "
            "junto o comment_prompt de cada corte: sem ele o short sai sem a pergunta na "
            "barra de cima."
        ),
        input_model=CommitInput,
        handler=commit_clips,
        output_limit=1200,
        read_only=False,
    ),
)
