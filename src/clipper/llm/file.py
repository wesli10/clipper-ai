"""`file` provider: Claude Code as the editor, at no API cost (SPEC 6.5).

Writes the prompt to ``select_request.md`` and waits for ``select_response.json``
next to it. A slash command in Claude Code reads the request, answers in the same
schema and writes the file - so the MAX quota is used instead of API credits.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from clipper.llm.base import BaseLLMProvider
from clipper.utils.console import console
from clipper.utils.errors import ClipperError, ExternalError

REQUEST_NAME = "select_request.md"
RESPONSE_NAME = "select_response.json"
#: How often to look for the answer while waiting.
POLL_INTERVAL_S = 2.0


class WaitingForFileError(ClipperError):
    """Raised with ``--no-wait``: the request is on disk, the answer is not."""

    exit_code = 0


class FileProvider(BaseLLMProvider):
    """Round-trips the prompt through two files."""

    name = "file"
    #: Claude reads the whole thing; no windowing needed.
    max_context_tokens = 1_000_000

    def __init__(
        self,
        request_dir: Path,
        *,
        logger: logging.Logger | None = None,
        wait: bool = True,
        timeout_s: float = 3600.0,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self.request_dir = request_dir
        self.logger = logger
        self.wait = wait
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    @property
    def request_path(self) -> Path:
        return self.request_dir / REQUEST_NAME

    @property
    def response_path(self) -> Path:
        return self.request_dir / RESPONSE_NAME

    def generate(self, system: str, user: str) -> str:
        self.request_dir.mkdir(parents=True, exist_ok=True)
        self.response_path.unlink(missing_ok=True)
        self.request_path.write_text(
            f"<!-- system -->\n{system}\n\n<!-- prompt -->\n{user}\n", encoding="utf-8"
        )
        if self.logger is not None:
            self.logger.info("request gravado em %s", self.request_path)

        console.print(
            f"\n[bold]Provider 'file'[/bold]: request gravado em\n  {self.request_path}\n"
            f"No Claude Code, rode:\n"
            f"  [bold]/select-clips {self.request_dir}[/bold]\n"
            f"Depois grave a resposta em\n  {self.response_path}\n"
        )
        if not self.wait:
            raise WaitingForFileError(
                "aguardando a resposta do Claude Code. Rode o mesmo comando de novo "
                "depois que o arquivo existir."
            )

        console.print("[dim]aguardando o arquivo de resposta... (ctrl+c para cancelar)[/dim]")
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.response_path.exists() and self.response_path.stat().st_size > 0:
                return self.response_path.read_text(encoding="utf-8")
            time.sleep(self.poll_interval_s)
        raise ExternalError(
            f"a resposta nao apareceu em {self.timeout_s:.0f}s: {self.response_path}"
        )
