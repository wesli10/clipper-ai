"""Closing the run (SPEC-agent 5.14).

The report is written from what the agent says it did *and* from what the state
database recorded while it did it - so a summary that does not match the facts is
visible side by side instead of being taken at face value.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from clipper.agent.models import FinishResult
from clipper.agent.tools.base import ToolContext, ToolSpec
from clipper.state import RunStatus


class SkippedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(description="clip_id, ou o tema do trecho quando nem chegou a virar corte.")
    reason: str


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    summary: str = Field(min_length=10, max_length=2000)
    delivered: list[str] = Field(default_factory=list, description="clip_ids publicados.")
    skipped: list[SkippedItem] = Field(default_factory=list)
    notes_for_channel: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="O que valeria guardar no perfil do canal para as proximas runs.",
    )


def _report(ctx: ToolContext, args: FinishInput, run_id: str) -> str:
    state = ctx.state()
    lines = [
        f"# Run {run_id}",
        "",
        f"- video: `{args.video_id}`",
        f"- fim: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "## O que o agent relatou",
        "",
        args.summary.strip(),
        "",
    ]

    if args.delivered:
        lines += ["## Entregue", ""] + [f"- `{clip_id}`" for clip_id in args.delivered] + [""]
    if args.skipped:
        lines += ["## Deixado de fora", ""]
        lines += [f"- `{item.what}`: {item.reason}" for item in args.skipped]
        lines += [""]

    records = state.run_clips(run_id) if run_id else []
    if records:
        lines += [
            "## Medido pelo runtime",
            "",
            "| corte | renders | veredito | detalhe |",
            "|---|---:|---|---|",
        ]
        for record in records:
            lines.append(
                f"| {record.profile} {record.clip_index:02d} | {record.renders} | "
                f"{record.verdict or '-'} | {record.detail or '-'} |"
            )
        lines.append("")

    if args.notes_for_channel:
        lines += ["## Para o perfil do canal", ""]
        lines += [f"- {note}" for note in args.notes_for_channel]
        lines += [""]

    # The "## Run" section is not written here on purpose: this tool runs *during*
    # the loop, when turns and cost are still zero. The runtime appends it once the
    # run is closed and the numbers are real (SPEC-agent 8.4).
    return "\n".join(lines)


def finish_run(ctx: ToolContext, args: FinishInput) -> FinishResult:
    run_id = ctx.run_id or f"manual-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    directory = ctx.settings.video_dir(args.video_id) / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.report.md"
    path.write_text(_report(ctx, args, run_id), encoding="utf-8")

    if ctx.run_id:
        ctx.state().finish_run(
            run_id,
            RunStatus.DONE,
            detail=args.summary.strip()[:200],
        )
    ctx.log("relatorio da run em %s", path)
    return FinishResult(
        run_id=run_id,
        report_path=str(path),
        delivered=len(args.delivered),
        skipped=len(args.skipped),
    )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="finish_run",
        description=(
            "Chame quando a run acabou - publicada ou nao. Resuma o que entregou, o que "
            "refez e por que, e o que deixou de fora. Depois disso a run encerra."
        ),
        input_model=FinishInput,
        handler=finish_run,
        output_limit=200,
        read_only=False,
    ),
)
