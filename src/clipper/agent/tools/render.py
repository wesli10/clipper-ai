"""Rendering one clip and judging the result (SPEC-agent 5.9 e 5.10).

The render itself is the pipeline's, untouched: same filter order, same degraded
retry, same verification. What is new is the verdict that comes after it.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from clipper.agent import progress
from clipper.agent.models import DropResult, RenderResultView, ReviewResult
from clipper.agent.review import clip_id_for, review_clip
from clipper.agent.tools.base import ToolContext, ToolSpec, parse_clip_id
from clipper.agent.tools.select import effective_profile
from clipper.models import Clip, ClipSet, ClipStatus
from clipper.state import StageStatus
from clipper.utils.errors import InputError


def _clip_of(clip_set: ClipSet, index: int) -> Clip:
    for clip in clip_set.clips:
        if clip.index == index:
            return clip
    known = ", ".join(str(c.index) for c in clip_set.clips) or "nenhum"
    raise InputError(f"nao existe corte de indice {index} nesse perfil (existem: {known})")


class RenderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    profile: str
    index: int = Field(ge=1, description="Indice do corte, como devolvido por commit_clips.")
    force: bool = Field(
        default=False,
        description="Refaz um corte ja renderizado. Gasta outro render do orcamento.",
    )


def render_clip(ctx: ToolContext, args: RenderInput) -> RenderResultView:
    from clipper import pipeline
    from clipper.stages.render import render_clip as run_render
    from clipper.utils.logging import stage_logger

    settings = ctx.settings
    profile = effective_profile(ctx, args.video_id, args.profile)
    clip_set = pipeline.load_clips(settings, args.video_id, args.profile)
    clip = _clip_of(clip_set, args.index)
    transcript = pipeline.load_transcript(settings, args.video_id)
    logger = ctx.logger or stage_logger(settings.logs_dir(args.video_id), "render", args.profile)

    state = ctx.state()
    state.set_stage(args.video_id, "render", StageStatus.RUNNING, profile=args.profile)
    started = time.monotonic()
    try:
        result = run_render(
            settings, args.video_id, clip, profile, transcript, force=args.force, logger=logger
        )
    except Exception as exc:
        state.set_stage(
            args.video_id,
            "render",
            StageStatus.FAILED,
            profile=args.profile,
            detail=str(exc)[:200],
        )
        raise

    clip.offsets = result.offsets
    pipeline.save_clips(settings, clip_set)

    progress.mark(ctx, args.video_id, args.profile, "render")
    if ctx.run_id:
        state.record_run_clip(
            ctx.run_id,
            args.video_id,
            args.profile,
            args.index,
            renders=0 if result.reused else 1,
        )

    return RenderResultView(
        clip_id=clip_id_for(args.video_id, args.profile, args.index),
        path=str(result.video_path),
        duration_s=round(result.duration, 2),
        width=result.width,
        height=result.height,
        degraded=result.degraded,
        reused=result.reused,
        elapsed_s=round(time.monotonic() - started, 1),
    )


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(description="Como devolvido por render_clip: <video>/<perfil>/<indice>.")


def review_render(ctx: ToolContext, args: ReviewInput) -> ReviewResult:
    from clipper import pipeline

    video_id, profile_name, index = parse_clip_id(args.clip_id)
    profile = effective_profile(ctx, video_id, profile_name)
    clip_set = pipeline.load_clips(ctx.settings, video_id, profile_name)
    clip = _clip_of(clip_set, index)
    transcript = pipeline.load_transcript(ctx.settings, video_id)

    result = review_clip(ctx.settings, video_id, clip, profile, transcript, logger=ctx.logger)
    if ctx.run_id:
        ctx.state().record_run_clip(
            ctx.run_id,
            video_id,
            profile_name,
            index,
            verdict=result.verdict,
            detail="; ".join(f"{c.check}={c.status}" for c in result.checks if c.status != "ok"),
        )
    return result


#: Checks a different render can fix. Failing one is not a verdict on the content.
FIXABLE_BY_RENDER = frozenset({"loudness"})


def _refuse_render_only(ctx: ToolContext, video_id: str, profile: str, index: int) -> None:
    """Refuse to drop a clip whose only fault the renderer can still fix.

    Loudness is a property of the render, not of what was said, and treating it as
    a verdict on the clip throws away good material: measured, a clip dropped at
    -16.9 LUFS came back at -14.1 from the same source once the normalisation
    measured before it normalised. So while the clip still has a render left in
    its budget, the answer to a loudness fail is another render, not the bin.
    """
    if not ctx.run_id:
        return
    record = next(
        (
            item
            for item in ctx.state().run_clips(ctx.run_id)
            if item.profile == profile and item.clip_index == index
        ),
        None,
    )
    if record is None or record.verdict != "fail" or not record.detail:
        return
    failed = {part.split("=")[0].strip() for part in record.detail.split(";") if part.strip()}
    if not failed or not failed <= FIXABLE_BY_RENDER:
        return
    left = ctx.settings.agent.max_clip_renders - record.renders
    if left <= 0:
        return
    raise InputError(
        f"o corte {clip_id_for(video_id, profile, index)} so reprovou em "
        f"{', '.join(sorted(failed))}, que e do render e nao do conteudo, e ainda "
        f"resta {left} render para ele. Refaca com render_clip(force=true) e revise "
        "de novo; se reprovar com o orcamento do corte esgotado, ai sim descarte."
    )


class DropInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(description="Como devolvido por render_clip: <video>/<perfil>/<indice>.")
    reason: str = Field(
        min_length=3,
        max_length=300,
        description="Por que este corte nao vai ser publicado. Vai para o relatorio da run.",
    )


def drop_clip(ctx: ToolContext, args: DropInput) -> DropResult:
    """Take a clip out of the run for good, and close the stages it was holding open.

    Abandoning a rendered clip in silence is not the same thing: the counters that
    close `render` and `package` keep waiting for it, and `publish_clips` would fill
    in the metadata the agent deliberately did not write and ship it anyway.
    """
    from clipper import pipeline

    video_id, profile_name, index = parse_clip_id(args.clip_id)
    clip_set = pipeline.load_clips(ctx.settings, video_id, profile_name)
    clip = _clip_of(clip_set, index)
    _refuse_render_only(ctx, video_id, profile_name, index)

    already = clip.status is ClipStatus.REJECTED
    if not already:
        clip.status = ClipStatus.REJECTED
        pipeline.save_clips(ctx.settings, clip_set)

    progress.mark(ctx, video_id, profile_name, "render")
    counted = progress.mark(ctx, video_id, profile_name, "package")
    if ctx.run_id:
        ctx.state().record_run_clip(
            ctx.run_id,
            video_id,
            profile_name,
            index,
            verdict="dropped",
            detail=args.reason[:200],
        )
    ctx.log("corte %s descartado: %s", args.clip_id, args.reason)

    return DropResult(
        clip_id=clip_id_for(video_id, profile_name, index),
        reason=args.reason,
        remaining=counted.total,
        rendered=counted.rendered,
        packaged=counted.packaged,
        reused=already,
    )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="render_clip",
        description=(
            "Use um corte por vez, depois de commit_clips. Cada render custa minutos de CPU "
            "e conta no orcamento da run: confira o que der por snap_clip e inspect_range "
            "antes de gastar um."
        ),
        input_model=RenderInput,
        handler=render_clip,
        output_limit=800,
        read_only=False,
    ),
    ToolSpec(
        name="review_render",
        description=(
            "Use depois de todo render. Devolve checagens objetivas (duracao, resolucao, "
            "audio, loudness, legenda, fronteiras, primeiro quadro) e, quando alguma falha, "
            "o parametro que costuma resolver. 'fail' justifica refazer; 'warn' nao."
        ),
        input_model=ReviewInput,
        handler=review_render,
        output_limit=2500,
    ),
    ToolSpec(
        name="drop_clip",
        description=(
            "Use quando desistir de um corte que ja foi renderizado - o laudo reprova por "
            "algo que esta na fonte, ou o trecho nao se sustenta. Sem isso o corte continua "
            "contando como trabalho por fazer e o publish_clips o envia assim mesmo."
        ),
        input_model=DropInput,
        handler=drop_clip,
        output_limit=400,
        read_only=False,
    ),
)
