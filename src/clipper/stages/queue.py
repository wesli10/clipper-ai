"""The publishing queue: the half of the delivery an upload session opens (SPEC 8.1).

The archive keeps the history of every video - metadata, thumbnail, INDEX.md,
one folder per clip - and nothing is ever published from it. The queue is the
working half: ``publicar/<data>/<destino>/`` with the videos of that day and a
single ``PUBLICAR.md`` beside them. One folder to open, one batch to download,
and the folder name says where the clip goes.

The video file lives only in the queue. Keeping it in both halves would double
what the Drive holds and give nothing back: the archive is there to remember
what was cut, not to publish from.

Dates are decided once, when the clip is delivered, and written to ``state.db``.
A clip already sitting in Thursday's folder must not move because another video
was processed in between.
"""

from __future__ import annotations

import os
import shutil
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from clipper.stages.deliver import RENDER_FILENAME
from clipper.state import QueuedClip

#: Folder for a clip the destination rules send nowhere - visible on purpose,
#: so a missing rule shows up as a folder instead of as a clip that vanished.
NO_DESTINATION = "sem-destino"


def destination_folder(destinations: Sequence[str]) -> str:
    """The folder a clip sits in inside its day.

    One destination reads as ``so-tiktok``, two as ``youtube-e-tiktok``: the
    folder name is the instruction, so nothing has to be looked up while
    publishing. The clip's own file keeps the title as its name - it is what the
    upload page pre-fills the title field from.
    """
    names = [name for name in destinations if name]
    if not names:
        return NO_DESTINATION
    if len(names) == 1:
        return f"so-{names[0]}"
    return "-e-".join(names)


def day_folder(when: date) -> str:
    """``2026-09-13`` - sorts chronologically by name, in every client."""
    return when.isoformat()


def queue_path(when: date, destinations: Sequence[str], filename: str) -> str:
    """Where a clip sits, relative to the queue folder."""
    return f"{day_folder(when)}/{destination_folder(destinations)}/{filename}"


def unique_filename(name: str, taken: Iterable[str]) -> str:
    """``titulo.mp4`` unless the day already holds that name, then ``titulo (2).mp4``.

    Two clips can honestly end up with the same title, and one silently
    overwriting the other in the Drive would lose a render.
    """
    existing = {item.casefold() for item in taken}
    if name.casefold() not in existing:
        return name
    stem, _, suffix = name.rpartition(".")
    stem, suffix = (stem, f".{suffix}") if stem else (name, "")
    counter = 2
    while f"{stem} ({counter}){suffix}".casefold() in existing:
        counter += 1
    return f"{stem} ({counter}){suffix}"


@dataclass(slots=True)
class Calendar:
    """Which days the queue already uses, so a new clip lands on a free one.

    Shorts fill a day up to ``per_day``; a long is the weekly piece and keeps
    ``long_every_days`` of distance from the next one.
    """

    daily: Counter[date] = field(default_factory=Counter)
    weekly: set[date] = field(default_factory=set)

    @classmethod
    def of(cls, entries: Iterable[QueuedClip], daily_profiles: Sequence[str]) -> Calendar:
        calendar = cls()
        for entry in entries:
            calendar.take(entry.publish_date, is_daily=entry.profile in daily_profiles)
        return calendar

    def take(self, when: date, *, is_daily: bool) -> None:
        if is_daily:
            self.daily[when] += 1
        else:
            self.weekly.add(when)

    def next_free(
        self,
        *,
        is_daily: bool,
        start: date,
        per_day: int,
        long_every_days: int,
    ) -> date:
        """The first day from ``start`` on that still has room for this clip."""
        when = start
        if is_daily:
            while self.daily[when] >= max(1, per_day):
                when += timedelta(days=1)
            return when
        gap = max(1, long_every_days)
        while any(abs((when - taken).days) < gap for taken in self.weekly):
            when += timedelta(days=1)
        return when


@dataclass(frozen=True, slots=True)
class StagedDay:
    """One day of the queue, laid out locally the way the Drive should hold it."""

    when: date
    directory: Path


def stage_days(
    root: Path,
    clips: Sequence[tuple[QueuedClip, Path]],
    plans: dict[date, str],
    *,
    plan_filename: str = "PUBLICAR.md",
) -> list[StagedDay]:
    """Lay the clips out under ``root`` as ``<dia>/<destino>/<titulo>.mp4``.

    By hard link: the render stays where the pipeline expects it, the queue gets
    the name the upload page reads the title from, and the second name costs no
    disk. ``plans`` is the day's ``PUBLICAR.md``, written beside its videos.

    Only the days present in ``clips`` or ``plans`` are staged, and only what is
    staged is uploaded - a day already in the Drive is not touched again.
    """
    days: dict[date, Path] = {}
    for entry, source in clips:
        directory = root / day_folder(entry.publish_date) / destination_folder(entry.destinations)
        directory.mkdir(parents=True, exist_ok=True)
        days[entry.publish_date] = directory.parent
        target = directory / entry.filename
        if target.exists():
            continue
        if not source.exists():
            raise FileNotFoundError(f"render nao encontrado para a fila: {source}")
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    for when, plan in plans.items():
        directory = days.get(when) or (root / day_folder(when))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / plan_filename).write_text(plan, encoding="utf-8")
        days[when] = directory
    return [StagedDay(when, days[when]) for when in sorted(days)]


def render_source(clip_dir: Path) -> Path:
    """The rendered file a queued clip is linked from."""
    return clip_dir / RENDER_FILENAME
