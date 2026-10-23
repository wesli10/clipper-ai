"""Running the eval: the agent over the baseline's videos (SPEC-agent 11.2).

Every run is a rehearsal. An eval that published would put a dozen clips in the
Drive that nobody asked for, and the comparison needs the render, not the upload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from clipper.config import Settings
from clipper.evals import baseline as baseline_mod
from clipper.evals import compare as compare_mod
from clipper.evals import report as report_mod
from clipper.evals.models import Baseline, EvalReport, VideoComparison
from clipper.utils.console import console, info, success, warn
from clipper.utils.errors import ClipperError

EVALS_DIR = Path("evals")


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def run_eval(
    settings: Settings,
    baseline: Baseline,
    *,
    profiles: list[str],
    channel: str = "",
    limit: int = 5,
    directory: Path | None = None,
    skip_run: bool = False,
) -> EvalReport:
    """Run the agent over each baseline video, then compare.

    ``skip_run`` compares what is already in the workspace, which is how a report
    is regenerated after you fill in the marks without paying for the runs again.
    """
    from clipper.agent.runtime import run_agent

    chosen = baseline.videos[:limit]
    if not chosen:
        raise ClipperError("o baseline nao tem nenhum video (rode 'clipper eval baseline')")

    out = directory or EVALS_DIR / stamp()
    comparisons: list[VideoComparison] = []
    for position, video in enumerate(chosen, start=1):
        console.rule(f"eval {position}/{len(chosen)}: {video.title or video.video_id}")
        if not skip_run:
            try:
                run_agent(
                    settings,
                    video.url,
                    video_id=video.video_id,
                    profiles=profiles,
                    dry_run=True,
                    channel=channel,
                )
            except ClipperError as exc:
                warn(f"{video.video_id}: a run falhou ({exc}); comparando o que houver")
        comparisons.append(
            compare_mod.compare(
                settings, video, profiles=profiles, built_at=baseline.built_at
            )
        )

    report = report_mod.build(out, comparisons)
    save_comparisons(out, report)
    success(f"eval em {out} ({len(comparisons)} videos)")
    notes = out / report_mod.NOTES_NAME
    info(f"[dim]preencha as notas em {notes} e rode 'clipper eval report'[/dim]")
    return report


def rebuild_report(directory: Path) -> EvalReport:
    """Re-read the marks and rewrite the report, measuring nothing again."""
    path = directory / report_mod.REPORT_NAME
    if not path.exists():
        raise ClipperError(f"nenhum eval em {directory}")
    previous = EvalReport(videos=[])
    stored = directory / "comparisons.json"
    if stored.exists():
        previous = EvalReport.model_validate_json(stored.read_text(encoding="utf-8"))
    return report_mod.build(directory, previous.videos)


def save_comparisons(directory: Path, report: EvalReport) -> Path:
    """Keep the measurements, so the report can be rebuilt from your marks alone."""
    path = directory / "comparisons.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def build_baseline(settings: Settings, *, channel: str | None = None, path: Path) -> Baseline:
    built = baseline_mod.build(settings, channel=channel)
    baseline_mod.save(built, path)
    success(
        f"baseline: {len(built.videos)} video(s), "
        f"{sum(len(v.clips) for v in built.videos)} corte(s) ja entregues"
    )
    return built
