"""Deterministic review of a rendered clip (SPEC-agent 6).

No model looks at a frame here. Everything is geometry, timing and what ffprobe
says - so the same clip always gets the same verdict, and the whole thing is
testable without a GPU, a network or an API key.

The checks that need a cropping reframe (``face_in_frame``, ``face_coverage``)
are dormant while face tracking is off (SPEC-agent 2.1): they are skipped, not
removed, and wake up by themselves the day the profile crops again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from clipper.agent.models import CheckStatus, CheckView, ReviewResult
from clipper.config import Settings
from clipper.models import Clip, TimeRange, Transcript
from clipper.profiles import Profile
from clipper.stages.render import runway_interval
from clipper.stages.select import ends_on_question, is_closing, opens_well, sentences
from clipper.utils.errors import RenderError
from clipper.video import ffmpeg
from clipper.video.concat import offsets_for
from clipper.video.faces import FaceSample, FaceTrack
from clipper.video.reframe import MIN_FACE_COVERAGE, Scene, coverage, smooth_centers, to_steps

#: How far the rendered duration may sit from the computed one.
DURATION_TOLERANCE_S = 0.5
#: Loudness this far from the profile target means the normalisation did not take.
LOUDNESS_TOLERANCE_LU = 1.0
#: The first caption may not start later than this into the clip.
CAPTION_START_MAX_S = 1.0
#: Below this many caption events per second of speech, the captions are dropping out.
MIN_CAPTION_DENSITY = 0.15
#: A line may exceed the configured width by this much before it counts as broken.
LINE_SLACK = 1.10
#: Average luma of the thumbnail outside this band means a black or blown frame.
DARK_LUMA = 8.0
BLOWN_LUMA = 250.0
#: Share of face samples whose centre must fall inside the crop window.
MIN_FACE_IN_FRAME = 0.80
#: How much of a face box may sit outside the crop before it reads as cut off.
MAX_FACE_OUTSIDE = 0.25
#: Reframe modes that discard part of the frame, and so can lose a face.
CROPPING_REFRAMES = ("face", "center")

_TAG_RE = re.compile(r"\{[^}]*\}")
_POS_RE = re.compile(r"\\pos\(\s*-?\d+\s*,\s*(-?\d+)\s*\)")


def ok(check: str, value: str = "", expected: str = "") -> CheckView:
    return CheckView(check=check, status="ok", value=value, expected=expected)


def warn(check: str, value: str, expected: str, fix: str = "") -> CheckView:
    return CheckView(check=check, status="warn", value=value, expected=expected, fix=fix)


def fail(check: str, value: str, expected: str, fix: str = "") -> CheckView:
    return CheckView(check=check, status="fail", value=value, expected=expected, fix=fix)


# --------------------------------------------------------------------------- #
# Reading the .ass back
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AssEvent:
    start: float
    end: float
    style: str
    text: str

    @property
    def lines(self) -> list[str]:
        """Visible lines, with override tags and drawing commands removed."""
        plain = _TAG_RE.sub("", self.text)
        return [part.strip() for part in plain.split("\\N") if part.strip()]


def parse_ass_time(value: str) -> float:
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_ass_events(document: str) -> list[AssEvent]:
    """Read back the events of a ``.ass`` we wrote (SPEC 7.3 step 5)."""
    events: list[AssEvent] = []
    for line in document.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        # Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        fields = line[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            continue
        try:
            start, end = parse_ass_time(fields[1]), parse_ass_time(fields[2])
        except ValueError:
            continue
        events.append(AssEvent(start=start, end=end, style=fields[3].strip(), text=fields[9]))
    return events


# --------------------------------------------------------------------------- #
# What the checks run on
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RenderFacts:
    """Everything measured from disk, gathered once so the checks stay pure."""

    clip_id: str
    media: ffmpeg.MediaInfo
    expected_duration: float
    speech_duration: float
    loudness: float | None = None
    thumbnail_luma: float | None = None
    events: list[AssEvent] = field(default_factory=list)
    face_track: FaceTrack | None = None
    skipped: list[str] = field(default_factory=list)


def rendered_intervals(clip: Clip, profile: Profile, transcript: Transcript) -> list[TimeRange]:
    """The parts the render actually cut, including the quiz split (SPEC 7.3)."""
    intervals = list(clip.segments)
    if profile.card_seconds > 0 and clip.reveal_at is not None:
        whole = intervals[0]
        intervals = [
            TimeRange(start=whole.start, end=clip.reveal_at),
            TimeRange(start=clip.reveal_at, end=whole.end),
        ]
    if profile.tail_roll_s > 0:
        intervals[-1] = runway_interval(intervals[-1], profile.tail_roll_s, transcript.duration)
    return intervals


def expected_duration(
    clip: Clip,
    profile: Profile,
    transcript: Transcript,
    *,
    transition: float,
    outro_duration: float = 0.0,
) -> float:
    """What the rendered file should last, by the same arithmetic the render uses.

    ``offsets_for`` is the one place that knows a crossfade overlaps two parts,
    so this reuses it instead of restating the rule and drifting from it.
    """
    intervals = rendered_intervals(clip, profile, transcript)
    durations = [interval.duration for interval in intervals]
    if profile.card_seconds > 0 and clip.reveal_at is not None:
        durations[0] += profile.card_seconds
    _, body = offsets_for(durations, transition)
    if outro_duration > 0:
        _, total = offsets_for([body, outro_duration], transition)
        return total
    return body


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def check_output(facts: RenderFacts, profile: Profile) -> list[CheckView]:
    """Duration, resolution, audio track and loudness of the delivered file."""
    media = facts.media
    checks = [
        (
            ok("duration", f"{media.duration:.2f}s", f"{facts.expected_duration:.2f}s")
            if abs(media.duration - facts.expected_duration) <= DURATION_TOLERANCE_S
            else fail(
                "duration",
                f"{media.duration:.2f}s",
                f"{facts.expected_duration:.2f}s (tolerancia {DURATION_TOLERANCE_S:g}s)",
                "refazer o render",
            )
        )
    ]

    expected_size = f"{profile.output_width}x{profile.output_height}"
    size = f"{media.width}x{media.height}"
    if profile.reframe == "none":
        # The long never upscales, so anything that fits inside the canvas is right.
        good = 0 < media.width <= profile.output_width and 0 < media.height <= profile.output_height
        expected_size = f"ate {expected_size}, sem upscale"
    else:
        good = size == expected_size
    checks.append(
        ok("resolution", size, expected_size)
        if good
        else fail("resolution", size, expected_size, "refazer o render")
    )

    checks.append(
        ok("audio_track", "presente")
        if media.has_audio
        else fail("audio_track", "ausente", "uma faixa de audio", "refazer o render")
    )

    if facts.loudness is None:
        checks.append(warn("loudness", "nao medida", f"{profile.loudnorm_i:g} LUFS"))
    else:
        drift = abs(facts.loudness - profile.loudnorm_i)
        checks.append(
            ok("loudness", f"{facts.loudness:.1f} LUFS", f"{profile.loudnorm_i:g} LUFS")
            if drift <= LOUDNESS_TOLERANCE_LU
            else fail(
                "loudness",
                f"{facts.loudness:.1f} LUFS",
                f"{profile.loudnorm_i:g} LUFS (tolerancia {LOUDNESS_TOLERANCE_LU:g} LU)",
                "refazer o render",
            )
        )
    return checks


def check_captions(facts: RenderFacts, profile: Profile) -> list[CheckView]:
    """Sync, density, line width and collision with the comment banner."""
    if profile.subtitle_style == "off":
        return []
    captions = [e for e in facts.events if e.style == "Default"]
    if not captions:
        return [fail("caption_sync", "nenhum evento", "legenda queimada", "refazer o render")]

    checks: list[CheckView] = []
    first, last = captions[0].start, captions[-1].end
    late = first > CAPTION_START_MAX_S
    spills = last > facts.speech_duration + 0.5
    if late or spills:
        value = f"primeira em {first:.2f}s, ultima termina em {last:.2f}s"
        checks.append(
            fail(
                "caption_sync",
                value,
                f"primeira ate {CAPTION_START_MAX_S:g}s, ultima ate {facts.speech_duration:.2f}s",
                "refazer o render; se persistir, subtitle_style: off",
            )
        )
    else:
        checks.append(ok("caption_sync", f"{first:.2f}s a {last:.2f}s"))

    density = len(captions) / facts.speech_duration if facts.speech_duration > 0 else 0.0
    checks.append(
        ok("caption_density", f"{density:.2f} eventos/s")
        if density >= MIN_CAPTION_DENSITY
        else fail(
            "caption_density",
            f"{density:.2f} eventos/s",
            f"ao menos {MIN_CAPTION_DENSITY:g}/s",
            "refazer o render",
        )
    )

    style = profile.subtitle
    limit = round(style.max_chars_per_line * LINE_SLACK)
    widest, wide_text = 0, ""
    most_lines = 0
    for event in captions:
        lines = event.lines
        most_lines = max(most_lines, len(lines))
        for line in lines:
            if len(line) > widest:
                widest, wide_text = len(line), line
    if widest > limit or most_lines > style.max_lines:
        checks.append(
            warn(
                "caption_fit",
                f"{widest} chars em {most_lines} linha(s): {wide_text[:40]!r}",
                f"ate {limit} chars e {style.max_lines} linha(s)",
                "ajustar subtitle.max_chars_per_line no config (fora da lista branca)",
            )
        )
    else:
        checks.append(ok("caption_fit", f"{widest} chars, {most_lines} linha(s)"))

    checks.extend(_banner_collision(facts, profile))
    return checks


def _banner_collision(facts: RenderFacts, profile: Profile) -> list[CheckView]:
    """The comment question must not sit on top of the captions."""
    banners = [e for e in facts.events if e.style == "Banner"]
    if not banners:
        return []
    match = _POS_RE.search(banners[0].text)
    if match is None:
        return []

    height = profile.output_height
    banner_size = max(10, round(profile.banner.size_pct * height))
    centre = int(match.group(1))
    banner_bottom = centre + banner_size / 2

    margin_v = max(10, round((1.0 - profile.subtitle.vertical_pos_pct) * height))
    caption_lines = max(1, profile.subtitle.max_lines)
    caption_top = height - margin_v - profile.subtitle.size * caption_lines

    if banner_bottom >= caption_top:
        return [
            fail(
                "caption_collision",
                f"pergunta ate y={banner_bottom:.0f}, legenda comeca em y={caption_top:.0f}",
                "faixas separadas",
                "banner.enabled: false",
            )
        ]
    return [ok("caption_collision", f"{caption_top - banner_bottom:.0f}px de folga")]


def check_boundaries(clip: Clip, profile: Profile, transcript: Transcript) -> list[CheckView]:
    """The opening holds on its own, the ending closes, and neither cuts a word."""
    units = sentences(transcript)
    if not units:
        return []
    first, last = clip.segments[0], clip.segments[-1]
    checks: list[CheckView] = []

    start_index = next((i for i, s in enumerate(units) if s.end > first.start + 0.05), None)
    if start_index is not None:
        opening = units[start_index].text.strip()
        good = opens_well(units, start_index, mode=profile.boundary_mode)
        checks.append(
            ok("opening", opening[:60])
            if good
            else fail(
                "opening",
                opening[:60],
                "uma frase que prende sozinha",
                "snap_clip com outro inicio",
            )
        )

    end_index = next((i for i in range(len(units) - 1, -1, -1) if units[i].start < last.end), None)
    if end_index is not None:
        closing = units[end_index].text.strip()
        if ends_on_question(transcript, last.end):
            checks.append(
                fail(
                    "closing",
                    closing[:60],
                    "um fecho, nao uma pergunta em aberto",
                    "snap_clip estendendo o fim ate a resposta",
                )
            )
        elif not is_closing(units, end_index):
            checks.append(
                fail("closing", closing[:60], "um fim que fecha o assunto", "snap_clip no fim")
            )
        else:
            checks.append(ok("closing", closing[:60]))

    gap_before, gap_after = edge_gaps(transcript, first.start, last.end)
    # What matters is that no word is cut in half, not that there is silence to
    # spare: the padding of SPEC 6.1 deliberately runs up to the neighbouring
    # word, so requiring a minimum gap would flag every correctly padded clip.
    cut_open = cuts_a_word(transcript, first.start)
    cut_close = cuts_a_word(transcript, last.end)
    where = " e ".join([side for side, cut in (("inicio", cut_open), ("fim", cut_close)) if cut])
    if where:
        checks.append(
            fail(
                "cut_in_pause",
                f"corta uma palavra no {where}",
                "as bordas caem entre palavras",
                "snap_clip",
            )
        )
    else:
        checks.append(ok("cut_in_pause", f"{gap_before:.2f}s / {gap_after:.2f}s de folga"))
    return checks


def cuts_a_word(transcript: Transcript, instant: float) -> bool:
    """Whether a word is being spoken at ``instant`` - the cut lands inside it."""
    return any(word.start < instant < word.end for word in transcript.words())


def edge_gaps(transcript: Transcript, start: float, end: float) -> tuple[float, float]:
    """Silence at each edge of the cut - zero when a word straddles it.

    A word that starts before the cut and ends after it *is* the failure this
    check exists for, and measuring the distance to the previous word's end would
    report a comfortable pause while the cut lands mid-syllable.
    """
    words = transcript.words()
    before = start
    after = max(0.0, transcript.duration - end) if transcript.duration else 1.0

    for word in words:
        if word.start < start < word.end:
            before = 0.0
        elif word.end <= start + 1e-6:
            before = start - word.end

    for word in words:
        if word.start < end < word.end:
            after = 0.0
            break
        if word.start >= end - 1e-6:
            after = word.start - end
            break
    return max(0.0, before), max(0.0, after)


def check_tail_roll(clip: Clip, profile: Profile, transcript: Transcript) -> list[CheckView]:
    """Whether the runway runs into the next subject.

    The roll is ambience by design and its speech is deliberately left
    uncaptioned - but the viewer still hears it, and on a panel where subjects
    run together the clip ends inside the next one. Found on the first real agent
    run: every other check passed and the short finished on somebody starting a
    different story.
    """
    if profile.tail_roll_s <= 0:
        return []
    last = clip.segments[-1]
    runway = runway_interval(last, profile.tail_roll_s, transcript.duration)
    room = runway.end - last.end
    if room <= 0:
        return []

    intruding = next((s for s in sentences(transcript) if last.end < s.start < runway.end), None)
    if intruding is None:
        return [ok("tail_roll", f"{room:.1f}s de passarela sem fala nova")]
    return [
        warn(
            "tail_roll",
            f'a passarela entra em "{intruding.text.strip()[:50]}"',
            "passarela sem fala nova",
            f"tail_roll_s abaixo de {intruding.start - last.end:.1f}s",
        )
    ]


def check_first_frame(facts: RenderFacts) -> list[CheckView]:
    if facts.thumbnail_luma is None:
        return []
    luma = facts.thumbnail_luma
    if luma < DARK_LUMA or luma > BLOWN_LUMA:
        return [
            fail(
                "first_frame",
                f"luma media {luma:.0f}",
                f"entre {DARK_LUMA:g} e {BLOWN_LUMA:g}",
                "render.thumbnail_at_s maior, ou outro inicio",
            )
        ]
    return [ok("first_frame", f"luma media {luma:.0f}")]


def check_quiz(clip: Clip, profile: Profile) -> list[CheckView]:
    if profile.card_seconds <= 0:
        return []
    if clip.reveal_at is None:
        return [fail("quiz_parts", "sem reveal_at", "uma revelacao dentro do corte", "snap_clip")]
    whole = clip.segments[0]
    lead = clip.reveal_at - whole.start
    tail = whole.end - clip.reveal_at
    minimum = profile.card_min_part_s
    if min(lead, tail) < minimum:
        return [
            fail(
                "quiz_parts",
                f"{lead:.1f}s de pergunta, {tail:.1f}s de resposta",
                f"ao menos {minimum:g}s de cada lado",
                "snap_clip movendo o reveal",
            )
        ]
    return [ok("quiz_parts", f"{lead:.1f}s / {tail:.1f}s")]


def check_faces(facts: RenderFacts, clip: Clip, profile: Profile) -> list[CheckView]:
    """Dormant unless the profile crops: without a crop nothing can leave the frame."""
    if profile.reframe not in CROPPING_REFRAMES or facts.face_track is None:
        return []
    track = facts.face_track
    samples: list[FaceSample] = []
    for interval in clip.segments:
        samples.extend(track.between(interval.start, interval.end))
    if not samples:
        return []

    share = coverage(samples)
    checks = [
        ok("face_coverage", f"{share:.0%}")
        if share >= MIN_FACE_COVERAGE
        else fail(
            "face_coverage",
            f"{share:.0%}",
            f"ao menos {MIN_FACE_COVERAGE:.0%}",
            "reframe: blur (mantem o quadro inteiro)",
        )
    ]

    width = track.width or facts.media.width
    height = track.height or facts.media.height
    if not width or not height:
        return checks

    target_aspect = profile.output_width / profile.output_height
    centres = smooth_centers(samples, target_aspect=target_aspect, frame_aspect=width / height)
    steps = to_steps(centres, end=samples[-1].t)
    crop_width = min(1.0, target_aspect / (width / height))

    inside = 0
    counted = 0
    cut_off = 0
    for sample in samples:
        face = sample.largest
        if face is None:
            continue
        counted += 1
        centre_x = _crop_centre_at(steps, sample.t)
        left, right = centre_x - crop_width / 2, centre_x + crop_width / 2
        if left <= face.cx <= right:
            inside += 1
        outside = max(0.0, left - (face.cx - face.width / 2)) + max(
            0.0, (face.cx + face.width / 2) - right
        )
        if face.width > 0 and outside / face.width > MAX_FACE_OUTSIDE:
            cut_off += 1

    if counted:
        share_inside = inside / counted
        if share_inside < MIN_FACE_IN_FRAME or cut_off:
            checks.append(
                fail(
                    "face_in_frame",
                    f"{share_inside:.0%} enquadrados, {cut_off} amostra(s) cortada(s)",
                    f"ao menos {MIN_FACE_IN_FRAME:.0%} e nenhuma cortada",
                    "reframe: blur",
                )
            )
        else:
            checks.append(ok("face_in_frame", f"{share_inside:.0%}"))
    return checks


def _crop_centre_at(steps: list[Scene], t: float) -> float:
    """Where the crop sat at instant ``t``, by the same steps the filter uses."""
    for scene in steps:
        if scene.start <= t < scene.end:
            return scene.center_x
    return steps[-1].center_x if steps else 0.5


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def gather(
    settings: Settings,
    video_id: str,
    clip: Clip,
    profile: Profile,
    transcript: Transcript,
    *,
    logger: logging.Logger | None = None,
) -> RenderFacts:
    """Measure everything the checks need, once."""
    from clipper.stages.render import clip_dir_for

    clip_dir = clip_dir_for(settings, video_id, profile, clip)
    video_path = clip_dir / "clip.mp4"
    if not video_path.exists():
        raise RenderError(
            f"corte {profile.name} {clip.index:02d} ainda nao foi renderizado "
            "(rode render_clip antes de revisar)"
        )

    skipped: list[str] = []
    media = ffmpeg.probe(video_path, logger=logger)

    outro_duration = 0.0
    if profile.outro is not None and profile.outro.exists():
        outro_duration = ffmpeg.probe(profile.outro, logger=logger).duration

    loudness: float | None = None
    if media.has_audio:
        try:
            result = ffmpeg.run(
                ffmpeg.loudness_args(video_path), logger=logger, description="ebur128"
            )
            loudness = ffmpeg.parse_loudness(result.stderr)
        except RenderError as exc:
            skipped.append(f"loudness: {exc}")

    thumbnail = clip_dir / "thumbnail.jpg"
    luma: float | None = None
    if thumbnail.exists():
        try:
            probe = ffmpeg.run(
                ffmpeg.frame_luma_args(thumbnail), logger=logger, description="signalstats"
            )
            luma = ffmpeg.parse_frame_luma(probe.stdout)
        except RenderError as exc:
            skipped.append(f"first_frame: {exc}")

    events: list[AssEvent] = []
    subtitles = clip_dir / "subtitles.ass"
    if profile.subtitle_style != "off" and subtitles.exists():
        events = parse_ass_events(subtitles.read_text(encoding="utf-8"))

    track: FaceTrack | None = None
    faces_path = settings.video_dir(video_id) / "faces.json"
    if profile.reframe in CROPPING_REFRAMES:
        if faces_path.exists():
            track = FaceTrack.model_validate_json(faces_path.read_text(encoding="utf-8"))
        else:
            skipped.append("face_in_frame: sem faces.json (deteccao nunca rodou)")
    else:
        skipped.append(f"face_in_frame: reframe '{profile.reframe}' nao recorta o quadro")

    return RenderFacts(
        clip_id=clip_id_for(video_id, profile.name, clip.index),
        media=media,
        expected_duration=expected_duration(
            clip,
            profile,
            transcript,
            transition=settings.render.transition_s,
            outro_duration=outro_duration,
        ),
        speech_duration=clip.duration,
        loudness=loudness,
        thumbnail_luma=luma,
        events=events,
        face_track=track,
        skipped=skipped,
    )


def clip_id_for(video_id: str, profile_name: str, index: int) -> str:
    return f"{video_id}/{profile_name}/{index}"


def verdict_of(checks: list[CheckView]) -> CheckStatus:
    if any(c.status == "fail" for c in checks):
        return "fail"
    return "warn" if any(c.status == "warn" for c in checks) else "ok"


def review_clip(
    settings: Settings,
    video_id: str,
    clip: Clip,
    profile: Profile,
    transcript: Transcript,
    *,
    logger: logging.Logger | None = None,
) -> ReviewResult:
    """Full report for one rendered clip."""
    facts = gather(settings, video_id, clip, profile, transcript, logger=logger)
    checks: list[CheckView] = []
    checks += check_output(facts, profile)
    checks += check_captions(facts, profile)
    checks += check_boundaries(clip, profile, transcript)
    checks += check_faces(facts, clip, profile)
    checks += check_tail_roll(clip, profile, transcript)
    checks += check_first_frame(facts)
    checks += check_quiz(clip, profile)
    return ReviewResult(
        clip_id=facts.clip_id,
        verdict=verdict_of(checks),
        checks=checks,
        skipped=facts.skipped,
    )
