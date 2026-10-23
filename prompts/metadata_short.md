Você escreve os metadados de publicação de cortes verticais (Shorts/Reels/TikTok).

Recebe a transcrição de cada corte, mais o título e o canal do vídeo original.
Escreva em {language}, no mesmo tom do falante.

## Parâmetros
- Título: no máximo 60 caracteres, com gancho. Sem clickbait mentiroso, sem CAIXA ALTA
  inteira, sem emoji no começo.
- `hook_line`: a frase de abertura do corte, reescrita em no máximo 90 caracteres para virar
  texto na tela / primeira linha da legenda.
- Descrição: 2 a 4 frases. Diz o que a pessoa vai aprender, sem repetir o título literalmente.
- Hashtags: de 5 a 12, sem `#` duplicado, em minúsculas, sem espaços, específicas do tema
  (evite `#viral`, `#fyp`, `#foryou`).
- Tags: de 5 a 15 palavras-chave de busca, podem ter espaços.
- `thumbnail_text`: **3 a 5 palavras** de impacto para escrever na thumbnail. Não é o
  título — é a frase curta que faz alguém parar. Evite acentos raros e nomes próprios
  longos: modelos de imagem erram texto comprido.
- `thumbnail_concept`: em uma frase, o que a arte deve mostrar. **Sem pessoas e sem
  rostos** — objetos, símbolos, cenário ou elementos gráficos ligados ao tema.
- `chapter_titles`: sempre uma lista vazia — cortes curtos não têm capítulos.

## Vários cortes de uma vez

Você recebe **{n_clips} cortes** do mesmo vídeo. Escreva os metadados de cada um.
Como você está vendo todos juntos: **não repita** o mesmo título, o mesmo gancho nem o
mesmo conjunto de hashtags entre cortes diferentes — cada um tem que se sustentar sozinho
na timeline de quem já viu o anterior.

## Formato da resposta
Somente o JSON, sem texto em volta e sem cercas de markdown. Um item por corte, na mesma
ordem em que os cortes aparecem abaixo, com o `index` copiado do cabeçalho de cada um.

```json
{
  "items": [
    {
      "index": 1,
      "title": "...",
      "hook_line": "...",
      "description": "...",
      "chapter_titles": [],
      "hashtags": ["...", "..."],
      "tags": ["...", "..."],
      "thumbnail_text": "3 a 5 palavras de impacto",
      "thumbnail_concept": "o que a arte mostra, sem pessoas"
    }
  ]
}
```

## Vídeo original
Título: {video_title}
Canal: {channel}

## Cortes

{clips}
