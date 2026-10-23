---
description: Responde a um select_request.md do Clipper, gravando select_response.json
argument-hint: <caminho da pasta do perfil, ex. workspace/<video_id>/profiles/short>
allowed-tools: Read, Write, Bash(ls:*)
---

O Clipper está usando o provider `file` (SPEC §6.5): ele gravou um prompt de seleção
de cortes e está esperando a resposta em um arquivo. Você é o modelo que vai responder.

Pasta do perfil: `$1`

Faça, nesta ordem:

1. Leia `$1/select_request.md`. Ele tem duas partes marcadas por comentários HTML:
   `<!-- system -->` (as instruções de sistema) e `<!-- prompt -->` (o prompt completo,
   incluindo a transcrição inteira com marcadores de tempo `[hh:mm:ss.s]`).
2. Siga o prompt como se fosse a sua instrução: ele já traz os critérios de score,
   a quantidade de cortes, as durações do perfil e o schema exato de resposta.
3. **Não invente timestamps.** Todo `start` e `end` deve ser copiado de um marcador
   que existe na transcrição — o pipeline reajusta as fronteiras depois, mas só
   funciona se os tempos vierem de segmentos reais.
4. Grave a resposta em `$1/select_response.json` com **apenas o objeto JSON**, sem
   cercas de markdown e sem texto em volta. O schema é o que o próprio prompt mostra.
5. Confirme para o usuário quantos cortes você propôs e o intervalo de cada um, para
   ele conferir antes da revisão.

Se `$1/select_request.md` não existir, avise o usuário para rodar antes:
`clipper select <video_id> --profile <perfil> --provider file`
