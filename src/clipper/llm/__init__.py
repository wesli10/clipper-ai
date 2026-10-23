"""LLM providers (SPEC 6.4). The pipeline only ever sees :class:`LLMProvider`."""

from __future__ import annotations

import logging
from pathlib import Path

from clipper.config import Settings
from clipper.llm.base import LLMProvider
from clipper.utils.errors import InputError

__all__ = ["LLMProvider", "build_provider"]


def build_provider(
    settings: Settings,
    name: str | None = None,
    *,
    stage: str | None = None,
    logger: logging.Logger | None = None,
    request_dir: Path | None = None,
    wait: bool = True,
) -> LLMProvider:
    """Instantiate the provider for ``stage``; ``name`` overrides everything."""
    provider = name or (settings.llm.for_stage(stage) if stage else settings.llm.provider)
    if provider == "gemini":
        from clipper.llm.gemini import GeminiProvider

        provider_impl: LLMProvider = GeminiProvider(
            settings.gemini_api_key or "", settings.llm, logger=logger
        )
        return provider_impl
    if provider == "claude":
        from clipper.llm.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(settings.llm, logger=logger)
    if provider == "ollama":
        from clipper.llm.ollama import OllamaProvider

        return OllamaProvider(settings.llm, logger=logger)
    if provider == "file":
        from clipper.llm.file import FileProvider

        if request_dir is None:
            raise InputError("o provider 'file' precisa de um diretorio para o request")
        return FileProvider(request_dir, logger=logger, wait=wait)
    raise InputError(
        f"provider de LLM desconhecido: '{provider}' (use gemini, claude, ollama ou file)"
    )
