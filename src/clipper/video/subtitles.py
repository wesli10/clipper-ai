"""ASS subtitle generation (SPEC 7.3 step 5).

Two styles: ``karaoke`` for shorts (a few words per line, the active word
highlighted with ``\\k``) and ``classic`` for longs (full sentences in a
semi-transparent box). Word times come from the transcript and are shifted by
the concat offsets, never guessed.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from clipper.models import TimeRange, Transcript, Word
from clipper.profiles import BannerStyle, CardStyle, Profile, SubtitleStyle
from clipper.utils.text import format_ass_time

#: Characters that end a caption line.
LINE_BREAK_PUNCT = ".?!,;:"
#: A pause at least this long forces a new caption line.
LINE_BREAK_GAP_S = 0.4


@dataclass(frozen=True, slots=True)
class CaptionLine:
    """One displayed line, with times already in the rendered clip's timeline."""

    words: list[Word]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True, slots=True)
class Card:
    """The held question card of the quiz format, in the rendered timeline.

    Drawn here rather than into the frame because the ``ass`` filter runs last
    (CLAUDE.md), which is the only point in the chain where the output
    resolution is final.
    """

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Banner:
    """The comment question, held above the frame for the whole clip."""

    text: str
    #: Vertical centre in output pixels, computed from the real bar geometry.
    y: int
    end: float


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #


def to_ass_colour(value: str) -> str:
    """``#RRGGBB`` or ``#RRGGBBAA`` to ASS ``&HAABBGGRR``.

    ASS alpha is inverted: 00 is opaque, FF is fully transparent.
    """
    text = value.strip().lstrip("#")
    if len(text) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"cor invalida: {value} (use #RRGGBB ou #RRGGBBAA)")
    if len(text) == 6:
        r, g, b, alpha = text[0:2], text[2:4], text[4:6], "00"
    else:
        r, g, b = text[0:2], text[2:4], text[4:6]
        alpha = f"{255 - int(text[6:8], 16):02X}"
    return f"&H{alpha}{b}{g}{r}".upper()


def escape_text(text: str) -> str:
    """Protect the ASS override syntax; accents and emoji pass through."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


# --------------------------------------------------------------------------- #
# Collecting the words of a clip
# --------------------------------------------------------------------------- #


def words_for_clip(
    transcript: Transcript, intervals: list[TimeRange], offsets: list[float]
) -> list[Word]:
    """Words inside the clip's intervals, re-timed to the rendered clip.

    ``offsets[i]`` is where interval ``i`` starts in the rendered file, as
    reported by :mod:`clipper.video.concat` - so captions stay in sync across
    crossfades.
    """
    if len(offsets) != len(intervals):
        raise ValueError("um offset por intervalo e obrigatorio")

    collected: list[Word] = []
    all_words = transcript.words()
    for interval, offset in zip(intervals, offsets, strict=True):
        shift = offset - interval.start
        for word in all_words:
            if word.end <= interval.start or word.start >= interval.end:
                continue
            start = max(word.start, interval.start) + shift
            end = min(word.end, interval.end) + shift
            if end > start:
                collected.append(
                    Word(text=word.text, start=round(start, 3), end=round(end, 3), prob=word.prob)
                )
    collected.sort(key=lambda w: w.start)
    return collected


# --------------------------------------------------------------------------- #
# Line breaking
# --------------------------------------------------------------------------- #


def _ends_line(word: Word) -> bool:
    return word.text.rstrip().endswith(tuple(LINE_BREAK_PUNCT))


def break_lines(words: list[Word], style: SubtitleStyle, *, kind: str) -> list[CaptionLine]:
    """Group words into caption lines (SPEC 7.3 step 5)."""
    if not words:
        return []

    max_words = style.max_words_per_line
    max_chars = style.max_chars_per_line * (style.max_lines if kind == "classic" else 1)

    lines: list[CaptionLine] = []
    current: list[Word] = []
    for index, word in enumerate(words):
        tentative = [*current, word]
        length = len(" ".join(w.text for w in tentative))
        too_long = length > max_chars or len(tentative) > max_words
        if current and too_long:
            lines.append(CaptionLine(words=current))
            current = [word]
        else:
            current = tentative

        gap_next = words[index + 1].start - word.end if index + 1 < len(words) else 0.0
        if current and (_ends_line(word) or gap_next > LINE_BREAK_GAP_S):
            lines.append(CaptionLine(words=current))
            current = []
    if current:
        lines.append(CaptionLine(words=current))
    return lines


def wrap_for_display(text: str, style: SubtitleStyle) -> str:
    """Insert ASS line breaks so a classic caption never exceeds ``max_lines``."""
    if style.max_lines <= 1:
        return text
    words = text.split()
    rows: list[list[str]] = [[]]
    for word in words:
        candidate = " ".join([*rows[-1], word])
        if rows[-1] and len(candidate) > style.max_chars_per_line and len(rows) < style.max_lines:
            rows.append([word])
        else:
            rows[-1] = [*rows[-1], word]
    return "\\N".join(" ".join(row) for row in rows if row)


# --------------------------------------------------------------------------- #
# ASS document
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def installed_font_families() -> frozenset[str]:
    """Lowercased font families known to fontconfig; empty when unavailable."""
    if shutil.which("fc-list") is None:
        return frozenset()
    try:
        result = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return frozenset()
    families: set[str] = set()
    for line in result.stdout.splitlines():
        for family in line.split(","):
            families.add(family.strip().lower())
    return frozenset(families)


def resolve_font(style: SubtitleStyle) -> str:
    """The configured font, or its fallback when fontconfig does not know it.

    libass would silently substitute something else; naming the fallback keeps
    the rendered result predictable (SPEC 7.3 step 5).
    """
    families = installed_font_families()
    if not families:
        return style.font
    wanted = style.font.strip().lower()
    if any(wanted == f or wanted.startswith(f) or f.startswith(wanted) for f in families):
        return style.font
    return style.fallback_font


def _style_line(profile: Profile) -> str:
    style = profile.subtitle
    primary = to_ass_colour(style.primary_color)
    secondary = to_ass_colour(style.highlight_color)
    outline_colour = "&H00000000"
    if profile.subtitle_style == "classic":
        border_style, outline, shadow = 3, 1, 0
        back = to_ass_colour(style.box_color)
    else:
        border_style, outline, shadow = 1, style.outline, style.shadow
        back = "&H80000000"

    # \an2 = bottom-centre; MarginV pushes the line up from the bottom edge.
    margin_v = max(10, round((1.0 - style.vertical_pos_pct) * profile.output_height))
    return (
        "Style: Default,"
        f"{resolve_font(style)},{style.size},"
        f"{primary},{secondary},{outline_colour},{back},"
        "-1,0,0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},2,60,60,{margin_v},1"
    )


def _card_style_line(profile: Profile) -> str:
    """Big centred text for the card. Alignment 5 is middle-centre."""
    card = profile.card
    size = max(12, round(card.size_pct * profile.output_height))
    colour = to_ass_colour(card.color)
    return (
        "Style: Card,"
        f"{resolve_font(profile.subtitle)},{size},"
        f"{colour},{colour},&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,"
        "1,4,0,5,60,60,60,1"
    )


def _banner_style_line(profile: Profile) -> str:
    """Smaller than the captions and outlined, so it reads over blur or bar."""
    banner = profile.banner
    size = max(10, round(banner.size_pct * profile.output_height))
    colour = to_ass_colour(banner.color)
    return (
        "Style: Banner,"
        f"{resolve_font(profile.subtitle)},{size},"
        f"{colour},{colour},&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,"
        "1,3,0,5,40,40,40,1"
    )


def _dim_style_line(profile: Profile) -> str:
    """A borderless black shape; the alpha comes from ``card.dim``."""
    alpha = f"{255 - round(max(0.0, min(1.0, profile.card.dim)) * 255):02X}"
    colour = f"&H{alpha}000000"
    return (
        "Style: Dim,"
        f"{resolve_font(profile.subtitle)},20,"
        f"{colour},{colour},{colour},{colour},"
        "0,0,0,0,100,100,0,0,"
        "1,0,0,7,0,0,0,1"
    )


def _header(profile: Profile) -> str:
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {profile.output_width}",
            f"PlayResY: {profile.output_height}",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
            "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
            _style_line(profile),
            _card_style_line(profile),
            _banner_style_line(profile),
            _dim_style_line(profile),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )


def _karaoke_text(line: CaptionLine, style: SubtitleStyle) -> str:
    """``\\k`` runs so the active word changes colour as it is spoken."""
    chunks: list[str] = []
    for index, word in enumerate(line.words):
        end = line.words[index + 1].start if index + 1 < len(line.words) else word.end
        centiseconds = max(1, round((end - word.start) * 100))
        text = word.text.upper() if style.uppercase else word.text
        chunks.append(f"{{\\k{centiseconds}}}{escape_text(text)}")
    return " ".join(chunks)


def _classic_text(line: CaptionLine, style: SubtitleStyle) -> str:
    text = line.text.upper() if style.uppercase else line.text
    return wrap_for_display(escape_text(text), style)


def wrap_card_text(text: str, style: CardStyle) -> str:
    """Break the card text into lines short enough for the big font."""
    rows: list[list[str]] = [[]]
    for word in text.split():
        candidate = " ".join([*rows[-1], word])
        if rows[-1] and len(candidate) > style.max_chars_per_line:
            rows.append([word])
        else:
            rows[-1] = [*rows[-1], word]
    return "\\N".join(" ".join(row) for row in rows if row)


def wrap_banner_text(text: str, style: BannerStyle) -> str:
    """Break the question into lines that fit the bar's width."""
    rows: list[list[str]] = [[]]
    for word in text.split():
        candidate = " ".join([*rows[-1], word])
        if rows[-1] and len(candidate) > style.max_chars_per_line:
            rows.append([word])
        else:
            rows[-1] = [*rows[-1], word]
    return "\\N".join(" ".join(row) for row in rows if row)


def banner_event(banner: Banner, profile: Profile) -> str:
    """One dialogue spanning the clip; the question has to be seen to be answered."""
    style = profile.banner
    text = banner.text.upper() if style.uppercase else banner.text
    return (
        f"Dialogue: 0,{format_ass_time(0.0)},{format_ass_time(banner.end)},"
        f"Banner,,0,0,0,,{{\\pos({profile.output_width // 2},{banner.y})}}"
        f"{wrap_banner_text(escape_text(text), style)}"
    )


def card_events(card: Card, profile: Profile) -> list[str]:
    """Dim, question and countdown, for the seconds the frame is held."""
    style = profile.card
    width, height = profile.output_width, profile.output_height
    events: list[str] = []

    if style.dim > 0:
        shape = f"m 0 0 l {width} 0 l {width} {height} l 0 {height}"
        events.append(
            f"Dialogue: 0,{format_ass_time(card.start)},{format_ass_time(card.end)},"
            f"Dim,,0,0,0,,{{\\p1\\pos(0,0)}}{shape}{{\\p0}}"
        )

    text = card.text.upper() if style.uppercase else card.text
    events.append(
        f"Dialogue: 1,{format_ass_time(card.start)},{format_ass_time(card.end)},"
        f"Card,,0,0,0,,{{\\pos({width // 2},{round(style.vertical_pos_pct * height)})}}"
        f"{wrap_card_text(escape_text(text), style)}"
    )

    if style.countdown:
        # One digit per whole second still left, counting down to the reveal.
        seconds = int(card.duration)
        for step in range(seconds):
            start = card.end - (seconds - step)
            events.append(
                f"Dialogue: 1,{format_ass_time(start)},{format_ass_time(start + 1.0)},"
                f"Card,,0,0,0,,{{\\pos({width // 2},{round(0.68 * height)})}}"
                f"{seconds - step}"
            )
    return events


def build_ass(
    transcript: Transcript,
    intervals: list[TimeRange],
    offsets: list[float],
    profile: Profile,
    *,
    card: Card | None = None,
    banner: Banner | None = None,
) -> str:
    """Full ``.ass`` document for one clip."""
    if profile.subtitle_style == "off":
        raise ValueError("subtitle_style 'off' nao gera arquivo .ass")

    words = words_for_clip(transcript, intervals, offsets)
    lines = break_lines(words, profile.subtitle, kind=profile.subtitle_style)

    events: list[str] = []
    for line in lines:
        if profile.subtitle_style == "karaoke":
            body = _karaoke_text(line, profile.subtitle)
        else:
            body = _classic_text(line, profile.subtitle)
        events.append(
            f"Dialogue: 0,{format_ass_time(line.start)},{format_ass_time(line.end)},"
            f"Default,,0,0,0,,{body}"
        )
    if banner is not None and banner.text.strip():
        events.append(banner_event(banner, profile))
    if card is not None:
        # The held frame carries no speech, so nothing here can collide with a
        # caption; ordering only matters for readability of the file.
        events += card_events(card, profile)
    return _header(profile) + "\n" + "\n".join(events) + "\n"
