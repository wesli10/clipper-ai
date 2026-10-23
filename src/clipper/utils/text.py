"""Small text helpers: slugs and timestamps."""

from __future__ import annotations

import re
import unicodedata

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 48) -> str:
    """ASCII, lowercase, dash-separated slug suitable for a directory name."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_text).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0] or slug[:max_length]
    return slug.strip("-") or "clip"


def format_timestamp(seconds: float, *, always_hours: bool = False) -> str:
    """``mm:ss`` (or ``hh:mm:ss``) for chapters and human-facing output."""
    total = max(0, round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours or always_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_ass_time(seconds: float) -> str:
    """ASS timestamp: ``h:mm:ss.cc`` (centiseconds)."""
    clamped = max(0.0, seconds)
    hours = int(clamped // 3600)
    minutes = int((clamped % 3600) // 60)
    secs = clamped % 60
    return f"{hours:d}:{minutes:02d}:{secs:05.2f}"


def parse_timecode(value: str) -> float:
    """Parse ``12.5``, ``mm:ss(.ms)`` or ``hh:mm:ss(.ms)`` into seconds."""
    text = value.strip()
    if not text:
        raise ValueError("tempo vazio")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"tempo invalido: {value}")
    try:
        numbers = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"tempo invalido: {value}") from exc
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


# --------------------------------------------------------------------------- #
# Numbers a title claims, against the cut that has to back them up
# --------------------------------------------------------------------------- #

#: Number words worth resolving: what a title actually says out loud in pt-BR.
#: "um" and "uma" are deliberately absent: in "um carro em dez mil" they are the
#: article, and a transcript is full of articles, so counting them as the number 1
#: makes the check pass or fail by accident rather than by what was promised.
_WORD_NUMBERS: dict[str, int] = {
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "onze": 11,
    "doze": 12,
    "treze": 13,
    "catorze": 14,
    "quatorze": 14,
    "quinze": 15,
    "dezesseis": 16,
    "dezessete": 17,
    "dezoito": 18,
    "dezenove": 19,
    "vinte": 20,
    "trinta": 30,
    "quarenta": 40,
    "cinquenta": 50,
    "sessenta": 60,
    "setenta": 70,
    "oitenta": 80,
    "noventa": 90,
    "cem": 100,
    "cento": 100,
    "duzentos": 200,
    "trezentos": 300,
    "quinhentos": 500,
    "mil": 1000,
}
_DIGITS = re.compile(r"\d[\d.,]*")
_WORDS = re.compile(r"[a-zà-ÿ]+")
_ACCENTS = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")


def _as_number(digits: str) -> int | None:
    """``1.500`` and ``1,5 mil`` both come out of a transcript; only the plain int matters."""
    plain = digits.rstrip(".,").replace(".", "").replace(",", "")
    return int(plain) if plain.isdigit() else None


def numbers_in(text: str) -> set[int]:
    """Every number the text states, digits and words alike.

    "10 mil" and "dez mil" are the same claim to whoever is watching, so both
    resolve to {10, 1000}: the pair is what a cut has to contain, not the spelling.
    """
    flat = text.lower().translate(_ACCENTS)
    found = {value for raw in _DIGITS.findall(flat) if (value := _as_number(raw)) is not None}
    found |= {_WORD_NUMBERS[word] for word in _WORDS.findall(flat) if word in _WORD_NUMBERS}
    return found


def unbacked_numbers(claim: str, spoken: str) -> list[int]:
    """Numbers ``claim`` states that ``spoken`` never says, smallest first.

    A title is a promise about the clip, and a number is the part of a promise
    that can be checked. Measured on a real one: a short was titled "briguei por
    10 mil num carro", and the "10 mil" is first said 0.1 s after the cut ends -
    the viewer hears the fight and the four years, never the ten thousand.
    """
    return sorted(numbers_in(claim) - numbers_in(spoken))
