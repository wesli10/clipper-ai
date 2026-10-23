from __future__ import annotations

from pathlib import Path

import pytest

from clipper.models import Segment, Transcript, Word

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prompts_dir() -> Path:
    return PROJECT_ROOT / "prompts"


def make_words(text: str, start: float, step: float = 0.4) -> list[Word]:
    words = []
    t = start
    for token in text.split():
        words.append(Word(text=token, start=round(t, 3), end=round(t + step * 0.9, 3), prob=0.95))
        t += step
    return words


def make_segment(seg_id: int, start: float, text: str, step: float = 0.4) -> Segment:
    words = make_words(text, start, step)
    return Segment(id=seg_id, start=start, end=words[-1].end, text=text, words=words)


@pytest.fixture
def transcript() -> Transcript:
    """Small synthetic transcript with gaps between some segments."""
    specs = [
        (0.0, "Bem vindos ao programa de hoje."),
        (3.0, "Hoje vamos falar sobre produtividade."),
        (7.5, "Entao a primeira coisa que voce precisa entender e o foco."),
        (14.0, "Foco nao e fazer mais coisas."),
        (18.0, "Agora mudando de assunto vamos falar de sono."),
        (25.0, "Dormir menos de seis horas destroi sua memoria."),
        (31.0, "E isso encerra a nossa conversa de hoje."),
    ]
    segments = [make_segment(i, start, text) for i, (start, text) in enumerate(specs)]
    return Transcript(
        video_id="test123",
        language="pt",
        duration=segments[-1].end + 2.0,
        segments=segments,
    )
