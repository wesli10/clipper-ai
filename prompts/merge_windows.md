Você está consolidando propostas de corte feitas por janelas de um mesmo vídeo.

Um modelo com contexto limitado leu a transcrição em pedaços sobrepostos e propôs cortes
para cada pedaço. Alguns cortes aparecem duas vezes (a sobreposição entre janelas), alguns
são fracos, e — no perfil long — trechos de janelas diferentes podem pertencer ao mesmo tema
e deveriam virar um único corte multi-intervalo.

## Parâmetros
- Perfil: {profile}
- Cortes finais desejados: {n_clips}
- Duração total por corte: mínimo {duration_min}s, alvo {duration_target}s, máximo {duration_max}s
- Intervalos por corte: até {max_segments}

## Regras
1. Deduplicar: cortes cujos intervalos se sobrepõem em mais de 30% são o mesmo corte;
   fique com a versão de maior score e some o que a outra tiver de melhor no `reason`.
2. Reordenar por score decrescente e devolver no máximo {n_clips} cortes.
3. Não invente tempos novos: todo `start`/`end` deve vir de um dos cortes de entrada.
4. Se `{max_segments}` for maior que 1, junte num só corte os intervalos de janelas
   diferentes que tratam do mesmo tema, em ordem cronológica, respeitando a duração máxima
   e mantendo um `chapters[].offset_hint` por intervalo.
5. Se dois cortes tratam de temas diferentes, mantenha-os separados mesmo que próximos.

## Formato da resposta
Somente o JSON, no mesmo schema da entrada, sem texto em volta e sem cercas de markdown.

```json
{
  "clips": [
    {
      "segments": [{"start": 0.0, "end": 0.0}],
      "hook": "...",
      "reason": "...",
      "score": 0,
      "self_contained": true,
      "topic": "...",
      "chapters": []
    }
  ]
}
```

## Cortes propostos por janela

{candidates}
