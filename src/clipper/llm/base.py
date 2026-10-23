"""LLM provider protocol and the shared JSON-response handling (SPEC 6.4)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from clipper.utils.errors import ExternalError

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can answer a prompt with a validated pydantic model."""

    name: str
    max_context_tokens: int

    def complete_json[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T:
        """Return an instance of ``schema`` built from the model's answer."""
        ...


def extract_json(text: str) -> str:
    """Pull the JSON object out of an answer that may be wrapped in markdown."""
    stripped = text.strip()
    fenced = _FENCE_RE.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("nenhum objeto JSON encontrado na resposta")
    return stripped[start : end + 1]


def parse_json_response[T: BaseModel](text: str, schema: type[T]) -> T:
    """Parse and validate; raises ``ValueError`` with a message fit for a retry."""
    payload = extract_json(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON invalido: {exc}") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"JSON fora do schema esperado: {exc}") from exc


CORRECTION_TEMPLATE = (
    "Sua resposta anterior nao pode ser lida como JSON valido no schema pedido.\n"
    "Erro: {error}\n\n"
    "Responda de novo com o mesmo conteudo, mas SOMENTE com o objeto JSON, "
    "sem texto em volta e sem cercas de markdown."
)


class BaseLLMProvider(ABC):
    """Implements the one-retry-on-bad-JSON rule of SPEC 6.1 step 3."""

    name: str = "base"
    max_context_tokens: int = 32_000

    @abstractmethod
    def generate(self, system: str, user: str) -> str:
        """Raw completion; subclasses handle transport, retries and fallbacks."""

    def complete_json[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T:
        text = self.generate(system, user)
        try:
            return parse_json_response(text, schema)
        except ValueError as first_error:
            retry_user = f"{user}\n\n---\n{CORRECTION_TEMPLATE.format(error=first_error)}"
            retry_text = self.generate(system, retry_user)
            try:
                return parse_json_response(retry_text, schema)
            except ValueError as second_error:
                raise ExternalError(
                    f"o provider '{self.name}' nao devolveu JSON valido em duas tentativas.\n"
                    f"1a: {first_error}\n2a: {second_error}"
                ) from second_error
