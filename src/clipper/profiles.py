"""Format profiles - the central concept (SPEC 4).

Every stage after transcription receives a ``Profile``. Resolution, durations and
subtitle styling live here and in ``config.yaml``; never hardcode them elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReframeMode = Literal["face", "center", "blur", "letterbox", "none"]
ZoomMode = Literal["none", "punch_in"]
SubtitleKind = Literal["karaoke", "classic", "off"]
BoundaryMode = Literal["sentence", "topic"]

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class ProfileError(ValueError):
    """Raised when a profile cannot be resolved or is internally inconsistent."""


class SubtitleStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font: str = "Montserrat ExtraBold"
    fallback_font: str = "DejaVu Sans Bold"
    size: int = 72
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    box_color: str = "#00000099"
    outline: int = 4
    shadow: int = 0
    vertical_pos_pct: float = Field(default=0.70, ge=0.0, le=1.0)
    max_words_per_line: int = Field(default=4, ge=1)
    max_chars_per_line: int = Field(default=22, ge=8)
    max_lines: int = Field(default=1, ge=1)
    uppercase: bool = False


class CardStyle(BaseModel):
    """The held question card of the quiz format.

    It is drawn as ASS events, not baked into the frame, so it lands after the
    reframe at the output resolution - a card drawn on a 16:9 source would end up
    shrunk into the middle band of a 9:16 short.
    """

    model_config = ConfigDict(extra="forbid")

    #: Text height as a share of the output height.
    size_pct: float = Field(default=0.055, gt=0.0, le=0.3)
    #: Opacity of the black laid over the held frame, 0 = none, 1 = solid.
    #:
    #: Nominal, not measured: libass blends gamma-encoded, so 0.75 takes about
    #: half the luma off a mid-bright frame, not three quarters.
    dim: float = Field(default=0.75, ge=0.0, le=1.0)
    color: str = "#FFFFFF"
    #: Vertical centre of the text, as a share of the output height.
    vertical_pos_pct: float = Field(default=0.42, ge=0.0, le=1.0)
    max_chars_per_line: int = Field(default=18, ge=6)
    countdown: bool = True
    uppercase: bool = True


class BannerStyle(BaseModel):
    """The comment question standing in the bar above the frame.

    Placed by geometry, not by a fixed percentage: the bar only exists when the
    source is wider than the output, and how tall it is depends on the source
    aspect. Guessing a position would put the text over someone's face on any
    footage that is not 16:9.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Text height as a share of the output height.
    size_pct: float = Field(default=0.032, gt=0.0, le=0.2)
    color: str = "#FFFFFF"
    #: Where to put it when there is no bar to sit in.
    vertical_pos_pct: float = Field(default=0.12, ge=0.0, le=1.0)
    #: A bar shorter than this (share of the output height) is not worth using.
    min_bar_pct: float = Field(default=0.10, ge=0.0, le=1.0)
    max_chars_per_line: int = Field(default=26, ge=8)
    uppercase: bool = True


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    # selection
    duration_min: float = Field(gt=0)
    duration_max: float = Field(gt=0)
    duration_target: float = Field(gt=0)
    n_clips: int = Field(ge=1)
    #: Candidates the model scores below this are dropped. Its own score is the
    #: best signal we have that it is padding to fill the quota.
    min_score: float = Field(default=0.0, ge=0.0, le=100.0)
    multi_segment: bool = False
    max_segments: int = Field(default=1, ge=1)
    boundary_mode: BoundaryMode = "sentence"
    #: How many extra candidates to ask the LLM for (SPEC 6.2 / 6.3).
    extra_candidates: int = Field(default=3, ge=0)
    select_prompt: Path
    metadata_prompt: Path

    # image
    output_width: int = Field(gt=0)
    output_height: int = Field(gt=0)
    fps: int = Field(default=30, gt=0)
    reframe: ReframeMode = "center"
    #: Bar colour for ``reframe: letterbox``.
    letterbox_color: str = "#000000"
    #: How much to dim the blurred background of ``reframe: blur``.
    #:
    #: Burned captions sit on that background. Left at full brightness the white
    #: text competes with it; dimming keeps the filled look and gives the caption
    #: its contrast back. 0 reproduces the plain blur of SPEC 7.3.
    blur_brightness: float = Field(default=-0.30, ge=-1.0, le=1.0)
    blur_saturation: float = Field(default=0.6, ge=0.0, le=3.0)
    zoom: ZoomMode = "none"

    #: The comment question above the frame; only drawn when the LLM wrote one.
    banner: BannerStyle = Field(default_factory=BannerStyle)
    #: Seconds of source footage kept rolling past the clip's final word.
    #:
    #: A runway into the end card, not part of the subject: the ending the model
    #: chose stays exactly where it is, the video simply does not stop dead on
    #: it. Speech that starts inside the runway is deliberately left uncaptioned
    #: - it is ambience, and captioning half a sentence of the next subject
    #: would read as the clip continuing.
    tail_roll_s: float = Field(default=0.0, ge=0.0, le=15.0)
    #: An end card joined after the clip. It is a finished video and never goes
    #: through the filter chain, so it keeps whatever it was made with.
    outro: Path | None = None

    # quiz card
    #: How long the frame is held on the question card. 0 disables the format.
    card_seconds: float = Field(default=0.0, ge=0.0)
    #: Shortest question and shortest answer either side of the hold.
    card_min_part_s: float = Field(default=3.0, gt=0.0)
    card: CardStyle = Field(default_factory=CardStyle)

    # subtitles
    subtitle_style: SubtitleKind = "karaoke"
    subtitle: SubtitleStyle = Field(default_factory=SubtitleStyle)

    # audio
    loudnorm_i: float = -14.0

    @field_validator("subtitle_style", mode="before")
    @classmethod
    def _yaml_off_is_not_a_boolean(cls, value: Any) -> Any:
        """Accept ``subtitle_style: off`` unquoted.

        YAML 1.1 turns a bare ``off`` into ``False``, which reaches here as a
        literal_error naming a value the user never typed. The intent is never
        ambiguous, so take it.
        """
        return "off" if value is False else value

    @model_validator(mode="after")
    def _consistent(self) -> Profile:
        if self.duration_min > self.duration_max:
            raise ProfileError(
                f"perfil '{self.name}': duration_min ({self.duration_min}) "
                f"maior que duration_max ({self.duration_max})"
            )
        if not self.duration_min <= self.duration_target <= self.duration_max:
            raise ProfileError(
                f"perfil '{self.name}': duration_target ({self.duration_target}) "
                f"fora de [{self.duration_min}, {self.duration_max}]"
            )
        if not self.multi_segment and self.max_segments != 1:
            raise ProfileError(
                f"perfil '{self.name}': max_segments deve ser 1 quando multi_segment e falso"
            )
        if self.card_seconds and self.subtitle_style == "off":
            raise ProfileError(
                f"perfil '{self.name}': card_seconds exige legenda; "
                "o card e desenhado como evento .ass (SPEC 7.3 passo 5)"
            )
        if self.zoom == "punch_in" and self.reframe in ("none", "letterbox"):
            raise ProfileError(
                f"perfil '{self.name}': zoom 'punch_in' exige recorte; "
                f"com reframe '{self.reframe}' seria upscale (SPEC 7.3 passo 4)"
            )
        return self

    @property
    def aspect(self) -> float:
        return self.output_width / self.output_height

    @property
    def is_vertical(self) -> bool:
        return self.output_height > self.output_width


BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "short": {
        "duration_min": 20.0,
        "duration_max": 90.0,
        "duration_target": 45.0,
        "n_clips": 5,
        "multi_segment": False,
        "max_segments": 1,
        "boundary_mode": "sentence",
        "extra_candidates": 3,
        "select_prompt": "select_short.md",
        "metadata_prompt": "metadata_short.md",
        "output_width": 1080,
        "output_height": 1920,
        "fps": 30,
        "reframe": "face",
        "zoom": "none",
        "subtitle_style": "karaoke",
        "subtitle": {
            "font": "Montserrat ExtraBold",
            "size": 72,
            "primary_color": "#FFFFFF",
            "highlight_color": "#FFD400",
            "outline": 4,
            "vertical_pos_pct": 0.70,
            "max_words_per_line": 4,
            "max_chars_per_line": 22,
            "max_lines": 1,
            "uppercase": True,
        },
        "loudnorm_i": -14.0,
    },
    "long": {
        "duration_min": 120.0,
        "duration_max": 480.0,
        "duration_target": 240.0,
        "n_clips": 2,
        "multi_segment": True,
        "max_segments": 4,
        "boundary_mode": "topic",
        "extra_candidates": 2,
        "select_prompt": "select_long.md",
        "metadata_prompt": "metadata_long.md",
        "output_width": 1920,
        "output_height": 1080,
        "fps": 30,
        "reframe": "none",
        "zoom": "none",
        "subtitle_style": "classic",
        "subtitle": {
            "font": "Montserrat SemiBold",
            "fallback_font": "DejaVu Sans",
            "size": 44,
            "primary_color": "#FFFFFF",
            "box_color": "#00000099",
            "outline": 0,
            "vertical_pos_pct": 0.90,
            "max_chars_per_line": 42,
            "max_lines": 2,
            "max_words_per_line": 12,
            "uppercase": False,
        },
        "loudnorm_i": -14.0,
    },
}


#: The quiz format: a short cut at the question, held on a card, then the answer.
BUILTIN_PROFILES["quiz"] = {
    **BUILTIN_PROFILES["short"],
    "duration_min": 15.0,
    "duration_max": 75.0,
    "duration_target": 40.0,
    "n_clips": 3,
    "extra_candidates": 4,
    "select_prompt": "select_quiz.md",
    "metadata_prompt": "metadata_short.md",
    # The card needs the whole frame: a centre crop would cut the held image the
    # question refers to. Blur keeps it all and gives the dim something to sit on.
    "reframe": "blur",
    "card_seconds": 2.4,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _raw_profile(
    name: str,
    overrides: dict[str, dict[str, Any]],
    _seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve ``name`` into a raw dict, following ``extends`` chains."""
    if name in _seen:
        raise ProfileError(f"perfil '{name}': ciclo em 'extends' ({' -> '.join(sorted(_seen))})")
    override = overrides.get(name, {})
    parent = override.get("extends")

    if parent is not None:
        if not isinstance(parent, str):
            raise ProfileError(f"perfil '{name}': 'extends' deve ser o nome de outro perfil")
        base = _raw_profile(parent, overrides, _seen | {name})
    elif name in BUILTIN_PROFILES:
        base = dict(BUILTIN_PROFILES[name])
    else:
        raise ProfileError(
            f"perfil '{name}' desconhecido: nao e built-in "
            f"({', '.join(sorted(BUILTIN_PROFILES))}) e nao declara 'extends'"
        )

    merged = _deep_merge(base, {k: v for k, v in override.items() if k != "extends"})
    merged["name"] = name
    return merged


def resolve_profile(
    name: str,
    overrides: dict[str, dict[str, Any]] | None = None,
    prompts_dir: Path | None = None,
) -> Profile:
    """Build the effective ``Profile`` for ``name`` from built-ins + config overrides."""
    raw = _raw_profile(name, overrides or {})
    prompts = prompts_dir or PROMPTS_DIR
    for key in ("select_prompt", "metadata_prompt"):
        value = Path(str(raw[key])).expanduser()
        raw[key] = value if value.is_absolute() else prompts / value
    try:
        return Profile.model_validate(raw)
    except ProfileError:
        raise
    except ValueError as exc:
        raise ProfileError(f"perfil '{name}' invalido: {exc}") from exc


def available_profiles(overrides: dict[str, dict[str, Any]] | None = None) -> list[str]:
    names = set(BUILTIN_PROFILES) | set(overrides or {})
    return sorted(names, key=lambda n: (n not in BUILTIN_PROFILES, n))
