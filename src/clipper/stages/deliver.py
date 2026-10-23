"""Stage 5 - delivery to Google Drive (SPEC 8).

Two modes: copy into the locally synced Drive folder, or ``rclone copy`` to a
remote the user configured. No OAuth of our own.

The delivery has two halves. Here is the archive - ``arquivo/<canal>/<video>/``
with the metadata, the thumbnail and the INDEX.md of every clip, the history of
what was cut. The videos themselves go to the publishing queue
(:mod:`clipper.stages.queue`), which is the half an upload session opens.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from clipper.config import Settings
from clipper.models import ClipMetadata, ClipSet, SourceInfo
from clipper.utils.errors import ExternalError, InputError
from clipper.utils.text import format_timestamp, slugify

#: The render always writes this; the delivered copy is renamed to the title.
RENDER_FILENAME = "clip.mp4"
#: Characters no filesystem or Drive client wants in a name.
_ILLEGAL_IN_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: YouTube caps a title at 100 characters; leave room for the extension.
MAX_FILENAME_STEM = 90


def delivered_name(title: str) -> str:
    """The filename a clip is uploaded under.

    YouTube pre-fills the title field from the filename, so the file is named
    after the clip - it is the only piece of metadata the upload page picks up
    from the file itself. Accents and spaces stay: the point is that the name is
    the title, not a slug.
    """
    cleaned = _ILLEGAL_IN_FILENAME.sub("", title)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if len(cleaned) > MAX_FILENAME_STEM:
        cleaned = cleaned[:MAX_FILENAME_STEM].rsplit(" ", 1)[0].strip(" .")
    return f"{cleaned}.mp4" if cleaned else RENDER_FILENAME


def video_folder_name(source: SourceInfo) -> str:
    """``<data>_<slug do video>`` (SPEC 8)."""
    raw = source.upload_date or ""
    if len(raw) == 8 and raw.isdigit():
        stamp = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    else:
        stamp = date.today().isoformat()
    return f"{stamp}_{slugify(source.title)}"


def channel_slug(source: SourceInfo) -> str:
    return slugify(source.channel) or "canal"


def archive_dir(settings: Settings, source: SourceInfo) -> Path:
    """``Clipper/arquivo/<canal>/<data>_<slug>`` - the history of one video.

    The videos are not here (SPEC 8.1): the archive holds what is needed to know
    what was cut and to rebuild an eval baseline, and the clip itself sits in the
    publishing queue.
    """
    return (
        Path(settings.deliver.root_folder)
        / settings.publish.archive_folder
        / channel_slug(source)
        / video_folder_name(source)
    )


def queue_dir(settings: Settings) -> Path:
    """``Clipper/publicar`` - one folder per day inside it."""
    return Path(settings.deliver.root_folder) / settings.publish.queue_folder


# --------------------------------------------------------------------------- #
# INDEX.md
# --------------------------------------------------------------------------- #


def build_index(
    source: SourceInfo,
    per_profile: dict[str, list[tuple[ClipSet, ClipMetadata]]],
) -> str:
    """One section per profile listing everything ready to publish (SPEC 8)."""
    lines = [f"# {source.title}", "", f"Canal: {source.channel}", f"Fonte: {source.url}", ""]
    for profile_name, entries in per_profile.items():
        lines.append(f"## {profile_name}")
        lines.append("")
        if not entries:
            lines.extend(["_nenhum corte entregue_", ""])
            continue
        for clip_set, metadata in entries:
            del clip_set
            ranges = ", ".join(
                f"{format_timestamp(start)}\u2013{format_timestamp(end)}"
                for start, end in metadata.source_segments
            )
            duration = sum(end - start for start, end in metadata.source_segments)
            lines.append(f"### {metadata.title}")
            lines.append("")
            lines.append(f"- Duração: {format_timestamp(duration)}")
            lines.append(f"- Trechos de origem: {ranges}")
            if metadata.hashtags:
                lines.append(f"- Hashtags: {' '.join(metadata.hashtags)}")
            if metadata.chapters:
                chapters = "; ".join(
                    f"{format_timestamp(c.offset)} {c.title}" for c in metadata.chapters
                )
                lines.append(f"- Capítulos: {chapters}")
            lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Copying
# --------------------------------------------------------------------------- #


#: Never travels to the archive: the working files of the render, and the videos,
#: which belong to the publishing queue and to nowhere else (SPEC 8.1).
ARCHIVE_IGNORES = ("intermediates", "*.mp4")


def require_drive_root(destination_root: Path) -> None:
    if not destination_root.exists():
        raise InputError(
            f"a pasta do Google Drive nao existe: {destination_root}\n"
            "Ajuste deliver.drive_root no config.yaml ou use deliver.mode: rclone."
        )


def deliver_folder(
    sources: list[tuple[str, Path]],
    destination_root: Path,
    relative: Path,
    *,
    index: str | None = None,
    logger: logging.Logger | None = None,
) -> Path:
    """Copy each ``(profile_folder, clip_dir)`` into the synced archive folder."""
    require_drive_root(destination_root)
    target = destination_root / relative
    for folder, clip_dir in sources:
        dest = target / folder / clip_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(clip_dir, dest, ignore=shutil.ignore_patterns(*ARCHIVE_IGNORES))
        if logger is not None:
            logger.info("copiado %s -> %s", clip_dir, dest)
    if index is not None:
        target.mkdir(parents=True, exist_ok=True)
        (target / "INDEX.md").write_text(index, encoding="utf-8")
    return target


def rclone_remotes() -> list[str]:
    """Remotes configured in rclone, without the trailing colon."""
    if shutil.which("rclone") is None:
        return []
    try:
        result = subprocess.run(
            ["rclone", "listremotes"], capture_output=True, text=True, timeout=20, check=False
        )
    except OSError:
        return []
    return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]


def require_rclone(remote: str, *, check_remote: bool = True) -> str:
    """The remote name, once rclone is installed and that remote exists."""
    if shutil.which("rclone") is None:
        raise ExternalError(
            "rclone nao encontrado no PATH. Instale-o e configure o remote "
            f"'{remote}' (rclone config), ou use deliver.mode: folder."
        )
    name = remote.rstrip(":")
    if not check_remote:
        return name
    configured = rclone_remotes()
    if not configured:
        raise ExternalError(
            "o rclone nao tem nenhum remote configurado.\n"
            f"Crie um com:  rclone config create {name} drive scope drive"
        )
    if name not in configured:
        raise ExternalError(
            f"o remote '{name}' nao esta configurado no rclone.\n"
            f"Remotes disponiveis: {', '.join(configured)}\n"
            f"Crie o remote com:  rclone config create {name} drive scope drive"
        )
    return name


def deliver_rclone(
    sources: list[tuple[str, Path]],
    remote: str,
    relative: Path,
    *,
    index: str | None = None,
    chunk_size: str = "128M",
    transfers: int = 4,
    workers: int = 3,
    logger: logging.Logger | None = None,
) -> str:
    """``rclone copy`` each clip directory to ``<remote><relative>/<folder>``.

    Metadata, thumbnail and subtitles only - the video goes to the queue.
    """
    name = remote.rstrip(":")
    if not sources and index is None:
        require_rclone(remote, check_remote=False)
        return f"{name}:{relative.as_posix()}"
    require_rclone(remote)
    base = f"{name}:{relative.as_posix()}"

    # One call per profile, never one per clip in parallel. Google Drive keys
    # folders by id, not by path, so concurrent uploads that each had to create
    # `shorts/` created *several* `shorts/` in the same parent and scattered the
    # clips between them. rclone still parallelises inside one call.
    by_folder: dict[str, list[Path]] = {}
    for folder, clip_dir in sources:
        by_folder.setdefault(folder, []).append(clip_dir)

    for folder, clip_dirs in by_folder.items():
        parent = clip_dirs[0].parent
        command = [
            "rclone",
            "copy",
            "--drive-chunk-size",
            chunk_size,
            "--transfers",
            str(max(1, workers)),
        ]
        # One ordered --filter list, never --include mixed with --exclude: given
        # both, rclone parses them in an indeterminate order (it warns about it)
        # and the excludes below were losing - every clip went up twice, as
        # clip.mp4 and again under its title.
        rules = ["- intermediates/**", "- *.mp4"]
        rules += [f"+ /{clip_dir.name}/**" for clip_dir in clip_dirs]
        # Everything else in the parent is another clip, rejected or from an
        # earlier selection, and must not travel.
        rules.append("- *")
        for rule in rules:
            command += ["--filter", rule]
        command += [str(parent), f"{base}/{folder}"]

        if logger is not None:
            logger.info("rclone: %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ExternalError(f"rclone falhou: {result.stderr.strip()[-500:]}")

    if index is not None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "INDEX.md"
            index_path.write_text(index, encoding="utf-8")
            result = subprocess.run(
                ["rclone", "copy", str(index_path), base],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ExternalError(f"rclone falhou ao enviar o INDEX.md: {result.stderr[-500:]}")
    return base


# --------------------------------------------------------------------------- #
# The publishing queue
# --------------------------------------------------------------------------- #


def upload_queue_folder(
    days: list[Path],
    destination_root: Path,
    queue_relative: Path,
    *,
    logger: logging.Logger | None = None,
) -> Path:
    """Copy each staged day into the synced queue folder.

    Additive on purpose, like the rest of the delivery: a day that already holds
    clips from another video keeps them, and only the plan is rewritten.
    """
    require_drive_root(destination_root)
    target = destination_root / queue_relative
    for day in days:
        dest = target / day.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(day, dest, dirs_exist_ok=True)
        if logger is not None:
            logger.info("copiado %s -> %s", day, dest)
    return target


def upload_queue_rclone(
    days: list[Path],
    remote: str,
    queue_relative: Path,
    *,
    chunk_size: str = "128M",
    workers: int = 3,
    logger: logging.Logger | None = None,
) -> str:
    """``rclone copy`` each staged day to ``<remote><queue>/<data>``.

    One call per day, never several in parallel: Google Drive keys folders by id,
    so two calls that each have to create the queue folder create two of them and
    scatter the days between them.
    """
    name = require_rclone(remote)
    base = f"{name}:{queue_relative.as_posix()}"
    for day in days:
        command = [
            "rclone",
            "copy",
            "--drive-chunk-size",
            chunk_size,
            "--transfers",
            str(max(1, workers)),
            str(day),
            f"{base}/{day.name}",
        ]
        if logger is not None:
            logger.info("rclone: %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ExternalError(f"rclone falhou ao enviar a fila: {result.stderr.strip()[-500:]}")
    return base


def move_file_rclone(
    remote: str,
    old: str,
    new: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Move one file inside the same remote.

    Server-side: Google Drive renames it in place, so a clip that is already in
    the Drive is not downloaded and uploaded again to change folders.
    """
    name = require_rclone(remote)
    command = ["rclone", "moveto", f"{name}:{old}", f"{name}:{new}"]
    if logger is not None:
        logger.info("rclone: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExternalError(f"rclone falhou ao mover {old}: {result.stderr.strip()[-400:]}")


def move_file_folder(
    root: Path, old: str, new: str, *, logger: logging.Logger | None = None
) -> None:
    source = root / old
    target = root / new
    if not source.exists():
        raise InputError(f"arquivo nao encontrado para mover: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    if logger is not None:
        logger.info("movido %s -> %s", source, target)


def move_tree_rclone(
    remote: str,
    old: str,
    new: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Move a whole folder inside the same remote, leaving no empty shell behind."""
    name = require_rclone(remote)
    command = [
        "rclone",
        "move",
        f"{name}:{old}",
        f"{name}:{new}",
        "--delete-empty-src-dirs",
    ]
    if logger is not None:
        logger.info("rclone: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExternalError(f"rclone falhou ao mover {old}: {result.stderr.strip()[-400:]}")


def move_tree_folder(
    root: Path, old: str, new: str, *, logger: logging.Logger | None = None
) -> None:
    source = root / old
    target = root / new
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copytree(source, target, dirs_exist_ok=True)
        shutil.rmtree(source)
    else:
        shutil.move(str(source), str(target))
    if logger is not None:
        logger.info("movido %s -> %s", source, target)
