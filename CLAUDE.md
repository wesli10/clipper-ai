# Clipper
Ferramenta pessoal de cortes de vídeo (shorts 9:16 e longs 16:9). Leia SPEC.md antes de qualquer mudança.
PLAN.md traduz a §12 da spec em fases; TASKS.md acompanha o progresso.

## Comandos
- uv sync          # instalar
- uv run clipper --help
- uv run pytest    # testes rápidos; `-m slow` para render real
- uv run mypy src  # deve passar em --strict
- uv run ruff check src tests && uv run ruff format

## Regras
- Python 3.12, type hints em tudo, pydantic para dados, sem dicts soltos entre módulos.
- Todo estágio depois da transcrição recebe um Profile; nunca hardcodar resolução, duração ou estilo de legenda fora de profiles.py/config.
- Short = 1080x1920 com reframe; long = 1920x1080 sem crop e sem upscale. Zoom só existe sobre quadro já recortado.
- Nunca chamar API real em testes; providers de LLM são mockados.
- Comandos ffmpeg são montados em video/ffmpeg.py e sempre logados antes de rodar. Ordem de filtros: reframe → zoom → scale → ass.
- Estágios são idempotentes: verificar state.db e arquivos antes de refazer. Ingest/transcrição por vídeo; o resto por (vídeo, perfil).
- Modelos de GPU: carregar → usar → descarregar → torch.cuda.empty_cache(). Nunca dois ao mesmo tempo.
- Timestamps do LLM nunca vão direto para o render; passam por select.snap_boundaries(). Offsets de capítulo e legenda após concat são calculados pelo código.
- Começo e fim do corte têm regras determinísticas (`opens_well` / `is_closing`) e as
  frases que o LLM cita (`hook` / `closing`) são conferidas contra a transcrição. Qualidade
  de corte não se confia ao prompt: o que dá para verificar, verifica-se no código.
- O título e a pergunta da barra só prometem o que o corte entrega: todo número que
  citam tem que ser dito dentro do próprio corte (`unbacked_numbers`, SPEC-agent §5.11.1).
  Loudness é do render, não do conteúdo — nunca é motivo para descartar corte.
- `clipper monitor` lê o state.db em `mode=ro` e o workspace. A página escreve por
  exatamente cinco rotas, cada uma com seu schema e um arquivo conhecido:
  `review_response.json` (SPEC §7.1), `fila.txt`, `channels/<id>.yaml`,
  `<run_id>.control.json` (SPEC-agent §9.5) e `evals/<rodada>/notas.md`
  (SPEC-agent §11.3). Nenhuma delas aceita caminho vindo do request — o nome da
  rodada e o do canal são casados contra o que existe em disco — e nenhuma outra
  escrita entra sem passar pelo mesmo desenho.
- A entrega tem duas metades (SPEC §8): `arquivo/<canal>/<video>/` guarda metadados,
  thumbnail e INDEX.md e nunca leva vídeo; `publicar/<dia>/<destino>/` leva os vídeos e é
  de onde se publica. O dia e o destino de um corte são decididos uma vez, na entrega, e
  ficam em `publish_queue`; quem data um corte é `queue.Calendar`, e ninguém mais.
- No rclone, nunca misturar `--include` com `--exclude`: a ordem de parse é indeterminada
  e os excludes somem. Uma lista única de `--filter`, em ordem, primeiro que casar vence.
- Mensagens ao usuário em português; código, identificadores e commits em inglês.
- Ao terminar uma fase do plano (SPEC §12), rodar testes, atualizar README se necessário e commitar.
