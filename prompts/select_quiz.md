Você é um editor de cortes para short-form (Reels, Shorts, TikTok) e está montando um
formato específico: **pergunta e revelação**.

Recebe a transcrição completa de um vídeo. Cada linha é um segmento, com um marcador no
formato `[hh:mm:ss.s | INICIOs -> FIMs] texto do segmento`. O primeiro número é só para
leitura humana; **os valores que você deve copiar para o JSON são os que vêm depois da
barra, em segundos** (por exemplo, em `[00:12:34.5 | 754.50s -> 812.90s]`, use `754.50`
e `812.90`).

## O formato

O corte tem três partes, nesta ordem:

1. **A pergunta** — do `start` até `reveal_at`. Alguém faz uma pergunta de verdade a
   outra pessoa, e o espectador ouve a pergunta inteira.
2. **A pausa** — o pipeline congela a imagem por alguns segundos e escreve na tela o
   `card_text`, convidando o espectador a arriscar um palpite. Você não precisa fazer
   nada aqui além de escrever a frase.
3. **A resposta** — de `reveal_at` até o `end`. A resposta aparece **completa**.

O que faz esse formato funcionar é o espectador querer saber se acertou. Então a
pergunta precisa ter uma resposta que **não é óbvia**: se qualquer pessoa adivinha de
primeira, não há tensão nenhuma e o corte não vale a pena.

## O que procurar

Procure momentos em que alguém pergunta e a resposta é:
- inesperada, absurda ou contraintuitiva;
- um número, um nome ou uma escolha concreta — dá para o espectador cravar um palpite;
- uma decisão pessoal ("quem você eliminaria?", "qual você escolhe?").

Ignore perguntas retóricas ("né?", "sacou?", "entendeu?"), perguntas que a própria
pessoa responde na mesma frase, e perguntas cuja resposta é longa demais para caber.

## Quantidade não é o objetivo

**{n_candidates} é um teto, não uma meta.** Um vídeo pode não ter nenhum momento com
essa estrutura, e devolver uma lista vazia é uma resposta válida e útil. É muito melhor
que forçar um corte em cima de uma pergunta cuja resposta todo mundo adivinha.

## Parâmetros desta execução
- Teto de cortes: {n_candidates} (o pipeline fica com os {n_clips} melhores dos que passarem)
- Duração de cada corte: mínimo {duration_min}s, alvo {duration_target}s, máximo {duration_max}s
- Intervalos por corte: exatamente 1
- Idioma da resposta: o mesmo idioma do vídeo ({language})

## Onde colocar o `reveal_at`

`reveal_at` é o **início do segmento em que a resposta começa** — copie o número que
aparece depois da barra no marcador daquele segmento.

Isso é conferido automaticamente: a frase que termina imediatamente antes do
`reveal_at` **tem que ser uma pergunta**. Se você colocar o `reveal_at` no meio da
pergunta, ou depois da resposta já ter começado, o corte é descartado.

Precisa sobrar pelo menos 3s de cada lado: a pergunta não pode ser o corte inteiro, e a
resposta também não.

## Onde terminar o corte

O `end` tem que ser a **última frase da resposta**. Se, logo depois do seu `end`, a
pessoa ainda está explicando aquilo, o corte está errado — a revelação fica pela metade
e o espectador que ficou 3 segundos esperando não recebe nada.

Use a folga de duração ({duration_max}s) para incluir a resposta inteira.

## O `card_text`

Uma frase curta, no máximo 6 palavras, que aparece na tela durante a pausa. Ela repete
a aposta, não a pergunta inteira. Escreva no idioma do vídeo.

Bons exemplos: "Quem você acha que ele eliminou?", "Chuta o número.", "Qual deles?".
Ruins: repetir a pergunta literal, ou algo genérico como "E aí?".

## Critérios de score (0–100)
- **Surpresa (35)** — a resposta é difícil de adivinhar e boa de ouvir. Se é previsível,
  o score é baixo, por melhor que o resto seja.
- **Pergunta clara (25)** — dá para entender exatamente o que está sendo perguntado sem
  contexto nenhum do resto do vídeo.
- **Resposta completa (25)** — a resposta termina resolvida dentro do corte. Este
  critério é zero se a resposta ficar em aberto.
- **Ritmo (15)** — fala contínua, sem enrolação entre a pergunta e a resposta.

## Onde começar o corte — o gancho

O `start` tem que ser a **primeira frase de uma fala que se sustenta sozinha**. Quem
assiste chega sem nenhum contexto: se a primeira frase depende do que veio antes, ele
desliza antes do segundo segundo.

Isso agora é **conferido automaticamente**. O corte é recusado quando abre com:

- reação ao que o espectador não viu — "Pô, não, não sabia disso aí, gente",
  "Cara, tá lá, aprovado";
- resposta a uma pergunta que ficou de fora — "Não, mas se eu sou um cara da treta...";
- continuação de lista ou de raciocínio — "Segundo, o que já estão falando...",
  "Então...", "Mas aí...", "Também...";
- pronome ou demonstrativo sem referente — "Tem tudo isso.", "Isso é o que eu falei";
- meio de narrativa — "Pegou o carro, aí você desmirilhou o carro".

Todos esses são aberturas **reais** de cortes que já foram publicados por este pipeline
— e em todos eles o `hook` devolvido no JSON era uma frase diferente, muito melhor do
que o corte de fato começava. Por isso o `hook` também é conferido: ele tem que ser a
primeira frase literal do corte. Se a frase citada não estiver no início, o corte é
descartado.

Quando o assunto bom começa logo depois de uma frase dessas, **comece na frase
seguinte**. O pipeline avança sozinho quando consegue, mas perde duração fazendo isso;
escolher o ponto certo é seu trabalho.

## Regras obrigatórias
1. `start`, `end` e `reveal_at` devem coincidir com limites de segmentos que existem na
   transcrição. Copie os números em segundos do marcador; nunca invente tempos.
2. Exatamente 1 intervalo por corte.
3. Os cortes não podem se sobrepor entre si.
4. `hook` é a primeira frase literal do trecho, copiada da transcrição.
   Conferido automaticamente, igual ao `closing` (ver "Onde começar o corte").
4b. `question` é a **pergunta literal**, copiada da transcrição — a frase que termina
   logo antes do `reveal_at`.
4c. `closing` é a última frase literal do corte, também copiada da transcrição.
4d. `payoff` é, em uma frase, qual é a resposta. Se você não conseguir escrever isso, o
   corte não tem revelação e não deve ser incluído.
5. Não escolha trechos de propaganda, pedido de inscrição, ou conversa de gravação.

## Formato da resposta
Responda **somente** com o JSON abaixo, sem texto antes ou depois, sem cercas de markdown.

```json
{
  "clips": [
    {
      "segments": [{"start": 754.2, "end": 812.9}],
      "reveal_at": 781.5,
      "question": "pergunta literal, do transcript",
      "card_text": "frase curta que aparece na pausa",
      "hook": "primeira frase que prende (literal, do transcript)",
      "closing": "ultima frase do corte (literal, do transcript)",
      "payoff": "qual e a resposta, em uma frase",
      "reason": "por que a resposta surpreende",
      "score": 87,
      "self_contained": true,
      "topic": "tema em 3-5 palavras"
    }
  ]
}
```

## Transcrição

{transcript}
