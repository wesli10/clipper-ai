# Clipper — Especificação do agent

Sucessora parcial da [SPEC.md](SPEC.md): **o código atual é a fonte de verdade**, e esta
spec descreve a camada nova. A §15 lista o que a SPEC.md não reflete mais.

Nada aqui está implementado. Entregável 1 de 3 do `PROMPT-agent-clipper.md`; a
implementação começa depois da sua aprovação.

---

## 1. Objetivo

O pipeline hoje é linear e as decisões editoriais estão distribuídas entre o
`config.yaml` (quantos cortes, quais perfis), o prompt de seleção (quais trechos) e você
(revisão). O agent passa a decidir, dentro de limites que o runtime impõe:

1. quantos cortes o vídeo rende e em quais dos perfis habilitados (§2.2);
2. se um trecho renderizado ficou bom e, se não, o que mudar e refazer;
3. estender ou encurtar um trecho por falta de contexto;
4. título, descrição e tags no estilo do canal.

O pipeline atual não deixa de existir: vira o conjunto de ferramentas do agent e
continua executável do jeito que está (§12).

**Critério de sucesso da v1:** empatar em qualidade com o pipeline atual usando menos
intervenção manual. Não é superar.

---

## 2. Decisões tomadas (2026-09-10)

| # | Decisão | Justificativa |
|---|---|---|
| 1 | O loop roda no **`claude-agent-sdk`** (Python), autenticado pela assinatura do Claude Code | A SPEC §2 descarta a API da Anthropic por não ter créditos; o SDK usa a credencial do CLI, que já está logado nesta máquina (nenhum `ANTHROPIC_API_KEY` definido). Ele dá loop, tool-calling, `max_turns`, hooks e permissão por ferramenta — código que eu teria de escrever de qualquer forma, com pior tratamento de erro. |
| 2 | **Autônomo até o Drive** | A run inteira roda sem parada; a auditoria é o log JSONL e o relatório final. O `review` humano continua existindo para o modo legado (§12). |
| 3 | `review_render` é **só determinístico** | Sem modelo de visão: geometria, `faces.json`, `ffprobe`, LUFS medido e o `.ass` conferido contra a transcrição. Reprodutível, testável sem rede e sem chamada extra por render. O custo: não pega "legenda em cima de um gráfico" (§14, risco 3). |
| 4 | Evals **reconstruídos do Drive** | Os `metadata.json` entregues têm `source_url` e `source_segments`: dá para montar o baseline dos trechos escolhidos pelo pipeline atual sem reprocessar nada do legado. |

### 2.1 Restrições de formato e de enquadramento

Duas coisas que o código permite mas que **não estão em uso**, e que o agent não pode
tratar como opção:

- **Enquadramento por rosto está desligado.** Não há solução de tracking boa o
  bastante, então o short sai com o quadro 16:9 inteiro sobre fundo desfocado
  (`reframe: blur`, o que o `config.yaml` já faz hoje). O agent não escolhe
  enquadramento: `reframe` e `zoom` ficam fora da lista branca (§5.8) e as duas
  checagens de rosto do laudo ficam dormentes (§6). Nada é removido do código — o dia
  em que o tracking prestar, volta pela configuração e as checagens acordam sozinhas.
- **Long está desabilitado.** A run gera só 9:16. `long`, `podcast` e `long_vertical`
  continuam implementados e testados, mas fora dos perfis habilitados do canal (§7):
  `list_profiles` não os devolve, e `find_candidates` recusa o perfil desligado com uma
  mensagem dizendo isso. Quem habilita é o perfil de canal, nunca o agent.

Consequência prática: a decisão "em quais perfis" da §1 fica entre `short` e `quiz` —
os dois 9:16 — e a decisão que mais pesa vira **quantos** cortes o vídeo rende.

### 2.2 Dois modelos, papéis diferentes

O prompt pede que eu proponha e justifique. Proposta:

| Papel | Quem | Por quê |
|---|---|---|
| **Loop** (decide, avalia, refaz, escreve título) | Claude pelo `claude-agent-sdk`, `model="claude-opus-5"` | É a decisão editorial e de orquestração. Nunca vê a transcrição inteira: vê o mapa (`get_outline`), trechos sob demanda (`inspect_range`) e resumos de ferramenta. |
| **Seleção** (varre a transcrição inteira e propõe candidatos) | O provider atual de `select` (`config.yaml → llm.select_provider`, hoje `claude`), com os prompts de `prompts/select_*.md` | Uma transcrição de 2 h tem ~2.000 frases. Enfiar isso no contexto do loop tornaria cada turno caro e jogaria fora prompts que já foram calibrados. A ferramenta `find_candidates` encapsula essa chamada e devolve ao loop só a lista resumida. |
| **Metadados** | O loop escreve; `set_metadata` valida | É onde "estilo do canal" entra, e o loop é quem tem o perfil de canal no contexto. O `build_metadata_batch` atual vira fallback (§5.11). |

Efeito colateral bom: o contexto do loop fica na casa de poucos milhares de tokens por
turno, então o limite de turnos é o que controla o custo, não o tamanho da transcrição.

---

## 3. Arquitetura

```
clipper run --agent <url>
        │
        ▼
  agent/runtime.py ──── claude-agent-sdk (ClaudeSDKClient) ──── Claude (loop)
        │                        │
        │                        └── intenções de chamada (tool_use)
        │
        ├── agent/budget.py      limites: turnos, renders, tempo, custo
        ├── agent/log.py         JSONL: run_id, tool, entrada, saída truncada, ms, erro
        ├── agent/channel.py     perfil de canal (config + aprendido)
        └── agent/tools/*.py     14 ferramentas → chamam pipeline.py / stages/*
                                        │
                                        ▼
                            o pipeline atual, sem alteração de comportamento
```

Regras que a implementação não pode violar:

1. **O modelo nunca executa nada.** `tools=[]` e `setting_sources=[]` nas opções do SDK:
   nenhuma ferramenta embutida (Bash, Read, Write, Edit, WebFetch) fica disponível, e o
   `CLAUDE.md` do repo não entra no contexto do agent. O modelo só enxerga as 14
   ferramentas do servidor MCP in-process.
2. **Validação no runtime.** Cada handler valida a entrada contra o schema declarado e
   contra o estado real (o corte existe? o perfil existe? o intervalo cabe no vídeo?)
   antes de tocar em qualquer coisa.
3. **Saídas resumidas.** Nenhuma ferramenta devolve transcrição inteira, `clips.json`
   inteiro nem stdout do ffmpeg. Cada uma tem um teto de caracteres declarado (§5).
4. **Erro é resultado, não exceção.** Todo handler captura `ClipperError` e devolve
   `{"is_error": True, "content": [...]}` com uma mensagem acionável em português. O
   loop segue.
5. **Efeito colateral declarado.** Só `publish_clips` escreve fora do workspace.
6. **Idempotência preservada.** Toda ferramenta consulta `state.db` + arquivos antes de
   refazer, como as etapas fazem hoje. Rodar o agent duas vezes no mesmo vídeo não
   re-transcreve nem re-renderiza.

### 3.1 Arquivos novos

```
src/clipper/agent/
    __init__.py
    runtime.py        loop, limites, encerramento, relatório final
    budget.py         contadores e a política de esgotamento
    log.py            JSONL por run
    models.py         pydantic dos resultados de ferramenta (nada de dict solto)
    channel.py        perfil de canal: carga, merge, escrita do bloco aprendido
    review.py         as checagens determinísticas de render (§6)
    tools/
        __init__.py   registro: nome → handler + schema + anotações
        video.py      get_video, transcribe_video, get_outline, inspect_range
        select.py     list_profiles, find_candidates, snap_clip, commit_clips
        render.py     render_clip, review_render
        publish.py    set_metadata, publish_clips
        channel.py    load_channel_profile, update_channel_profile
        finish.py     finish_run
src/clipper/monitor/
    agent.py          JSONL -> AgentRunView, leitura incremental com offset (§9)
    models.py         + AgentRunView, BudgetView, AgentStepView, AgentClipView, CheckView
    server.py         + rotas /api/agent*, + o controle da sessão (§9.5)
    static/index.html + a seção da sessão, nos dois modos
prompts/agent_system.md
channels/<id>.yaml                       configuração do canal, escrita por você
workspace/channels/<id>.learned.json     o que o agent aprendeu, escrito por ele
workspace/<video_id>/agent/<run_id>.jsonl
workspace/<video_id>/agent/<run_id>.report.md
workspace/<video_id>/agent/<run_id>.control.json   pausar/parar/vetar, vindo da página
```

Nada em `stages/`, `video/`, `profiles.py` ou `models.py` muda de comportamento. As
mudanças nesses arquivos são aditivas e estão listadas na §4.

---

## 4. Mapeamento: etapa atual → ferramenta

| Etapa hoje | Ferramenta | Reaproveitado | O que muda |
|---|---|---|---|
| `pipeline.ingest` | `get_video` | tudo | Passa a devolver resumo em vez de imprimir no console; o estado `running`/`done` continua igual. |
| `pipeline.transcribe` | `transcribe_video` | tudo | Nada. Whisper continua local na GPU. |
| — (novo) | `get_outline` | `select.sentences`, `TOPIC_GAP_S` | Mapa determinístico do vídeo (blocos por pausa de tópico) para o loop decidir onde olhar, sem ler a transcrição inteira. |
| `review` (preview/leitura) | `inspect_range` | `Transcript.text_between`, `select.opens_well`, `is_closing`, `sentences` | Substitui "olhar no terminal": devolve o texto com margem e diz se as fronteiras servem. |
| `settings.profile` / `clipper profiles` | `list_profiles` | `profiles.resolve_profile` | O agent precisa saber quais formatos existem e com que faixa de duração antes de planejar. |
| `pipeline.select` | `find_candidates` | `select.select_clips` inteiro (prompt, janelamento, retry, `snap_boundaries`) | Deixa de gravar `clips.json` direto; devolve candidatos ao loop, inclusive **os rejeitados com o motivo** — hoje isso só vai para o log, e é exatamente o que o agent precisa para corrigir. |
| `select.snap_candidate` | `snap_clip` | idem | Expõe o snap isolado: quando o agent quer estender/encurtar um trecho, o intervalo dele passa pelas mesmas regras determinísticas. Nunca existe timestamp de modelo indo direto para o render. |
| `pipeline.save_clips` | `commit_clips` | `models.ClipSet`, `select.to_clips` | Materializa a decisão do agent em `profiles/<perfil>/clips.json` — o formato que render, package, deliver, `status` e `monitor` já leem. |
| `pipeline.review` | *(sem ferramenta)* | — | No modo agent não há revisão humana (decisão 2). O código continua para o modo legado. |
| `pipeline.render` | `render_clip` | `stages.render.render_clip` inteiro | Um corte por chamada (o paralelismo por `ThreadPoolExecutor` fica no modo legado; aqui o paralelo é o agent pedir vários renders em sequência dentro do orçamento). Aceita overrides de perfil da lista branca (§5.8). |
| `stages.render.verify` | `review_render` | `verify`, `faces.json`, `reframe.crop_expression`, `subtitles`, `ffprobe` | **Novo de fato.** Hoje o `verify` só checa duração/resolução/áudio e levanta exceção. Vira uma bateria de checagens que devolve laudo (§6). |
| `pipeline.package` | `set_metadata` | `package.assemble`, `chapter_offsets`, `source_url_for`, `to_markdown` | O texto passa a vir do loop; offsets de capítulo continuam calculados pelo código. `build_metadata_batch` vira fallback. |
| `pipeline.deliver` + `deliver_index` | `publish_clips` | tudo | Única ferramenta com efeito colateral externo. Uma chamada por vídeo, com a lista de perfis. |
| `clipper publish-plan` | — | — | Fora do escopo do agent: continua um comando à parte, rodado depois. |
| — (novo) | `load_channel_profile` / `update_channel_profile` | — | §7. |
| — (novo) | `finish_run` | — | Encerramento explícito com relatório estruturado. |

### 4.1 Mudanças aditivas no código existente

- `stages/select.py`: expor `RejectedClip` no resultado já é feito (`SelectionResult.rejected`);
  falta só um `to_public()` para resumir. `snap_candidate` já é chamável isolado.
- `stages/render.py`: `render_clip` ganha um parâmetro `overrides: ProfileOverride | None`
  que é aplicado com `profile.model_copy(update=...)` **depois** da validação do
  `Profile` — a validação do pydantic continua sendo a única porta de entrada.
- `stages/package.py`: `assemble` passa a aceitar um `MetadataResponse` vindo de fora
  (é o que `build_metadata_batch` já produz internamente).
- `state.py`: nova tabela `runs` (run_id, video_id, começo, fim, status, turnos, custo) e
  `run_clips` (run_id, perfil, índice, renders gastos, veredito). Migração aditiva como a
  de `upload_date`.
- `cli.py`: `clipper run --agent` e `clipper agent-report <run_id>`.

Nenhuma assinatura existente muda de forma incompatível.

---

## 5. Ferramentas

Convenções para todas:

- Nome e parâmetros em inglês (CLAUDE.md); `description` em português, orientada a
  **quando usar**, não ao que faz.
- Schema declarado como JSON Schema completo (`required`, `enum`, mínimos e máximos) —
  o dict simplificado do `@tool` não expressa enum nem faixa.
- Toda saída é um pydantic de `agent/models.py` serializado como JSON compacto, com o
  teto de caracteres da tabela. Passou do teto, trunca e diz que truncou.
- `readOnlyHint=True` em tudo que não escreve, para o SDK poder paralelizar.
- Erro sempre volta como `is_error` com a mensagem em português e, quando existir, a
  correção sugerida.
- Toda ferramenta é idempotente: chamada duas vezes com a mesma entrada, o segundo
  resultado é igual ao primeiro e nada é refeito (o campo `reused: true` diz isso).

| Ferramenta | Escrita? | Teto da saída |
|---|---|---|
| `list_profiles` | não | 1.500 |
| `get_video` | workspace | 600 |
| `transcribe_video` | workspace | 600 |
| `get_outline` | não | 4.000 |
| `inspect_range` | não | 3.000 |
| `find_candidates` | workspace (`candidates.json`) | 4.000 |
| `snap_clip` | não | 1.200 |
| `commit_clips` | workspace (`clips.json`) | 1.200 |
| `render_clip` | workspace | 800 |
| `review_render` | não | 2.500 |
| `drop_clip` | workspace (`clips.json`) | 400 |
| `set_metadata` | workspace | 800 |
| `publish_clips` | **Drive** | 800 |
| `load_channel_profile` | não | 3.000 |
| `update_channel_profile` | workspace | 400 |
| `finish_run` | workspace (relatório) | 200 |

### 5.1 `list_profiles`

> Use antes de planejar a run, para saber quais formatos existem, quanto cada um dura e
> quantos cortes ele costuma render.

Entrada: `{}`.

Devolve **só os perfis habilitados para o canal da run** (§2.1, §7): hoje `short` e
`quiz`. Os desabilitados não aparecem, para o modelo não planejar em cima deles.

Saída: por perfil — `name`, `duration_min/target/max`, `n_clips`, `multi_segment`,
`max_segments`, `boundary_mode`, `reframe` (informativo, não ajustável),
`subtitle_style`, `card_seconds`, `output_width/height`, `min_score`, e `overridable`
(a lista branca da §5.8).

### 5.2 `get_video`

> Use como primeira ferramenta da run: baixa o vídeo se ainda não estiver no workspace e
> devolve o que se sabe sobre ele.

```json
{"type":"object","properties":{
  "url":{"type":"string","description":"URL do YouTube. Omita se já souber o video_id."},
  "video_id":{"type":"string"}},
 "required":[]}
```
Exige exatamente um dos dois. Saída: `video_id`, `title`, `channel`, `duration_s`,
`width`, `height`, `language`, `upload_date`, `reused`, `stages_done` (o que o
`state.db` já registra para este vídeo, por perfil).

Erros: URL não reconhecida, vídeo privado, duração acima de `max_source_duration`.

### 5.3 `transcribe_video`

> Use depois de `get_video`. Roda o whisper local na GPU (minutos, não segundos) ou
> reaproveita a transcrição em cache.

Entrada: `video_id`, `language` opcional. Saída: `language`, `duration_s`,
`n_segments`, `n_words`, `transcript_path`, `reused`, `elapsed_s`.

Nunca devolve texto. O texto se lê por `get_outline` e `inspect_range`.

### 5.4 `get_outline`

> Use para escolher onde olhar. Devolve o vídeo dividido em blocos por pausa de tópico,
> com a primeira frase de cada bloco.

Entrada: `video_id`, `max_blocks` (padrão 40, máx. 80).

Saída: lista de `{start, end, first_sentence, n_sentences}`. Construção determinística:
`sentences()` + corte onde o gap passa de `TOPIC_GAP_S`, blocos vizinhos fundidos até
caber em `max_blocks`. Nenhuma chamada de modelo.

### 5.5 `inspect_range`

> Use antes de decidir estender, encurtar ou aceitar um trecho: mostra o que é dito ali,
> com margem antes e depois, e diz se as fronteiras servem de abertura e de fecho.

Entrada: `video_id`, `start`, `end`, `margin_s` (padrão 8, máx. 30).

Saída: frases dentro do intervalo com `[start→end]`, frases da margem marcadas como
`antes:` / `depois:`, e um bloco `boundaries`:

```json
{"opens_well": false, "opening_sentence": "Segundo, o que já estão falando…",
 "is_closing": true, "gap_before_s": 0.12, "gap_after_s": 1.8,
 "ends_on_question": false,
 "suggested_start": 412.4, "suggested_end": 468.9}
```

`suggested_*` vem de `snap_range` + `advance_to_opening`, ou seja: o que o pipeline faria
com esse intervalo. Texto truncado no teto, sempre mantendo as bordas.

### 5.6 `find_candidates`

> Use para pedir uma varredura da transcrição inteira em busca de trechos de um perfil.
> É a chamada mais cara da run (um modelo lê todo o texto); não peça duas vezes o mesmo
> perfil sem mudar os critérios.

```json
{"type":"object","properties":{
  "video_id":{"type":"string"},
  "profile":{"type":"string","description":"Nome de um perfil de list_profiles."},
  "n_candidates":{"type":"integer","minimum":1,"maximum":20},
  "focus":{"type":"string","description":"Instrução extra, ex.: 'só trechos sobre CLT'. Opcional."},
  "time_window":{"type":"object","properties":{"start":{"type":"number"},"end":{"type":"number"}},
                 "description":"Restringe a varredura. Opcional."}},
 "required":["video_id","profile"]}
```

Faz o que `pipeline.select` faz, menos gravar `clips.json`: monta o prompt do perfil,
chama o provider de seleção, roda `snap_boundaries`. Saída:

```json
{"accepted":[{"id":"short#1","start":412.4,"end":468.9,"duration":56.5,
              "segments":[[412.4,468.9]],"score":86,"topic":"…","hook":"…",
              "reason":"…","self_contained":true}],
 "rejected":[{"topic":"…","reason":"duração 14.2s abaixo do mínimo do perfil (20s)"}],
 "n_prompt_chars":184213,"provider":"claude"}
```

Os rejeitados vêm com o motivo porque é isso que permite ao agent corrigir (estender um
trecho curto, trocar a abertura) em vez de repetir a mesma varredura.

`focus` e `time_window` são acrescentados ao prompt como um bloco separado, nunca
substituem o template do perfil.

### 5.7 `snap_clip`

> Use sempre que quiser mudar um intervalo — estender para pegar o contexto que falta,
> encurtar, juntar dois pedaços. Nunca invente um timestamp e mande direto para o render.

Entrada: `video_id`, `profile`, `segments` (lista de `[start, end]`, 1..`max_segments`),
`hook` e `closing` opcionais (as frases citadas, conferidas contra a transcrição).

Saída: ou o candidato ajustado (`segments` finais, duração, o que mudou e por quê), ou
`accepted: false` com o motivo exato vindo de `snap_candidate` — abaixo do mínimo, abre
no meio de outra fala, termina numa pergunta, fecho citado não está no fim, etc.

Não grava nada. É a ferramenta que o agent usa para iterar sobre fronteiras de graça,
antes de gastar um render.

### 5.8 `commit_clips`

> Use quando tiver decidido os cortes de um perfil. Fixa a lista, que é o que o render e
> a entrega vão usar.

Entrada:

```json
{"video_id":"…","profile":"short",
 "clips":[{"segments":[[412.4,468.9]],"topic":"…","hook":"…","reason":"…","score":86,
           "card_text":"","comment_prompt":"","reveal_at":null,
           "chapters":[{"offset_hint":"…"}]}],
 "overrides":{"reframe":"blur","tail_roll_s":3}}
```

Cada item passa por `snap_candidate` de novo (defesa em profundidade), recebe índice e
slug, e o conjunto vira `ClipSet` em `profiles/<perfil>/clips.json` com status
`approved` — o `renderable()` atual já trata isso. Sobrescrever um `clips.json` existente
apaga os diretórios órfãos pelo `prune_stale_clips` que já existe.

`overrides` é a lista branca de campos do `Profile` que o agent pode ajustar por perfil,
por vídeo:

`n_clips`, `tail_roll_s`, `subtitle_style`, `banner.enabled`, `card_seconds`,
`duration_min/target/max` (só **dentro** da faixa do perfil configurado), `min_score`.

`reframe` e `zoom` ficam **fora** enquanto o enquadramento por rosto estiver desligado
(§2.1): o quadro inteiro é decisão de configuração, e `punch_in` sobre `blur` ampliaria
o fundo desfocado em vez de um quadro recortado — o que a regra "zoom só sobre quadro já
recortado" do CLAUDE.md proíbe.

Fora dessa lista é erro. Resolução, fonte, cores, caminho de outro e prompts **não** são
ajustáveis pelo modelo: são configuração (CLAUDE.md), e a lista branca é validada pelo
`Profile` do pydantic antes de valer.

### 5.9 `render_clip`

> Use um corte por vez. Cada render custa minutos de CPU e conta no orçamento da run;
> confira o que puder por `snap_clip` e `inspect_range` antes.

Entrada: `video_id`, `profile`, `index`, `overrides` opcional (mesma lista branca),
`force` (padrão `false`; refazer um corte já renderizado exige `force: true` e gasta
outro render do orçamento).

Saída: `clip_id` (`<video>/<perfil>/<índice>`), `path`, `duration_s`, `width`, `height`,
`degraded`, `reused`, `elapsed_s`, `renders_left`.

O handler chama `stages.render.render_clip` sem tocar na cadeia de filtros: mesma ordem
reframe → zoom → scale → ass, mesmo fallback degradado, mesmo `verify`.

### 5.10 `review_render`

> Use depois de todo render. Devolve um laudo de checagens objetivas e, quando alguma
> falha, o parâmetro que costuma resolver.

Entrada: `clip_id`. Saída: §6.

### 5.10.1 `drop_clip`

> Use quando desistir de um corte que já foi renderizado — o laudo reprova por algo que
> está na fonte, ou o trecho não se sustenta.

Entrada: `clip_id`, `reason`. Saída: `clip_id`, `reason`, `remaining`, `rendered`,
`packaged`, `reused` (`true` quando o corte já estava descartado).

Marca o corte como `rejected` no `clips.json` e recalcula as linhas de `render` e
`package` (§8.4.1).

**Recusa descarte por defeito de render.** Se o único `fail` do corte for `loudness` e
ainda sobrar render no orçamento dele (`max_clip_renders`), a ferramenta recusa e manda
refazer. Loudness é propriedade do render, não do que foi dito, e tratá-la como veredito
sobre o conteúdo joga fora material bom: medido, o corte da reação ao Rolex foi
descartado a −16,9 LUFS e voltou a −14,1 da mesma fonte, sem mexer na fronteira, depois
que a normalização passou a medir antes de normalizar. Esgotado o orçamento do corte, o
descarte passa. Sem essa ferramenta, desistir de um corte renderizado é ambíguo e o
sistema resolve a ambiguidade das duas maneiras erradas: o contador que fecha `package`
fica esperando o corte para sempre, e o `publish_clips` escreve pelo lote os metadados
que o agent deliberadamente não escreveu e publica o corte assim mesmo.

O arquivo do render **não** é apagado: ele continua no workspace para ser conferido, só
sai da lista de `renderable()`.

### 5.11 `set_metadata`

> Use depois que o corte passou na revisão. Escreva o título e as tags no estilo do canal
> (`load_channel_profile`), não no estilo genérico de YouTube.

```json
{"type":"object","properties":{
  "clip_id":{"type":"string"},
  "title":{"type":"string","maxLength":100},
  "hook_line":{"type":"string","maxLength":120},
  "description":{"type":"string","maxLength":2000},
  "hashtags":{"type":"array","items":{"type":"string"},"maxItems":5},
  "tags":{"type":"array","items":{"type":"string"},"maxItems":15},
  "chapter_titles":{"type":"array","items":{"type":"string"}},
  "thumbnail_text":{"type":"string","maxLength":40},
  "thumbnail_concept":{"type":"string","maxLength":200}},
 "required":["clip_id","title"]}
```

O handler **recusa um título que promete um número que o corte não diz** — ver §5.11.1 —
e valida (tamanho, hashtag sem espaço, idioma igual ao da transcrição, número de
`chapter_titles` igual ao número de intervalos), calcula **os offsets de capítulo pelo
código** a partir de `clip.offsets`, monta o `source_url` com `&t=`, e grava
`metadata.json` + `metadata.md` com o `to_markdown` atual.

Se o agent não chamar `set_metadata` para um corte renderizado, o runtime chama
`build_metadata_batch` (o caminho atual) antes de publicar. Nenhum corte vai para o Drive
sem metadados.

#### 5.11.1 O que um título pode prometer

O agent lê o vídeo inteiro; quem assiste vê 40 segundos. A diferença entre as duas
leituras vaza para o título, e o laudo (§6) não enxerga isso: mede duração, loudness,
legenda e fronteira, e um título mentiroso passa nas doze checagens.

Medido numa rodada real: um short saiu titulado *"briguei por 10 mil num carro e passei
4 anos sem falar com ele"*, sobre um corte que termina em 159,0 s — e a primeira vez que
alguém fala em dez mil é 159,0 s, a fala seguinte, fora do corte. A pergunta na barra
(`comment_prompt`) repetia o mesmo número. Foi o único corte da rodada com nota 1.

A parte verificável de uma promessa é o número que ela cita. Então: **todo número do
título e do `comment_prompt` tem que ser dito dentro do próprio corte**, conferido contra
`clip_text`. Dígito e palavra são a mesma promessa (`10 mil` = `dez mil`); o artigo `um`/
`uma` não conta como número, porque em "um carro em dez mil" ele não é. `set_metadata`
recusa o título, `commit_clips` recusa a pergunta, e o gerador em lote (§5.11, o caminho
que o runtime usa por você) registra o aviso no log do estágio.

É o mesmo princípio do `hook`/`closing` conferidos contra a transcrição (CLAUDE.md): o
que dá para verificar, verifica-se no código.

### 5.12 `publish_clips`

> Última ferramenta da run. Envia para o Drive tudo que foi renderizado e tem metadados.
> Só chame quando não pretender refazer mais nada: é a única ação que sai desta máquina.

Entrada: `video_id`, `profiles` (lista), `dry_run` (padrão `false`).

`dry_run: true` devolve o que seria enviado sem enviar — útil para o agent conferir a
lista antes. Saída: destino, número de arquivos, bytes, e os cortes pulados com o motivo.
Reaproveita `deliver` + `deliver_index` inteiros, incluindo o `INDEX.md` por vídeo.

Limite de orçamento: no máximo 2 chamadas efetivas por run (uma normal, uma para
recuperar de falha parcial).

### 5.13 `load_channel_profile` / `update_channel_profile`

§7.

### 5.14 `finish_run`

> Chame quando a run acabou — publicada ou não. Resuma o que entregou, o que refez e o
> que deixou de fora.

Entrada: `summary` (texto), `delivered` (lista de `clip_id`), `skipped`
(`[{clip_id|topic, reason}]`), `notes_for_channel` (opcional: o que valeria guardar no
perfil do canal). O runtime grava o relatório e encerra o loop.

---

## 6. `review_render`: as checagens

Todas determinísticas, todas sobre arquivos que já existem. Cada uma devolve
`{"check": nome, "status": "ok|warn|fail", "value": …, "expected": …, "fix": …}`.

| Checagem | Como | Falha quando | `fix` sugerido |
|---|---|---|---|
| `duration` | `ffprobe` vs duração esperada | diferença > 0,3 s (`DURATION_TOLERANCE_S`, já existe) | re-render |
| `resolution` | `ffprobe` vs perfil | diferente do esperado | re-render |
| `audio_track` | `ffprobe` | sem faixa de áudio | re-render |
| `loudness` | `ffmpeg -af ebur128` no arquivo final | \|I − `loudnorm_i`\| > 1,0 LU | re-render |
| `face_in_frame` *(dormente)* | amostras de `faces.json` reprojetadas na janela de crop reconstruída por `to_steps`/`crop_expression` | < 80% das amostras com o centro do rosto dentro do crop, **ou** alguma com mais de 25% da caixa fora | aviso para você: o enquadramento precisa voltar a `blur` |
| `face_coverage` *(dormente)* | `reframe.coverage` | < 40% | idem |
| `caption_sync` | primeiro evento do `.ass` vs primeira palavra do intervalo; último evento vs última palavra | primeiro evento > 0,5 s depois da fala, ou último evento depois do fim escolhido | re-render; se persistir, `subtitle_style: off` |
| `caption_density` | eventos por segundo de fala | < 0,15 evento/s em trecho com fala (legenda sumindo) | re-render |
| `caption_fit` | chars por linha e nº de linhas dos eventos gerados vs `SubtitleStyle` | qualquer linha > `max_chars_per_line` + 10% ou nº de linhas > `max_lines` | `subtitle.max_chars_per_line` (fora da lista branca → vira aviso para você, §14) |
| `caption_collision` | y da legenda vs y do banner vs barra do `blur` | sobreposição vertical | `banner.enabled: false` |
| `opening` | `opens_well` + `hook_is_present` na primeira frase do corte | abertura não se sustenta sozinha | `snap_clip` com outro início |
| `closing` | `is_closing` + `ends_on_question` no fim | termina em pergunta sem resposta | `snap_clip` estendendo o fim |
| `cut_in_pause` | os *word timestamps* nas bordas | alguma palavra está sendo dita no instante do corte | `snap_clip` |
| `tail_roll` | frases que começam dentro da passarela | alguma fala nova começa depois do fim escolhido | `tail_roll_s` menor (aviso, não reprovação) |
| `first_frame` | luminância média do `thumbnail.jpg` (`signalstats`) | < 8/255 (frame preto) ou > 250/255 (estourado) | `render.thumbnail_at_s` maior, ou outro início |
| `quiz_parts` | `reveal_at` vs `card_min_part_s` | pergunta ou resposta mais curta que o mínimo | `snap_clip` movendo o reveal |

`tail_roll` não estava na spec: apareceu na primeira run real, em que todas as outras
checagens passaram e o short mesmo assim terminava dentro do assunto seguinte, porque a
passarela de 3 s pulou a pausa. É aviso, não reprovação — a passarela é ambiência de
propósito, e o que o laudo faz é dizer quanto dela virou fala nova.

`cut_in_pause` mudou de forma na implementação: a spec pedia um mínimo de silêncio
em cada borda, e isso reprovaria todo corte bem feito — o padding da SPEC 6.1 corre de
propósito até a palavra vizinha, então a folga costuma ser exatamente zero. O que a
checagem verifica é o que de fato importa e o pipeline garante: que nenhuma palavra
esteja sendo dita no instante do corte.

As duas checagens de rosto só rodam quando o perfil usa um `reframe` que recorta
(`face` ou `center`). Com o enquadramento por rosto desligado (§2.1) elas nunca disparam,
e o código delas fica testado com `faces.json` sintético esperando o dia em que voltarem.
No lugar delas, quem carrega o peso no formato atual são `caption_collision` e
`caption_fit`: com `blur` existe barra acima do quadro, é lá que o banner mora, e é onde
legenda e pergunta de comentário podem se atropelar.

Saída resumida: `verdict` (`ok` | `warn` | `fail`), a lista só com o que não deu `ok`, e
`renders_left`. `verdict: fail` é o sinal de "vale gastar um render"; `warn` é "aceitável,
registre e siga".

Tudo isso é testável com `faces.json` sintético, `.ass` gerado e `ffprobe` real — o
padrão que os testes atuais já usam.

---

## 7. Perfil de canal

"Canal" aqui é **o seu canal de publicação**, não o canal de origem do vídeo. O nome
`profile` já está tomado por formato, então no código isto é `Channel` / `ChannelProfile`,
nunca `profile`.

Duas metades, deliberadamente separadas:

**`channels/<id>.yaml` — sua configuração, versionada, o agent nunca escreve:**

```yaml
id: example-channel
name: "Canal de exemplo"
language: pt-BR
# Formatos habilitados. É esta lista que o `list_profiles` devolve; o que não está
# aqui não existe para o agent. `long` sai por decisão sua (§2.1), não por falta de
# código — para religar, basta acrescentá-lo.
profiles: [short, quiz]
weekly_target: {short: 5}
title:
  tone: "direto, em minúsculas, sem promessa que o corte não cumpre"
  max_chars: 70
  avoid: ["VOCÊ NÃO VAI ACREDITAR", "emoji no começo", "reticências"]
  examples: ["o que ninguém te conta sobre ser CLT", "…"]
editorial:
  prefer: ["história pessoal com desfecho", "discordância entre os convidados"]
  avoid: ["jabá", "piada interna sem contexto"]
render_defaults:
  short: {tail_roll_s: 3}
  quiz: {card_seconds: 2.4}
```

`reframe` não aparece aqui de propósito: enquanto o enquadramento por rosto estiver
desligado, ele é decisão do `config.yaml` (hoje `blur` no short), não do canal — um lugar
só para mexer, e nenhum caminho pelo qual o agent chegue nele.

**`workspace/channels/<id>.learned.json` — o que o agent aprendeu, só ele escreve:**

```json
{"updated_at":"2026-09-10T21:00:00Z","entries":[
  {"run_id":"…","video_id":"…","note":"short com reframe center perdeu o rosto em plano aberto; blur resolveu","kind":"render","confidence":"observed"},
  {"note":"cortes abaixo de 78 de score foram rejeitados pela revisão nas 3 últimas runs","kind":"editorial"}]}
```

`load_channel_profile` devolve o merge (config primeiro, aprendido depois, com rótulo).
`update_channel_profile` só acrescenta entradas ao `learned.json`, no máximo 5 por run,
cada uma ≤ 300 caracteres, com as mais antigas descartadas acima de 50. Não dá para o
agent alterar sua configuração, e você pode apagar o `learned.json` inteiro sem perder
nada seu.

Sinal disponível hoje para "o que funcionou": só a sua aprovação/rejeição no modo legado
e a nota manual dos evals. Não há métrica de audiência, e o agent não deve fingir que há
— por isso `confidence: observed|assumed`.

Qual canal a run usa: `--channel <id>`, ou o único arquivo em `channels/` quando só há
um, ou nenhum perfil de canal (o agent segue só os perfis de formato).

---

## 8. Runtime

### 8.1 Loop

```python
options = ClaudeAgentOptions(
    model="claude-opus-5",
    system_prompt=AGENT_SYSTEM,          # prompts/agent_system.md, §10
    mcp_servers={"clipper": clipper_server},
    allowed_tools=["mcp__clipper__*"],
    tools=[],                            # nenhuma ferramenta embutida
    setting_sources=[],                  # nenhum CLAUDE.md, nenhum settings.json
    permission_mode="default",
    can_use_tool=budget_gate,            # §8.2
    max_turns=MAX_TURNS,
    cwd=str(workspace),
)
async with ClaudeSDKClient(options=options) as client:
    await client.query(opening_prompt)   # objetivo + vídeo + perfil de canal
    async for message in client.receive_response():
        record(message)                  # §8.3
```

`ClaudeSDKClient` e não `query()` porque o runtime precisa poder mandar uma segunda
mensagem (o aviso de orçamento esgotado, §8.2) e `interrupt()` na parada dura.

O prompt de abertura carrega: a URL, o perfil de canal já resolvido, o que já está feito
no `state.db` para esse vídeo (retomada), e o orçamento da run em números.

### 8.2 Quanto uma run consome da assinatura

Medido, não estimado: o log da seleção do vídeo que está no workspace
(`RfCT-LJkfx8`, 41:56, PT-BR, 2.142 frases), com `claude -p`:

| Medida | Valor |
|---|---|
| Prompt de seleção | 91.285 chars → **62.743 tokens** (1,46 char/token em PT-BR com timestamps) |
| Saída da seleção | **12.230 tokens** (7 candidatos com as citações de gancho e fecho) |
| Regra prática | **~1,5 mil tokens de entrada por minuto de vídeo** |

O loop não vê a transcrição (§2.2), então o contexto dele é pequeno e cresce só com o
histórico de chamadas. Projeção para uma run de 4-5 shorts, 28 turnos:

| Componente | Entrada nova | Leitura de cache | Saída |
|---|---|---|---|
| `find_candidates`, 1 chamada, fonte de 40 min | ~63k | — | ~12k |
| Loop, 28 turnos (~700 tokens de histórico por turno) | ~20k | ~270k | ~28k |
| **Total, fonte de 40 min** | **~85k** | **~270k** | **~40k** |
| **Total, fonte de 2 h** | ~200k | ~400k | ~55k |

**O que muda em relação a hoje.** O `clipper run` atual já gasta da assinatura os
~63k de entrada e ~12k de saída da seleção (`select_provider: claude`). O que o agent
acrescenta é o loop: **~20k de entrada nova, ~270k de leitura de cache e ~28k de saída
por vídeo**. Leitura de cache é o componente barato em qualquer métrica — na API valeria
10% de um token de entrada. Em ordem de grandeza: a run com agent consome cerca de
**duas vezes** o que uma run de shorts consome hoje, não dez.

Para dar um número em dinheiro, que a assinatura não cobra mas serve de régua: aos
preços do Opus 5 na API (US$ 5 / US$ 25 por milhão), uma run de 40 min sairia por
**~US$ 1,60** e uma de 2 h por **~US$ 2,90** — dos quais ~US$ 0,70 já são gastos hoje.

**Como calibrar com número seu, não meu:** o `ResultMessage` do SDK devolve `usage` e
`total_cost_usd` por run, e o runtime grava os dois no `run_end` do JSONL; `/usage` no
Claude Code mostra quanto da sua janela e da sua semana a run consumiu.

### 8.2.1 O que 5 runs reais mediram (2026-09-11)

| | |
|---|---|
| Custo por vídeo | US$ 0,86 (42 min, ensaio) a US$ 1,92 (47 min) |
| Tempo de loop | 6 a 13 min por vídeo |
| Chamadas de ferramenta | 29 a 45 |
| Respostas do modelo | 75 a 83 numa run que o SDK conta como bem menos turnos |
| Leitura de cache | ~977 mil tokens numa run de 39 chamadas, contra 41 mil de entrada nova |
| Renders por corte entregue | 1,0 a 1,75 |

Três coisas que só apareceram rodando:

- **A conta de tokens tem outra forma.** `input_tokens` sozinho deu 38 numa run de 86
  centavos: numa conversa com cache tudo que importa está em `cache_creation` (cobrado) e
  `cache_read` (um décimo do preço). O limite de entrada passou a somar os dois primeiros
  e a reportar o terceiro à parte, sem teto.
- **Turno é definição do SDK.** Contar respostas do modelo e interromper nisso cortou uma
  run em 16 chamadas; e interromper perde o `usage`, que só chega na mensagem final. O
  limite de turnos saiu dos fusíveis e ficou só o do SDK.
- **40 turnos era a mediana da necessidade.** Uma run gasta de 39 a 54 chamadas de
  ferramenta, e o turno do SDK anda perto disso — então o fusível estava no meio da
  distribuição e pegou 2 das 5 runs. Nas duas ele bateu **no fecho**, depois de todo
  corte renderizado, revisado e publicado: o que se perdeu foi o relatório do próprio
  agent, substituído por "Encerrada pelo runtime: max_turns". O limite subiu para 80 e
  ganhou uma margem de pouso (abaixo).
- **Metade das chamadas é fronteira.** `snap_clip` 8–13 e `inspect_range` 7–12 por run,
  uma por vez. É onde estaria a economia se 80 também apertar.

| Limite | Padrão | Onde é imposto |
|---|---|---|
| Turnos do agent | 80 | `max_turns` do SDK, que é dono da definição de turno |
| Saída do loop | 60k tokens | soma de `usage.output_tokens` dos `ResultMessage` |
| Entrada nova do loop | 150k tokens | `input_tokens` + `cache_creation`; leitura de cache não conta |
| Renders por vídeo | 10 | `budget.py`, contado em renders novos e `render_clip(force=true)` |
| Re-renders do mesmo corte | 2 | idem, por `clip_id` |
| `find_candidates` por perfil | 2 | idem |
| Publicações efetivas | 2 | idem |
| Tempo de parede da run | 90 min | relógio no runtime |
| Margem de pouso | 12 chamadas / 85% | `budget.landing()`, colado na resposta da ferramenta |
| Timeout por ferramenta | 60 s padrão; 30 min `transcribe_video`; 20 min `render_clip`; 30 min `publish_clips` | `asyncio.wait_for` no handler |

Política de esgotamento, em três etapas:

1. **Pouso**: antes de qualquer limite duro estourar — 12 chamadas antes do teto de
   turnos, 85% dos tokens ou do relógio — a resposta da ferramenta seguinte vem com um
   aviso do runtime colado nela: "não comece nada novo; descarte com `drop_clip` o que
   não vai usar, publique o que está pronto e chame `finish_run`". Vai junto da resposta,
   e não como pergunta própria, porque um aviso que custa um turno gasta o que está
   tentando poupar. É dito **uma vez** por run, e não é dito depois do `finish_run`.

   É a única etapa que salva o relatório do agent. As outras duas salvam os arquivos.

2. **Aviso**: quando um limite é atingido, a chamada é recusada antes de rodar e o
   motivo volta como resposta da própria ferramenta — "orçamento de renders esgotado
   (10/10); publique o que já está pronto e finalize". A ferramenta negada não roda; o
   loop continua e o modelo ainda pode publicar e chamar `finish_run`.

   *Nota da implementação:* a spec previa um `can_use_tool` devolvendo
   `PermissionResultDeny`. Não funciona: uma entrada de `allowed_tools` que libera a
   ferramenta inteira auto-aprova a chamada **antes** do callback, que nunca é
   consultado (o próprio SDK avisa isso em tempo de execução). O porteiro ficou dentro
   do handler, com o mesmo efeito — o trabalho não acontece, o motivo chega ao modelo e
   o loop segue.
3. **Parada dura**: turnos, tokens ou tempo de parede estourados → `interrupt()`, e o
   runtime executa ele mesmo o fecho: `build_metadata_batch` para o que falta,
   `deliver` do que está completo, relatório marcado como `incomplete`. Um corte
   renderizado e conferido não fica preso na máquina porque o loop acabou.

### 8.3 Log

`workspace/<video_id>/agent/<run_id>.jsonl`, uma linha por evento, em ordem:

```json
{"ts":"2026-09-10T21:03:11.412Z","run_id":"20260910-210311-RfCT","seq":14,
 "type":"tool_call","tool":"render_clip",
 "input":{"video_id":"RfCT-LJkfx8","profile":"short","index":2},
 "output":"{\"clip_id\":\"RfCT-LJkfx8/short/2\",\"duration_s\":56.4,…}",
 "output_truncated":false,"duration_ms":184302,"error":null,
 "budget":{"renders_used":3,"renders_left":9,"turns":14}}
```

Tipos: `run_start`, `turn` (texto do modelo, truncado em 2.000 chars), `tool_call`,
`tool_error`, `budget_deny`, `run_end` (com `num_turns`, `usage`, `total_cost_usd`,
`terminal_reason`). Saída truncada em 4.000 chars com `output_truncated: true`.

Segredos: o log grava só o que passou pelos schemas das ferramentas. Nenhum handler
recebe ou devolve chave, e `GEMINI_API_KEY` nunca aparece em resultado de ferramenta — o
provider de seleção lê a chave do `Settings` dentro do processo. Um filtro em `log.py`
redige qualquer valor que case com `AIza[0-9A-Za-z_\-]{20,}` ou `sk-[A-Za-z0-9\-]{20,}`,
como rede de segurança para mensagem de erro de biblioteca.

Este JSONL é a única fonte da sessão do agent no monitor (§9): tudo que a página
mostra é projeção dele, o que obriga o log a ser completo o bastante para reconstruir a
run inteira — e é de graça, porque ele teria de existir para auditoria de qualquer jeito.

### 8.4 Falhas

- Erro de ferramenta → `is_error`, o loop continua. Duas falhas seguidas da **mesma**
  ferramenta com a **mesma** entrada viram `budget_deny` com instrução de mudar de
  abordagem: evita o laço "tenta, falha, tenta igual".
- Erro do próprio SDK (CLI ausente, sessão caída, rede) → aborta a run com código de
  saída 4 (`ExternalError`, como hoje) e o relatório marcado `failed`. O workspace fica
  intacto; rodar de novo retoma pelo `state.db`.
- `Ctrl+C` → `interrupt()`, relatório `cancelled`, nada é publicado.

#### 8.4.1 Nenhum estágio fica em `running` depois da run

`render` e `package` são escritos corte a corte: enquanto faltar um, a linha do
`state.db` fica em `running` com o parcial (`3/4 cortes`). Quem fecha essa linha é o
último corte — e se ele nunca chegar, ninguém fecha.

Por isso a run, ao terminar de qualquer maneira (`finish_run`, orçamento, `Ctrl+C` ou
exceção), reconcilia os estágios do vídeo: toda linha ainda em `running` é recontada a
partir do que existe em disco e vira `done`, ou volta para `pending` com o parcial e a
nota `run encerrada`. Um estágio em `running` é uma afirmação sobre um processo vivo; a
run acabou, então a afirmação é falsa.

O monitor não confia só nisso (uma run morta por `SIGKILL` não reconcilia nada): quando
não há nenhum processo do clipper na máquina, a página mostra a linha como `parado` em
vez de `running` — ver §15.

---

## 9. A sessão do agent no monitor

O `clipper monitor` deixa de ser só o painel da fila e passa a ser **a janela da run do
agent**. Uma run = uma sessão, e a sessão é o lugar onde se vê o que o modelo decidiu,
com que dado decidiu, quanto do orçamento gastou e o que isso produziu.

A razão de ser aqui e não num painel novo: o monitor já tem servidor stdlib, snapshot
com SSE, galeria de cortes com player, os modos Simples/Detalhes e o tema. A sessão do
agent é mais uma seção dessa página, alimentada pelo mesmo `Snapshot`.

### 9.1 O que a sessão mostra

Nove blocos, na ordem em que se olha quando algo dá errado:

1. **Cabeçalho** — vídeo, canal, `run_id`, estado (`rodando` / `concluída` /
   `incompleta` / `falhou` / `cancelada`), tempo decorrido, turno atual sobre o limite.
2. **Orçamento** — uma barra por limite da §8.2 (turnos, renders, tokens de saída,
   entrada nova, publicações, tempo de parede), com usado/limite e a cor virando quando
   passa de 80%. É o dado que não existe em nenhum outro lugar do sistema, e é o que
   responde "essa run vai terminar?".
3. **Agora** — a última frase do modelo (o que ele disse antes de chamar), a ferramenta
   em execução e um cronômetro. Um `render_clip` de 4 min deixa de parecer travado.
4. **Plano × realizado** — o plano que o agent declarou no começo ("3 shorts, 1 quiz")
   ao lado do que existe de fato: uma faixa por corte, com o estágio em que está,
   quantos renders consumiu, o veredito do último laudo e o motivo de cada refação.
5. **Trilha** — uma linha por chamada: sequência, ferramenta, resumo dos argumentos em
   português, duração, ok/erro. Clicar expande a entrada e a saída (truncadas como no
   JSONL). É o log de auditoria, legível.
6. **Laudos** — por corte, as checagens da §6 que não deram `ok`, com valor medido,
   esperado e o `fix` sugerido. É aqui que se vê *por que* um corte foi refeito.
7. **Fronteiras mexidas** — toda vez que o agent chamou `snap_clip` e o intervalo mudou:
   antes/depois em `hh:mm:ss`, quanto entrou ou saiu, e as frases do começo e do fim nos
   dois casos. Responde "ele cortou no meio de uma frase?" sem abrir o vídeo.
8. **Erros e negações** — chamadas com `is_error` e cada `budget_deny`, com o que o
   modelo fez em seguida. Um agent que insiste no mesmo erro fica visível na hora.
9. **Entrega** — o que subiu para o Drive, o destino, e o que ficou de fora com o motivo
   (o `skipped` do `finish_run`).

A galeria de cortes e o diagrama de fluxo que já existem continuam valendo sem mudança:
o agent grava o mesmo `clips.json` e os mesmos diretórios, então o corte aparece lá como
sempre — só ganha dois selos novos, `nº de renders` e o veredito do laudo.

**Simples** mostra 1, 2, 3, 4 e 9 — o suficiente para acompanhar sem saber o que é uma
ferramenta. **Detalhes** acrescenta 5, 6, 7 e 8.

### 9.2 De onde vêm os dados

| Bloco | Fonte |
|---|---|
| Cabeçalho, orçamento, trilha, erros | `workspace/<video_id>/agent/<run_id>.jsonl` |
| Estado da run, histórico de runs | tabela `runs` do `state.db` (`mode=ro`, como hoje) |
| Plano × realizado, laudos, fronteiras | os próprios eventos `tool_call` da trilha (`commit_clips`, `render_clip`, `review_render`, `snap_clip`) |
| Cortes, thumbnails, player | o que o monitor já lê hoje: `clips.json`, `clip.mp4`, `thumbnail.jpg` |

Nada é calculado de novo e nada é escrito: a sessão é uma projeção do JSONL. Se o log
existe, a sessão existe — inclusive depois da run acabar, e inclusive se o processo
morreu no meio (o estado vira `incompleta`, com a última linha escrita como fim).

**Leitura incremental.** O JSONL só cresce, então `monitor/agent.py` guarda
`(path, size, mtime, offset, estado parcial)` e lê apenas o que chegou desde a última
vez. Sem isso, reconstruir o snapshot a cada 0,7 s (`REFRESH_S`) reparsearia o arquivo
inteiro a cada vez — o mesmo cuidado que o `tail()` já toma com os logs de estágio.

### 9.3 Endpoints

O `Snapshot` leva só o **resumo** da run ativa, com teto de ~4 KB, para o SSE continuar
leve: estado, orçamento, turno, o "agora" e as últimas 5 entradas da trilha. O resto é
buscado sob demanda:

| Rota | Devolve |
|---|---|
| `GET /api/snapshot` (campo `agent`) | resumo da run ativa do vídeo em foco |
| `GET /api/agent/runs?video_id=` | as runs daquele vídeo, mais recente primeiro |
| `GET /api/agent?run_id=&from_seq=` | a trilha, paginada por sequência |
| `GET /api/agent/clip?run_id=&clip_id=` | laudos, renders e mudanças de fronteira de um corte |

Os parâmetros passam pelo `_safe_name()` que já existe, e `run_id` casa com
`^[0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]{1,16}$`: nenhuma rota nova aceita caminho.

### 9.4 Modelos novos (`monitor/models.py`)

`AgentRunView` (run_id, video_id, channel, state, started_at, ended_at, turn, budget,
now, clips, trail_tail, errors, delivered, skipped), `BudgetView` (uma entrada por
limite: nome, usado, limite, unidade), `AgentStepView` (seq, ts, kind, tool, summary,
duration_ms, error, input, output, truncated), `AgentClipView` (clip_id, profile, topic,
stage, renders, verdict, checks, boundary_changes) e `CheckView` (check, status, value,
expected, fix). Pydantic, como o resto do monitor — nada de dict solto.

### 9.5 Controle da sessão

A página deixou de ser só leitura. São quatro escritas, cada uma com seu schema e um
arquivo conhecido — `review_response.json`, `fila.txt`, `channels/<id>.yaml` e o controle
abaixo —, nenhuma aceitando caminho vindo do request. O CLAUDE.md foi atualizado de
acordo.

O controle da sessão é `workspace/<video_id>/agent/<run_id>.control.json`, no mesmo padrão
de arquivo da revisão:
`workspace/<video_id>/agent/<run_id>.control.json`, com três ações na página —

- **pausar / retomar**: o runtime lê o arquivo antes de cada chamada e **espera** ali
  enquanto estiver pausado. Bloquear é o ponto: recusar gastaria um turno e uma fatia de
  contexto a cada dois segundos, e uma run pausada comeria o próprio orçamento parada.
  O processo não morre, nada fica pela metade, e retomar segue de onde estava.
- **parar**: `interrupt()` e o fecho da §8.2 (publica o que já está pronto e conferido,
  ou nada, conforme a opção marcada no botão).
- **vetar um corte**: marca um `clip_id` como proibido; `publish_clips` o exclui e diz
  ao agent que você vetou. É o meio-termo entre autonomia total e ter de matar a run
  porque um corte ficou ruim.

O arquivo é validado com um pydantic e tem o mesmo teto de tamanho do `/api/review`. Os
vetos se acumulam: um corte que você recusou continua recusado, porque perder um veto no
meio de uma run seria pior que não ter o botão.

Junto vieram as outras duas escritas que você pediu: a **fila** (`fila.txt` — acrescentar,
tirar e reordenar, com o cabeçalho de comentários preservado e cada URL validada por
`pick_source`) e os **canais** (`channels/<id>.yaml`, escrito a partir dos campos
validados pelo `ChannelProfile`, nunca de um YAML cru vindo do navegador — o custo é que
comentários escritos à mão não sobrevivem a uma edição pela página, e o cabeçalho do
arquivo avisa).

### 9.6 Comparar duas runs

Na aba de histórico, escolher duas runs do mesmo vídeo mostra lado a lado: cortes
escolhidos com sobreposição entre eles, renders gastos, turnos, tokens e tempo. É o
mesmo cálculo que o relatório de evals (§11) usa, e evita ter que abrir dois JSONL no
editor para entender por que a run de ontem ficou melhor.

### 9.7 Testes

Tudo com JSONL sintético, sem rede e sem modelo: construção da view a partir de uma
trilha conhecida, run truncada no meio (processo morto), leitura incremental (duas
leituras com o arquivo crescendo entre elas dão o mesmo resultado de uma leitura só),
teto do resumo no snapshot, as rotas novas com `run_id` inválido, e o round-trip do
controle. O monitor já tem `tests/test_monitor.py` com esse formato.

---

## 10. System prompt do agent

`prompts/agent_system.md`, em português como os demais prompts. Esqueleto (o texto final
entra na Fase C, calibrado nos evals):

```markdown
Você é o editor de cortes deste canal. Você recebe um vídeo e decide o que dele vira
corte, em quais formatos, e com qual título — chamando ferramentas. Você nunca executa
nada diretamente: pede, lê o resultado e decide o próximo passo.

## Objetivo
Entregar no Drive os cortes que valem a pena deste vídeo. Menos cortes bons vale mais do
que muitos medianos: entregar dois shorts fortes é um bom resultado; entregar cinco para
bater cota, não.

## Como trabalhar
1. `get_video`, `transcribe_video`, `get_outline` — entenda do que o vídeo trata.
2. `load_channel_profile` — o canal define tom, formatos e o que evitar.
3. Decida o plano: quantos cortes, de quais perfis. Diga o plano em voz alta antes de
   executá-lo.
4. `find_candidates` por perfil. Leia os rejeitados: o motivo diz o que corrigir.
5. Antes de renderizar, use `inspect_range` e `snap_clip` — ajustar fronteira é de graça,
   render custa minutos.
6. `commit_clips`, `render_clip`, `review_render`. Um `fail` no laudo é motivo para
   refazer com o parâmetro sugerido; um `warn` não é.
7. `set_metadata` para cada corte aprovado, `publish_clips` no fim, `finish_run`.

## Critério de pronto
Todo corte entregue tem: laudo sem `fail`, abertura que se sustenta sozinha, fecho que
fecha o assunto, e título que descreve o que o corte realmente entrega.

## Regras
- Nunca invente timestamp: todo intervalo passa por `snap_clip`.
- Nunca peça `render_clip` para consertar fronteira; conserte por `snap_clip` antes.
- Se um trecho não tem contexto suficiente no início, estenda para trás até a frase que
  o explica — e verifique por `inspect_range` que a nova abertura se sustenta.
- Se `find_candidates` devolver lista vazia ou só rejeitados, esse formato não cabe
  neste vídeo. Diga isso e siga; não force.
- Publique uma vez só, no fim.
- Escreva em {language}. Títulos no tom do canal, não no tom genérico de YouTube.
```

O `{language}` é preenchido pelo runtime a partir da transcrição.

---

## 11. Evals

### 11.1 Baseline, reconstruído do Drive

`clipper eval build-baseline` roda `rclone copy --filter` (uma lista única, em ordem —
CLAUDE.md) para trazer os `metadata.json` já entregues e monta `evals/baseline.json`:

```json
{"videos":[{"video_id":"1-BgoqVRrYQ","url":"…","title":"…","channel":"…",
  "clips":[{"profile":"short","title":"…","segments":[[412.4,468.9]]}]}]}
```

`source_segments` e `source_url` já estão em todo `ClipMetadata`, então o baseline sai
sem reprocessar nada. Escolho os 3 a 5 vídeos com mais cortes entregues; se der menos de
3, aviso e você indica os que faltam.

### 11.2 A run do agent

`clipper eval run --baseline evals/baseline.json` re-ingere e re-transcreve cada vídeo
(whisper local, ~11 min por 2 h) e roda o agent com `publish_clips` em `dry_run`, para o
eval não encher seu Drive.

Comparação só entre cortes 9:16: os longs do baseline ficam de fora da conta de
sobreposição, já que o agent não gera esse formato (§2.1). Eles continuam listados no
relatório como "não coberto por esta versão", para a ausência ficar visível em vez de
sumir na média.

### 11.3 Métricas

| Métrica | Como |
|---|---|
| Sobreposição dos trechos | IoU entre cada corte do agent e o melhor par do baseline, pareamento guloso; reporta média e quantos cortes acharam par acima de 0,5 |
| Renders por corte entregue | contador do orçamento ÷ cortes publicados |
| Turnos e tokens | `ResultMessage.num_turns` e `usage`; custo estimado por `total_cost_usd` (a assinatura não cobra por token, então o número serve para comparar runs, não para pagar conta) |
| Tempo total | relógio do runtime, separando whisper de render de loop |
| Laudo | quantos `fail`/`warn` por corte na primeira tentativa e no fim |
| **Sua nota** | `evals/<rodada>/notas.md`, tabela com um corte por linha, coluna vazia para 1–5 e um campo de comentário; o relatório final lê esse arquivo, e a página escreve nele (abaixo) |

#### As notas pela página

Editar markdown à mão para dar uma nota é atrito onde menos convém: a nota é a única
métrica que o sistema não consegue produzir sozinho, e a que mais se deixa de dar. Então
o monitor tem a seção **Notas dos cortes** — uma rodada por aba, um corte por linha, com
thumbnail, o vídeo que abre ali mesmo ao clicar, os botões de 1 a 5 e um campo de
comentário. Salva sozinha a cada clique; não tem botão de salvar para esquecer.

A escrita é a quinta da página (§9.5, CLAUDE.md) e segue o mesmo desenho das outras:
schema próprio (`EvalEdit`), um arquivo conhecido, e o nome da rodada **casado contra as
que existem em disco** em vez de virar caminho. Duas diferenças valem registro:

- **A página só escreve duas colunas.** O `notas.md` continua sendo seu para editar à
  mão — um link para o corte, uma observação no título — e um salvamento pela página
  reescreve `nota` e `comentario` linha a linha, nunca a tabela inteira. O que você
  escreveu nas outras colunas sobrevive.
- **Nota 0 apaga.** É como se tira uma nota dada por engano, e o `read_notes` ignora a
  linha de novo.

O corte que o agent descartou (`drop_clip`) aparece na lista marcado como tal. É a linha
mais informativa da rodada: o laudo diz que ele estava tecnicamente certo em descartar, e
só a sua nota diz se ele estava certo.

A mesma comparação aparece na aba de histórico do monitor (§9.6); o markdown existe
para você anotar e para versionar o resultado. Comparação lado a lado em
`evals/<run_id>/report.md`: por vídeo, os cortes do baseline e
os do agent, com IoU, duração, título dos dois e o link do arquivo local para você
assistir.

### 11.4 Critério

A v1 passa quando, nos 3–5 vídeos: sua nota média ≥ a do baseline, nenhum corte entregue
com `fail` no laudo, e a intervenção manual cai de "revisar N candidatos por perfil" para
"assistir o que foi entregue".

---

## 12. Compatibilidade

- `clipper run <url>` continua sendo **o pipeline atual**, inalterado, incluindo
  `--review ask|auto|web`. Se a decisão de não gerar longs vale também para ele, é uma
  linha no `config.yaml` (`profiles.default: [short]`) — faço na Fase A se você
  confirmar; do jeito que está, `clipper run` sem `--profile` ainda gera short e long.
- `clipper run <url> --agent` roda o agent. `--legacy` existe desde a Fase A como
  sinônimo explícito do padrão, para os evals poderem pedir um ou outro sem ambiguidade.
- Quando os evals empatarem, a proposta é inverter o padrão em um commit só (`--agent`
  vira padrão, `--legacy` continua funcionando). Decisão sua, depois de ver os números.
- `batch`, `monitor`, `status`, `clean`, `doctor`, `publish-plan` funcionam igual nos dois
  modos: o agent grava os mesmos `clips.json`, `metadata.json` e estados.
- `doctor` ganha duas linhas: `claude-agent-sdk` instalado e `claude` no PATH autenticado.

---

## 13. Fases

Cada fase termina com `pytest`, `mypy --strict`, `ruff` verdes, README atualizado se
precisar, e commit. **Ao fim de cada fase eu paro e te mostro.**

**Fase A — ferramentas, registro, validação.** `agent/models.py`, `agent/tools/*`,
`agent/review.py`, os overrides de perfil, a tabela `runs` no `state.db`. Sem loop: as
ferramentas são chamáveis por um `clipper agent-tool <nome> <json>` escondido, usado
pelos testes. Teste unitário por ferramenta, incluindo entrada inválida e o caminho de
erro. Pipeline legado intacto — a suíte atual continua verde sem alteração.
*Pronto quando:* dá para fazer um vídeo inteiro chamando ferramentas à mão, na ordem, e o
resultado é bit a bit o mesmo do `clipper run` de hoje.

**Fase B — runtime e sessão no monitor.** `runtime.py`, `budget.py`, `log.py`,
`clipper run --agent`, dependência `claude-agent-sdk` num extra `agent`; e a sessão da
§9 em modo leitura: `monitor/agent.py`, as rotas novas e a seção da página. Testes do
loop com um transporte falso (o SDK aceita `transport=`), sem rede e sem modelo:
sequências roteirizadas de tool_use exercitam limites, negação por orçamento, erro de
ferramenta, parada dura e o formato do JSONL; a sessão é testada com JSONL sintético.
*Pronto quando:* uma run real de ponta a ponta num vídeo curto, acompanhada do começo ao
fim pelo monitor, sem abrir o terminal.

**Fase C — system prompt, perfil de canal e controle.** `prompts/agent_system.md`,
`channel.py`, `channels/<id>.yaml`, as duas ferramentas de canal, e o controle da sessão
(§9.5: pausar, parar, vetar corte) se você aprovar.
*Pronto quando:* duas runs no mesmo canal produzem títulos reconhecivelmente do canal, a
segunda usa o que a primeira registrou, e dá para pausar e retomar uma run pela página.

**Fase D — evals.** `clipper eval baseline` / `run` / `report`, o `notas.md`, o
relatório comparativo. Rodar nos 3–5 vídeos e te entregar os números.

### O que cada fase entregou (2026-09-11)

As quatro estão implementadas e commitadas. `TASKS.md` tem o item a item; o resumo:

| Fase | Entregue | Achado rodando de verdade |
|---|---|---|
| A | 15 ferramentas, laudo determinístico, overrides, tabela `runs` | `cut_in_pause` virou "corta uma palavra?" em vez de "tem silêncio?" — a regra da spec reprovaria todo corte bem feito |
| B | runtime, orçamento, log JSONL, sessão no monitor | o porteiro em `can_use_tool` nunca era consultado; turno contava resultado de ferramenta; laudo não via a passarela |
| C | prompt calibrado, perfil de canal, escrita pela página | título passou a sair no tom do canal e o jabá foi recusado pelo `avoid`; a barra de pergunta saía vazia porque o campo não chegava ao agent |
| D | baseline do Drive, comparação IoU, relatório + notas | `loudnorm` de uma passada erra o alvo e o laudo mandava refazer o que refazer não conserta; o eval se acusava de usar baseline próprio |

Em números: **76% de sobreposição média** com o que o pipeline linear tinha escolhido, em
3 vídeos com baseline independente, 1,0–1,75 renders por corte entregue, US$ 4,82 no
total. A nota manual ainda não foi dada.

---

## 14. Riscos

1. **Dependência nova (`claude-agent-sdk`).** Traz `anyio`, `jsonschema`, `mcp`,
   `sniffio` e exige o CLI `claude` no PATH (já exigido pelo provider `claude`). Fica em
   `[project.optional-dependencies].agent`: quem só usa o modo legado não instala nada.
   O comentário em `llm/claude_code.py` que rejeita o SDK continua correto para **uma
   completion só** — aqui o que se quer é exatamente o loop.
2. **Quota da assinatura.** O loop consome a mesma cota do seu Claude Code: ~85k de
   entrada nova e ~40k de saída por vídeo de 40 min (§8.2), cerca do dobro de uma run de
   hoje. Um vídeo por vez é tranquilo; uma fila de 10 numa tarde não é, e é a fila que
   pode esbarrar no limite semanal. Os tetos de token da §8.2 são o freio, e `batch` com
   agent só entra depois dos evals, com consumo medido.
3. **O laudo determinístico não vê tudo.** Legenda sobre um gráfico, corte no meio de um
   gesto, texto na tela do vídeo original brigando com a legenda queimada — nada disso é
   medido. O risco caiu com o enquadramento por rosto desligado (§2.1): sem recorte, o
   erro clássico de "pessoa fora do quadro" não existe. Se algo aparecer nos evals, o
   caminho é acrescentar a checagem de visão como `warn` opcional, não abrir mão do laudo.
4. **Loop caro em vídeo longo.** `find_candidates` num podcast de 3 h é uma chamada de
   ~200k tokens no provider de seleção. Limite de 2 por perfil e `time_window` existem
   para isso.
5. **Deriva entre `clips.json` do agent e do legado.** Mitigado por `commit_clips` passar
   por `snap_candidate` e produzir o mesmo `ClipSet` — os testes da Fase A comparam os
   dois caminhos no mesmo vídeo.

---

## 15. O que a SPEC.md não reflete mais

Levantado lendo o código, para você atualizar quando quiser:

| SPEC.md diz | Código faz |
|---|---|
| §6.4 providers: gemini, ollama, file | Existe também `claude` (`llm/claude_code.py`, `claude -p`), e `llm.select_provider` / `metadata_provider` escolhem provider **por estágio** |
| §7.1 revisão no terminal | Existe `--review web`: `review_request.json` → página do monitor → `review_response.json` (§15 cobre o monitor, mas não a revisão) |
| §4 perfis short/long | Existem `quiz` (card de pergunta com `card_seconds`, `reveal_at`, `CardStyle`), `podcast` e `short_podcast` por `extends` |
| §7.3 render | Não menciona `tail_roll_s` (passarela), `outro` (card de fim concatenado fora da cadeia de filtros), `BannerStyle` (pergunta de comentário na barra), nem `blur_brightness`/`blur_saturation` |
| §6.1 seleção | Não menciona `min_score`, `opens_well`/`advance_to_opening`, `hook_is_present`/`closing_is_present`, `ends_on_question`, nem o retry com os motivos de rejeição |
| §6.2 candidato | O `ClipCandidate` ganhou `closing`, `payoff`, `question`, `card_text`, `comment_prompt`, `reveal_at`, `self_contained` |
| §9 CLI | Existem `batch` (fila com `fila.txt`, `--min-free`), `monitor`, `publish-plan`, `disk` |
| §8 entrega | O arquivo sobe com o nome do título (`name_for_upload`), não como `clip.mp4`; `deliver` tem modo `rclone` com `workers`/`chunk_size` |
| §5.3 transcrição | O padrão do `config.yaml` é `large-v3-turbo` com fallback `large-v3`, medido e justificado no próprio arquivo |
| §3.4 workspace | Tem `cache/` (outros normalizados), `logs/` por estágio e `publish.log` |
| §11.2 aceite | 1 dos 3 vídeos feito; a ressalva de "canal que já queima legenda própria" (usar `subtitle_style: off`) não está na spec |

O nome do arquivo também: o prompt chama de `SPEC-clipper.md`, o repositório tem
`SPEC.md`.

---

## 16. Dúvidas em aberto

O que ficou decidido durante a implementação saiu daqui; sobrou o que ainda depende de
você ou de medição.

### Esperando você

1. **Sua nota nos evals.** `evals/rodada1/notas.md` tem 12 linhas, uma por corte, com a
   coluna vazia. É a única métrica que a sobreposição não cobre — se o agent divergiu do
   baseline, só você diz se divergiu para melhor. Depois, `clipper eval report evals/rodada1`.
2. **Os dois shorts do UNO no Drive** foram publicados antes do conserto da barra de
   pergunta, então estão sem ela. Dá para corrigir sem chamar modelo: copiar a pergunta do
   `candidates.json` para o `clips.json`, re-renderizar os dois e substituir no Drive.
3. **`profiles.default: [short]` no `config.yaml`.** O `clipper run` legado ainda gera
   short **e** long; o agent já só vê o que o canal habilita.
4. **Inverter o padrão do `clipper run`** para `--agent` depois dos evals, ou `--agent`
   continua explícito para sempre?

### Esperando medição

5. **`max_turns`.** 40 cortou a run do vídeo de 47 min. Precisa de um número medido em
   vídeos longos, não de um chute maior.
6. **Quantas inspeções por trecho.** O agent gastou 12 `inspect_range` antes do primeiro
   render num vídeo difícil. É de graça em ferramenta e caro em contexto; apertar isso no
   prompt antes de ver um terceiro vídeo seria calibrar no ruído.
7. **`batch` com agent.** Um vídeo por run continua sendo o escopo. Uma fila de 10 numa
   tarde é ~US$ 15 e pode esbarrar no limite semanal da assinatura.

### Decisões que já estão valendo

- Canal é o de publicação (`channels/<id>.yaml` + `learned.json` no workspace).
- Parada dura publica o que está pronto; parar pela página tem as duas opções.
- `subtitle.max_chars_per_line` fica fora da lista branca: legenda estourando vira aviso.
- Perfis habilitados vêm do canal — hoje só `short`; `quiz` está implementado e desligado.
- A página escreve por quatro rotas validadas (CLAUDE.md atualizado).
- `publish-plan` continua manual, fora do agent.
- O agent não tem sinal de audiência: anota só o que observou na própria run.
