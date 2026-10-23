"""The agent's toolbox: one registry, in the order a run uses them.

The registry is the single place that knows what the model may ask for. The
runtime (SPEC-agent 8) wraps these specs for the SDK; the CLI calls them directly
with ``clipper agent-tool``, which is how they are exercised without a model.
"""

from __future__ import annotations

from clipper.agent.tools import channel, finish, publish, render, select, video
from clipper.agent.tools.base import (
    ToolContext,
    ToolResult,
    ToolSpec,
    error_result,
    parse_clip_id,
    text_result,
    truncate,
)
from clipper.utils.errors import InputError

__all__ = [
    "SPECS",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "error_result",
    "get",
    "names",
    "parse_clip_id",
    "registry",
    "text_result",
    "truncate",
]

#: Declared in the order of a run, which is also the order they are documented in.
SPECS: tuple[ToolSpec, ...] = (
    *select.SPECS[:1],  # list_profiles
    *channel.SPECS[:1],  # load_channel_profile
    *video.SPECS,
    *select.SPECS[1:],
    *render.SPECS,
    *publish.SPECS,
    *channel.SPECS[1:],  # update_channel_profile
    *finish.SPECS,
)


def registry() -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in SPECS}


def names() -> list[str]:
    return [spec.name for spec in SPECS]


def get(name: str) -> ToolSpec:
    spec = registry().get(name)
    if spec is None:
        raise InputError(f"ferramenta desconhecida: '{name}' (existem: {', '.join(names())})")
    return spec
