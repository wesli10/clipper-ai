# Clipper

Ferramenta pessoal de linha de comando que recebe o link de um vídeo do YouTube,
transcreve localmente, usa um LLM para escolher os melhores trechos e entrega
cortes prontos para publicar — **shorts** (9:16, ≤90 s) e **longs** (16:9, 2–8 min) —
com legendas queimadas, título, descrição e hashtags, numa pasta do Google Drive.

A especificação completa está em [SPEC.md](SPEC.md) e é a fonte de verdade.
[PLAN.md](PLAN.md) traduz a §12 em fases; [TASKS.md](TASKS.md) acompanha o progresso.

## Como funciona

```
link ──▶ ingest ──▶ transcribe ──┬──▶ select ──▶ review ──▶ render ──▶ package ──┐
         yt-dlp     faster-whisper│    (perfil    (humano,   (perfil    (perfil    │
                    (GPU)         │     short)     opc.)      short)     short)    ├──▶ deliver
                                  └──▶ ... perfil long ...                         ┘
```

Ingest e transcrição acontecem **uma vez por vídeo**; o resto acontece **uma vez por
(vídeo, perfil)**. Todo estágio é idempotente: rodar de novo não refaz nada, a menos
que você passe `--force`.

## Instalação

Requisitos: Python 3.12, [`uv`](https://docs.astral.sh/uv/), `ffmpeg` **e `ffprobe`** no PATH.
Para transcrição na GPU, uma NVIDIA com CUDA (a spec assume uma RTX 4060 de 8 GB).

```bash
uv sync                                    # núcleo (CLI, config, seleção)
uv sync --extra gpu                        # + faster-whisper e as libs CUDA (transcrição)
uv sync --extra vision                     # + mediapipe/opencv (reframe por rosto)
uv sync --extra llm                        # + google-genai/httpx (providers)
uv sync --extra gpu --extra vision --extra llm   # tudo
```

`uv run clipper doctor` checa cada dependência externa e diz o que falta.

O extra `gpu` traz `nvidia-cublas-cu12` e `nvidia-cudnn-cu12` junto: o CTranslate2
carrega essas bibliotecas por `dlopen` em tempo de execução e, sem elas, toda
tentativa de usar CUDA falha e a transcrição cai para a CPU silenciosamente.
O `stages/transcribe.py` pré-carrega as duas, então não é preciso mexer em
`LD_LIBRARY_PATH`.

### ffmpeg e ffprobe

O render depende dos dois. Builds estáticos que trazem só o `ffmpeg` não bastam:
a verificação da saída (duração, resolução, faixa de áudio) usa o `ffprobe`.

```bash
sudo apt install ffmpeg        # traz os dois
```

### Modelo de transcrição

`large-v3-turbo`, com `large-v3` como fallback. Medido em 41:56 de áudio numa
RTX 4060: **6min44s → 1min58s**, 3,4× mais rápido.

A diferença de texto entre os dois é de 13% das palavras — mas duas rodadas do
próprio turbo no mesmo áudio já diferem em 10%, então quase tudo isso é variação
de execução, não perda de qualidade. O que o pipeline realmente consome não
mudou: 99,7% das frases terminam em pontuação nos dois, e a proporção de finais
que fecham assunto é a mesma (76%).

O custo real é pequeno e vale conhecer: 2,2% das palavras saem com duração ≤ 0
contra 1,5% no `large-v3`, e essas são descartadas da legenda queimada. Se a
legenda começar a perder palavras demais, `transcribe.model: large-v3` reverte —
nada mais no pipeline depende do modelo.

### Fonte das legendas

O padrão é `Montserrat ExtraBold` (shorts) e `Montserrat SemiBold` (longs), com
fallback para `DejaVu Sans`. Para instalar a Montserrat no Linux:

```bash
mkdir -p ~/.local/share/fonts
# baixe de https://fonts.google.com/specimen/Montserrat e descompacte ali
fc-cache -f
```

### Escolhendo o provider de LLM

O LLM faz exatamente duas coisas: **escolher os trechos** (`select`) e **escrever
os metadados** (`package`). Nada mais passa por ele — em particular, os tempos que
ele devolve nunca vão direto para o render, sempre passam pelo ajuste de
fronteiras. Quatro opções, em `llm.provider`:

| provider | Autenticação | Custo | Observação |
|---|---|---|---|
| `gemini` | `GEMINI_API_KEY` | free tier | Padrão. Rápido (~3 s). O free tier só tem cota nos modelos `flash`. |
| `claude` | sua assinatura do Claude Code | cota da assinatura | Roda `claude -p`. Julgamento editorial melhor; ~20 s por chamada e ~10 k tokens de overhead do harness por chamada. |
| `ollama` | nenhuma | zero | Modelo local, com janelamento. Só com o whisper já descarregado. |
| `file` | nenhuma | zero | Grava o prompt e espera você responder pelo `/select-clips`. |

### Quantas chamadas por vídeo

Duas de seleção (uma por perfil) e **uma de metadados por perfil** — os metadados
de todos os cortes saem numa chamada só. São **4 chamadas por vídeo**, não uma por
corte. Além de gastar menos, o modelo vê os cortes lado a lado e para de escrever
cinco títulos quase idênticos para o mesmo vídeo.

Dá para dividir por estágio, pagando o modelo forte só onde ele decide:

```yaml
llm:
  select_provider: claude     # a decisão editorial
  metadata_provider: gemini   # título e hashtags, no free tier
```

Isso deixa 2 chamadas de Claude e 2 de Gemini por vídeo.

Para usar o Claude com a sua assinatura, basta ter o Claude Code instalado e
logado (`claude` no PATH) — não precisa de `ANTHROPIC_API_KEY`:

```bash
uv run clipper select <video_id> --profile short --provider claude
# ou, permanentemente, em config.yaml:  llm: { provider: claude }
```

A spec descarta a API da Anthropic porque o Claude MAX não inclui créditos de API
(§2). Este provider contorna isso pelo mesmo caminho que a §6.5 já previa —
a cota da assinatura — só que sem o passo manual.

### Chave da Gemini

```bash
cp .env.example .env
# edite e coloque a chave do Google AI Studio em GEMINI_API_KEY
```

Sem chave você ainda pode usar `--provider ollama` (modelo local) ou
`--provider file` (o Claude Code responde, sem custo de API — veja abaixo).

### Google Drive

Dois modos, em `config.yaml`.

### `deliver.mode: rclone` (padrão)

Envia por API. Não depende do cliente do Google Drive no Windows nem de montar
unidade dentro do WSL, e não ocupa disco local com uma cópia sincronizada.

```bash
# 1. instalar (Linux/WSL)
curl -sL -o /tmp/rclone.zip https://downloads.rclone.org/rclone-current-linux-amd64.zip
python3 -c "import zipfile;z=zipfile.ZipFile('/tmp/rclone.zip');n=next(x for x in z.namelist() if x.endswith('/rclone'));z.extract(n,'/tmp')"
install -m 755 /tmp/rclone-v*/rclone ~/.local/bin/rclone

# 2. autorizar (abre o navegador uma vez)
rclone config create gdrive drive scope drive

# 3. conferir
rclone lsd gdrive:
uv run clipper doctor
```

No WSL, o rclone sobe um servidor em `127.0.0.1:53682` e imprime a URL de
autorização; abra no navegador do Windows (o WSL2 encaminha o localhost de
volta). Se o navegador não abrir sozinho, use
`rclone config create gdrive drive scope drive config_is_local false`: ele só
imprime a URL e espera o código colado.

O nome do remote precisa bater com `deliver.rclone_remote` no `config.yaml`.

**Atenção ao `client_id` compartilhado.** Por padrão o rclone usa um `client_id`
do Google compartilhado entre todos os usuários, que o próprio rclone avisa que
**será desativado durante 2026**. Quando isso acontecer a entrega para de
funcionar com um erro de autenticação. Para não depender disso, crie um
`client_id` próprio (grátis, ~5 min no Google Cloud Console) seguindo
<https://rclone.org/drive/#making-your-own-client-id> e depois:

```bash
rclone config update gdrive client_id SEU_ID client_secret SEU_SECRET
rclone config reconnect gdrive:   # reautoriza com as credenciais novas
```

**A entrega é aditiva de propósito.** Ela usa `rclone copy`, nunca `sync`: o que já
está no Drive fica até você publicar e remover à mão. Uma consequência a ter em mente:
reprocessar um vídeo cujos cortes mudaram deixa os antigos ao lado dos novos, com a
mesma aparência. Se isso acontecer, o `INDEX.md` do vídeo sempre reflete a última
execução — use ele como referência do que é atual.

### Como fica no Drive

Duas metades, e só uma delas você abre para publicar:

```
Clipper/
├── publicar/                         # a fila: uma pasta por dia
│   └── 2026-09-13/
│       ├── PUBLICAR.md               # título, descrição, tags e destino dos cortes do dia
│       ├── youtube-e-tiktok/
│       │   └── eu roubei mesmo no jogo das celebridades.mp4
│       └── so-tiktok/
│           └── a pior resposta sobre sistema binário.mp4
└── arquivo/                          # o histórico, sem vídeo nenhum
    └── example-channel/2026-08-25_quem-e-clt/
        ├── INDEX.md
        └── shorts/01_alimento-proibido/{metadata.json,metadata.md,thumbnail.jpg}
```

Publicar é abrir a pasta do dia, baixar os três a cinco arquivos de uma vez e colar
os campos do `PUBLICAR.md` que está ali dentro. O nome da subpasta diz onde cada corte
vai; o nome do arquivo é o título.

Quem vai para onde está em `publish.destinations`, no `config.yaml`, por canal de
origem e por formato — a chave mais específica vence:

```yaml
publish:
  per_day: 4                         # shorts por dia
  long_every_days: 7                 # um long por semana
  destinations:
    default: [tiktok]                # canal de cortes em geral
    example-channel: [youtube, tiktok]
    example-channel/long: [youtube]    # long 16:9 não vai para o TikTok
```

O dia de cada corte é decidido na entrega e guardado no `state.db`: um corte que já
está na pasta de quinta não muda de dia porque outro vídeo entrou no meio. O vídeo
existe só na fila — o arquivo guarda o que foi cortado, não uma segunda cópia.

Para ver a fila sem abrir o Drive:

```bash
uv run clipper queue                       # os próximos dias
uv run clipper queue --all                 # inclui o que já passou
uv run clipper queue --remover "<titulo>"  # tira da fila (o arquivo fica no Drive)
uv run clipper queue --replanejar          # reescreve o PUBLICAR.md de cada dia
```

`--remover` é para o corte que saiu da fila sem ser publicado pela fila — desistiu dele,
ou publicou fora do plano. Depois de remover, `--replanejar` põe o `PUBLICAR.md` de cada
dia de acordo (o vídeo em si continua no Drive, para você apagar se quiser).

**Já tinha entrega no formato antigo** (uma pasta por corte, vídeo lá dentro)?
`queue-migrate` adota o que está lá: move cada vídeo para o dia dele e o resto para
`arquivo/`. Dentro do Drive o move é server-side — nada sobe de novo.

```bash
uv run clipper queue-migrate                     # só mostra o plano
uv run clipper queue-migrate --skip 4 --aplicar  # os 4 primeiros você já publicou
uv run clipper queue-migrate --pular-video jogam-uno --aplicar   # esse vídeo já saiu
```

O que você deixa de fora fica no arquivo, com vídeo e tudo — a fila só carrega o que
ainda falta publicar.

### `deliver.mode: folder`

Copia para `deliver.drive_root`, que deve apontar para a pasta local
sincronizada pelo cliente do Google Drive. No WSL isso normalmente é a unidade
do Google Drive para Desktop montada em `/mnt/g` — por exemplo
`drive_root: "/mnt/g/Meu Drive"`.

## Uso

```bash
uv run clipper run "https://www.youtube.com/watch?v=..."          # pipeline inteiro
uv run clipper run "<url>" --profile short --auto                 # só shorts, sem revisão
uv run clipper run "<url>" --lang pt --provider gemini
```

### Fila (vários vídeos, sem supervisão)

```bash
uv run clipper batch fila.txt              # arquivo com uma URL por linha
uv run clipper batch "<url1>" "<url2>"     # ou as URLs direto
```

O arquivo é **relido antes de cada vídeo**, então dá para ir acrescentando URLs
com a fila já rodando. Linhas vazias e tudo depois de `#` são ignorados.

A fila sempre roda em `--auto` (não há ninguém para revisar) e **limpa o workspace
de cada vídeo assim que ele é entregue** — sem isso, uma fila longa enche o disco
enquanto você não está na máquina. Use `--keep` para preservar.

Outras proteções para rodar desatendido:

- vídeos já entregues são pulados, então reexecutar a fila não refaz trabalho;
- uma falha em um vídeo é registrada e a fila continua (`--stop-on-error` muda isso);
- a fila para sozinha se o disco cair abaixo de `--min-free` (15 GB por padrão),
  em vez de encher a partição.

Ao fim sai uma tabela com o que deu certo, o que falhou e quanto foi liberado.
Para um vídeo só, `clipper run <url> --clean` faz a mesma limpeza.

Ou estágio a estágio:

```bash
uv run clipper ingest "<url>"
uv run clipper transcribe <video_id>
uv run clipper select <video_id> --profile short
uv run clipper review <video_id> --profile short
uv run clipper render <video_id> --profile short
uv run clipper package <video_id> --profile short
uv run clipper deliver <video_id>
```

Utilitários:

```bash
uv run clipper monitor                 # painel web para acompanhar tudo ao vivo
uv run clipper status [<video_id>]     # o que já foi feito, por perfil
uv run clipper profiles                # parâmetros efetivos de cada perfil
uv run clipper doctor                  # dependências externas
uv run clipper queue                   # a fila: que corte sai em que dia e para onde
uv run clipper publish-plan            # a fila inteira num arquivo, pronta para colar
uv run clipper disk                    # o que o workspace ocupa
uv run clipper clean <video_id> [--keep-source]
uv run clipper clean --delivered       # limpa tudo que ja foi para o Drive
```

### Acompanhando ao vivo (`clipper monitor`)

```bash
uv run clipper monitor                 # http://127.0.0.1:8787
uv run clipper monitor --port 9000 --queue fila.txt --open
```

Abre uma página que mostra a pipeline inteira em tempo real e pode rodar **junto
com uma fila em andamento**: o `state.db` é aberto em modo somente-leitura e
nada no workspace é alterado (só um cache próprio em `workspace/.monitor`).

A página é feita para quem não conhece o pipeline: os estágios aparecem com
nome em português (`baixando`, `ouvindo`, `escolhendo`, `montando`,
`descrevendo`, `entregando`) e o vocabulário técnico fica no modo **Detalhes**.

- **o que está acontecendo agora** — uma frase (“Montando os cortes com
  legenda”), o vídeo da vez, há quanto tempo começou e uma barra de progresso;
- **o caminho de cada vídeo** — os sete passos em linha, o atual pulsando e a
  contagem do que está parado em cada um;
- **seus cortes** — galeria com a thumbnail de cada corte, formato, duração e a
  situação em palavras (“Já no Drive”, “Montando agora”). Clicar abre o
  `clip.mp4` ali mesmo;
- **vídeos** e **próximos da fila** — uma linha por item, com a situação;
- **Detalhes** (botão no topo) — acrescenta o diagrama por perfil com os nomes
  reais dos estágios, nota do modelo, tamanho do arquivo, pasta e o log ao vivo.

Tem também alternância entre tema claro e escuro; a preferência fica salva no
navegador.

O monitor não roda nada: ele observa. Escrever, escreve cinco coisas, cada uma
com seu schema e seu arquivo — a **revisão** (abaixo), a **fila**, os **canais**,
os **controles** de uma run em andamento e as **notas dos cortes** (abaixo).
Nenhuma delas aceita caminho vindo do navegador.

A atualização chega por SSE (`/api/events`), sem recarregar a página. O download
do yt-dlp e o upload do rclone não são registrados no banco enquanto acontecem,
então o monitor os deduz do log que está sendo escrito — um vídeo aparece na
página assim que o download começa, antes mesmo de existir no `state.db`.

Como `batch` apaga o workspace de cada vídeo entregue, o monitor guarda em
`workspace/.monitor` o que já viu (incluindo as thumbnails), e os cortes
entregues continuam na galeria depois da limpeza.

### Notas dos cortes

A nota que você dá a um corte é a única métrica que o sistema não produz sozinho.
O laudo do `review_render` mede o que é objetivo — duração, loudness, fronteira,
legenda — e um corte pode passar em tudo e ainda assim não prestar. Sem a sua
nota, a única opinião sobre qualidade no sistema é a do próprio agent sobre o
próprio trabalho.

Por isso ela é dada na página, não no editor de texto: seção **Notas dos cortes**,
uma aba por rodada de eval, um corte por linha com thumbnail, o vídeo abrindo ali
mesmo ao clicar, botões de 1 a 5 e um campo de comentário. Salva a cada clique.

```bash
uv run clipper eval run --baseline evals/baseline.json   # gera a rodada
uv run clipper monitor                                   # dê as notas na página
uv run clipper eval report --dir evals/rodada1           # a sua média ao lado do medido
```

O arquivo por trás é `evals/<rodada>/notas.md` e continua seu para editar à mão —
a página reescreve só as colunas `nota` e `comentario`, linha a linha. Nota 0
apaga. O corte que o agent descartou aparece marcado como tal: o laudo já disse
que ele estava tecnicamente certo em descartar, e só a sua nota diz se ele estava
certo.

### Revisão pelo navegador

A revisão do §7.1 também acontece no monitor, que é bem mais fácil do que o
terminal para quem não vive nele:

```bash
uv run clipper monitor                          # deixe aberto numa aba
uv run clipper run "<url>" --review web         # um vídeo
uv run clipper batch fila.txt --review web      # a fila inteira
uv run clipper review <video_id> --web          # só a revisão de um vídeo já selecionado
```

Ou fixe no `config.yaml` (`review: { mode: web }`) e todo `clipper run` já pede
pelo navegador.

Quando chega a vez da revisão, a pipeline **para** e grava
`review_request.json` na pasta do perfil — o mesmo padrão do provider `file`,
um arquivo de ida e um de volta, sem ninguém segurando conexão. A página mostra
uma chamada no topo (“Escolha os cortes”) e abre uma tela por corte:

- o trecho escolhido tocando **direto do vídeo original**, e só ele: ao chegar
  no fim do intervalo o player emenda no próximo trecho do corte ou para;
- tema, gancho, o motivo que o modelo deu e o texto do que é dito;
- **Descartar** / **Aprovar** em botões grandes (ou as teclas `r` e `a`, setas
  para navegar), com as bolinhas embaixo mostrando o que já foi decidido.

Só dá para enviar depois de decidir todos. O envio grava
`review_response.json`, a pipeline percebe em cerca de 1,5 s e segue para o
render. Se ninguém responder dentro de `review.timeout_s` (1 h por padrão), a
pipeline desiste com erro e, numa fila, passa para o próximo vídeo.

### Publicando

O `deliver` deixa tudo pronto no Drive, mas publicar continua sendo manual — a
API do YouTube trava como **privado** todo vídeo enviado por um projeto não
auditado, e não há recurso. Na prática, o caminho é o agendamento do próprio
YouTube Studio.

Cada dia já leva o seu `PUBLICAR.md` dentro da pasta, no Drive. Para ter a fila
inteira num arquivo só, aberto ao lado do Studio:

```bash
uv run clipper publish-plan
uv run clipper publish-plan --channel example-channel
uv run clipper publish-plan --start 2026-09-15    # só a partir desta data
```

Sai um `PUBLICAR.md` com um bloco por corte, na ordem de publicação: caminho do
arquivo na fila, dia, destino, e título, descrição, capítulos, hashtags e tags em
blocos de código prontos para copiar. Cada corte tem um `[ ]` para marcar ao publicar.

O arquivo na fila leva o **título do corte** como nome (`Qual é a comida
proibida na marmita.mp4`, não `clip.mp4`): o YouTube preenche o campo de título
a partir do nome do arquivo, e é o único metadado que ele lê do próprio arquivo
— descrição e tags têm de ser coladas. No workspace o render continua sendo
`clip.mp4`; o nome titulado é um hard link criado na hora de montar a fila, então
não custa disco e a idempotência do render não muda.

O que é igual em todo corte (crédito da fonte, links, hashtags do canal) cabe
nos **Padrões de upload** do YouTube Studio, que entram sozinhos em cada envio.
Só sobra para colar o que é específico do corte.

Cada long traz também um prompt de thumbnail pronto para colar em um gerador de
imagem: arte sem pessoas, com uma frase curta de 3 a 5 palavras que o estágio de
metadados escreve (`thumbnail_text`), porque modelo de imagem erra título longo
com acento. Shorts não levam — o feed toca o próprio vídeo. `--no-thumbnails`
tira os blocos.

Ele lê a fila do `state.db`, não o workspace — que já foi limpo — e não o Drive:
o dia e o destino foram decididos na entrega, e um roteiro que recalculasse isso
discordaria das pastas onde os cortes já estão. A ordem dentro de cada vídeo é a do
número do corte, que é a ordem de score.

### Espaço em disco

Um vídeo de 20 min ocupa cerca de **1 GB** no workspace: a fonte em 1080p (~600 MB),
o `audio.wav` sem compressão (~35 MB) e os cortes renderizados (~600 MB, os longs
em 1080p com CRF 18 são pesados). Nada disso é necessário depois que os cortes
chegam ao Drive.

```bash
uv run clipper disk                # mostra por vídeo e quanto já pode sair
uv run clipper clean --delivered   # remove os já entregues
```

Fora do workspace, a instalação também ocupa espaço fixo:

| | tamanho | pode remover? |
|---|---|---|
| `.venv` | ~3 GB | não (2,2 GB são as libs CUDA do `--extra gpu`) |
| modelo `large-v3` (`~/.cache/huggingface`) | 2,9 GB | não, é o transcritor |
| modelo `medium` (fallback) | 1,5 GB | sim, só é usado se o `large-v3` falhar |
| cache do `uv` (`~/.cache/uv`) | vários GB | sim: `uv cache prune` |

### Vídeos longos

O gargalo não é a transcrição, é o **upload**. Medido nesta máquina: 114 MB
levaram 422 s com o chunk padrão do rclone e 177 s com `--drive-chunk-size 128M`,
que agora é o default (`deliver.rclone_chunk_size`). Ainda assim são ~0,65 MB/s,
que costuma ser o teto do link de subida.

Se a entrega estiver demorando, o outro lado da conta é o tamanho dos arquivos:
`render.crf: 18` é praticamente sem perdas. Subir para 20 ou 21 reduz bastante o
arquivo — as plataformas reencodam tudo de qualquer forma.

Limites: `max_source_duration` recusa fontes acima de 3 h (configurável). Um vídeo
de 2 h transcreve em 11–18 min na RTX 4060 e cabe folgado no contexto dos dois
providers de LLM.

Códigos de saída: `0` ok, `2` erro de entrada, `3` erro externo (rede/API), `4` erro de render.

### Revisão

`clipper review` mostra uma tabela com os cortes propostos. Ações:

| tecla | efeito |
|---|---|
| `a 1 3` | aprova os cortes 1 e 3 |
| `r 2` | rejeita o corte 2 |
| `e 1 2 12:30 13:45` | edita o intervalo 2 do corte 1 (reaplica o ajuste de fronteiras) |
| `d 1 2` | remove o intervalo 2 do corte 1 (longs multi-trecho) |
| `p 1` | preview com `mpv` ou `ffplay` |
| `s` | salva e sai (pendentes viram aprovados) |
| `q` | sai sem salvar |

### Deixando o agent conduzir (`--agent`)

O pipeline linear faz o que voce mandou, na ordem que voce mandou. Com `--agent` quem
conduz e um loop de Claude que decide **quantos** cortes o video rende e de quais
perfis, confere o proprio render e refaz o que ficou ruim, escreve os titulos e so
entao publica:

```bash
uv sync --extra agent          # + os extras que voce ja usa (--extra gpu --extra vision --extra llm)
uv run clipper run <url> --agent --dry-run   # ensaio: faz tudo, nao envia nada ao Drive
uv run clipper run <url> --agent             # de verdade
```

Nao precisa de chave: o loop roda no `claude-agent-sdk`, que usa a mesma assinatura do
seu Claude Code. `--legacy` continua sendo o padrao de `clipper run`.

As ferramentas do agent sao as etapas que ja existem — ele nao tem acesso a mais nada:
sem Bash, sem ler arquivo, sem rede. Todo intervalo que ele escolhe passa pelas mesmas
regras de fronteira do pipeline, e a unica ferramenta que sai desta maquina e a de
publicar.

**O orcamento** vive em `config.yaml`, no bloco `agent:`, e sao fusiveis, nao metas:

```yaml
agent:
  model: claude-opus-5
  max_turns: 40
  max_renders: 10           # por video
  max_clip_renders: 2       # refazer o mesmo corte
  max_scans_per_profile: 2  # find_candidates e a chamada cara
  wall_clock_s: 5400
  profiles: [short]         # o que o agent pode escolher; vazio = o canal, depois profiles.default
```

Quando um limite estoura, a ferramenta e recusada com o motivo e o loop segue — e se o
que estourar for turno, token ou relogio, o runtime entrega o que ja estava pronto em
vez de deixar na maquina.

**O canal** (`channels/<id>.yaml`) e o que da estilo ao agent: em que tom escrever
titulo, o que rende e o que nao rende, quais formatos publicar e que ajustes de render
valem sempre neste canal. Copie `channels/exemplo.yaml`, edite, e rode com
`--channel <id>` — com um unico arquivo em `channels/`, nem precisa do parametro.

As hashtags fixas do canal (`title.hashtags`) sao garantidas pelo codigo em
`set_metadata`, nao pedidas ao modelo: uma tag do canal que ele esquece uma vez e um corte
que sai da busca do proprio canal. O modelo escreve so as do tema.

O arquivo e seu: o agent le e nunca escreve nele. O que ele aprende numa run (no maximo
cinco anotacoes, datadas, com o id da run) vai para
`workspace/channels/<id>.learned.json`, que voce pode apagar inteiro sem perder nada da
sua configuracao. Ele tambem nao tem dado de audiencia: o que nao foi observado na
propria run e anotado como suposicao, ou nao e anotado.

**Pela página** (`clipper monitor`) voce ainda: **pausa** uma run em andamento (o agent
para na proxima chamada e espera, sem gastar turno), **para** com ou sem entregar o que ja
ficou pronto, **veta** um corte que nao quer ver publicado, edita a **fila** (acrescentar,
tirar, reordenar), edita os **canais** e dá as **notas** de uma rodada de eval. Editar um canal pela pagina reescreve o YAML a
partir dos campos, entao comentarios que voce tenha escrito ali a mao somem.

**Acompanhando:** cada run grava `workspace/<video>/agent/<run_id>.jsonl` e o
`clipper monitor` mostra a sessao: orcamento em barras, o que esta rodando agora com
cronometro, os cortes com quantos renders cada um custou e o veredito do laudo, e — no
modo Detalhes — a trilha de chamadas, os laudos e as fronteiras que ele mexeu. No fim
sai um `<run_id>.report.md` com o que o agent relatou ao lado do que o runtime mediu.

### Medindo o agent contra o que ja foi publicado

```bash
uv run clipper eval baseline                      # le os metadata.json do Drive
uv run clipper eval run --limit 5                 # roda o agent nos mesmos videos, em ensaio
# preencha a coluna nota em evals/<data>/notas.md
uv run clipper eval report evals/<data>
```

O baseline nao e mantido a mao: todo corte entregue carrega `source_url` e
`source_segments`, entao a lista do que o pipeline linear escolheu e lida de volta da
propria entrega. O relatorio mostra, por video, quantos cortes o agent achou dos mesmos
momentos (sobreposicao IoU), quantos renders gastou por corte entregue, turnos, custo e
quantos `fail` o laudo pegou — ao lado da sua nota, que e a unica coisa que a
sobreposicao nao mede.

Uma armadilha que o relatorio avisa: depois que o agent publica um video, os cortes dele
sao o que esta no Drive, entao reconstruir o baseline devolve o trabalho do proprio agent
para ser comparado com ele mesmo. Esses videos aparecem marcados e ficam fora da media.

### Claude Code como editor (sem custo de API)

```bash
uv run clipper select <video_id> --profile short --provider file
```

Isso grava `workspace/<video_id>/profiles/short/select_request.md` e espera. No
Claude Code, rode `/select-clips workspace/<video_id>/profiles/short` — o comando
lê o request, responde no mesmo schema e grava `select_response.json`. Use
`--no-wait` para sair na hora e rodar o comando de novo depois.

## Perfis

Um **perfil** é o conjunto de parâmetros de um tipo de corte. Os built-ins:

| | `short` | `long` | `quiz` |
|---|---|---|---|
| Duração (mín/alvo/máx) | 20 / 45 / 90 s | 120 / 240 / 480 s | 15 / 40 / 75 s |
| Cortes | 5 | 2 | 3 |
| Trechos por corte | 1 | até 4, não contíguos | 1, partido na revelação |
| Resolução | 1080×1920 (9:16) | 1920×1080 (16:9), nunca upscale | 1080×1920 (9:16) |
| Enquadramento | `letterbox` (ver abaixo) | quadro inteiro, sem crop | `blur` |
| Legenda | karaokê, 4 palavras/linha | sem legenda queimada | karaokê |
| Pasta de entrega | `shorts/` | `longs/` | `shorts/` |

### Pergunta e revelação (`quiz`)

Um formato, não um recorte: o corte para na pergunta, **congela o quadro** por
2,4 s com um card perguntando o palpite do espectador, e só então mostra a
resposta. É o formato que mais puxa comentário, e é o que separa um corte
transformado de um trecho reaproveitado — o que as duas plataformas passaram a
exigir para não derrubar o alcance.

```bash
uv run clipper run <url> --profile short,quiz
```

Ele não entra no `profiles.default` de propósito: nem todo vídeo tem um momento
com essa estrutura. Quando não tem, o modelo devolve lista vazia — resposta
válida, e melhor que forçar uma pergunta cuja resposta todo mundo adivinha.

Três verificações determinísticas rodam antes do render, porque nenhuma delas
precisa ser confiada ao prompt:

- o `reveal_at` é puxado para o **início de uma frase real** da transcrição,
  como `start` e `end` já são;
- a frase imediatamente anterior tem que ser **uma pergunta de verdade** — tag
  retórica ("né?", "sacou?") não conta, senão o card segura o nada;
- tem que sobrar pelo menos `card_min_part_s` (3 s) de pergunta antes e de
  resposta depois.

O card é desenhado como evento `.ass`, não gravado no quadro: o `ass` é o último
filtro da cadeia (CLAUDE.md), o único ponto em que a resolução de saída já é
final. Um card desenhado sobre a fonte 16:9 acabaria encolhido na faixa central
do short.

A pausa é obtida estendendo a própria primeira parte (`tpad` + `apad`), em vez
de gerar um arquivo de card separado: um arquivo só, com um só conjunto de
parâmetros de stream, e o crossfade seguinte não tem nada para reconciliar.

Ajustes ficam em `profiles.quiz` no `config.yaml`: `card_seconds`, `card.dim`,
`card.size_pct`, `card.countdown`. O `dim` é opacidade nominal — libass mistura
em gamma, então 0.75 tira cerca de metade da luminância, não três quartos.

### Enquadramento dos shorts (`reframe`)

Levar um quadro 16:9 para 9:16 obriga a escolher entre perder imagem ou perder tamanho:

| modo | o que faz | quando usar |
|---|---|---|
| `letterbox` | quadro inteiro, centralizado, barras em cima e embaixo | **padrão**. Várias pessoas, gráficos na tela, ação fora do centro |
| `blur` | mesma geometria, barras substituídas pelo vídeo desfocado e escurecido | quando as barras pretas incomodam |
| `center` | corte central fixo | uma pessoa só, centralizada |
| `face` | rastreia o rosto e acompanha | talking head onde a pessoa se move; custa uma passada de detecção no vídeo inteiro |

O fundo do `blur` é escurecido por padrão (`blur_brightness: -0.30`,
`blur_saturation: 0.6`): a legenda queimada fica em cima dele, e sobre um blur
claro e movimentado o texto branco some. Ponha `blur_brightness: 0` e
`blur_saturation: 1` para o blur puro da spec.

Com `letterbox` a imagem ocupa cerca de um terço da altura e a legenda cai na barra
de baixo, o que costuma deixá-la mais legível. Com `center` a imagem preenche a tela,
mas ~68% da largura do quadro original é descartada.

### Legenda queimada só nos shorts

O `short` queima a legenda; o `long` não (`subtitle_style: "off"`). O feed de
Shorts toca mudo por padrão e sem legenda o corte não se entende; em 16:9 o
espectador está no desktop ou na TV com som, e o YouTube já gera legenda
automática que ele liga se quiser.

Atenção ao editar: em YAML, `off` sem aspas vira booleano. O `Profile` aceita os
dois, mas escreva `"off"` para não depender disso.

### Card de fim (`outro`)

`profiles.<perfil>.outro` aponta para um mp4 colado no fim de cada corte. Ele é
um vídeo pronto, não material bruto: **não** passa pelo reframe, zoom ou
legenda — entra depois do encode filtrado, senão o card seria recortado e
ganharia legenda por cima.

O arquivo é normalizado uma vez por (arquivo, formato) e cacheado em
`workspace/cache`: qualquer resolução, qualquer fps, com ou sem faixa de áudio
vira exatamente os parâmetros de saída do perfil. Sem isso o `xfade` e o
`acrossfade` da junção não teriam como casar os dois lados.

São **dois arquivos**, um por proporção: gerador de vídeo entrega 16:9 ou 9:16,
e um único card entraria esticado em metade dos cortes.

Se o arquivo sumir ou não puder ser lido, o render cai no modo degradado e sai
**sem o card** — do mesmo jeito que a falha de detecção de rosto cai para
`center`. Um card é cosmético; o corte não é, e uma fila noturna não pode morrer
por causa de um mp4 fora do lugar.

Antes do card entra uma **passarela** (`tail_roll_s`, 3 s): o vídeo continua
rodando depois da última frase do corte, para a entrada do card não ser um corte
seco. O fim que o modelo escolheu não muda — a legenda para nele, e a fala que
começar dentro da passarela fica **sem legenda de propósito**: é ambiência, e
legendar meia frase do assunto seguinte leria como se o corte continuasse.

Congelar o último quadro em vez disso foi testado e descartado: lê como vídeo
travado. Esticar a seleção também não serve — num episódio medido, só 1,1% dos
fins de frase tinham 3 s de silêncio depois (mediana 0,18 s), então estender o
corte seria entrar no assunto seguinte.

O card não encurta nada — um corte de 30s vira um arquivo de 34s. Mas vale
pensar antes de ligar no short: o Shorts pesa porcentagem assistida, e quem vê
os 30s de conteúdo e sai marca ~88% em vez de 100%. No long é padrão.

### Pergunta de comentário

O `short` escreve na barra acima da imagem uma pergunta para o espectador
responder nos comentários — `"O que você não levaria na marmita?"` num corte
sobre marmita. Comentário é o sinal mais forte que existe nos dois algoritmos, e
a barra do 9:16 é espaço morto de qualquer jeito.

Quem escreve é o modelo, no estágio de seleção (`comment_prompt`), com uma regra
que o prompt cobra: **a resposta não pode estar no corte**. Ninguém comenta algo
que acabou de ouvir. Se o corte não sugerir nenhuma pergunta honesta, o campo
volta vazio e nada é desenhado — melhor sem do que com uma genérica.

A posição vem da **geometria real**, não de uma porcentagem fixa: `blur` e
`letterbox` encaixam a fonte inteira no quadro, então a barra é exatamente
`(altura − altura encaixada) / 2`, e o texto vai no meio dela. Com `center` ou
`face` a tela é toda imagem e a pergunta cairia no rosto de alguém — nesse caso
ela não é desenhada. Uma fonte já vertical também não deixa barra utilizável.

Ajustes em `profiles.short.banner`: `enabled`, `size_pct`, `max_chars_per_line`,
`min_bar_pct`.

### O começo e o fim do corte

Os dois extremos do corte são verificados por regras determinísticas, não pela
confiança no modelo.

No **fim**: o corte não pode terminar numa pergunta sem resposta nem numa frase
que não fecha, e o modelo precisa citar a última frase (`closing`) — se a frase
citada não estiver lá, o corte é descartado.

No **começo**: a primeira frase é o gancho, então ela precisa se sustentar
sozinha. São recusadas aberturas em reação ao que não se vê ("Pô, não sabia
disso aí, gente"), resposta a pergunta que ficou de fora ("Não, mas se eu sou um
cara da treta..."), continuação de lista ou raciocínio ("Segundo, o que já estão
falando..."), pronome sem referente ("Tem tudo isso.") e meio de narrativa
("Pegou o carro, aí você desmirilhou o carro") — todos exemplos reais de cortes
que este pipeline publicou antes da regra existir. O `hook` citado também é
conferido, como o `closing`.

Quando a abertura não presta, o pipeline **avança o início** para a próxima frase
que abre bem, desde que a duração continue dentro do perfil; se não houver
nenhuma por perto, o corte é recusado e o motivo volta para o modelo na segunda
tentativa. Medido numa transcrição real de 42 min: 64% das frases servem de
abertura e 95% dos pontos têm uma abertura válida a até duas frases de distância,
então o caso comum é avançar meia frase, não perder o corte.

Nos **longs** a marca de tema é abertura legítima ("Agora, falando de
importação...") — lá o corte começa onde o assunto começa.

### Quantidade não é meta

`n_clips` é um **teto**, não um alvo. O prompt diz isso explicitamente, e três
verificações descartam candidatos fracos antes que eles cheguem ao render:

- **`min_score`** (78 no `config.yaml`) — o modelo pontua os próprios cortes;
  abaixo disso ele está enchendo a lista para bater a cota.
- **`closing` conferido** — o modelo tem que citar a última frase do corte, e o
  código verifica que ela realmente está lá. Uma citação inventada significa que
  ele não olhou onde o corte termina.
- **Não termina em pergunta** sem a resposta.

O que sobrar é o que vale a pena publicar. Sair com 2 cortes bons é o
comportamento correto, não uma falha.

Não há limite no código: `n_clips` e as durações são do perfil. Para fontes longas
o `config.yaml` já traz dois perfis prontos, **fora do default** (você opta):

| perfil | cortes | duração |
|---|---|---|
| `short_podcast` | 12 | 20 s – 3 min |
| `podcast` | 4 | 5 – 20 min |

```bash
uv run clipper run "<url>" --profile short_podcast,podcast
```

Sobrescreva campos ou crie perfis novos em `config.yaml`. `extends` herda de outro:

```yaml
profiles:
  long_vertical:
    extends: long
    output_width: 1080
    output_height: 1920
    reframe: blur
```

## Desenvolvimento

```bash
uv run pytest                  # testes rápidos
uv run pytest -m slow          # + render real com ffmpeg
uv run mypy src                # precisa passar em --strict
uv run ruff check src tests && uv run ruff format src tests
```

Nenhum teste chama API de verdade: os providers de LLM são sempre mockados.

Se o `yt-dlp` quebrar por mudança do YouTube:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```
