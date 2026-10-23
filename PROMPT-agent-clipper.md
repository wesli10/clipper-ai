# Transformar o pipeline de cortes em um agent

## Contexto

O pipeline de cortes deste repositório está funcionando de ponta a ponta e você já
trabalhou nele. **O código atual é a fonte de verdade**, não `SPEC-clipper.md`, que
ficou para trás em relação à implementação. Continue de onde está: use o que você já
sabe do projeto e confirme percorrendo o código antes de propor qualquer coisa. Se
achar útil, inclua na spec nova uma seção curta com o que a `SPEC-clipper.md` não
reflete mais, para eu atualizar depois.

## Objetivo

Evoluir o pipeline linear para um **agent**: um loop em que um LLM recebe um objetivo,
decide quais ferramentas chamar, avalia os resultados e decide se refaz, ajusta ou
finaliza. O pipeline atual continua existindo e vira o conjunto de ferramentas do agent.

O que o agent deve passar a decidir, e o pipeline hoje não decide:

- quantos cortes o vídeo rende e em quais perfis (ex.: "3 shorts e 1 corte longo")
- se um trecho candidato ficou bom após o render (corte no meio de frase, rosto fora
  de quadro, legenda ilegível) e, se não, refazer com outros parâmetros
- estender ou encurtar um trecho por falta de contexto no início ou no fim
- título e tags coerentes com o estilo do canal

## Princípios de arquitetura

1. **O modelo nunca executa nada.** Ele só devolve intenções de chamada; o runtime
   executa e devolve o resultado. Toda validação de argumentos fica no runtime.
2. **Ferramentas estreitas e idempotentes.** Cada ferramenta faz uma coisa, valida sua
   entrada contra um schema e devolve um resultado resumido (nunca a transcrição inteira
   ou um JSON gigante; o modelo precisa do suficiente para decidir, não do dado bruto).
3. **O runtime controla o loop.** Limite de iterações, orçamento de tokens e de renders
   por vídeo, timeout por ferramenta. Erros de ferramenta voltam como `tool_result` com
   `is_error`, nunca derrubam a run.
4. **Efeitos colaterais só onde declarado.** Somente a ferramenta de publicação escreve
   no Drive. Todas as outras trabalham em diretório de trabalho local.
5. **Não reescrever o que funciona.** Refatore as etapas existentes em ferramentas
   preservando o comportamento de render. O pipeline atual deve continuar executável
   em um modo `--legacy` para comparação.
6. **Estado por canal.** Cada canal tem um perfil (durações preferidas, tom de título,
   regras de estilo, histórico do que funcionou) carregado no contexto a cada run.
   É a base para, no futuro, cada usuário ter "o seu" agent.

## Ferramentas

Derive as ferramentas das etapas que existem hoje no código, com os nomes e a
granularidade que fazem sentido para ele. A tabela abaixo mostra só o **formato**
esperado (entrada enxuta, saída resumida) e algumas capacidades que o pipeline
provavelmente ainda não expõe, como revisão de render e perfil de canal:

| Ferramenta | Entrada | Saída (resumida) |
|---|---|---|
| `transcrever` | video_id | resumo, duração, caminho da transcrição em cache |
| `listar_candidatos` | video_id, perfil, critérios | lista de trechos (início, fim, motivo, score) |
| `inspecionar_trecho` | início, fim | texto do trecho com margem, marcadores de fala |
| `renderizar` | trecho, perfil, parâmetros de enquadramento | clip_id, caminho, duração real |
| `revisar_render` | clip_id | checagens objetivas: corte em pausa?, rosto em quadro?, legenda sincronizada? |
| `publicar_no_drive` | clip_id, título, tags | link/caminho no Drive |
| `carregar_perfil_canal` / `atualizar_perfil_canal` | canal | perfil atual |

Cada ferramenta precisa de: schema JSON (nome, descrição orientada a *quando usar*,
parâmetros), validação de entrada, tratamento de erro e teste unitário.

## Entregáveis, nesta ordem

### 1. `SPEC-agent.md` (não implemente antes de eu aprovar)

Deve conter:

- mapeamento de cada etapa atual → ferramenta, com o que muda e o que é reaproveitado
- schemas das ferramentas
- desenho do runtime: loop, limites, log de cada chamada (JSONL com run_id, ferramenta,
  entrada, saída truncada, duração, erro)
- system prompt do agent: objetivo, critério de "pronto", regras de qualidade, perfis
- formato do perfil de canal e onde é persistido
- plano de evals (ver abaixo)
- lista de dúvidas e decisões em aberto que você precisa que eu responda

### 2. Implementação por fases, com testes

- Fase A: ferramentas + registro + validação, pipeline `--legacy` intacto
- Fase B: runtime do loop com limites e log
- Fase C: system prompt + perfil de canal
- Fase D: evals

Ao final de cada fase, pare e me mostre o resultado antes de seguir.

### 3. Evals

Use 3 a 5 vídeos que o pipeline atual já processou. Rode o agent nos mesmos vídeos e
compare: sobreposição dos trechos escolhidos, número de renders por corte final, custo em
tokens, tempo total, e uma nota manual minha de qualidade por corte. O objetivo da
primeira versão é **empatar em qualidade com menos intervenção manual**, não superar.

## Restrições

- whisper continua rodando local na GPU; não mover transcrição para API sem justificar
- não adicionar dependências sem explicar o motivo
- o modelo que roda o loop e o modelo que seleciona trechos podem ser diferentes;
  proponha e justifique
- segredos e credenciais nunca aparecem em log nem em resultado de ferramenta

## Antes de começar

Liste tudo que ficou ambíguo para você e me pergunte. Só depois escreva a spec.
