"""Metadata and the one tool that leaves this machine (SPEC-agent 5.11 e 5.12).

The words are the model's; every number is ours. Chapter offsets come from the
concat offsets and the source URL from the clip's first interval - the same rule
the linear pipeline follows (CLAUDE.md).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from clipper.agent import progress
from clipper.agent.models import MetadataResult, PublishedClip, PublishResult, RejectedView
from clipper.agent.review import clip_id_for
from clipper.agent.tools.base import ToolContext, ToolSpec, parse_clip_id
from clipper.agent.tools.render import _clip_of
from clipper.agent.tools.select import effective_profile
from clipper.models import ClipMetadata, MetadataResponse
from clipper.utils.errors import InputError
from clipper.utils.text import unbacked_numbers

#: YouTube truncates a title around here; longer is not refused, only warned about.
TITLE_SOFT_LIMIT = 70
#: Room for the channel's own tags plus the clip's topical ones.
MAX_HASHTAGS = 5


def merge_hashtags(fixed: list[str], written: list[str]) -> tuple[list[str], list[str]]:
    """The channel's tags first, then the clip's, without repeating either.

    Deduplicated case- and hash-insensitively, because "#Humor" and "humor" are
    the same tag to YouTube and two of them on one clip read as a mistake.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*fixed, *written]:
        key = tag.strip().lstrip("#").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(tag.strip())
    dropped = merged[MAX_HASHTAGS:]
    return merged[:MAX_HASHTAGS], dropped


class MetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str
    title: str = Field(min_length=3, max_length=100)
    hook_line: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    hashtags: list[str] = Field(default_factory=list, max_length=5)
    tags: list[str] = Field(default_factory=list, max_length=15)
    chapter_titles: list[str] = Field(
        default_factory=list, description="Um por intervalo, na ordem. So para perfis multi-trecho."
    )
    thumbnail_text: str = Field(default="", max_length=40)
    thumbnail_concept: str = Field(default="", max_length=200)


def set_metadata(ctx: ToolContext, args: MetadataInput) -> MetadataResult:
    from clipper import pipeline
    from clipper.stages.package import assemble, clip_text, to_markdown
    from clipper.stages.render import clip_dir_for

    video_id, profile_name, index = parse_clip_id(args.clip_id)
    profile = effective_profile(ctx, video_id, profile_name)
    clip_set = pipeline.load_clips(ctx.settings, video_id, profile_name)
    clip = _clip_of(clip_set, index)
    transcript = pipeline.load_transcript(ctx.settings, video_id)
    source = pipeline.source_info(ctx.settings, video_id)

    clip_dir = clip_dir_for(ctx.settings, video_id, profile, clip)
    if not (clip_dir / "clip.mp4").exists():
        raise InputError(
            f"o corte {args.clip_id} ainda nao foi renderizado; "
            "metadados sao escritos depois do render"
        )

    # A title is a promise about the clip, and the checkable part of a promise is
    # its numbers. Measured on a real one: "briguei por 10 mil num carro" over a
    # cut that ends 0.1 s before anybody says ten thousand - the laudo passed all
    # twelve checks and the clip still lied (SPEC 1.2).
    spoken = clip_text(transcript, clip)
    missing = unbacked_numbers(args.title, spoken)
    if missing:
        raise InputError(
            f"o titulo promete {', '.join(str(n) for n in missing)}, que o corte "
            f"{args.clip_id} nao diz. Titule o que esta dentro do corte, ou refaca o "
            "corte ate a fala que traz esse numero."
        )

    warnings: list[str] = []
    fixed = list(ctx.channel.profile.title.hashtags) if ctx.channel else []
    hashtags, dropped = merge_hashtags(fixed, list(args.hashtags))
    if dropped:
        warnings.append(f"hashtags acima de {MAX_HASHTAGS} foram cortadas: {', '.join(dropped)}")
    if ctx.channel and len(args.title) > ctx.channel.profile.title.max_chars:
        warnings.append(
            f"titulo com {len(args.title)} chars; o canal pede ate "
            f"{ctx.channel.profile.title.max_chars}"
        )
    if len(args.title) > TITLE_SOFT_LIMIT:
        warnings.append(
            f"titulo com {len(args.title)} chars; acima de {TITLE_SOFT_LIMIT} o YouTube corta"
        )
    wants_chapters = profile.multi_segment and len(clip.segments) > 1
    if wants_chapters and len(args.chapter_titles) != len(clip.segments):
        warnings.append(
            f"{len(args.chapter_titles)} titulos de capitulo para {len(clip.segments)} trechos; "
            "os que faltarem saem do tema do trecho"
        )
    if args.chapter_titles and not wants_chapters:
        warnings.append("perfil de um trecho so: os titulos de capitulo foram ignorados")

    metadata = assemble(
        clip,
        profile,
        source,
        transcript,
        MetadataResponse(
            index=clip.index,
            title=args.title,
            hook_line=args.hook_line,
            description=args.description,
            chapter_titles=args.chapter_titles,
            hashtags=hashtags,
            tags=args.tags,
            thumbnail_text=args.thumbnail_text,
            thumbnail_concept=args.thumbnail_concept,
        ),
    )
    (clip_dir / "metadata.json").write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
    (clip_dir / "metadata.md").write_text(to_markdown(metadata), encoding="utf-8")

    progress.mark(ctx, video_id, profile_name, "package")
    return MetadataResult(
        clip_id=clip_id_for(video_id, profile_name, index),
        title=metadata.title,
        chapters=[(c.offset, c.title) for c in metadata.chapters],
        hashtags=metadata.hashtags,
        warnings=warnings,
    )


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    profiles: list[str] = Field(min_length=1, description="Perfis a enviar, ex.: ['short'].")
    dry_run: bool = Field(default=False, description="Lista o que seria enviado, sem enviar nada.")


def publish_clips(ctx: ToolContext, args: PublishInput) -> PublishResult:
    from clipper import pipeline
    from clipper.stages.render import clip_dir_for

    settings = ctx.settings
    published: list[PublishedClip] = []
    skipped: list[RejectedView] = []
    notes: list[str] = []
    # A rehearsal run may not ship, however the call was written.
    dry_run = args.dry_run or ctx.dry_run
    if ctx.dry_run and not args.dry_run:
        notes.append("esta run e um ensaio: nada foi enviado")

    for profile_name in args.profiles:
        profile = effective_profile(ctx, args.video_id, profile_name)
        clip_set = pipeline.load_clips(settings, args.video_id, profile_name)
        missing: list[int] = []
        for clip in pipeline.renderable(clip_set):
            clip_dir = clip_dir_for(settings, args.video_id, profile, clip)
            clip_id = clip_id_for(args.video_id, profile_name, clip.index)
            if clip_id in ctx.vetoed:
                skipped.append(RejectedView(topic=clip_id, reason="vetado por voce no monitor"))
                continue
            if not (clip_dir / "clip.mp4").exists():
                skipped.append(RejectedView(topic=clip_id, reason="sem render"))
                continue
            if not (clip_dir / "metadata.json").exists():
                missing.append(clip.index)
                continue
            metadata = ClipMetadata.model_validate_json(
                (clip_dir / "metadata.json").read_text(encoding="utf-8")
            )
            published.append(
                PublishedClip(
                    clip_id=clip_id,
                    title=metadata.title,
                    size_bytes=(clip_dir / "clip.mp4").stat().st_size,
                )
            )

        if missing and not dry_run:
            # Nothing reaches the Drive without metadata; the batch writer is the
            # fallback for clips the agent never got to (SPEC-agent 5.11).
            notes.append(
                f"{profile_name}: {len(missing)} corte(s) sem set_metadata, "
                "preenchidos pelo gerador em lote"
            )
            pipeline.package(settings, args.video_id, profile_name)
            for index in missing:
                clip = _clip_of(clip_set, index)
                clip_dir = clip_dir_for(settings, args.video_id, profile, clip)
                path = clip_dir / "metadata.json"
                if path.exists():
                    metadata = ClipMetadata.model_validate_json(path.read_text(encoding="utf-8"))
                    published.append(
                        PublishedClip(
                            clip_id=clip_id_for(args.video_id, profile_name, index),
                            title=metadata.title,
                            size_bytes=(clip_dir / "clip.mp4").stat().st_size,
                        )
                    )
        elif missing:
            for index in missing:
                skipped.append(
                    RejectedView(
                        topic=clip_id_for(args.video_id, profile_name, index),
                        reason="sem metadados (chame set_metadata)",
                    )
                )

    if dry_run:
        return PublishResult(
            video_id=args.video_id,
            destination="(simulacao)",
            dry_run=True,
            published=published,
            skipped=skipped,
            notes=notes,
        )

    if not published:
        raise InputError("nada para publicar: nenhum corte renderizado e com metadados")

    pipeline.deliver(settings, args.video_id, args.profiles, write_index=False)
    pipeline.deliver_index(settings, args.video_id, args.profiles)

    record = ctx.state().get_stage(args.video_id, "deliver", profile=args.profiles[0])
    return PublishResult(
        video_id=args.video_id,
        destination=record.detail if record else "",
        published=published,
        skipped=skipped,
        notes=notes,
    )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="set_metadata",
        description=(
            "Use depois que o corte passou na revisao. Escreva titulo, descricao e tags no "
            "estilo do canal, nao no estilo generico de YouTube. Os offsets de capitulo e o "
            "link da fonte sao calculados aqui; voce so escreve as palavras."
        ),
        input_model=MetadataInput,
        handler=set_metadata,
        output_limit=800,
        read_only=False,
    ),
    ToolSpec(
        name="publish_clips",
        description=(
            "Ultima ferramenta da run: envia para o Drive tudo que esta renderizado e com "
            "metadados. E a unica acao que sai desta maquina - so chame quando nao "
            "pretender refazer mais nada. Use dry_run para conferir a lista antes."
        ),
        input_model=PublishInput,
        handler=publish_clips,
        output_limit=800,
        read_only=False,
    ),
)
