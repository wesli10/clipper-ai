"""Gemini API provider (SPEC 6.4).

The whole transcript of a 2 h video fits in one call, so there is no windowing
here - that is the local provider's problem.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from clipper.config import LLMSettings
from clipper.llm.base import BaseLLMProvider
from clipper.utils.errors import ExternalError

#: Gemini 2.5 Pro accepts ~1M tokens; we stay well under it.
MAX_CONTEXT_TOKENS = 900_000


class GeminiProvider(BaseLLMProvider):
    """Calls ``google-genai`` with JSON mode, backing off on 429 and falling back to flash."""

    name = "gemini"
    max_context_tokens = MAX_CONTEXT_TOKENS

    def __init__(
        self,
        api_key: str,
        cfg: LLMSettings,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not api_key:
            raise ExternalError(
                "GEMINI_API_KEY nao definida. Copie .env.example para .env e coloque a chave "
                "do Google AI Studio, ou use --provider file / --provider ollama."
            )
        self.cfg = cfg
        self.logger = logger
        self._client = self._make_client(api_key)

    @staticmethod
    def _make_client(api_key: str) -> Any:
        try:
            from google import genai
        except ImportError as exc:
            raise ExternalError(
                "google-genai nao esta instalado. Rode: uv sync --extra llm"
            ) from exc
        return genai.Client(api_key=api_key)

    @property
    def models(self) -> list[str]:
        names = [self.cfg.gemini_model]
        if self.cfg.gemini_fallback_model and self.cfg.gemini_fallback_model != names[0]:
            names.append(self.cfg.gemini_fallback_model)
        return names

    def generate(self, system: str, user: str) -> str:
        last_error: Exception | None = None
        for model in self.models:
            try:
                return self._generate_with_backoff(model, system, user)
            except ExternalError as exc:
                last_error = exc
                if self.logger is not None:
                    self.logger.warning("modelo %s falhou (%s); tentando o proximo", model, exc)
        raise ExternalError(f"todos os modelos do Gemini falharam: {last_error}")

    def _generate_with_backoff(self, model: str, system: str, user: str) -> str:
        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                return self._call(model, system, user)
            except Exception as exc:  # transport/quota errors from the SDK
                last_error = exc
                if has_no_quota(exc):
                    # `limit: 0` means this key has no quota for this model at all.
                    # Waiting cannot help; the next model can.
                    raise ExternalError(
                        f"gemini ({model}): a chave nao tem cota para este modelo "
                        "(free tier). Use um modelo do free tier ou ative o billing."
                    ) from exc
                if not _is_retryable(exc) or attempt == self.cfg.max_retries:
                    break
                sleep_for = retry_after(exc) or (delay + random.uniform(0, 1.0))
                if self.logger is not None:
                    self.logger.warning(
                        "gemini %s tentativa %d falhou (%s); nova tentativa em %.1fs",
                        model,
                        attempt,
                        exc,
                        sleep_for,
                    )
                time.sleep(sleep_for)
                delay *= 2
        raise ExternalError(f"gemini ({model}): {last_error}")

    def _call(self, model: str, system: str, user: str) -> str:
        from google.genai import types

        if self.logger is not None:
            self.logger.info("gemini %s: %d chars de prompt", model, len(user))
        response = self._client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.4,
            ),
        )
        text = getattr(response, "text", None)
        if not text:
            raise ExternalError(f"gemini ({model}) devolveu uma resposta vazia")
        return str(text)


_RETRYABLE_MARKERS = (
    "429",
    "resource_exhausted",
    "rate limit",
    "quota",
    "503",
    "unavailable",
    "500",
    "internal",
    "deadline",
    "timeout",
)


def _is_retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


_NO_QUOTA_RE = re.compile(r"limit:\s*0\b")
_RETRY_AFTER_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
#: Never wait longer than this on the API's own hint.
MAX_RETRY_AFTER_S = 120.0


def has_no_quota(exc: Exception) -> bool:
    """True for a 429 that reports a zero quota, which no amount of waiting fixes."""
    message = str(exc)
    return "429" in message and bool(_NO_QUOTA_RE.search(message))


def retry_after(exc: Exception) -> float | None:
    """The delay the API itself suggests, when it gives one."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if match is None:
        return None
    return min(float(match.group(1)), MAX_RETRY_AFTER_S)
