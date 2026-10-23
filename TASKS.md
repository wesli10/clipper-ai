# Clipper — Tasks

Legenda: `[ ]` pendente · `[~]` em andamento · `[x]` concluído

## Fase 1 — Esqueleto
- [x] 1.1 `pyproject.toml` (uv, typer, rich, pydantic, pydantic-settings, pyyaml) + deps de dev (pytest, mypy, ruff)
- [x] 1.2 `models.py`: Word, Segment, Transcript, TimeRange, ClipCandidate, Clip, Chapter, ClipMetadata
- [x] 1.3 `config.py`: Settings pydantic a partir de `config.yaml` + `.env`
- [x] 1.4 `profiles.py`: built-ins `short`/`long`, override por config, `extends`, validação (`zoom: punch_in` + `reframe: none` = erro)
- [x] 1.5 `state.py`: SQLite com status por (vídeo, estágio) e (vídeo, perfil, estágio)
- [x] 1.6 `cli.py`: todos os comandos da §9 registrados; `profiles` e `status` funcionais
- [x] 1.7 `CLAUDE.md`, `.gitignore`, `.env.example`, `config.yaml`
- [x] 1.8 testes: resolução de perfis, state, config
- [x] 1.9 pytest + mypy --strict + ruff verdes; commit

## Fase 2 — Ingest + Transcribe
- [x] 2.1 `sources/base.py` (Protocol VideoSource) e `sources/youtube.py` (normalização de URL, ignorar playlist)
- [x] 2.2 `stages/ingest.py`: yt-dlp, `source.mp4`, `source.info.json`, `audio.wav` 16 kHz mono
- [x] 2.3 Erros claros: privado, age-restricted, > `max_source_duration`
- [x] 2.4 `stages/transcribe.py`: faster-whisper large-v3 int8_float16, word_timestamps, vad_filter
- [x] 2.5 Fallback OOM → `medium`; unload + `torch.cuda.empty_cache()`
- [x] 2.6 testes: seleção de fonte por URL, normalização, parsing do info.json
- [x] 2.7 commit

## Fase 3 — Select short (Gemini)
- [x] 3.1 `llm/base.py` Protocol LLMProvider
- [x] 3.2 `llm/gemini.py`: JSON mode, fallback flash, backoff 429 (3x)
- [x] 3.3 `prompts/select_short.md`
- [x] 3.4 `stages/select.py`: render do transcript com timestamps, parse tolerante (fence markdown), retry 1x
- [x] 3.5 `snap_boundaries` modo `sentence` (recuo, silêncio 0,3 s, fim de frase, padding 0,25/0,4)
- [x] 3.6 Rejeição por sobreposição > 30% e duração fora de [min,max]
- [x] 3.7 testes de fronteira + parsing (LLM mockado)
- [x] 3.8 commit

## Fase 4 — Render short mínimo
- [x] 4.1 `video/ffmpeg.py`: builder de comando, log antes de rodar, `run()` com captura
- [x] 4.2 `video/probe.py`: wrapper ffprobe + verificação de saída
- [x] 4.3 `stages/render.py`: extração por intervalo, `reframe=center`, scale 1080×1920, loudnorm, x264 crf 18
- [x] 4.4 Thumbnail em 1,0 s; retry 1x com `reframe=center`/`zoom=none`
- [x] 4.5 testes de construção de comando; commit

## Fase 5 — Legendas karaokê
- [x] 5.1 `video/subtitles.py`: quebra de linha por palavras/chars/pontuação/gap
- [x] 5.2 Estilo `karaoke` com `\k`, fonte 72, contorno 4, 70% da altura
- [x] 5.3 Escape de `{}`/`\` e acentos PT-BR; deslocamento por offsets
- [x] 5.4 Queima com `ass=` como último filtro
- [x] 5.5 testes do gerador; commit

## Fase 6 — Reframe por rosto
- [x] 6.1 `video/faces.py`: amostragem 1 frame/0,5 s com mediapipe → `faces.json`
- [x] 6.2 `video/reframe.py`: EMA α=0,2, zona morta 5%, regra de 2 rostos
- [x] 6.3 Cenas por `scdet`; crop fixo por cena; fallback `center` se rosto < 60% das amostras
- [x] 6.4 modo `blur` (boxblur de fundo)
- [x] 6.5 testes com `faces.json` sintético; commit

## Fase 7 — Package + Deliver (short)
- [x] 7.1 `prompts/metadata_short.md` + `stages/package.py`
- [x] 7.2 `metadata.json` / `metadata.md` com `source_url` com `&t=`
- [x] 7.3 `stages/deliver.py` modo `folder`
- [x] 7.4 modo `rclone` + `INDEX.md` por vídeo
- [x] 7.5 testes; commit

## Fase 8 — Select long
- [x] 8.1 `prompts/select_long.md` com `chapters[].offset_hint`
- [x] 8.2 `boundary_mode: topic` (gap > 1,5 s, conectores, janela 8 s, fallback sentence)
- [x] 8.3 Multi-segment: ordenação, fusão < 2 s, rejeição de intervalo < 8 s
- [x] 8.4 testes; commit

## Fase 9 — Render long
- [x] 9.1 `reframe=none` com scale só para baixo (nunca upscale)
- [x] 9.2 `video/concat.py` com `xfade`/`acrossfade` de 0,3 s + offsets resultantes
- [x] 9.3 Legenda `classic` (BorderStyle=3, 42 chars, 2 linhas, 90%)
- [x] 9.4 `prompts/metadata_long.md` + capítulos com offsets calculados no código
- [x] 9.5 testes de offsets; commit

## Fase 10 — Review interativo + run
- [x] 10.1 `stages/review.py`: tabela rich, ações a/r/e/d/p, `--auto`
- [x] 10.2 Reaplicar snap após edição; preview com mpv/ffplay
- [x] 10.3 `pipeline.py`: `run` multi-perfil, idempotência, `--force`
- [x] 10.4 `clipper status` com estágios por perfil; códigos de saída 0/2/3/4
- [x] 10.5 testes; commit

## Fase 11 — Zoom dinâmico
- [x] 11.1 `video/zoom.py`: keyframes em fronteiras de frase (gap > 0,4 s, a cada 2–4 frases)
- [x] 11.2 100%→108%, transição 0,15 s, aplicado depois do reframe
- [x] 11.3 testes; commit

## Fase 12 — Providers ollama e file
- [x] 12.1 `llm/ollama.py`: janelas ~6k tokens, 10% overlap
- [x] 12.2 `prompts/merge_windows.md` + merge/dedup
- [x] 12.3 provider `file`: `select_request.md` / `select_response.json`, `--wait`
- [x] 12.4 slash command `/select-clips`
- [x] 12.5 testes; commit

## Fase 13 — Polimento
- [x] 13.1 `clipper clean [--keep-source]`
- [x] 13.2 `clipper doctor` (ffmpeg/ffprobe/yt-dlp/GPU/chave/fonte)
- [x] 13.3 README completo (instalação, rclone, fonte Montserrat)
- [x] 13.4 Mensagens de erro revisadas; commit

---

## Agent (SPEC-agent.md)

### Onde paramos (2026-09-11)

As quatro fases estão implementadas, commitadas e verdes (767 testes rápidos, 12 `slow`,
`mypy --strict`, `ruff`). O agent já publicou de verdade no Drive uma vez e rodou em
ensaio em outros quatro vídeos.

**Para voltar do zero:**

```bash
uv sync --extra agent --extra gpu --extra vision --extra llm --extra torch
uv run clipper run <url> --agent --channel example-channel            # publica
uv run clipper run <url> --agent --channel example-channel --dry-run  # ensaia
uv run clipper monitor                                                # sessão, fila, canais
uv run clipper eval baseline && uv run clipper eval run --limit 3 --profile short
```

**O que está esperando você**, em ordem de valor:

1. Preencher `evals/rodada1/notas.md` (12 cortes, coluna `nota` de 1 a 5) e rodar
   `uv run clipper eval report evals/rodada1`. É a única métrica que falta.
2. Dizer se conserto os dois shorts do UNO que estão no Drive sem a pergunta na barra
   (publicados antes do conserto; dá para refazer sem gastar cota de modelo).
3. `profiles.default: [short]` no `config.yaml`, se o `clipper run` legado também não
   deve mais gerar long.
4. Se `--agent` vira o padrão do `clipper run`.

Detalhes e o que ficou decidido: SPEC-agent §16.


### Fase A — ferramentas, registro, validação
- [x] A.1 `state.py`: tabelas `runs` e `run_clips`, `RunStatus`, ordenação estável por run_id
- [x] A.2 `agent/models.py`: resultados das ferramentas + `ProfileOverride` (lista branca da §5.8)
- [x] A.3 `agent/review.py`: laudo determinístico — duração, resolução, áudio, loudness
      (ebur128), legenda (sync, densidade, largura, colisão com o banner), fronteiras,
      primeiro quadro (signalstats), quiz; checagens de rosto dormentes (§2.1)
- [x] A.4 `video/ffmpeg.py`: `loudness_args`/`parse_loudness`, `frame_luma_args`/`parse_frame_luma`
- [x] A.5 `stages/select.py`: `window` e `extra` em `build_prompt`/`select_clips`; `prompt_chars`
- [x] A.6 `stages/render.py`: `RenderResult.reused` (reaproveitar não gasta render do orçamento)
- [x] A.7 `agent/tools/`: 13 ferramentas com schema, validação, erro como `is_error` e teto de saída
- [x] A.8 `clipper agent-tools` / `agent-tool` (escondidos) para dirigir a caixa de ferramentas à mão
- [x] A.9 testes: 77 unitários (28 ferramentas, 28 laudo, 12 overrides, 5 CLI, 4 state) + 2 de
      integração `slow` com ffmpeg real (vídeo inteiro pelas ferramentas)
- [ ] A.10 confirmar com você: `profiles.default: [short]` no `config.yaml` (hoje ainda gera long
      no modo legado)

### Fase B — runtime do loop
- [x] B.1 `budget.py` (fusíveis moles e duros), `log.py` (JSONL com redação de segredo),
      `runtime.py` (loop, nudge, parada dura, fecho pelo runtime), extra `agent` no pyproject
- [x] B.2 `clipper run --agent/--legacy` e `--dry-run`; bloco `agent:` no `config.yaml`
- [x] B.3 sessão do agent no monitor: `monitor/agent.py` (leitura incremental por offset),
      3 rotas novas, resumo no snapshot e a seção na página (simples e detalhes)
- [x] B.4 54 testes novos com sessão falsa + fiação real do SDK sem chamar modelo
- [x] B.5 run real de ponta a ponta, em ensaio, num espelho do workspace (hard links) do
      `RfCT-LJkfx8`: 4 shorts, 5 renders, 29 chamadas, 0 erros, 6,1 min, US$ 0,86 de custo
      estimado no loop. Quatro defeitos encontrados e corrigidos:
      - o porteiro de orçamento em `can_use_tool` nunca era consultado (`allowed_tools`
        auto-aprova antes); passou para dentro do handler
      - turnos contavam mensagens de resultado do SDK (1 por run inteira) em vez de
        respostas do modelo
      - `input_tokens` sozinho é quase zero numa conversa com cache; passou a somar
        `cache_creation` e a reportar `cache_read` à parte
      - o laudo não via a passarela: nova checagem `tail_roll` (aviso)
      - o relatório lia a linha da run antes de ela ser fechada e saía com zeros

### Fase C — system prompt, perfil de canal e controle
- [x] C.1 `prompts/agent_system.md` calibrado na 1ª run: laudo antes do próximo render (ele
      renderizou 3 e revisou depois), `commit_clips` substitui a lista inteira, faixa de
      duração é regra e não sugestão, e o canal como primeira leitura
- [x] C.2 `channel.py` + `channels/exemplo.yaml` + `learned.json` (5 notas por run, teto de
      50, datadas) + `load_channel_profile`/`update_channel_profile` + `--channel`;
      `render_defaults` do canal entram por baixo dos overrides da run
- [x] C.3 `config.yaml`: short passa a 25/50/63s de fala (≈32/57/70s de arquivo)
- [x] C.4 `channels/exemplo.yaml`: perfil de canal (tom de título, hashtags fixas
      garantidas pelo código em `set_metadata`)
- [x] C.5 a página deixou de ser só leitura: quatro escritas, cada uma com schema e um
      arquivo conhecido (`monitor/writes.py`). Controle da sessão (pausar — que **espera**
      em vez de recusar —, parar com ou sem entregar, vetar corte), fila editável
      (acrescentar, tirar, reordenar, cabeçalho preservado) e editor de canal. CLAUDE.md
      e SPEC-agent §9.5 atualizados
- [x] C.6 run real com o canal: 2 shorts publicados no Drive, títulos no tom do canal,
      jabá recusado pelo `avoid`, 3 anotações no `learned.json`. US$ 1,45, 10,2 min

### Depois (pedidos seus, fora das fases da spec)
- [ ] Tom de título por formato: o canal hoje tem um `title.tone` só, mas os títulos reais
      têm duas vozes (shorts em caixa baixa, longs em Title Case). Só importa quando o
      `long` voltar.

### Fase D — evals
- [x] D.1 `clipper eval baseline`: reconstrói dos `metadata.json` no Drive (5 vídeos,
      22 cortes já entregues, sem nada mantido à mão)
- [x] D.2 `clipper eval run` (sempre em ensaio) e `report`: sobreposição IoU com
      pareamento guloso, renders por corte, turnos, custo, fails do laudo, e `notas.md`
      separado para a sua nota — o relatório é reescrito, as notas não
- [x] D.3 o relatório avisa quando o baseline de um vídeo veio do próprio agent (já
      publicado por ele), em vez de reportar 100% de sobreposição consigo mesmo
- [x] D.4 rodado de verdade nos 3 vídeos com baseline do pipeline linear: **76% de
      sobreposição média**, 1,0–1,75 renders por corte, US$ 4,82, 37:52 de loop. Achou
      dois defeitos:
      - `loudnorm` de uma passada não acerta o alvo (mediana -14,3, pior -16,9 contra
        -14). O laudo reprovava e o agent gastava um render que não consertava nada.
        Render passou a medir antes e normalizar com o medido: o corte que reprovava
        com -15,2 foi para -14,3
      - `self_baseline` marcava todo vídeo do eval, porque a run do próprio eval conta
        como run anterior; agora compara com a data do baseline
- [ ] D.5 sua nota em `evals/rodada1/notas.md` (12 cortes), depois `clipper eval report`
- [ ] D.6 `max_turns` de 40 cortou a run do vídeo de 47 min pela metade; medir e subir

---

## Validado nesta sessão

Com ffmpeg 7.0.2 + ffprobe 6.0 reais, sem rede e sem API:

- [x] 301 testes rápidos + 6 testes `slow` de render real, `mypy --strict` e `ruff` verdes
- [x] short renderiza 1080×1920 com legenda karaokê queimada, thumbnail e áudio
- [x] long de fonte 1280×720 sai em 1280×720 (nunca upscale)
- [x] long multi-trecho junta 2 intervalos com crossfade; offsets `[0.0, 19.7]` para 20 s + 18 s
- [x] corte de 1,0 s → 4,0 s dá exatos 3,00 s (frame-exato, sem `-c copy`)
- [x] todas as cadeias de filtro rodam: center, blur, face, punch_in, none, sem legenda
- [x] pipeline completo select → review → render → package → deliver, com pasta do Drive
      contendo `shorts/`, `longs/` e `INDEX.md`, sem `intermediates/`
- [x] reexecutar não chama o LLM nem re-renderiza

## Pendente de validação (precisa de você)

Depende de rede e de coisas que não estão configuradas nesta máquina:

- [x] `GEMINI_API_KEY` configurada e validada. Os `gemini-2.5-*` da spec foram
      descontinuados para contas novas e o free tier tem `limit: 0` nos modelos `pro`;
      o padrão agora é `gemini-flash-latest` com fallback `gemini-flash-lite-latest`
- [x] Seleção real: 5 shorts (43–67 s) e 1 long multi-trecho (2 intervalos, 135 s),
      todos começando e terminando em frase completa
- [x] Metadados reais gerados pelo LLM nos dois perfis, com capítulos do long
      (`00:00`, `01:25`) calculados dos offsets do concat
- [x] Detecção de rosto validada com foto real: 100% de cobertura, zona morta
      segurando ~1,5 s por passo, 7 degraus acompanhando um rosto que atravessa o quadro
- [x] Download do modelo `large-v3` (2,9 GB) e transcrição real na 4060 — 26 min de
      entrevista PT-BR em 146 s (10,7× tempo real; extrapolando, 2 h em ~11 min, dentro
      dos 15 min da §11.2). 547 segmentos, 3.315 palavras, 99,5% com timestamp,
      confiança média 0,941, VRAM liberada ao fim (1992 MiB, igual à linha de base)
- [x] Ajuste de fronteiras validado contra a transcrição real: 800 fronteiras nos dois
      modos, nenhuma fora de um limite de segmento; 200 intervalos com padding, nenhum
      cortando dentro de uma palavra
- [x] Short real renderizado com karaokê e acentuação PT-BR corretos, a −14,8 LUFS
- [~] Aceite da §11.2, vídeo 1 de 3 (YouTube `1-BgoqVRrYQ`, 19:19, PT-BR, conversa
      com várias pessoas): 5 shorts + 2 longs entregues no Drive, `rclone check` com
      0 diferenças em 25 arquivos. Nenhum corte começa ou termina no meio de frase;
      shorts em 1080×1920 com rosto enquadrado em 4 de 5 (o 5º cai para `center` por
      ser plano aberto); longs em 1920×1080 sem crop; capítulos com offsets corretos.
      **Ressalva:** o canal já queima legendas próprias no vídeo, então os shorts saem
      com legenda dupla — use `subtitle_style: off` para fontes assim.
      Faltam os outros 2 vídeos do aceite (podcast 1 pessoa, aula com slides).
- [ ] Fonte Montserrat instalada (hoje cai no fallback DejaVu, que funciona)
- [x] Google Drive conectado via `rclone` 1.75.1, remote `gdrive` autorizado.
      Entrega real verificada: `shorts/`, `longs/` e `INDEX.md` na estrutura da §8,
      sem `intermediates/`, e `rclone check` com **0 diferenças / 5 arquivos** por
      checksum. Pendencia futura: o `client_id` compartilhado do rclone sera
      desativado durante 2026 (ver README).
- [x] `clipper monitor`: painel web somente-leitura (stdlib, sem dependência nova) com
      fila, diagrama de fluxo por vídeo/perfil, galeria dos cortes com player e log ao
      vivo por SSE. Roda junto com um `batch` em andamento; `ingest` e `deliver` passaram
      a registrar `running` no `state.db` para o painel não parecer travado
- [x] Revisao pelo navegador (`--review web`): a pipeline grava `review_request.json`
      e espera; o monitor mostra um corte por vez tocando o trecho do proprio
      `source.mp4`, e grava `review_response.json` com as decisoes
- [x] Porteiro de abertura (`opens_well`, `hook_is_present`, `advance_to_opening`): o
      comeco do corte passou a ter regras deterministicas como o fim ja tinha. Medido
      numa transcricao real: 64% das frases servem de abertura, 95% dos pontos tem uma
      a <=2 frases; das 11 aberturas reais dos cortes ja publicados, 10 sao reprovadas
      e nenhuma abertura boa do controle foi
- [x] Fila de publicação no Drive (SPEC §8.1): a entrega passou a ter duas metades —
      `arquivo/<canal>/<video>/` com metadados, thumbnail e INDEX.md (sem vídeo) e
      `publicar/<dia>/<destino>/` com os vídeos daquele dia e um `PUBLICAR.md` do lado.
      Publicar deixou de ser navegar quatro níveis e baixar corte por corte: é abrir a
      pasta do dia e baixar de uma vez. Destino por canal de origem e formato em
      `publish.destinations`; dia decidido uma vez na entrega e gravado em
      `publish_queue`. `clipper queue` mostra a fila e `clipper queue-migrate` adota a
      entrega antiga com move server-side (nada sobe de novo)

