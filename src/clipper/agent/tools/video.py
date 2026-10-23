"""Tools for getting the video and reading it (SPEC-agent 5.2 a 5.5)."""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipper.agent.models import (
    BoundaryReport,
    InspectResult,
    OutlineBlock,
    OutlineResult,
    StageState,
    TranscribeResult,
    VideoResult,
)
from clipper.agent.tools.base import ToolContext, ToolSpec
from clipper.models import TimeRange, Transcript
from clipper.stages.select import (
    TOPIC_GAP_S,
    advance_to_opening,
    ends_on_question,
    is_closing,
    opens_well,
    sentences,
    snap_range,
)
from clipper.utils.errors import InputError

#: Blocks the outline may show before neighbouring ones get merged.
DEFAULT_OUTLINE_BLOCKS = 40
#: Context shown either side of an inspected interval.
DEFAULT_MARGIN_S = 8.0


class GetVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(
        default=None, description="URL do YouTube. Omita se o video ja estiver no workspace."
    )
    video_id: str | None = Field(
        default=None, description="Id de um video ja baixado. Use um dos dois, nunca os dois."
    )

    @model_validator(mode="after")
    def _one_of(self) -> GetVideoInput:
        if bool(self.url) == bool(self.video_id):
            raise ValueError("informe 'url' ou 'video_id', exatamente um dos dois")
        return self


def get_video(ctx: ToolContext, args: GetVideoInput) -> VideoResult:
    from clipper import pipeline

    settings = ctx.settings
    if args.url:
        from clipper.stages.ingest import pick_source

        video_id = pick_source(args.url).video_id(args.url)
        existed = (settings.video_dir(video_id) / "source.mp4").exists()
        video_id = pipeline.ingest(settings, args.url)
    else:
        video_id = str(args.video_id)
        if not settings.video_dir(video_id).exists():
            raise InputError(
                f"o video '{video_id}' nao esta no workspace; chame get_video com a url"
            )
        existed = True

    info = pipeline.source_info(settings, video_id)
    stages = [
        StageState(stage=record.stage, profile=record.profile, status=str(record.status))
        for record in ctx.state().stages_for(video_id)
        if str(record.status) != "pending"
    ]
    return VideoResult(
        video_id=video_id,
        title=info.title,
        channel=info.channel,
        url=info.url,
        duration_s=round(info.duration, 1),
        width=info.width,
        height=info.height,
        language=info.language or "",
        upload_date=info.upload_date or "",
        reused=existed,
        stages_done=stages,
    )


class TranscribeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    language: str | None = Field(
        default=None, description="Forca o idioma (ex.: 'pt'). Omita para deteccao automatica."
    )


def transcribe_video(ctx: ToolContext, args: TranscribeInput) -> TranscribeResult:
    from clipper import pipeline

    path = ctx.settings.video_dir(args.video_id) / "transcript.json"
    existed = path.exists() and ctx.state().is_done(args.video_id, "transcribe")

    started = time.monotonic()
    transcript = pipeline.transcribe(ctx.settings, args.video_id, language=args.language)
    return TranscribeResult(
        video_id=args.video_id,
        language=transcript.language,
        duration_s=round(transcript.duration, 1),
        n_segments=len(transcript.segments),
        n_words=len(transcript.words()),
        transcript_path=str(path),
        reused=existed,
        elapsed_s=round(time.monotonic() - started, 1),
    )


class OutlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    max_blocks: int = Field(default=DEFAULT_OUTLINE_BLOCKS, ge=5, le=80)


def build_outline(transcript: Transcript, max_blocks: int) -> OutlineResult:
    """Split the talk at its longest pauses - the topic boundaries of SPEC 6.1.

    Deterministic on purpose: the map of the video is the one thing the loop
    reads in full, and it must not cost a model call or change between runs.
    """
    units = sentences(transcript)
    if not units:
        return OutlineResult(
            video_id=transcript.video_id, duration_s=transcript.duration, n_sentences=0
        )

    gaps = [
        (units[i + 1].start - units[i].end, i + 1)
        for i in range(len(units) - 1)
        if units[i + 1].start - units[i].end >= TOPIC_GAP_S
    ]
    # The widest pauses win: with more candidates than blocks, the small ones are
    # the ones a listener would not hear as a change of subject anyway.
    gaps.sort(reverse=True)
    cuts = sorted(index for _, index in gaps[: max(0, max_blocks - 1)])

    blocks: list[OutlineBlock] = []
    start = 0
    for cut in [*cuts, len(units)]:
        group = units[start:cut]
        if group:
            blocks.append(
                OutlineBlock(
                    start=round(group[0].start, 2),
                    end=round(group[-1].end, 2),
                    n_sentences=len(group),
                    first_sentence=group[0].text.strip(),
                )
            )
        start = cut

    return OutlineResult(
        video_id=transcript.video_id,
        duration_s=round(transcript.duration, 1),
        n_sentences=len(units),
        blocks=blocks,
    )


def get_outline(ctx: ToolContext, args: OutlineInput) -> OutlineResult:
    from clipper import pipeline

    transcript = pipeline.load_transcript(ctx.settings, args.video_id)
    return build_outline(transcript, args.max_blocks)


class InspectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    start: float = Field(ge=0.0, description="Inicio do trecho, em segundos do video.")
    end: float = Field(gt=0.0, description="Fim do trecho, em segundos do video.")
    margin_s: float = Field(
        default=DEFAULT_MARGIN_S, ge=0.0, le=30.0, description="Contexto antes e depois."
    )
    profile: str | None = Field(
        default=None, description="Perfil, quando quiser as fronteiras no modo dele (topic)."
    )

    @model_validator(mode="after")
    def _ordered(self) -> InspectInput:
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) deve ser maior que start ({self.start})")
        return self


def inspect_range(ctx: ToolContext, args: InspectInput) -> InspectResult:
    from clipper import pipeline
    from clipper.agent.review import edge_gaps

    transcript = pipeline.load_transcript(ctx.settings, args.video_id)
    mode = "sentence"
    profile = None
    if args.profile:
        profile = ctx.profile(args.profile)
        mode = profile.boundary_mode

    units = sentences(transcript)
    interval = TimeRange(start=args.start, end=args.end)
    snapped = snap_range(
        transcript,
        interval,
        mode=mode,
        pad_before=ctx.settings.render.pad_before_s,
        pad_after=ctx.settings.render.pad_after_s,
    )
    opened = advance_to_opening(
        transcript,
        snapped,
        mode=mode,
        floor_s=profile.duration_min if profile else 0.0,
        pad_before=ctx.settings.render.pad_before_s,
    )

    start_index = next((i for i, s in enumerate(units) if s.end > args.start + 0.05), None)
    end_index = next((i for i in range(len(units) - 1, -1, -1) if units[i].start < args.end), None)
    gap_before, gap_after = edge_gaps(transcript, args.start, args.end)

    boundaries = BoundaryReport(
        opens_well=start_index is not None and opens_well(units, start_index, mode=mode),
        opening_sentence=units[start_index].text.strip() if start_index is not None else "",
        is_closing=end_index is not None and is_closing(units, end_index),
        closing_sentence=units[end_index].text.strip() if end_index is not None else "",
        ends_on_question=ends_on_question(transcript, args.end),
        gap_before_s=round(gap_before, 2),
        gap_after_s=round(gap_after, 2),
        suggested_start=round(opened.start if opened else snapped.start, 2),
        suggested_end=round(snapped.end, 2),
    )
    return InspectResult(
        video_id=args.video_id,
        start=args.start,
        end=args.end,
        duration_s=round(args.end - args.start, 2),
        before=transcript.text_between(max(0.0, args.start - args.margin_s), args.start),
        text=transcript.text_between(args.start, args.end),
        after=transcript.text_between(args.end, args.end + args.margin_s),
        boundaries=boundaries,
    )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_video",
        description=(
            "Use como primeira ferramenta da run: baixa o video se ainda nao estiver no "
            "workspace e devolve titulo, canal, duracao, resolucao e o que ja foi feito "
            "com ele. Chamar de novo nao rebaixa nada."
        ),
        input_model=GetVideoInput,
        handler=get_video,
        output_limit=600,
        read_only=False,
    ),
    ToolSpec(
        name="transcribe_video",
        description=(
            "Use depois de get_video. Roda o whisper local na GPU (minutos, nao segundos) "
            "ou reaproveita a transcricao em cache. Nao devolve texto: o texto se le por "
            "get_outline e inspect_range."
        ),
        input_model=TranscribeInput,
        handler=transcribe_video,
        output_limit=600,
        read_only=False,
    ),
    ToolSpec(
        name="get_outline",
        description=(
            "Use para escolher onde olhar. Devolve o video dividido em blocos pelas maiores "
            "pausas, com a primeira frase de cada um - o mapa do assunto, sem ler a "
            "transcricao inteira."
        ),
        input_model=OutlineInput,
        handler=get_outline,
        output_limit=4000,
    ),
    ToolSpec(
        name="inspect_range",
        description=(
            "Use antes de decidir estender, encurtar ou aceitar um trecho: mostra o que e "
            "dito ali com margem antes e depois, e diz se as fronteiras servem de abertura "
            "e de fecho, alem de onde o pipeline as colocaria."
        ),
        input_model=InspectInput,
        handler=inspect_range,
        output_limit=3000,
    ),
)
