"""Publishing plan: everything needed to post the clips, in one file.

Two readers. The queue writes one plan per day, which travels to the Drive next
to that day's videos (SPEC 8.1); ``clipper publish-plan`` writes the whole queue
as a single file to keep open beside YouTube Studio. Both come from the queue in
``state.db``, so the workspace being wiped after delivery costs nothing.

``collect`` still reads delivered ``metadata.json`` files back from the archive:
it is how the queue was first filled from what had already been shipped, and how
the eval baseline is rebuilt (SPEC-agent 11.1).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from clipper.config import Settings
from clipper.models import ClipMetadata
from clipper.stages.queue import day_folder, destination_folder
from clipper.state import QueuedClip
from clipper.utils.errors import ExternalError, InputError
from clipper.utils.text import format_timestamp

WEEKDAYS_PT = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")


@dataclass(frozen=True, slots=True)
class PublishItem:
    """One clip, ready to post."""

    video_folder: str
    profile_folder: str
    clip_folder: str
    metadata: ClipMetadata
    #: Source channel, the folder the archive groups a video under.
    channel: str = ""
    #: Where the clip's folder sits in the delivery, relative to its root.
    folder: str = ""
    when: date | None = None
    #: The file as it actually sits in the delivery, which is the clip's title
    #: for anything shipped after that change and ``clip.mp4`` before it.
    filename: str = "clip.mp4"
    #: Where the file sits when it is not in the archive tree: the queue path,
    #: relative to whichever root the plan is written for.
    location: str = ""
    #: Where this clip is published. Empty for an item read out of the archive,
    #: which says nothing about destinations.
    destinations: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        if self.location:
            return self.location
        return f"{self.video_folder}/{self.profile_folder}/{self.clip_folder}/{self.filename}"


def _fetch_metadata_rclone(remote: str, root: str, logger: logging.Logger | None = None) -> Path:
    """Copy every delivered metadata.json into a temporary tree."""
    if shutil.which("rclone") is None:
        raise ExternalError("rclone nao encontrado no PATH")
    destination = Path(tempfile.mkdtemp(prefix="clipper-plan-"))
    command = [
        "rclone",
        "copy",
        f"{remote.rstrip(':')}:{root}",
        str(destination),
        "--include",
        "**/metadata.json",
    ]
    if logger is not None:
        logger.info("rclone: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExternalError(f"rclone falhou ao ler o Drive: {result.stderr.strip()[-400:]}")
    return destination


def list_videos_rclone(remote: str, root: str, logger: logging.Logger | None = None) -> list[str]:
    """Every video in the delivery, as paths relative to ``root``.

    Read back rather than recomputed from the titles: clips shipped before the
    rename still sit there as ``clip.mp4``, and a plan pointing at a name that is
    not in the folder would be worse than useless.
    """
    command = [
        "rclone",
        "lsf",
        f"{remote.rstrip(':')}:{root}",
        "--recursive",
        "--files-only",
        "--include",
        "**/*.mp4",
    ]
    if logger is not None:
        logger.info("rclone: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExternalError(f"rclone falhou ao listar o Drive: {result.stderr.strip()[-400:]}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def list_videos_local(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*.mp4"))


def video_id_of(url: str) -> str:
    """The source video id of a delivered clip, from its ``source_url``."""
    from clipper.sources.youtube import YouTubeSource

    source = YouTubeSource()
    try:
        return source.video_id(url) if source.matches(url) else ""
    except Exception:
        return ""


def filenames_by_folder(paths: Iterable[str]) -> dict[str, str]:
    """One filename per folder, which is what a clip folder holds."""
    names: dict[str, str] = {}
    for path in paths:
        folder, _, name = path.rpartition("/")
        if folder:
            names[folder] = name
    return names


def _filename_for(clip_dir: Path, key: str, filenames: dict[str, str] | None) -> str:
    """The clip's real video file: from the listing, from disk, or the old name.

    ``deliver.mode: folder`` copies the videos themselves into the tree, so they
    can simply be looked at; ``rclone`` only fetches the metadata, and the names
    come from a separate listing.
    """
    if filenames and key in filenames:
        return filenames[key]
    local = sorted(clip_dir.glob("*.mp4"))
    return local[0].name if local else "clip.mp4"


def collect(
    root: Path,
    channel: str | None = None,
    filenames: dict[str, str] | None = None,
) -> list[PublishItem]:
    """Read a delivered archive into publish items.

    Layout is ``<canal>/<video>/<perfil>/<corte>/metadata.json`` as
    :mod:`clipper.stages.deliver` writes it, under whatever prefix the archive
    sits at.
    """
    items: list[PublishItem] = []
    for path in sorted(root.rglob("metadata.json")):
        parts = path.relative_to(root).parts
        if len(parts) < 4:
            continue
        *prefix, video_folder, profile_folder, clip_folder, _ = parts
        if channel and (not prefix or prefix[-1] != channel):
            continue
        try:
            metadata = ClipMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        key = "/".join([*prefix, video_folder, profile_folder, clip_folder])
        items.append(
            PublishItem(
                video_folder,
                profile_folder,
                clip_folder,
                metadata,
                channel=prefix[-1] if prefix else "",
                folder=key,
                filename=_filename_for(path.parent, key, filenames),
            )
        )
    return items


def publish_order(
    items: Iterable[PublishItem],
    *,
    shorts_folder: str = "shorts",
) -> list[PublishItem]:
    """The order clips enter the queue in.

    Clip directories are numbered by score when they are selected, so ordering by
    name is ordering by the model's own ranking - the strongest clip of a video
    goes out first. Which day each one lands on is decided by
    :class:`clipper.stages.queue.Calendar`, the only place that dates a clip.
    """
    return sorted(
        items,
        key=lambda i: (i.video_folder, i.profile_folder != shorts_folder, i.clip_folder),
    )


def queue_items(
    settings: Settings,
    clips: Iterable[QueuedClip],
    *,
    prefix: str = "",
) -> list[PublishItem]:
    """Queue rows as plan items.

    ``prefix`` is what the paths in the plan are relative to: empty for the plan
    that travels inside a day folder, the queue folder for the global one.
    """
    items: list[PublishItem] = []
    for clip in clips:
        # A clip with no destination still says so in the plan: an empty line
        # there would read as "nothing to know", and what there is to know is
        # that no rule sent this clip anywhere.
        destinations = clip.destinations or ("nenhum (ajuste publish.destinations)",)
        location = f"{destination_folder(clip.destinations)}/{clip.filename}"
        if prefix:
            location = f"{prefix}/{day_folder(clip.publish_date)}/{location}"
        items.append(
            PublishItem(
                video_folder="",
                profile_folder=settings.deliver.folder_for(clip.profile),
                clip_folder="",
                metadata=clip.metadata,
                when=clip.publish_date,
                filename=clip.filename,
                location=location,
                destinations=destinations,
            )
        )
    return items


def day_plan(settings: Settings, when: date, clips: Iterable[QueuedClip]) -> str:
    """The ``PUBLICAR.md`` that sits in the Drive beside that day's videos."""
    items = queue_items(settings, clips)
    weekday = WEEKDAYS_PT[when.weekday()]
    return render_plan(
        items,
        heading=f"Publicar em {when.isoformat()} ({weekday})",
        intro=(
            f"{len(items)} cortes nesta pasta. A subpasta diz onde cada um vai; "
            "o nome do arquivo e o titulo."
        ),
        shorts_folder=settings.deliver.folder_for("short"),
    )


def thumbnail_prompt(item: PublishItem) -> str:
    """The prompt to paste into Gemini to get this clip's thumbnail.

    Text and concept come from the metadata stage; older clips delivered before
    those fields existed fall back to the title, which image models render worse
    but still render.
    """
    meta = item.metadata
    text = meta.thumbnail_text.strip() or meta.title.strip()
    concept = meta.thumbnail_concept.strip() or meta.hook_line.strip() or meta.title.strip()
    return "\n".join(
        [
            "Crie uma thumbnail 16:9 (1280x720) para um video do YouTube.",
            "",
            f"Tema: {concept}",
            f'Texto na arte, exatamente assim: "{text}"',
            "",
            "Regras:",
            "- Sem pessoas, sem rostos, sem maos.",
            "- Ilustracao grafica ousada: cores saturadas, alto contraste, fundo simples.",
            "- O texto ocupa cerca de um terco da arte, fonte pesada, legivel no celular.",
            "- Escreva o texto uma unica vez, sem erros de acentuacao, sem outras palavras.",
            "- Deixe uma margem de 60px livre nas bordas.",
        ]
    )


def render_plan(
    items: list[PublishItem],
    *,
    channel: str | None = None,
    thumbnails: bool = True,
    shorts_folder: str = "shorts",
    heading: str | None = None,
    intro: str | None = None,
) -> str:
    """The file to keep open next to YouTube Studio."""
    if not items:
        raise InputError("nenhum corte na fila para montar o roteiro")

    title = heading or f"Roteiro de publicacao{f' - {channel}' if channel else ''}"
    lines = [
        f"# {title}",
        "",
        intro
        or (
            f"{len(items)} cortes. Copie campo a campo no YouTube Studio; "
            "marque o `[ ]` ao publicar."
        ),
        "",
    ]
    if thumbnails:
        lines += [
            "Shorts nao levam thumbnail: o feed toca o proprio video, entao o prompt"
            " de arte aparece so nos longs.",
            "",
        ]

    for number, item in enumerate(items, start=1):
        meta = item.metadata
        when = ""
        if item.when is not None:
            when = f" — {item.when.isoformat()} ({WEEKDAYS_PT[item.when.weekday()]})"
        lines += [
            "---",
            "",
            f"## [ ] {number}. {meta.title}",
            "",
            f"- **arquivo:** `{item.path}`",
            f"- **formato:** {item.profile_folder}{when}",
            *([f"- **destino:** {' + '.join(item.destinations)}"] if item.destinations else []),
            "",
            "**Titulo**",
            "```",
            meta.title,
            "```",
            "",
            "**Descricao**",
            "```",
            meta.description,
        ]
        if meta.chapters:
            lines.append("")
            lines += [f"{format_timestamp(c.offset)} {c.title}" for c in meta.chapters]
        if meta.hashtags:
            lines += ["", " ".join(meta.hashtags)]
        lines += ["```", ""]
        if meta.tags:
            lines += ["**Tags**", "```", ", ".join(meta.tags), "```", ""]
        if thumbnails and item.profile_folder != shorts_folder:
            lines += [
                "**Thumbnail** (cole no Gemini)",
                "```",
                thumbnail_prompt(item),
                "```",
                "",
            ]
        lines += [f"[fonte]({meta.source_url})", ""]

    return "\n".join(lines) + "\n"
