"""Evals: the agent against what the linear pipeline already delivered (SPEC-agent 11)."""

from __future__ import annotations

from clipper.evals.models import Baseline, BaselineVideo, EvalReport, VideoComparison

__all__ = ["Baseline", "BaselineVideo", "EvalReport", "VideoComparison", "run_eval"]


def run_eval(*args: object, **kwargs: object) -> object:  # pragma: no cover - thin re-export
    from clipper.evals.runner import run_eval as implementation

    return implementation(*args, **kwargs)  # type: ignore[arg-type]
