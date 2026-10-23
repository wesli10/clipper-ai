"""Local LLM provider via Ollama (SPEC 6.4).

A 7B model has nowhere near the context of Gemini, so the transcript is split
into overlapping windows, each window is asked for candidates, and a final merge
call deduplicates and re-ranks them. Only usable once whisper has been unloaded
(CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from clipper.config import LLMSettings
from clipper.llm.base import BaseLLMProvider
from clipper.utils.errors import ExternalError

#: Rough characters-per-token ratio, good enough for window sizing.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Window:
    index: int
    text: str


def split_windows(text: str, *, window_tokens: int, overlap: float) -> list[Window]:
    """Split ``text`` into overlapping windows, never cutting a line in half."""
    lines = text.splitlines()
    if not lines:
        return []
    budget = max(1, window_tokens * CHARS_PER_TOKEN)

    windows: list[Window] = []
    start = 0
    while start < len(lines):
        size = 0
        end = start
        while end < len(lines) and size + len(lines[end]) + 1 <= budget:
            size += len(lines[end]) + 1
            end += 1
        if end == start:  # a single line longer than the budget
            end = start + 1
        windows.append(Window(index=len(windows), text="\n".join(lines[start:end])))
        if end >= len(lines):
            break
        step_back = max(1, int((end - start) * overlap))
        # Always move forward: with a single over-long line the overlap would
        # otherwise put us back where we started, looping forever.
        start = max(start + 1, end - step_back)
    return windows


class OllamaProvider(BaseLLMProvider):
    """Talks to a local Ollama server; JSON mode via ``format: json``."""

    name = "ollama"

    def __init__(self, cfg: LLMSettings, *, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.max_context_tokens = cfg.ollama_window_tokens

    def generate(self, system: str, user: str) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise ExternalError("httpx nao esta instalado. Rode: uv sync --extra llm") from exc

        payload = {
            "model": self.cfg.ollama_model,
            "prompt": user,
            "system": system,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.4},
        }
        if self.logger is not None:
            self.logger.info("ollama %s: %d chars de prompt", self.cfg.ollama_model, len(user))
        try:
            response = httpx.post(
                f"{self.cfg.ollama_host.rstrip('/')}/api/generate",
                json=payload,
                timeout=self.cfg.timeout_s,
            )
            response.raise_for_status()
        except Exception as exc:
            raise ExternalError(
                f"nao consegui falar com o Ollama em {self.cfg.ollama_host}: {exc}\n"
                "Verifique se o servidor esta rodando (ollama serve) e se o modelo "
                f"'{self.cfg.ollama_model}' foi baixado (ollama pull)."
            ) from exc

        data = response.json()
        text = data.get("response")
        if not text:
            raise ExternalError("o Ollama devolveu uma resposta vazia")
        return str(text)


def merge_prompt(
    template: str,
    candidates: list[dict[str, object]],
    *,
    profile_name: str,
    n_clips: int,
    duration_min: float,
    duration_target: float,
    duration_max: float,
    max_segments: int,
) -> str:
    """Fill ``prompts/merge_windows.md`` (literal braces, so no ``str.format``)."""
    values = {
        "profile": profile_name,
        "n_clips": str(n_clips),
        "duration_min": f"{duration_min:g}",
        "duration_target": f"{duration_target:g}",
        "duration_max": f"{duration_max:g}",
        "max_segments": str(max_segments),
        "candidates": json.dumps({"clips": candidates}, ensure_ascii=False, indent=2),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template
