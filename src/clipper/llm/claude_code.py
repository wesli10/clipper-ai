"""Claude Code as an LLM provider, on the user's subscription (SPEC 6.5).

SPEC 2 rules out the Anthropic API because Claude MAX does not include API
credits. The Claude Code CLI, however, is already authenticated with that
subscription, and ``claude -p`` is a non-interactive completion. This provider
shells out to it - the same idea as the ``file`` provider, minus the manual step.

Deliberately a subprocess rather than ``claude-agent-sdk``: that package wraps
this same binary and adds an async agent loop, tool plumbing and an ``mcp``
dependency, none of which a single JSON completion needs.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from clipper.config import LLMSettings
from clipper.llm.base import BaseLLMProvider
from clipper.utils.errors import ExternalError

#: Opus 5 takes 1M tokens; leave room for the harness preamble.
MAX_CONTEXT_TOKENS = 900_000

#: Nothing here needs a tool. Turning them off keeps the model on the task and
#: stops a stray Bash call from touching the workspace.
DISALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Task,TodoWrite,NotebookEdit"


class ClaudeCodeProvider(BaseLLMProvider):
    """Runs one ``claude -p`` per completion, prompt piped through stdin."""

    name = "claude"
    max_context_tokens = MAX_CONTEXT_TOKENS

    def __init__(self, cfg: LLMSettings, *, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.binary = shutil.which(cfg.claude_binary)
        if self.binary is None:
            raise ExternalError(
                f"'{cfg.claude_binary}' nao encontrado no PATH. Instale o Claude Code "
                "(https://claude.com/code) e faca login, ou use outro provider."
            )

    def _command(self, system: str) -> list[str]:
        args = [
            self.binary or "claude",
            "-p",
            "--system-prompt",
            system,
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--disallowed-tools",
            DISALLOWED_TOOLS,
        ]
        if self.cfg.claude_model:
            args += ["--model", self.cfg.claude_model]
        return args

    def generate(self, system: str, user: str) -> str:
        command = self._command(system)
        if self.logger is not None:
            self.logger.info(
                "claude code (%s): %d chars de prompt",
                self.cfg.claude_model or "modelo padrao",
                len(user),
            )
        try:
            result = subprocess.run(
                command,
                input=user,
                capture_output=True,
                text=True,
                timeout=self.cfg.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalError(
                f"o Claude Code nao respondeu em {self.cfg.timeout_s:.0f}s"
            ) from exc
        except OSError as exc:
            raise ExternalError(f"falha ao executar o Claude Code: {exc}") from exc

        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-500:]
            raise ExternalError(f"claude -p falhou (codigo {result.returncode}): {tail}")

        return self._unwrap(result.stdout)

    def _unwrap(self, stdout: str) -> str:
        """Pull the answer out of the CLI's JSON envelope."""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ExternalError(
                f"resposta do Claude Code ilegivel: {exc}\n{stdout.strip()[:300]}"
            ) from exc

        if payload.get("is_error"):
            detail = payload.get("result") or payload.get("api_error_status") or "sem detalhe"
            raise ExternalError(f"o Claude Code reportou erro: {detail}")

        answer = payload.get("result")
        if not isinstance(answer, str) or not answer.strip():
            raise ExternalError("o Claude Code devolveu uma resposta vazia")

        if self.logger is not None:
            usage = payload.get("usage") or {}
            self.logger.info(
                "claude code: %s tokens de entrada (+%s de cache), %s de saida",
                usage.get("input_tokens"),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("output_tokens"),
            )
        return answer
