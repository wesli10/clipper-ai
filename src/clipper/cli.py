"""Typer CLI (SPEC 9). User-facing text is Portuguese; identifiers are English."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from clipper.config import ConfigError, Settings, load_settings
from clipper.models import SourceInfo
from clipper.profiles import ProfileError
from clipper.state import NO_PROFILE, PROFILE_STAGES, VIDEO_STAGES, RunStatus, State
from clipper.utils.console import console, error, success, warn
from clipper.utils.errors import EXIT_EXTERNAL, EXIT_INPUT, ClipperError

app = typer.Typer(
    name="clipper",
    help="Transforma videos do YouTube em cortes prontos para publicar.",
    no_args_is_help=True,
    add_completion=False,
)

_CONFIG_OPTION = typer.Option("--config", "-c", help="Caminho para um config.yaml alternativo.")


def handle_errors[F: Callable[..., Any]](func: F) -> F:
    """Map :class:`ClipperError` to the exit codes of SPEC 9."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (ConfigError, ProfileError) as exc:
            error(str(exc))
            raise typer.Exit(EXIT_INPUT) from exc
        except ClipperError as exc:
            if exc.exit_code == 0:
                console.print(f"[yellow]{exc}[/yellow]")
            else:
                error(str(exc))
            raise typer.Exit(exc.exit_code) from exc

    return wrapper  # type: ignore[return-value]


def _settings(config: Path | None) -> Settings:
    return load_settings(config)


def _indices(raw: str | None) -> list[int]:
    """Parse ``1,3`` into clip indices."""
    if not raw:
        return []
    try:
        return [int(part) for part in raw.replace(" ", ",").split(",") if part]
    except ValueError as exc:
        raise ClipperError(f"lista de indices invalida: {raw!r} (use por exemplo 1,3)") from exc


def _review_mode(settings: Settings, requested: str | None, *, auto: bool, default: str) -> Any:
    """`--review` wins over `--auto`, and both win over the config."""
    from clipper.config import ReviewMode

    chosen = requested or ("auto" if auto else default)
    if chosen not in ("ask", "auto", "web"):
        raise ClipperError(f"modo de revisao desconhecido: {chosen!r} (use ask, auto ou web)")
    mode: ReviewMode = chosen  # type: ignore[assignment]
    return mode


def _resolve_profiles(settings: Settings, requested: str | None) -> list[str]:
    """Turn ``--profile short,long`` (or the config default) into validated names."""
    if requested:
        names = [p.strip() for p in requested.split(",") if p.strip()]
    else:
        names = list(settings.default_profiles)
    if not names:
        raise ClipperError("nenhum perfil selecionado (use --profile ou profiles.default)")
    for name in names:
        settings.profile(name)
    return names


# --------------------------------------------------------------------------- #
# Introspection commands
# --------------------------------------------------------------------------- #


@app.command("profiles")
@handle_errors
def profiles_cmd(
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
    name: Annotated[str | None, typer.Argument(help="Mostra apenas este perfil.")] = None,
) -> None:
    """Lista os perfis e seus parametros efetivos."""
    settings = _settings(config)
    names = [name] if name else settings.profile_names()

    table = Table(title="Perfis efetivos", show_lines=False)
    table.add_column("perfil", style="bold")
    table.add_column("dur min/alvo/max", justify="right")
    table.add_column("n", justify="right")
    table.add_column("interv", justify="right")
    table.add_column("fronteira")
    table.add_column("saida")
    table.add_column("reframe")
    table.add_column("zoom")
    table.add_column("legenda")

    for profile_name in names:
        profile = settings.profile(profile_name)
        default_mark = " *" if profile_name in settings.default_profiles else ""
        table.add_row(
            f"{profile.name}{default_mark}",
            f"{profile.duration_min:g}/{profile.duration_target:g}/{profile.duration_max:g}s",
            str(profile.n_clips),
            str(profile.max_segments) if profile.multi_segment else "1",
            profile.boundary_mode,
            f"{profile.output_width}x{profile.output_height}@{profile.fps}",
            profile.reframe,
            profile.zoom,
            profile.subtitle_style,
        )
    console.print(table)
    console.print("[dim]* perfil incluido em profiles.default[/dim]")


@app.command("status")
@handle_errors
def status_cmd(
    video_id: Annotated[str | None, typer.Argument(help="Video a inspecionar.")] = None,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Mostra os estagios concluidos, por video e por perfil."""
    settings = _settings(config)
    state = State(settings.state_db)

    videos = state.list_videos()
    if video_id:
        videos = [v for v in videos if v.video_id == video_id]
        if not videos:
            raise ClipperError(f"video '{video_id}' nao esta no workspace")
    if not videos:
        console.print("[dim]nenhum video no workspace ainda[/dim]")
        return

    for info in videos:
        records = {(r.profile, r.stage): r for r in state.stages_for(info.video_id)}
        resolution = f"{info.width}x{info.height}" if info.width else "resolucao desconhecida"
        table = Table(title=f"{info.video_id} - {info.title or '(sem titulo)'} [{resolution}]")
        table.add_column("escopo", style="bold")
        for stage in (*VIDEO_STAGES, *PROFILE_STAGES):
            table.add_column(stage)

        def cell(profile_key: str, stage: str, *, applicable: bool) -> str:
            if not applicable:
                return "[dim]-[/dim]"
            record = records.get((profile_key, stage))  # noqa: B023
            if record is None:
                return "[dim].[/dim]"
            colors = {"done": "green", "failed": "red", "running": "yellow"}
            color = colors.get(str(record.status), "dim")
            return f"[{color}]{record.status}[/{color}]"

        table.add_row(
            "video",
            *[cell(NO_PROFILE, s, applicable=True) for s in VIDEO_STAGES],
            *[cell(NO_PROFILE, s, applicable=False) for s in PROFILE_STAGES],
        )
        seen = {p for p, _ in records if p != NO_PROFILE}
        profile_keys = sorted(seen | set(settings.default_profiles))
        for profile_key in profile_keys:
            table.add_row(
                profile_key,
                *[cell(profile_key, s, applicable=False) for s in VIDEO_STAGES],
                *[cell(profile_key, s, applicable=True) for s in PROFILE_STAGES],
            )
        console.print(table)
        _warn_about_source_resolution(settings, info)


def _warn_about_source_resolution(settings: Settings, info: SourceInfo) -> None:
    """SPEC 13: a source below a profile's resolution is never upscaled - say so."""
    if not info.width or not info.height:
        return
    for name in settings.default_profiles:
        profile = settings.profile(name)
        if profile.reframe != "none":
            continue
        if info.width < profile.output_width or info.height < profile.output_height:
            warn(
                f"perfil '{name}': a fonte e {info.width}x{info.height}, menor que "
                f"{profile.output_width}x{profile.output_height}. Os cortes saem na "
                "resolucao da fonte (nunca fazemos upscale)."
            )


# --------------------------------------------------------------------------- #
# Pipeline commands
# --------------------------------------------------------------------------- #

_PROFILE_OPTION = typer.Option(
    "--profile", "-p", help="Perfis, separados por virgula (default: profiles.default)."
)
_FORCE_OPTION = typer.Option("--force", help="Refaz o estagio mesmo se ja concluido.")


@app.command("run")
@handle_errors
def run_cmd(
    url: Annotated[str, typer.Argument(help="URL do video no YouTube.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    auto: Annotated[bool, typer.Option("--auto", help="Aprova os melhores sem revisao.")] = False,
    review: Annotated[
        str | None,
        typer.Option("--review", help="ask (terminal) | auto | web (decide no monitor)."),
    ] = None,
    lang: Annotated[
        str | None, typer.Option("--lang", help="Forca o idioma da transcricao.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="gemini | ollama | file")
    ] = None,
    agent: Annotated[
        bool,
        typer.Option(
            "--agent/--legacy",
            help="Deixa o agent decidir a run (SPEC-agent) em vez do pipeline linear.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="So com --agent: ensaia sem enviar nada ao Drive."),
    ] = False,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="So com --agent: canal de publicacao (channels/<id>.yaml)."),
    ] = None,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Remove o workspace do video depois de entregar."),
    ] = False,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Executa o pipeline inteiro: ingest, transcribe e, por perfil, ate a entrega.

    Com ``--agent`` quem conduz e o agent (SPEC-agent): ele decide quantos cortes,
    revisa o proprio render e refaz o que precisar. ``--legacy`` e o padrao.
    """
    from clipper import pipeline

    settings = _settings(config)

    if agent:
        from clipper.agent.runtime import run_agent

        names = _resolve_profiles(settings, profile) if profile else []
        outcome = run_agent(
            settings, url, profiles=names or None, dry_run=dry_run, channel=channel or ""
        )
        if clean and outcome.status is not RunStatus.FAILED:
            pipeline.clean_video(settings, outcome.video_id)
        if outcome.status is RunStatus.FAILED:
            raise typer.Exit(EXIT_EXTERNAL)
        return

    if dry_run or channel:
        raise ClipperError("--dry-run e --channel so existem com --agent")
    names = _resolve_profiles(settings, profile)
    video_id = pipeline.run(
        settings,
        url,
        names,
        review_mode=_review_mode(settings, review, auto=auto, default=settings.review.mode),
        language=lang,
        provider=provider,
        force=force,
    )
    if clean and video_id:
        pipeline.clean_video(settings, video_id)


@app.command("batch")
@handle_errors
def batch_cmd(
    queue: Annotated[
        list[str],
        typer.Argument(
            help="Arquivo de fila (uma URL por linha) ou as URLs direto.",
        ),
    ],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="gemini | claude | ollama | file")
    ] = None,
    review: Annotated[
        str | None,
        typer.Option("--review", help="auto (padrao) | web: a fila espera voce no monitor."),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep", help="Nao limpa o workspace depois de entregar."),
    ] = False,
    min_free: Annotated[
        float,
        typer.Option("--min-free", help="Interrompe a fila abaixo deste espaco livre, em GB."),
    ] = 15.0,
    stop_on_error: Annotated[
        bool,
        typer.Option("--stop-on-error", help="Para na primeira falha em vez de seguir."),
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Processa uma fila de videos sem supervisao, limpando cada um ao entregar.

    Por padrao aprova sozinha (--review auto): nao ha ninguem no terminal. Com
    --review web a fila para em cada perfil e espera voce decidir no monitor. Se
    voce passar um arquivo, ele e relido a cada video, entao da para ir
    acrescentando URLs com a fila rodando.
    """
    from clipper import pipeline

    settings = _settings(config)
    names = _resolve_profiles(settings, profile)

    source: Path | list[str]
    if len(queue) == 1 and not queue[0].startswith(("http://", "https://")):
        source = Path(queue[0])
    else:
        source = queue

    results = pipeline.batch(
        settings,
        source,
        names,
        provider=provider,
        review_mode=_review_mode(settings, review, auto=False, default="auto"),
        clean=not keep,
        min_free_gb=min_free,
        stop_on_error=stop_on_error,
    )
    if any(r.status in ("falhou", "sem espaco") for r in results):
        raise typer.Exit(EXIT_EXTERNAL)


@app.command("ingest")
@handle_errors
def ingest_cmd(
    url: Annotated[str, typer.Argument(help="URL do video no YouTube.")],
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Baixa o video e extrai o audio."""
    from clipper import pipeline

    pipeline.ingest(_settings(config), url, force=force)


@app.command("transcribe")
@handle_errors
def transcribe_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video ja baixado.")],
    lang: Annotated[str | None, typer.Option("--lang", help="Forca o idioma.")] = None,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Transcreve o audio com timestamps por palavra."""
    from clipper import pipeline

    pipeline.transcribe(_settings(config), video_id, language=lang, force=force)


@app.command("select")
@handle_errors
def select_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    n: Annotated[int | None, typer.Option("--n", help="Sobrescreve n_clips do perfil.")] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="gemini | ollama | file")
    ] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help="Provider 'file': espera o select_response.json aparecer.",
        ),
    ] = True,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Escolhe os trechos com o LLM e ajusta as fronteiras."""
    from clipper import pipeline

    settings = _settings(config)
    for name in _resolve_profiles(settings, profile):
        pipeline.select(
            settings, video_id, name, n_clips=n, provider=provider, wait=wait, force=force
        )


@app.command("review")
@handle_errors
def review_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    auto: Annotated[bool, typer.Option("--auto", help="Aprova os melhores sem perguntar.")] = False,
    web: Annotated[
        bool, typer.Option("--web", help="Decide no monitor, no navegador, em vez do terminal.")
    ] = False,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="Aprova estes indices, sem prompt (ex.: 1,3)."),
    ] = None,
    reject: Annotated[
        str | None,
        typer.Option("--reject", help="Rejeita estes indices, sem prompt (ex.: 2)."),
    ] = None,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Revisao interativa: aprovar, rejeitar, editar intervalos.

    Precisa de um terminal. Para decidir sem interacao - de um script, ou de um
    shell sem tty - use --approve/--reject.
    """
    from clipper import pipeline

    settings = _settings(config)
    for name in _resolve_profiles(settings, profile):
        pipeline.review(
            settings,
            video_id,
            name,
            mode=_review_mode(settings, "web" if web else None, auto=auto, default="ask"),
            approve=_indices(approve),
            reject=_indices(reject),
            force=force,
        )


@app.command("render")
@handle_errors
def render_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    clip: Annotated[int | None, typer.Option("--clip", help="Renderiza so este indice.")] = None,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Renderiza os cortes aprovados."""
    from clipper import pipeline

    settings = _settings(config)
    for name in _resolve_profiles(settings, profile):
        pipeline.render(settings, video_id, name, only_clip=clip, force=force)


@app.command("package")
@handle_errors
def package_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="gemini | ollama | file")
    ] = None,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Gera titulo, descricao, hashtags e capitulos de cada corte."""
    from clipper import pipeline

    settings = _settings(config)
    for name in _resolve_profiles(settings, profile):
        pipeline.package(settings, video_id, name, provider=provider, force=force)


@app.command("deliver")
@handle_errors
def deliver_cmd(
    video_id: Annotated[str, typer.Argument(help="Id do video.")],
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    force: Annotated[bool, _FORCE_OPTION] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Copia os cortes para o Google Drive."""
    from clipper import pipeline

    settings = _settings(config)
    pipeline.deliver(settings, video_id, _resolve_profiles(settings, profile), force=force)


@app.command("disk")
@handle_errors
def disk_cmd(
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Mostra o que o workspace esta ocupando e quanto ja pode ser removido."""
    from clipper import pipeline

    pipeline.disk_usage(_settings(config))


@app.command("clean")
@handle_errors
def clean_cmd(
    video_id: Annotated[
        str | None, typer.Argument(help="Id do video. Omita e use --delivered.")
    ] = None,
    keep_source: Annotated[
        bool,
        typer.Option("--keep-source", help="Preserva source.mp4, audio.wav e transcript.json."),
    ] = False,
    delivered: Annotated[
        bool,
        typer.Option("--delivered", help="Limpa todos os videos ja entregues no Drive."),
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Remove os artefatos do video do workspace."""
    from clipper import pipeline

    settings = _settings(config)
    if delivered:
        pipeline.clean_delivered(settings, keep_source=keep_source)
        return
    if video_id is None:
        raise ClipperError("informe o id do video ou use --delivered")
    pipeline.clean_video(settings, video_id, keep_source=keep_source)


def _as_date(value: str | None) -> date | None:
    """``AAAA-MM-DD`` from the command line, or nothing."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClipperError(f"data invalida: {value!r} (use AAAA-MM-DD)") from exc


@app.command("publish-plan")
@handle_errors
def publish_plan_cmd(
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="So este canal de origem (ex.: example-channel)."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option("--start", help="So a partir desta data (AAAA-MM-DD). Default: hoje."),
    ] = None,
    all_days: Annotated[
        bool, typer.Option("--all", help="Inclui tambem os dias que ja passaram.")
    ] = False,
    thumbnails: Annotated[
        bool,
        typer.Option(
            "--thumbnails/--no-thumbnails",
            help="Incluir o prompt de thumbnail para o Gemini nos longs.",
        ),
    ] = True,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Arquivo de saida.")] = None,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Monta um arquivo unico com a fila de publicacao, em ordem.

    Le a fila (o dia e o destino de cada corte foram decididos na entrega) e
    deixa titulo, descricao, tags e caminho do arquivo prontos para colar no
    YouTube Studio. Cada dia tambem leva o seu proprio PUBLICAR.md no Drive.
    """
    from clipper import pipeline

    pipeline.publish_plan(
        _settings(config),
        channel=channel,
        start=_as_date(start),
        thumbnails=thumbnails,
        output=output,
        all_days=all_days,
    )


@app.command("queue")
@handle_errors
def queue_cmd(
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="So este canal de origem (ex.: example-channel)."),
    ] = None,
    all_days: Annotated[
        bool, typer.Option("--all", help="Inclui tambem os dias que ja passaram.")
    ] = False,
    remove: Annotated[
        list[str] | None,
        typer.Option(
            "--remover",
            help="Tira da fila (id do video, ou parte do titulo). O arquivo fica no Drive.",
        ),
    ] = None,
    replan: Annotated[
        bool,
        typer.Option("--replanejar", help="Reescreve o PUBLICAR.md de cada dia no Drive."),
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Mostra a fila de publicacao: que corte sai em que dia e para onde."""
    from clipper import pipeline

    settings = _settings(config)
    if remove:
        pipeline.queue_remove(settings, remove)
        return
    if replan:
        pipeline.queue_replan(settings, all_days=all_days)
        return
    pipeline.queue_view(settings, channel=channel, all_days=all_days)


@app.command("queue-migrate")
@handle_errors
def queue_migrate_cmd(
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="So este canal de origem (ex.: example-channel)."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option("--start", help="Primeiro dia da fila (AAAA-MM-DD). Default: amanha."),
    ] = None,
    skip: Annotated[
        int,
        typer.Option(
            "--skip",
            help="Deixa os N primeiros cortes fora da fila (os que voce ja publicou).",
        ),
    ] = 0,
    skip_video: Annotated[
        list[str] | None,
        typer.Option(
            "--pular-video",
            help="Deixa todo corte deste video fora da fila (id ou parte do nome da pasta).",
        ),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--aplicar", help="Move de verdade. Sem isso, so mostra o plano.")
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Coloca na fila o que ja esta no Drive da entrega antiga.

    Move cada video para `publicar/<dia>/<destino>/` (dentro do Drive, sem
    baixar nem subir nada) e manda o resto para `arquivo/`. Mostra o plano e nao
    mexe em nada sem `--aplicar`.
    """
    from clipper import pipeline

    pipeline.queue_migrate(
        _settings(config),
        channel=channel,
        start=_as_date(start),
        skip=skip,
        skip_videos=skip_video or [],
        apply=apply,
    )


@app.command("monitor")
@handle_errors
def monitor_cmd(
    port: Annotated[int | None, typer.Option("--port", "-p", help="Porta do servidor.")] = None,
    host: Annotated[str | None, typer.Option("--host", help="Endereco de escuta.")] = None,
    queue: Annotated[
        Path | None, typer.Option("--queue", help="Arquivo de fila a acompanhar.")
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Abre o navegador ao subir.")
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Abre uma pagina web para acompanhar a pipeline em tempo real.

    So le: state.db e aberto em modo somente-leitura, entao pode rodar junto com
    um `clipper batch` em andamento.
    """
    from clipper.monitor import serve

    settings = _settings(config)
    queue_path = queue
    if queue_path is None:
        default_queue = Path("fila.txt")
        queue_path = default_queue if default_queue.exists() else None
    serve(
        settings,
        host=host or settings.monitor.host,
        port=port or settings.monitor.port,
        queue_path=queue_path,
        open_browser=open_browser,
    )


@app.command("doctor")
@handle_errors
def doctor_cmd(
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Verifica dependencias externas: ffmpeg, ffprobe, yt-dlp, GPU, chave da API, fontes."""
    from clipper import pipeline

    pipeline.doctor(_settings(config))


# --------------------------------------------------------------------------- #
# Agent (SPEC-agent). Hidden while the loop itself is not wired up: these exist
# so the toolbox can be driven by hand and by the tests, without a model.
# --------------------------------------------------------------------------- #


@app.command("agent-tools", hidden=True)
@handle_errors
def agent_tools_cmd(
    name: Annotated[str | None, typer.Argument(help="Mostra o schema desta ferramenta.")] = None,
) -> None:
    """Lista as ferramentas do agent, ou o schema de uma delas."""
    import json

    from clipper.agent import tools

    if name is not None:
        spec = tools.get(name)
        console.print(f"[bold]{spec.name}[/bold]  {'(le)' if spec.read_only else '(escreve)'}")
        console.print(spec.description)
        console.print_json(json.dumps(spec.input_schema, ensure_ascii=False))
        return

    table = Table(title="ferramentas do agent")
    table.add_column("nome")
    table.add_column("efeito")
    table.add_column("teto")
    table.add_column("quando usar")
    for spec in tools.SPECS:
        table.add_row(
            spec.name,
            "le" if spec.read_only else "escreve",
            str(spec.output_limit),
            spec.description.split(".")[0] + ".",
        )
    console.print(table)


@app.command("agent-tool", hidden=True)
@handle_errors
def agent_tool_cmd(
    name: Annotated[str, typer.Argument(help="Nome da ferramenta.")],
    payload: Annotated[str, typer.Argument(help="Argumentos em JSON.")] = "{}",
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Perfis habilitados nesta chamada (ex.: short,quiz)."),
    ] = None,
    run_id: Annotated[
        str, typer.Option("--run-id", help="Id da run, para o registro no state.db.")
    ] = "",
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Executa uma ferramenta do agent e imprime a resposta como o modelo a veria."""
    import json

    from clipper.agent import tools

    settings = _settings(config)
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ClipperError(f"argumentos nao sao JSON valido: {exc}") from exc
    if not isinstance(raw, dict):
        raise ClipperError("os argumentos devem ser um objeto JSON")

    enabled = tuple(_resolve_profiles(settings, profile))
    context = tools.ToolContext(settings=settings, run_id=run_id, enabled_profiles=enabled)
    result = tools.get(name).call(context, raw)
    console.print(result.text)
    if result.is_error:
        raise typer.Exit(EXIT_INPUT)


# --------------------------------------------------------------------------- #
# Evals (SPEC-agent 11)
# --------------------------------------------------------------------------- #

evals_app = typer.Typer(
    name="eval",
    help="Compara o agent com o que o pipeline linear ja entregou.",
    no_args_is_help=True,
)
app.add_typer(evals_app)

_BASELINE_OPTION = typer.Option("--baseline", help="Arquivo do baseline.")


@evals_app.command("baseline")
@handle_errors
def eval_baseline_cmd(
    path: Annotated[Path, _BASELINE_OPTION] = Path("evals/baseline.json"),
    channel: Annotated[
        str | None, typer.Option("--channel", help="So os videos deste canal de origem.")
    ] = None,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Reconstroi o baseline a partir dos cortes ja entregues no Drive."""
    from clipper.evals.runner import build_baseline

    built = build_baseline(_settings(config), channel=channel, path=path)
    table = Table(title="baseline")
    table.add_column("video")
    table.add_column("cortes", justify="right")
    for video in built.videos:
        table.add_row(video.title or video.video_id, str(len(video.clips)))
    console.print(table)


@evals_app.command("run")
@handle_errors
def eval_run_cmd(
    path: Annotated[Path, _BASELINE_OPTION] = Path("evals/baseline.json"),
    profile: Annotated[str | None, _PROFILE_OPTION] = None,
    channel: Annotated[str | None, typer.Option("--channel", help="Canal do agent.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Quantos videos comparar.")] = 5,
    out: Annotated[Path | None, typer.Option("--out", help="Pasta do resultado.")] = None,
    compare_only: Annotated[
        bool,
        typer.Option("--compare-only", help="So compara o que ja esta no workspace."),
    ] = False,
    config: Annotated[Path | None, _CONFIG_OPTION] = None,
) -> None:
    """Roda o agent nos videos do baseline (sempre em ensaio) e compara."""
    from clipper.evals.baseline import load
    from clipper.evals.runner import run_eval

    settings = _settings(config)
    names = _resolve_profiles(settings, profile) if profile else settings.agent_profiles()
    run_eval(
        settings,
        load(path),
        profiles=names,
        channel=channel or "",
        limit=limit,
        directory=out,
        skip_run=compare_only,
    )


@evals_app.command("report")
@handle_errors
def eval_report_cmd(
    directory: Annotated[Path, typer.Argument(help="Pasta de um eval ja rodado.")],
) -> None:
    """Rele as suas notas e reescreve o relatorio, sem medir nada de novo."""
    from clipper.evals.runner import rebuild_report

    report = rebuild_report(directory)
    if report.notes:
        success(f"nota media {report.mean_note:.1f} em {len(report.notes)} cortes")
    else:
        warn("nenhuma nota preenchida ainda")


if __name__ == "__main__":  # pragma: no cover
    app()
