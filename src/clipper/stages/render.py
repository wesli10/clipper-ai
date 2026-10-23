"""Stage 3 - rendering (SPEC 7.3).

Pipeline per clip: extract intervals -> join with crossfades -> reframe -> zoom
-> scale -> burn subtitles -> normalise audio -> encode -> verify.
Filter order is fixed by CLAUDE.md: reframe -> zoom -> scale -> ass.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from clipper.config import Settings
from clipper.models import Clip, TimeRange, Transcript
from clipper.profiles import Profile
from clipper.utils.errors import RenderError
from clipper.utils.logging import stage_logger
from clipper.video import concat as concat_mod
from clipper.video import ffmpeg, subtitles
from clipper.video import outro as outro_mod

#: How far the rendered duration may drift from the expected one (SPEC 7.3 step 7).
DURATION_TOLERANCE_S = 0.3


@dataclass(frozen=True, slots=True)
class ZoomContext:
    """What ``punch_in`` needs to place its keyframes on sentence boundaries."""

    transcript: Transcript
    intervals: list[TimeRange]
    offsets: list[float]
    duration: float


@dataclass(frozen=True, slots=True)
class RenderResult:
    clip_dir: Path
    video_path: Path
    thumbnail_path: Path
    subtitles_path: Path | None
    offsets: list[float]
    duration: float
    width: int
    height: int
    degraded: bool
    #: True when the file was already there and nothing was encoded again.
    reused: bool = False


#: What the card asks when the model did not write anything for it.
DEFAULT_CARD_TEXT = {
    "pt": "O que voce acha que ele respondeu?",
    "en": "What do you think the answer was?",
    "es": "Que crees que respondio?",
}


def card_overlay(
    clip: Clip, language: str, intervals: list[TimeRange], offsets: list[float]
) -> subtitles.Card:
    """Where the held frame sits in the rendered timeline.

    The hold was appended to the first part, so it starts where that part's real
    footage ends and runs until the second part's offset - which is already short
    by the crossfade, so the card is gone before the answer starts blending in.
    """
    start = intervals[0].duration
    text = clip.card_text.strip() or DEFAULT_CARD_TEXT.get(language, DEFAULT_CARD_TEXT["pt"])
    return subtitles.Card(start=round(start, 3), end=offsets[1], text=text)


#: Reframe modes that leave a bar above the frame instead of filling it.
BARRED_REFRAMES = ("blur", "letterbox")


def banner_position(profile: Profile, media: ffmpeg.MediaInfo, *, reframe: str) -> int | None:
    """Vertical centre for the comment question, or ``None`` when it has no room.

    ``blur`` and ``letterbox`` fit the whole source inside the canvas, so the
    space above it is exactly ``(H - fitted height) / 2``. Every other mode fills
    the canvas with image, and a question drawn over it would sit on somebody's
    face - so the profile's fallback position is used only if it was configured
    deliberately, never as a guess.
    """
    height = profile.output_height
    if reframe in BARRED_REFRAMES and media.width > 0 and media.height > 0:
        fitted = min(height, round(profile.output_width * media.height / media.width))
        bar = (height - fitted) / 2
        if bar >= profile.banner.min_bar_pct * height:
            return round(bar / 2)
    return None


def runway_interval(last: TimeRange, roll: float, source_duration: float) -> TimeRange:
    """Extend the final interval into the source, without running off the end.

    ``source_duration`` of 0 means the transcript never reported one; the roll is
    still applied, and ffmpeg simply stops at the real end of the file.
    """
    if roll <= 0:
        return last
    ceiling = source_duration if source_duration > 0 else last.end + roll
    return TimeRange(start=last.start, end=min(ceiling, last.end + roll))


def clip_dir_for(settings: Settings, video_id: str, profile: Profile, clip: Clip) -> Path:
    return settings.profile_dir(video_id, profile.name) / "clips" / clip.dir_name


# --------------------------------------------------------------------------- #
# Filter chain
# --------------------------------------------------------------------------- #


def output_size(profile: Profile, media: ffmpeg.MediaInfo) -> tuple[int, int]:
    """Resolution of the rendered file.

    ``reframe: none`` keeps the source aspect and never upscales (SPEC 4.2);
    every cropping mode produces the profile resolution exactly.
    """
    if profile.reframe == "none":
        return ffmpeg.fit_size(
            media.width, media.height, profile.output_width, profile.output_height
        )
    # letterbox, like the cropping modes, fills the profile canvas exactly - the
    # bars are part of the output, not an absence of it.
    return ffmpeg.even(profile.output_width), ffmpeg.even(profile.output_height)


def build_video_filters(
    profile: Profile,
    media: ffmpeg.MediaInfo,
    *,
    width: int,
    height: int,
    reframe: str,
    zoom: str,
    subtitles_path: Path | None,
    face_filter: str | None = None,
    zoom_context: ZoomContext | None = None,
) -> list[str]:
    """Assemble the chain in the mandatory order."""
    filters: list[str] = []

    # 1. reframe
    if reframe == "face" and face_filter:
        filters.append(face_filter)
        filters.append(ffmpeg.scale_filter(width, height))
    elif reframe in ("face", "center"):
        filters.append(ffmpeg.center_crop_filter(width, height))
    elif reframe == "blur":
        filters.append(
            ffmpeg.blur_pad_filter(width, height, profile.blur_brightness, profile.blur_saturation)
        )
    elif reframe == "letterbox":
        filters.append(ffmpeg.letterbox_filter(width, height, profile.letterbox_color))
    else:  # "none"
        filters.append(ffmpeg.scale_filter(width, height))

    # 2. zoom (only ever over an already-cropped frame)
    if zoom == "punch_in":
        from clipper.video import zoom as zoom_mod

        filters.append(
            zoom_mod.punch_in_filter(
                width,
                height,
                transcript=zoom_context.transcript if zoom_context else None,
                intervals=zoom_context.intervals if zoom_context else None,
                offsets=zoom_context.offsets if zoom_context else None,
                duration=zoom_context.duration if zoom_context else None,
            )
        )

    # 3. scale is already part of every branch above; 4. subtitles last
    if subtitles_path is not None:
        filters.append(ffmpeg.subtitles_filter(subtitles_path))
    return filters


# --------------------------------------------------------------------------- #
# Rendering one clip
# --------------------------------------------------------------------------- #


def render_clip(
    settings: Settings,
    video_id: str,
    clip: Clip,
    profile: Profile,
    transcript: Transcript,
    *,
    force: bool = False,
    logger: logging.Logger | None = None,
) -> RenderResult:
    """Render a single clip, retrying once in a degraded mode on failure."""
    log = logger or stage_logger(settings.logs_dir(video_id), "render", profile.name)
    source = settings.video_dir(video_id) / "source.mp4"
    if not source.exists():
        raise RenderError(f"video de origem nao encontrado: {source}")

    out_dir = clip_dir_for(settings, video_id, profile, clip)
    video_path = out_dir / "clip.mp4"
    if video_path.exists() and not force:
        media = ffmpeg.probe(video_path, logger=log)
        log.info("clip %s ja renderizado, reaproveitando", clip.dir_name)
        return RenderResult(
            clip_dir=out_dir,
            video_path=video_path,
            thumbnail_path=out_dir / "thumbnail.jpg",
            subtitles_path=(out_dir / "subtitles.ass" if profile.subtitle_style != "off" else None),
            offsets=clip.offsets or [0.0],
            duration=media.duration,
            width=media.width,
            height=media.height,
            degraded=False,
            reused=True,
        )

    try:
        return _render(settings, source, clip, profile, transcript, out_dir, log, degraded=False)
    except RenderError as exc:
        log.warning("render de %s falhou (%s); tentando modo degradado", clip.dir_name, exc)
        return _render(settings, source, clip, profile, transcript, out_dir, log, degraded=True)


def _render(
    settings: Settings,
    source: Path,
    clip: Clip,
    profile: Profile,
    transcript: Transcript,
    out_dir: Path,
    log: logging.Logger,
    *,
    degraded: bool,
) -> RenderResult:
    """One render attempt. ``degraded`` drops face tracking, zoom and the end card.

    The card is cosmetic and the clip is not: a missing or unreadable outro file
    must cost the card, never the render.
    """
    reframe = "center" if degraded and profile.reframe == "face" else profile.reframe
    zoom = "none" if degraded else profile.zoom

    work = out_dir / "intermediates"
    if work.exists():
        shutil.rmtree(work)

    # The quiz format splits its one interval at the reveal, so the parts are not
    # the clip's segments any more: the first one carries the held card frame.
    card_hold = profile.card_seconds if clip.reveal_at is not None else 0.0
    intervals = list(clip.segments)
    if card_hold > 0:
        whole = clip.segments[0]
        intervals = [
            TimeRange(start=whole.start, end=clip.reveal_at or whole.end),
            TimeRange(start=clip.reveal_at or whole.end, end=whole.end),
        ]

    # The runway into the end card. The clip's own ending does not move: only
    # the rendered tail is longer, and `intervals` - what the captions are built
    # from - keeps the chosen end, so speech inside the runway stays silent on
    # screen.
    rendered = list(intervals)
    if profile.tail_roll_s > 0:
        rendered[-1] = runway_interval(rendered[-1], profile.tail_roll_s, transcript.duration)

    single = len(rendered) == 1
    if single:
        # One interval: the cut is just a trim on the final encode. Extracting it
        # first would encode the whole clip twice for nothing.
        trim: tuple[float, float] | None = (rendered[0].start, rendered[0].end)
        joined = concat_mod.ConcatResult(path=source, offsets=[0.0], duration=rendered[0].duration)
        media = ffmpeg.probe(source, logger=log)
    else:
        trim = None
        work.mkdir(parents=True, exist_ok=True)

        # Step 1 - frame-exact extraction of each interval.
        parts: list[Path] = []
        for index, interval in enumerate(rendered, start=1):
            part = work / f"{index:02d}.mp4"
            ffmpeg.run(
                ffmpeg.cut_args(
                    source,
                    part,
                    interval.start,
                    interval.end,
                    crf=settings.render.intermediate_crf,
                    preset=settings.render.preset,
                    hold=card_hold if index == 1 else 0.0,
                ),
                logger=log,
                description=f"cut {index}",
            )
            parts.append(part)

        # Step 2 - join with crossfades; offsets feed chapters and subtitles.
        joined = concat_mod.concat(
            parts,
            work / "joined.mp4",
            transition=settings.render.transition_s,
            crf=settings.render.intermediate_crf,
            preset=settings.render.preset,
            logger=log,
        )
        media = ffmpeg.probe(joined.path, logger=log)
    width, height = output_size(profile, media)

    # Step 3 - face tracking, when the profile asks for it.
    face_filter: str | None = None
    if reframe == "face":
        from clipper.video import reframe as reframe_mod

        try:
            face_filter = reframe_mod.face_crop_filter(
                settings,
                source_video_id=transcript.video_id,
                clip=clip,
                offsets=joined.offsets,
                media=media,
                target_aspect=profile.output_width / profile.output_height,
                logger=log,
            )
        except Exception as exc:
            # Any detector problem - missing package, changed API, corrupt model -
            # is a reason to fall back, never a reason to lose the clip.
            log.warning("deteccao de rosto indisponivel (%s); caindo para center", exc)
            face_filter = None
        if face_filter is None:
            log.warning("sem rostos utilizaveis em %s; caindo para center", clip.dir_name)
            reframe = "center"

    # Step 5 - subtitles, shifted by the concat offsets.
    card = (
        card_overlay(clip, transcript.language, intervals, joined.offsets)
        if card_hold > 0
        else None
    )
    banner: subtitles.Banner | None = None
    if profile.banner.enabled and clip.comment_prompt.strip():
        y = banner_position(profile, media, reframe=reframe)
        if y is None:
            log.info("sem barra util para a pergunta de comentario em %s", clip.dir_name)
        else:
            banner = subtitles.Banner(text=clip.comment_prompt.strip(), y=y, end=joined.duration)
    subtitles_path: Path | None = None
    if profile.subtitle_style != "off":
        subtitles_path = out_dir / "subtitles.ass"
        subtitles_path.parent.mkdir(parents=True, exist_ok=True)
        subtitles_path.write_text(
            subtitles.build_ass(
                transcript, intervals, joined.offsets, profile, card=card, banner=banner
            ),
            encoding="utf-8",
        )

    # Two passes for the audio: measure the exact cut, then normalise with what
    # was measured. One pass only approximates the target, and the verification
    # of SPEC-agent 6 was failing clips that a re-render could not fix.
    loudness = measure_loudness(joined.path, profile, trim=trim, log=log)

    filters = build_video_filters(
        profile,
        media,
        width=width,
        height=height,
        reframe=reframe,
        zoom=zoom,
        subtitles_path=subtitles_path,
        face_filter=face_filter,
        zoom_context=ZoomContext(
            transcript=transcript,
            intervals=intervals,
            offsets=joined.offsets,
            duration=joined.duration,
        ),
    )

    video_path = out_dir / "clip.mp4"
    tail = (
        None
        if degraded
        else outro_mod.prepare(
            profile,
            settings.workspace.expanduser() / "cache",
            crf=settings.render.intermediate_crf,
            preset=settings.render.preset,
            audio_bitrate=settings.render.audio_bitrate,
            logger=log,
        )
    )

    # With an end card the filtered pass is an intermediate: the card is already
    # at output resolution and must not go through reframe, zoom or subtitles.
    body = video_path
    expected = joined.duration
    if tail is not None:
        work.mkdir(parents=True, exist_ok=True)
        body = work / "body.mp4"

    ffmpeg.run(
        ffmpeg.encode_args(
            joined.path,
            body,
            video_filters=filters,
            audio_filters=[ffmpeg.loudnorm_filter(profile.loudnorm_i, loudness)],
            crf=settings.render.intermediate_crf if tail is not None else settings.render.crf,
            preset=settings.render.preset,
            fps=profile.fps,
            audio_bitrate=settings.render.audio_bitrate,
            trim=trim,
        ),
        logger=log,
        description="encode",
    )

    if tail is not None:
        joined_tail = concat_mod.concat(
            [body, tail],
            video_path,
            transition=settings.render.transition_s,
            crf=settings.render.crf,
            preset=settings.render.preset,
            logger=log,
        )
        expected = joined_tail.duration

    thumbnail_path = out_dir / "thumbnail.jpg"
    ffmpeg.run(
        ffmpeg.thumbnail_args(video_path, thumbnail_path, at=settings.render.thumbnail_at_s),
        logger=log,
        description="thumbnail",
    )

    verify(video_path, expected_duration=expected, width=width, height=height, logger=log)
    shutil.rmtree(work, ignore_errors=True)

    final = ffmpeg.probe(video_path, logger=log)
    return RenderResult(
        clip_dir=out_dir,
        video_path=video_path,
        thumbnail_path=thumbnail_path,
        subtitles_path=subtitles_path,
        # The quiz parts are not the clip's segments, so reporting their offsets
        # would give packaging two chapters for a one-interval clip.
        offsets=[0.0] if card_hold > 0 else joined.offsets,
        duration=final.duration,
        width=final.width,
        height=final.height,
        degraded=degraded,
    )


def measure_loudness(
    source: Path,
    profile: Profile,
    *,
    trim: tuple[float, float] | None,
    log: logging.Logger,
) -> ffmpeg.LoudnessStats | None:
    """Measure once so the normalisation can be exact; ``None`` if it cannot.

    A failed measurement costs accuracy, never the clip: the render falls back to
    the one-pass filter, which is what it always did.
    """
    try:
        result = ffmpeg.run(
            ffmpeg.loudnorm_measure_args(source, target_i=profile.loudnorm_i, trim=trim),
            logger=log,
            description="loudnorm (medicao)",
        )
    except RenderError as exc:
        log.warning("medicao de loudness falhou (%s); normalizando em uma passada", exc)
        return None
    stats = ffmpeg.parse_loudnorm_stats(result.stderr)
    if stats is None:
        log.warning("loudnorm nao imprimiu a medicao; normalizando em uma passada")
    else:
        log.info("loudness medida: I=%s LUFS, TP=%s", stats.input_i, stats.input_tp)
    return stats


def verify(
    path: Path,
    *,
    expected_duration: float,
    width: int,
    height: int,
    logger: logging.Logger | None = None,
) -> ffmpeg.MediaInfo:
    """Check duration, resolution and the audio track (SPEC 7.3 step 7)."""
    media = ffmpeg.probe(path, logger=logger)
    problems: list[str] = []
    if abs(media.duration - expected_duration) > DURATION_TOLERANCE_S:
        problems.append(
            f"duracao {media.duration:.2f}s difere da esperada {expected_duration:.2f}s "
            f"(tolerancia {DURATION_TOLERANCE_S}s)"
        )
    if (media.width, media.height) != (width, height):
        problems.append(
            f"resolucao {media.width}x{media.height} difere da esperada {width}x{height}"
        )
    if not media.has_audio:
        problems.append("o arquivo nao tem faixa de audio")
    if problems:
        raise RenderError(f"verificacao de {path.name} falhou: " + "; ".join(problems))
    return media
