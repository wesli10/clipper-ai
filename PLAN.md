# Clipper — Plano de implementação

Fonte de verdade: [SPEC.md](SPEC.md). Este arquivo é a tradução da §12 da spec em
fases executáveis, com o critério de "pronto" de cada uma. `TASKS.md` acompanha o
progresso item a item.

## Ambiente verificado (2026-09-08)

| Item | Status |
|---|---|
| Python 3.12.3 | ok |
| `uv` 0.11.7 | ok |
| ffmpeg 7.0.2 static (`ass`, `xfade`, `acrossfade`, `zoompan`, `scdet`, `loudnorm`, libx264, aac) | ok |
| GPU NVIDIA RTX 4060 8 GB + CUDA | ok — risco de GPU AMD (§13) descartado |
| yt-dlp | ok |
| ollama 0.21.2 | ok (fase 12) |
| `ffprobe` | ok — instalado em `~/.local/bin` nesta sessão (build estático 6.0) |
| `rclone` | **ausente** — só necessário para `deliver.mode: rclone` |
| `GEMINI_API_KEY` | **ausente** — necessário só em execução real; testes usam mock |
| Fonte Montserrat | **ausente** — fallback `DejaVu Sans Bold` documentado |
| Pasta local do Google Drive | **ausente** — `deliver.drive_root` configurável |

Nenhuma ausência bloqueia o desenvolvimento. Cada uma vira um *preflight check*
com mensagem de erro acionável (`clipper doctor`).

## Decisões tomadas ao aterrissar a spec

1. **`ffprobe`** — a verificação de saída da §7.3 depende dele. O código chama
   `ffprobe`; se não estiver no PATH, `clipper doctor` explica como instalar
   (`apt install ffmpeg` ou baixar o build estático completo). Sem fallback
   silencioso: verificação de render não pode ser pulada em silêncio.
   Instalado nesta sessão em `~/.local/bin/ffprobe`.
2. **Ordem de fases** — segue a §12 literalmente. Fases 1–3 e 8 são puramente
   testáveis sem GPU/rede; 4–7, 9, 11 exigem ffmpeg; 2 exige GPU e rede.
3. **Commits** — um commit por fase, em inglês, com testes verdes antes
   (`pytest`, `mypy --strict`, `ruff`), conforme CLAUDE.md da §14.
4. **Mensagens ao usuário em português; código e commits em inglês** (§14).

## Fases

| # | Fase | Pronto quando |
|---|---|---|
| 1 | Esqueleto | `uv run clipper profiles` imprime os perfis efetivos; pytest/mypy/ruff verdes |
| 2 | Ingest + Transcribe | `transcript.json` de um vídeo real; VRAM liberada ao fim |
| 3 | Select short (Gemini) | `profiles/short/clips.json` gerado; testes de `snap_boundaries` (`sentence`) |
| 4 | Render short mínimo | mp4 1080×1920, `reframe=center`, áudio normalizado, sem legenda |
| 5 | Legendas karaokê | `.ass` queimado; testes do gerador |
| 6 | Reframe por rosto | `faces.json`, EMA α=0,2, zona morta 5%, fallback `center` |
| 7 | Package + Deliver (short) | `metadata.json`/`.md`, `INDEX.md`, modo `folder`, depois `rclone` |
| 8 | Select long | `boundary_mode: topic`, multi-segment com fusão/rejeição; testes |
| 9 | Render long | 1920×1080 sem upscale, `concat` com xfade/acrossfade, legenda `classic`, capítulos |
| 10 | Review interativo + `run` | edição de intervalos, idempotência, `status` |
| 11 | Zoom dinâmico | `punch_in` sobre quadro já recortado, amplitude ≤8% |
| 12 | Providers `ollama` e `file` | janelamento + merge; slash command `/select-clips` |
| 13 | Polimento | erros, README, `clean`, perfil custom via `extends` |

## Estado ao fim da primeira sessão

As 13 fases estão implementadas. `TASKS.md` lista o que foi validado com ffmpeg
real e o que ainda depende de você (chave da Gemini, modelo do whisper, pasta do
Drive, aceite manual da §11.2).

Três bugs foram encontrados pelos próprios testes e corrigidos:

1. `[Events] Format` do `.ass` omitia `MarginV`, então o libass caía no layout
   padrão de 10 campos e uma vírgula vazava para o texto — **toda** legenda
   queimada saía como `,FRASE ...`. Só aparecia num frame renderizado.
2. `split_windows` entrava em loop infinito quando uma linha da transcrição
   excedia o orçamento da janela (provider `ollama`).
3. `upload_date` nunca era gravado no `state.db`, então a pasta de entrega no
   Drive recebia a data de hoje em vez da data de publicação do vídeo.

### Nota de ambiente

A rede da WSL2 estava a 6–76 kB/s durante a sessão. Causa: `Large Send Offload`
ligado no adaptador `vEthernet (WSL*)` — o mesmo problema documentado nos
comentários do `.wslconfig` em 2026-08-26. Correção, em PowerShell **como
Administrador**:

```powershell
Disable-NetAdapterLso -Name "vEthernet (WSL*)"
```
