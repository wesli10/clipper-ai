# Clipper — Especificação do MVP

Ferramenta pessoal de linha de comando que recebe o link de um vídeo do YouTube, transcreve localmente, usa um LLM para identificar os melhores trechos, renderiza cortes em dois formatos — **shorts** (vertical, ≤90 s) e **longs** (horizontal, 2–8 min) — com legendas queimadas, e entrega tudo em uma pasta do Google Drive com título, descrição e hashtags prontos para publicação manual.

Este documento é a fonte de verdade para implementação via Claude Code. Está organizado para ser lido do início ao fim uma vez e depois consultado por seção.

---

## 1. Objetivo e escopo

### 1.1 Objetivo do MVP
Dado um link do YouTube e um ou mais **perfis de formato** (§4), produzir cortes de alta qualidade (não quantidade), cada um com:
- vídeo `.mp4` na resolução do perfil, legendas queimadas conforme o perfil, áudio normalizado;
- `metadata.json` e `metadata.md` com título, descrição, hashtags, hook e referência ao(s) trecho(s) original(is);
- tudo dentro de uma pasta sincronizada com o Google Drive, separada por perfil.

### 1.2 Métrica de sucesso
Um corte é "bom" quando: começa e termina em fronteira natural de fala (nunca no meio de uma frase), tem gancho nos primeiros 3 s, é autocontido, respeita a duração do perfil, o falante fica enquadrado e a legenda está sincronizada. Um **long** ainda precisa ter arco completo (contexto → desenvolvimento → conclusão) e, quando montado de trechos não contíguos, as junções não devem ser perceptíveis como corte brusco. O usuário deve aprovar >70% dos cortes propostos sem edição.

### 1.3 Fora do escopo (não implementar agora)
- Publicação automática em qualquer plataforma.
- Instagram/TikTok como **fonte** (a arquitetura deve permitir, ver §5.2, mas só YouTube é implementado).
- Interface web/GUI para *operar* o pipeline. É CLI.
  (Existe desde 2026-09-10 um monitor web **só de leitura** — `clipper monitor`, §15 —
  cuja única escrita é a resposta da revisão do §7.1.)
- Multi-usuário, autenticação, deploy em nuvem.
- Geração de B-roll, efeitos, música de fundo, detecção de quem está falando.
- Tradução das legendas.

Zoom dinâmico ("punch-in") **está** no escopo como opção do perfil, mas é a última fase do plano (§12).

---

## 2. Restrições de ambiente

| Item | Detalhe |
|---|---|
| Uso | Pessoal, máquina única, execução local |
| GPU | RTX 4060, 8 GB VRAM (assume-se NVIDIA + CUDA 12; se for AMD RX, ver §13) |
| Claude MAX | Cobre uso do **Claude Code** (desenvolvimento e execução agentic). **Não inclui créditos de API** — chamadas programáticas à API da Anthropic são cobradas à parte. Portanto o pipeline não deve depender da API da Anthropic. |
| Gemini Pro | Assinatura do app Gemini. Para uso programático usar a **Gemini API** (Google AI Studio), que tem free tier com limites de RPM/RPD; configurar chave via `.env`. |
| Google Drive | Entrega via pasta local sincronizada pelo cliente Google Drive **ou** upload por `rclone`. Ver §8. |
| SO | Assumir Linux ou Windows com WSL2. Tudo deve rodar com `ffmpeg` no PATH. |

Regra de ouro de VRAM: **nunca carregar o modelo de transcrição e um LLM local ao mesmo tempo**. Etapas de GPU são sequenciais e liberam memória ao terminar (`del model; torch.cuda.empty_cache()`).

---

## 3. Arquitetura

### 3.1 Visão geral
Pipeline em estágios, cada um idempotente, com cache em disco por vídeo. Ingest e transcrição acontecem **uma vez por vídeo**; seleção, revisão, render e package acontecem **uma vez por (vídeo, perfil)**. Reexecutar um comando nunca refaz um estágio já concluído, a menos que `--force`.

```
link ──▶ ingest ──▶ transcribe ──┬──▶ select ──▶ review ──▶ render ──▶ package ──┐
         yt-dlp     faster-whisper│    (perfil    (humano,   (perfil    (perfil    │
                    (GPU)         │     short)     opc.)      short)     short)    ├──▶ deliver
                                  │                                                │    drive
                                  └──▶ select ──▶ review ──▶ render ──▶ package ──┘
                                       (perfil long)  ...
```

### 3.2 Decisões de tecnologia

| Decisão | Escolha | Motivo |
|---|---|---|
| Linguagem | Python 3.12 | Ecossistema de ML/vídeo (whisper, yt-dlp, mediapipe) é Python-first. O autor vem de Java/Spring; manter o código explícito e tipado (pydantic, type hints, `mypy --strict`) para ficar confortável. |
| Gerenciador | `uv` | Rápido, lockfile, sem virtualenv manual |
| CLI | `typer` + `rich` | Comandos, progresso, tabelas de revisão |
| Download | `yt-dlp` | Padrão de fato; extrai também metadados do canal/vídeo |
| Transcrição | `faster-whisper` (CTranslate2) | `large-v3` em `int8_float16` cabe em ~4 GB VRAM na 4060 com timestamps por palavra. Fallback `medium`. |
| Seleção de trechos | Gemini API (`gemini-2.5-pro`, fallback `gemini-2.5-flash`) | Contexto longo: transcrição inteira de 2 h cabe em uma chamada; free tier suficiente para uso pessoal. Provider abstraído (§6.4). |
| Fallback local de LLM | Ollama (`qwen2.5:7b-instruct-q4_K_M` ou `llama3.1:8b-instruct-q4_K_M`) | Roda em 8 GB **apenas com whisper descarregado**. Contexto limitado: processar transcrição em janelas. |
| Detecção de rosto | `mediapipe` (Face Detection) | Leve, CPU-friendly; suficiente para reframe |
| Vídeo | `ffmpeg` (libx264, libass) via `subprocess` | Sem wrappers mágicos; comandos explícitos e logados |
| Legendas | Formato `.ass` gerado a partir de timestamps por palavra | Permite estilo karaokê (shorts) e estilo convencional (longs), ambos queimados via `libass` |
| Estado | SQLite (`state.db`) + arquivos em `workspace/<video_id>/` | Simples, inspecionável |
| Entrega | Pasta local sincronizada **ou** `rclone copy` | Sem OAuth próprio no MVP |
| Config | `config.yaml` + `.env` (chaves) | Perfis, estilo de legenda, provider |

### 3.3 Estrutura do repositório

```
clipper/
├── CLAUDE.md                  # instruções para o Claude Code (§14)
├── README.md
├── pyproject.toml
├── config.yaml                # defaults do usuário, inclui perfis
├── .env.example
├── prompts/
│   ├── select_short.md        # seleção p/ perfil short (§6.2)
│   ├── select_long.md         # seleção p/ perfil long (§6.3)
│   ├── metadata_short.md      # título/descrição/tags p/ short
│   ├── metadata_long.md       # idem long, com capítulos
│   └── merge_windows.md       # só p/ provider local (janelas)
├── src/clipper/
│   ├── cli.py                 # typer app
│   ├── config.py              # pydantic Settings + Profile
│   ├── models.py              # Transcript, Word, Segment, Clip, ClipMetadata
│   ├── state.py               # SQLite: status por estágio e por perfil
│   ├── pipeline.py            # orquestra estágios, idempotência
│   ├── profiles.py            # carga/validação de perfis, defaults short/long
│   ├── stages/
│   │   ├── ingest.py          # yt-dlp
│   │   ├── transcribe.py      # faster-whisper
│   │   ├── select.py          # chama provider LLM, valida, ajusta fronteiras (por perfil)
│   │   ├── review.py          # tabela interativa aprovar/rejeitar/editar
│   │   ├── render.py          # despacha p/ render do perfil
│   │   ├── package.py         # metadata json/md
│   │   └── deliver.py         # copia p/ drive ou rclone
│   ├── llm/
│   │   ├── base.py            # Protocol LLMProvider
│   │   ├── gemini.py
│   │   └── ollama.py
│   ├── sources/
│   │   ├── base.py            # Protocol VideoSource (§5.2)
│   │   └── youtube.py
│   ├── video/
│   │   ├── reframe.py         # detecção de rosto + crop suavizado (9:16)
│   │   ├── zoom.py            # punch-in dinâmico (opcional)
│   │   ├── concat.py          # junção de trechos não contíguos (longs)
│   │   ├── subtitles.py       # geração .ass (karaokê e convencional)
│   │   └── ffmpeg.py          # builders de comando
│   └── utils/
├── tests/
│   ├── fixtures/              # transcript.json de exemplo, vídeo curto CC0
│   └── ...
└── workspace/                 # gitignored; cache por vídeo
```

### 3.4 Layout do workspace (cache por vídeo)

```
workspace/<video_id>/
├── source.mp4               # melhor qualidade ≤1080p, mp4
├── source.info.json         # metadados do yt-dlp
├── audio.wav                # 16 kHz mono, para whisper
├── transcript.json          # Transcript (§5.4)
├── faces.json               # amostras de posição de rosto (calculado 1x, reutilizado por todo short)
├── profiles/
│   ├── short/
│   │   ├── candidates.json  # saída bruta do LLM + score
│   │   ├── clips.json       # após ajuste de fronteiras e revisão
│   │   └── clips/
│   │       └── 01_<slug>/
│   │           ├── clip.mp4
│   │           ├── subtitles.ass
│   │           ├── thumbnail.jpg
│   │           ├── metadata.json
│   │           └── metadata.md
│   └── long/
│       └── (mesma estrutura)
└── logs/
    └── <stage>[_<perfil>].log
```

---

## 4. Perfis de formato (conceito central)

Um **perfil** é o conjunto de parâmetros que define um tipo de corte. Todo estágio depois da transcrição recebe um `Profile`. O MVP entrega dois perfis built-in, `short` e `long`; o usuário pode sobrescrever campos ou criar novos em `config.yaml`.

### 4.1 Modelo
```python
class Profile(BaseModel):
    name: str
    # seleção
    duration_min: float
    duration_max: float
    duration_target: float
    n_clips: int
    multi_segment: bool           # LLM pode devolver vários intervalos por clipe
    max_segments: int             # limite de intervalos por clipe (1 se multi_segment=False)
    boundary_mode: Literal["sentence", "topic"]
    select_prompt: Path
    metadata_prompt: Path
    # imagem
    output_width: int
    output_height: int
    fps: int
    reframe: Literal["face", "center", "blur", "none"]
    zoom: Literal["none", "punch_in"]
    # legenda
    subtitle_style: Literal["karaoke", "classic", "off"]
    subtitle: SubtitleStyle       # fonte, tamanho, cores, posição, palavras/linha
    # áudio
    loudnorm_i: float
```

### 4.2 Perfis padrão

| Campo | `short` | `long` |
|---|---|---|
| Duração (min / alvo / máx) | 20 / 45 / 90 s | 120 / 240 / 480 s |
| `n_clips` | 5 | 2 |
| `multi_segment` / `max_segments` | não / 1 | sim / 4 |
| `boundary_mode` | `sentence` | `topic` |
| Resolução | **1080×1920 (9:16)** | **1920×1080 (16:9)** |
| `reframe` | `face` (crop vertical seguindo o rosto) | `none` (mantém o quadro original inteiro; sem crop) |
| `zoom` | `punch_in` (opcional, default `none` até a fase 11) | `none` |
| Legenda | `karaoke`, 4 palavras/linha, fonte 72, a 70% da altura | `classic`, frase inteira, fonte 44, na base (90%), fundo semitransparente |
| Metadados | título ≤60 chars com gancho; 5–12 hashtags | título descritivo ≤80 chars; descrição com **capítulos** (`00:00 …`); 3–6 hashtags; tags de SEO |
| Pasta de entrega | `<vídeo>/shorts/` | `<vídeo>/longs/` |

Por que a distinção de resolução importa para o render (§7.3):
- **Short 9:16** recorta a imagem: perde-se ~68% da largura de um 16:9. O enquadramento (`reframe`) decide *o que* sobrevive ao recorte — daí rastrear o rosto. Zoom dinâmico aqui é barato porque já se está recortando.
- **Long 16:9** preserva o quadro. Não há recorte para "esconder"; qualquer zoom seria upscale a partir de 1080p e degrada. Se o fonte for < 1080p, o long sai na resolução da fonte (nunca upscale). Long em 9:16 é permitido como perfil customizado (`reframe: face` ou `blur`), mas não é o default.

### 4.3 Regras entre perfis
- Um short **pode** coincidir com parte de um long (é o "melhor minuto" dele). Isso é desejável; não deduplicar entre perfis.
- Dentro do mesmo perfil, candidatos não podem sobrepor > 30%.
- `faces.json` (§7.3) é calculado uma vez por vídeo e reaproveitado por qualquer perfil com `reframe: face`.

---

## 5. Estágio 1 — Ingest e Transcrição

### 5.1 Ingest (`stages/ingest.py`)
- Entrada: URL do YouTube (aceitar `youtube.com/watch`, `youtu.be`, Shorts, com/sem playlist param — ignorar playlist).
- `yt-dlp` com formato `bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]`, `--write-info-json`, saída `source.mp4`.
- Extrair com ffmpeg `audio.wav` (16 kHz, mono, pcm_s16le).
- Persistir: `video_id`, título, canal, duração, idioma declarado, URL, resolução da fonte.
- Erro claro se vídeo privado, com restrição de idade, ou > `max_source_duration` (default 3 h).

### 5.2 Abstração de fonte (preparação para IG/TikTok)
```python
class VideoSource(Protocol):
    def matches(self, url: str) -> bool: ...
    def fetch(self, url: str, dest: Path) -> SourceInfo: ...
```
Só `YouTubeSource` é implementado. `pipeline.py` escolhe a fonte por `matches()`. Nenhum código fora de `sources/` pode saber que é YouTube.

### 5.3 Transcrição (`stages/transcribe.py`)
- `faster-whisper`, modelo `large-v3`, `device=cuda`, `compute_type=int8_float16`, `word_timestamps=True`, `vad_filter=True`.
- Idioma: auto-detectar; permitir `--lang pt`.
- Se OOM: tentar `medium` automaticamente e logar aviso.
- Saída `transcript.json` conforme §5.4.
- Após concluir, descarregar o modelo e liberar VRAM.

### 5.4 Modelo `Transcript`
```python
class Word(BaseModel):
    text: str
    start: float   # segundos
    end: float
    prob: float

class Segment(BaseModel):     # frase/segmento do whisper
    id: int
    start: float
    end: float
    text: str
    words: list[Word]

class Transcript(BaseModel):
    video_id: str
    language: str
    duration: float
    segments: list[Segment]
```

---

## 6. Estágio 2 — Seleção de trechos (o coração do produto)

### 6.1 Fluxo (comum aos perfis)
1. Montar texto da transcrição com marcador de tempo por segmento: `[00:12:34.5] texto do segmento`.
2. Enviar ao `LLMProvider` com o prompt do perfil (`profile.select_prompt`) + parâmetros (`n_clips`, durações, `max_segments`).
3. Receber JSON estrito (lista de candidatos). Validar com pydantic; em caso de JSON inválido, repetir 1x com instrução de correção.
4. **Ajuste de fronteiras (determinístico; nunca confiar no LLM para precisão de tempo)** — aplicado a cada intervalo de cada candidato, conforme `boundary_mode`:
   - `sentence` (short): `start` → recuar até o início do segmento que contém esse instante; se houver silêncio (gap > 0,3 s entre palavras) nos 1,5 s anteriores, começar no fim do silêncio. `end` → avançar até fim da frase (pontuação `.?!` ou gap > 0,5 s).
   - `topic` (long): igual, mas preferir fronteiras mais fortes: gap > 1,5 s, ou fim de segmento seguido de conector de mudança de assunto ("então", "agora", "outra coisa", "mudando"). Procurar até 8 s ao redor do instante proposto; se não achar, cair para `sentence`.
   - Padding: 0,25 s antes, 0,4 s depois (por intervalo).
   - Se `multi_segment`: ordenar intervalos por tempo, fundir os que ficarem a < 2 s de distância, rejeitar clipe se duração total fora de `[min, max]` ou se algum intervalo < 8 s.
   - Rejeitar se sobrepõe > 30% outro candidato do mesmo perfil com score maior.
5. Ordenar por score, cortar em `n_clips`, gravar `candidates.json` e `clips.json` na pasta do perfil.

### 6.2 Prompt de seleção — short (`prompts/select_short.md`)
Instruir o modelo a agir como editor de cortes para short-form e devolver **somente JSON**:
```json
{
  "clips": [
    {
      "segments": [{"start": 754.2, "end": 812.9}],
      "hook": "primeira frase que prende (literal, do transcript)",
      "reason": "por que funciona em short-form",
      "score": 87,
      "self_contained": true,
      "topic": "tema em 3-5 palavras"
    }
  ]
}
```
Critérios de score (0–100): **Gancho** (30), **Autocontido** (25), **Valor/payoff** (25), **Ritmo** (10), **Duração-alvo 30–60 s** (10). Regras: exatamente 1 intervalo por clipe; `start`/`end` devem coincidir com início/fim de segmentos existentes; propor `n_clips + 3` candidatos; não sobrepor; idioma da resposta = idioma do vídeo.

### 6.3 Prompt de seleção — long (`prompts/select_long.md`)
Mesmo schema, mas o modelo age como editor de "melhores momentos" de 2–8 min. Cada clipe é um **tema fechado** e pode ter até `max_segments` intervalos não contíguos, listados em ordem cronológica. Critérios (0–100):
- **Arco completo** (30): tem abertura que situa, desenvolvimento e fechamento; não termina "no ar".
- **Coesão** (25): se houver vários intervalos, todos falam do mesmo tema e a junção faz sentido sem contexto omitido.
- **Gancho** (20): os primeiros 10 s justificam continuar.
- **Densidade** (15): remover trechos de enrolação, propaganda, "vamos ver se está gravando".
- **Duração-alvo 3–5 min** (10).
Regras: 1 a `max_segments` intervalos por clipe, cada um ≥ 15 s; propor `n_clips + 2` candidatos; entre intervalos do mesmo clipe pode haver qualquer distância, mas não devem se sobrepor; campo extra `"chapters": [{"offset_hint": "tema do intervalo"}]` com um item por intervalo, usado nos metadados.

### 6.4 Providers
```python
class LLMProvider(Protocol):
    name: str
    max_context_tokens: int
    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...
```
- `GeminiProvider`: modelo configurável; transcrição inteira em uma chamada; `response_mime_type="application/json"`. Tratar 429 com backoff exponencial (3 tentativas).
- `OllamaProvider`: dividir transcrição em janelas de ~6k tokens com 10% de sobreposição, selecionar por janela, depois uma chamada de merge (`prompts/merge_windows.md`) que reordena e deduplica. Para o perfil long, o merge também pode combinar intervalos de janelas diferentes num mesmo clipe. Só ativar se `provider: ollama` no config; garantir que whisper já foi descarregado.

### 6.5 Modo "Claude Code como editor" (opcional, custo zero de API)
`clipper select <id> --profile short --provider file`: grava `profiles/short/select_request.md` (prompt + transcrição) e espera `select_response.json`. Um slash command `/select-clips <video_id> <perfil>` no Claude Code lê o request, responde no mesmo schema e grava o arquivo. Usa a cota do MAX em vez de API. Implementar por último; o pipeline só precisa tratar `file` como provider que bloqueia até o arquivo existir (ou `--wait=false` e sair com instrução).

---

## 7. Estágio 3 — Revisão, Metadados e Render

### 7.1 Revisão (`stages/review.py`)
- `clipper review <video_id> --profile short`: tabela `rich` com índice, intervalos (para long, mostrar cada um), duração total, score, hook, topic.
- Ações por linha: `a` aprovar, `r` rejeitar, `e` editar start/end de um intervalo (em `mm:ss` ou segundos), `d` remover um intervalo de um clipe long, `p` preview (abre o trecho com `ffplay` ou `mpv` se disponível; para multi-segment, toca os intervalos em sequência).
- `--auto` pula a revisão e aprova os `n_clips` melhores.
- Editar reaplica o ajuste de fronteiras do §6.1 passo 4.
- A revisão tem três modos (`--review ask|auto|web`, ou `review.mode` no config):
  `ask` é a tabela acima, `auto` é o antigo `--auto`, e `web` publica
  `review_request.json` na pasta do perfil e espera `review_response.json` — o mesmo
  aperto de mão em dois arquivos do provider `file` (§6.5). Quem responde é a página do
  monitor (§15), que mostra um corte por vez tocando o trecho do próprio `source.mp4`.
  Sem resposta em `review.timeout_s`, o estágio falha e a fila segue para o próximo vídeo.

### 7.2 Metadados (`stages/package.py`)
Por clipe, uma chamada ao LLM com `profile.metadata_prompt` recebendo texto do(s) trecho(s) + título/canal do vídeo. Saída:
```python
class Chapter(BaseModel):
    offset: float         # segundos dentro do clipe renderizado
    title: str

class ClipMetadata(BaseModel):
    profile: str
    title: str            # short: ≤60 chars, gancho; long: ≤80 chars, descritivo
    hook_line: str
    description: str      # short: 2–4 frases; long: 1 parágrafo + capítulos
    chapters: list[Chapter]      # vazio para short; ≥2 para long multi-segment
    hashtags: list[str]   # short 5–12; long 3–6
    tags: list[str]       # 5–15 palavras-chave
    source_url: str       # URL com &t=<start do 1º intervalo>s
    source_segments: list[tuple[float, float]]
    language: str
```
`chapters.offset` é calculado pelo código (soma das durações dos intervalos anteriores + transições), não pelo LLM; o LLM só fornece os títulos a partir de `chapters[].offset_hint` do §6.3.

`metadata.md` (legível para copiar/colar):
```
# <title>

<description>

<capítulos, se houver — formato "00:00 Título">

<hashtags em uma linha>

Fonte: <source_url> (12:34 – 13:29[, 21:10 – 22:40])
```

### 7.3 Render (`stages/render.py` → `video/*`)
`render.py` recebe `(video, clip, profile)` e monta o pipeline abaixo. Tudo em ffmpeg; um único comando final por clipe sempre que possível.

**Passo 1 — Extração dos intervalos.** Para cada intervalo, `-ss <start> -to <end>` com re-encode (não `-c copy`) para cortar em frame exato. Saída intermediária em `intermediates/<n>.mp4` na resolução da fonte.

**Passo 2 — Junção (`video/concat.py`, só se > 1 intervalo).** Concatenar com `xfade` de 0,3 s (`fade`) e `acrossfade` de 0,3 s no áudio. Nunca cortar seco entre intervalos não contíguos. Registrar em `clips.json` os offsets resultantes de cada intervalo (usados pelos capítulos e pelas legendas).

**Passo 3 — Enquadramento por perfil.**

| `reframe` | Uso típico | Comportamento |
|---|---|---|
| `none` | long 16:9 | Sem crop. `scale` para `output_width×output_height` **apenas para baixo**; se a fonte for menor, manter a resolução da fonte. |
| `face` | short 9:16 | Rastrear rosto (`video/reframe.py`): usar `faces.json` (amostra 1 frame a cada 0,5 s com mediapipe, centro x/y e tamanho; calculado 1x por vídeo). Suavizar centro com EMA (α=0,2) e **zona morta** de 5% da largura. Se não houver rosto em > 40% das amostras do clipe, cair para `center`. Aplicar como `crop` com expressão por tempo; se muito complexo, 1 crop fixo por "cena" (mudança de posição > 25% ou corte de câmera detectado por `scdet`). Com 2 rostos: enquadrar o maior; se ambos couberem num crop 9:16, centralizar entre eles. |
| `center` | fallback | Crop central fixo 9:16. |
| `blur` | short com slides/tela | Vídeo 16:9 inteiro centralizado sobre fundo = mesmo vídeo escalado e desfocado (`boxblur`). |

**Passo 4 — Zoom dinâmico (`video/zoom.py`, só se `zoom: punch_in`).** Alternar entre 100% e ~108% de escala em fronteiras de frase (a cada 2–4 frases, escolhidas por gap > 0,4 s), com transição de 0,15 s. Implementado como `zoompan`/`scale+crop` com keyframes; o zoom é aplicado **depois** do reframe, sobre o quadro 9:16, para não competir com o rastreamento. Nunca em `reframe: none` (seria upscale).

**Passo 5 — Legendas (`video/subtitles.py`).** Gerar `.ass` a partir das `words` dos intervalos, com os tempos deslocados pelos offsets do passo 2.
- `karaoke` (short): linhas de até `max_words_per_line` (4) / `max_chars` (22), quebra em pontuação/gap; palavra ativa muda de cor (`\k`). Fonte 72, contorno 4, a 70% da altura.
- `classic` (long): frase inteira até 42 chars por linha, máx. 2 linhas, fonte 44, caixa semitransparente (`BorderStyle=3`), na base (90%). Sem karaokê.
- `off`: pular.
- Fonte default `Montserrat ExtraBold` (documentar instalação) com fallback `DejaVu Sans Bold`. Queimar com `-vf "ass=subtitles.ass"`.

**Passo 6 — Áudio.** `loudnorm=I=<profile.loudnorm_i>:TP=-1.5:LRA=11` (default −14), `aac 192k`.

**Passo 7 — Saída.** `libx264 -crf 18 -preset medium -pix_fmt yuv420p`, `fps` do perfil. Thumbnail: frame em `1,0 s` do clipe final. Verificar com `ffprobe` (duração ± 0,3 s do esperado, resolução exata do perfil ou da fonte quando menor, faixa de áudio presente). Falha → retry 1x com `reframe=center` e `zoom=none`.

Ordem dos filtros de vídeo: `crop/reframe → zoom → scale → ass`. Legenda é sempre o último filtro para não ser cortada nem escalada.

---

## 8. Estágio 4 — Entrega (`stages/deliver.py`)

Dois modos, config `deliver.mode`:
- `folder` (default): copiar para `<drive_root>/Clipper/...`. `drive_root` aponta para a pasta local do cliente Google Drive.
- `rclone`: `rclone copy ... gdrive:Clipper/...`; exige remote `gdrive` configurado pelo usuário (documentar no README).

A entrega tem duas metades e elas não se misturam:

**Arquivo** — `Clipper/arquivo/<canal>/<data>_<slug_do_video>/<pasta_do_perfil>/<n>_<slug>/`
com `metadata.json`, `metadata.md`, `thumbnail.jpg` e `subtitles.ass`. É o histórico do
que foi cortado e a base do baseline de eval (SPEC-agent §11.1). **Nenhum vídeo vai para
o arquivo** e nada é publicado de lá. Na pasta do vídeo, um único `INDEX.md` com uma seção
por perfil listando título, duração, hashtags e trechos de origem.

**Fila** (§8.1) — os vídeos. É a única metade que uma sessão de publicação abre.

### 8.1 Fila de publicação (`stages/queue.py`)

```
Clipper/publicar/2026-09-13/
├── PUBLICAR.md                       # título, descrição, tags e destino dos cortes do dia
├── youtube-e-tiktok/
│   └── eu roubei mesmo no jogo das celebridades.mp4
└── so-tiktok/
    └── a pior resposta sobre sistema binário.mp4
```

- **Uma pasta por dia**, nome em `AAAA-MM-DD` (ordena sozinho em qualquer cliente). Abrir
  uma pasta e baixar de uma vez é todo o trabalho de navegação que publicar exige.
- **Uma subpasta por destino**, `so-<x>` ou `<a>-e-<b>`: o nome da pasta é a instrução.
  O arquivo continua se chamando exatamente como o título — é dele que o YouTube preenche
  o campo de título, e um prefixo ali viraria trabalho manual em toda publicação.
- **Destino por canal de origem e perfil**, `publish.destinations`: a chave mais específica
  vence (`<canal>/<perfil>`, `<canal>`, `default/<perfil>`, `default`). Sem regra, o corte
  vai para `sem-destino/` — visível, nunca perdido.
- **O vídeo mora só aqui.** Duas cópias no Drive custariam o dobro e não dariam nada.
- **A data é decidida uma vez**, na entrega, e gravada em `state.db` (tabela
  `publish_queue`, junto com destino, nome do arquivo e uma cópia do `metadata.json`).
  Um corte que já está na pasta de quinta não muda de dia porque outro vídeo foi
  processado no meio; `Calendar` é o único lugar que data um corte. Shorts enchem o dia
  até `publish.per_day`; long é peça semanal e guarda `publish.long_every_days` do próximo.
- A fila é montada localmente por **hard link** (`queue-staging` no workspace, apagada
  depois) e enviada com uma chamada por dia — o Drive indexa pasta por id, e duas chamadas
  paralelas criam duas pastas com o mesmo nome.
- `publish_queue` sobrevive ao `clipper clean`: ela descreve o que está no Drive, não o
  workspace.
- `clipper queue --remover` tira um corte da fila (a linha é o que diz que ele está
  esperando no Drive); `--replanejar` reescreve o `PUBLICAR.md` de cada dia, porque o plano
  do dia é um retrato da fila e envelhece quando ela muda.
- `clipper queue-migrate` adota uma entrega feita antes da fila: move cada vídeo para o dia
  dele e o resto para `arquivo/`. Dentro do Drive o move é server-side, então nada sobe de
  novo. Mostra o plano e só mexe com `--aplicar`. O que já foi publicado fica de fora com
  `--skip N` (prefixo do plano) ou `--pular-video` (um vídeo inteiro) e continua no
  arquivo, com vídeo e tudo.

---

## 9. CLI

```
clipper run <url> [--profile short,long] [--auto] [--lang pt] [--provider gemini|ollama|file]
clipper ingest <url>
clipper transcribe <video_id>
clipper select <video_id> --profile <p> [--n 5]
clipper review <video_id> --profile <p> [--web]
clipper render <video_id> --profile <p> [--clip 3]
clipper package <video_id> --profile <p>
clipper deliver <video_id> [--profile <p>]
clipper batch <fila.txt|urls...> [--review auto|web] [--keep] [--min-free 15]
clipper monitor [--port 8787] [--queue fila.txt] [--open]   # painel web (§15)
clipper status [<video_id>]              # estágios concluídos por perfil
clipper profiles                         # lista perfis e seus parâmetros efetivos
clipper doctor                           # dependências externas
clipper disk                             # o que o workspace ocupa
clipper queue [--all] [--channel <c>] [--remover <t>] [--replanejar]   # a fila
clipper queue-migrate [--aplicar] [--skip N] [--pular-video <v>] [--start <data>]
clipper publish-plan [--all] [--start <data>]                   # a fila inteira num arquivo
clipper clean <video_id> [--keep-source] | --delivered
```
`--profile` aceita lista; default = `profiles.default` do config (`short,long`). `run` executa ingest e transcrição uma vez e depois o restante para cada perfil, parando em `review` de cada um (a menos que `--auto`). Toda saída principal via `rich`; logs completos em `workspace/<id>/logs/`. Códigos de saída: 0 ok, 2 erro de entrada, 3 erro externo (rede/API), 4 erro de render.

---

## 10. Configuração (`config.yaml`)

```yaml
workspace: ./workspace
llm:
  provider: gemini            # gemini | ollama | file
  gemini_model: gemini-2.5-pro
  gemini_fallback_model: gemini-2.5-flash
  ollama_model: qwen2.5:7b-instruct-q4_K_M
transcribe:
  model: large-v3
  compute_type: int8_float16
  language: auto
render:
  crf: 18
  preset: medium
profiles:
  default: [short, long]
  short:                      # sobrescreve campos do built-in
    n_clips: 5
    duration_min: 20
    duration_max: 90
    duration_target: 45
    output_width: 1080
    output_height: 1920
    fps: 30
    reframe: face
    zoom: none                # punch_in quando a fase 11 estiver pronta
    subtitle_style: karaoke
    subtitle:
      font: Montserrat ExtraBold
      size: 72
      primary_color: "#FFFFFF"
      highlight_color: "#FFD400"
      outline: 4
      vertical_pos_pct: 0.70
      max_words_per_line: 4
      max_chars_per_line: 22
  long:
    n_clips: 2
    duration_min: 120
    duration_max: 480
    duration_target: 240
    multi_segment: true
    max_segments: 4
    boundary_mode: topic
    output_width: 1920
    output_height: 1080
    fps: 30
    reframe: none
    zoom: none
    subtitle_style: classic
    subtitle:
      font: Montserrat SemiBold
      size: 44
      primary_color: "#FFFFFF"
      box_color: "#00000099"
      vertical_pos_pct: 0.90
      max_chars_per_line: 42
      max_lines: 2
  # exemplo de perfil custom: long vertical
  # long_vertical:
  #   extends: long
  #   output_width: 1080
  #   output_height: 1920
  #   reframe: blur
deliver:
  mode: folder
  drive_root: "~/Google Drive/My Drive"
  rclone_remote: "gdrive:"
  folder_names: {short: shorts, long: longs}
publish:
  queue_folder: publicar
  archive_folder: arquivo
  per_day: 4                  # shorts por dia
  long_every_days: 7          # um long por semana
  daily_profiles: [short]
  destinations:               # a chave mais específica vence
    default: [tiktok]
    example-channel: [youtube, tiktok]
    example-channel/long: [youtube]
```
`.env`: `GEMINI_API_KEY=...`

---

## 11. Testes e critérios de aceite

### 11.1 Testes automatizados (pytest)
- Unit: ajuste de fronteiras (§6.1 passo 4) em ambos os `boundary_mode` — start no meio da frase, end no meio da palavra, silêncio antes, sobreposição, fusão de intervalos próximos, rejeição por intervalo curto.
- Unit: cálculo de offsets de capítulos e deslocamento de tempos de legenda após concat com transições.
- Unit: geração `.ass` nos dois estilos — quebra de linha, karaokê, `BorderStyle`, escape de caracteres especiais.
- Unit: parsing/validação do JSON do LLM (short e long), incluindo resposta com markdown fence e `segments` fora de ordem.
- Unit: resolução de perfis (built-in + override + `extends`), incluindo erro em `zoom: punch_in` com `reframe: none`.
- Unit: seleção de `VideoSource` por URL.
- Integração (marcada `slow`, opcional): render de um vídeo CC0 de 60 s em ambos os perfis, long com 2 intervalos; checar com ffprobe resolução, duração e presença de áudio.
- Providers de LLM mockados nos testes; nunca chamar API real em CI.

### 11.2 Aceite manual do MVP
Rodar `clipper run` em 3 vídeos reais (um podcast com 1 pessoa, uma conversa com 2 pessoas, uma aula com slides) com `--profile short,long` e verificar:
- [ ] nenhum corte começa/termina no meio de frase, em nenhum perfil;
- [ ] shorts em 1080×1920 com rosto enquadrado sem tremor; `blur` funciona na aula;
- [ ] longs em 1920×1080 (ou na resolução da fonte se menor), sem crop, sem upscale;
- [ ] junções entre intervalos de um long não soam nem parecem corte seco;
- [ ] legendas sincronizadas (desvio perceptível < 100 ms) nos dois estilos, inclusive depois das junções;
- [ ] `metadata.md` do long traz capítulos com offsets corretos;
- [ ] Drive com `publicar/<dia>/<destino>/` e `arquivo/<canal>/.../INDEX.md`;
- [ ] reexecutar `run` no mesmo link não refaz nada e termina em segundos;
- [ ] vídeo de 2 h transcreve em < 15 min na 4060.

---

## 12. Plano de implementação (ordem para o Claude Code)

Cada fase termina com testes passando e um commit. Não avançar sem o critério cumprido.

1. **Esqueleto**: pyproject com `uv`, `typer`, `pydantic`, `rich`; `config.py`, `profiles.py` (built-ins short/long + override), `models.py`, `state.py`, `cli.py` com comandos vazios; `CLAUDE.md`; `pytest` rodando; `clipper profiles` funciona.
2. **Ingest + Transcribe**: `youtube.py`, `ingest.py`, `transcribe.py`; `transcript.json` de um vídeo real; VRAM liberada ao final.
3. **Select short (Gemini)**: `gemini.py`, `select_short.md`, validação, ajuste `sentence` com testes; `profiles/short/clips.json` gerado.
4. **Render short mínimo**: corte + `reframe=center` + áudio, 1080×1920, sem legenda.
5. **Legendas karaokê** + queima. Testes do gerador.
6. **Reframe por rosto**: `faces.json`, EMA, zona morta, fallback. Comparar visualmente com `center`.
7. **Package + Deliver (short)**: metadata json/md, `INDEX.md`, modo `folder`; depois `rclone`.
8. **Select long**: `select_long.md`, `boundary_mode: topic`, multi-segment com fusão/rejeição; testes.
9. **Render long**: `reframe=none` 1920×1080 sem upscale, `concat.py` com `xfade`/`acrossfade`, legenda `classic` com offsets, capítulos nos metadados, pasta `longs/`.
10. **Review interativo** (com edição de intervalos) e `run` completo multi-perfil com idempotência e `status`.
11. **Zoom dinâmico** (`punch_in`) para short; validar que não briga com o reframe.
12. **Provider Ollama** (janelas + merge) e **provider `file`** + slash command.
13. Polimento: mensagens de erro, README, `clean`, perfil custom via `extends`.

---

## 13. Riscos e decisões pendentes

| Risco | Mitigação |
|---|---|
| GPU é AMD (RX) e não NVIDIA (RTX) | `faster-whisper` exige CUDA. Com AMD, usar `whisper.cpp` com Vulkan/ROCm ou CPU (`medium`, muito mais lento). Confirmar `nvidia-smi` antes de começar. |
| Free tier da Gemini API insuficiente / mudanças de modelo | Provider abstraído; fallback `flash`; provider `file` via Claude Code. |
| Timestamps do LLM imprecisos | Nunca usar o tempo do LLM diretamente; sempre snap a segmentos reais (§6.1). |
| Long multi-segment com junção incoerente | Critério "coesão" no prompt; revisão mostra cada intervalo; `d` remove intervalo; crossfade obrigatório. |
| Reframe por rosto tremendo ou perdendo o falante em vídeos com 2 pessoas | Zona morta + EMA; regra do maior rosto/centralizar; cortes por "cena"; fallback `center`. |
| Fonte < 1080p deixando o long "pequeno" | Aceitar a resolução da fonte; nunca upscale; avisar no `status`. |
| Zoom dinâmico brigando com o rastreamento de rosto | Zoom aplicado depois do crop 9:16, amplitude ≤ 8%, só em fronteiras de frase. |
| Fonte sem suporte a acentos/emoji | Testar com texto PT-BR; fallback DejaVu. |
| yt-dlp quebrando por mudanças do YouTube | Fixar versão no lock, documentar `uv lock --upgrade-package yt-dlp`. |
| Vídeos longos estourando contexto do Ollama | Janelamento obrigatório no provider local. |

---

## 14. Conteúdo do `CLAUDE.md`

> O bloco abaixo é o conteúdo inicial. O arquivo real na raiz é a fonte e ganhou regras
> novas conforme o projeto andou (revisão web, monitor, filtros do rclone).

```
# Clipper
Ferramenta pessoal de cortes de vídeo (shorts 9:16 e longs 16:9). Leia SPEC.md antes de qualquer mudança.

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
- Mensagens ao usuário em português; código, identificadores e commits em inglês.
- Ao terminar uma fase do plano (SPEC §12), rodar testes, atualizar README se necessário e commitar.
```

---

## 15. Monitor web (`clipper monitor`)

Acrescentado depois do MVP, em 2026-09-10. Fora do escopo original (§1.3) como
interface de *operação*: o monitor não roda nada, ele observa. O que ele escreve
está listado uma vez, em `monitor/writes.py`, e cada escrita tem schema próprio e
arquivo conhecido — ver CLAUDE.md e SPEC-agent §9.5 e §11.3.

### 15.1 O que ele é
Servidor HTTP de biblioteca padrão (nenhuma dependência nova), escutando em
`127.0.0.1:8787` por padrão (`monitor.host` / `monitor.port`). Pode rodar ao lado de
um `clipper batch` em andamento: o `state.db` é aberto em `mode=ro`.

### 15.2 De onde vêm os dados
- `state.db` (somente leitura): vídeos e estágios;
- o workspace: `clips.json`, `clip.mp4`, `thumbnail.jpg`, `metadata.json`, logs;
- o arquivo de fila (`fila.txt`), para saber o que ainda não começou;
- `review_request.json`, quando um perfil está parado esperando revisão;
- `workspace/.monitor/` — cache do próprio monitor: histórico dos vídeos já entregues
  (o `batch --clean` apaga workspace e linha do banco) e cópia das thumbnails.

`ingest` e `deliver` gravam `running` no banco desde 2026-09-10; quando o registro não
existe (execução antiga), o monitor deduz o estágio ativo pelo log que está sendo
escrito nos últimos 40 s.

A dedução vale nos dois sentidos. `render` e `package` são escritos corte a corte e
ficam em `running` enquanto faltar um; se a run morrer aí, a linha sobrevive à run e a
página fica acesa sobre um processo que não existe. Então: **sem nenhum processo do
clipper na máquina, uma linha em `running` é exibida como `parado`** — o vídeo sai de
"trabalhando", o item da fila sai de "em produção", e o parcial (`3/4 cortes`) continua
à vista. O agent também fecha essas linhas quando a run termina (SPEC-agent §8.4.1);
isto aqui é o que cobre a run que morreu sem poder fechar nada.

### 15.3 Endpoints
| rota | o quê |
|---|---|
| `GET /` | a página (um HTML estático, sem CDN) |
| `GET /api/snapshot` | o estado inteiro em JSON |
| `GET /api/events` | o mesmo por SSE, empurrado a cada mudança |
| `GET /api/thumbnail?video&profile&clip` | thumbnail do corte (workspace ou cache) |
| `GET /api/clip?video&profile&clip` | `clip.mp4`, com suporte a Range |
| `GET /api/source?video` | `source.mp4`, com Range (a revisão toca trechos dele) |
| `POST /api/review` | grava `review_response.json` (SPEC §7.1) |
| `POST /api/evals` | grava as notas de uma rodada em `evals/<rodada>/notas.md` |

Nomes de vídeo, perfil e corte são validados como um único componente de caminho; a
resposta de revisão só é aceita se houver um pedido pendente e os índices existirem nele.

### 15.4 A página
Duas leituras do mesmo estado, alternadas no topo e lembradas no navegador:
**Simples** (padrão) fala em passos — Baixando, Ouvindo, Escolhendo, Montando,
Descrevendo, Entregando — com o que está acontecendo agora em uma frase, a trilha dos
sete pontos, a galeria de cortes (clicar assiste) e a fila. **Detalhes** acrescenta o
diagrama por perfil com os nomes reais dos estágios, nota do modelo, tamanho, pasta e o
log ao vivo. Tema claro/escuro. Tudo em português, sem jargão de pipeline no modo
simples.
