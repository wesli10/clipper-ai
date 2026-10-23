from __future__ import annotations

import pytest

from clipper.utils.text import format_ass_time, format_timestamp, parse_timecode, slugify


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Como Ficar Rico", "como-ficar-rico"),
        ("Ação, ção & atenção!", "acao-cao-atencao"),
        ("   ", "clip"),
        ("---", "clip"),
        ("emoji 🚀 aqui", "emoji-aqui"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_truncates_on_word_boundary():
    slug = slugify("uma frase bastante longa que precisa ser cortada em algum ponto", max_length=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00"), (61.4, "01:01"), (3600, "01:00:00"), (3725.6, "01:02:06")],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_format_ass_time():
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(3661.235) == "1:01:01.24"
    assert format_ass_time(-5) == "0:00:00.00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12.5", 12.5), ("01:30", 90.0), ("1:02:03", 3723.0), (" 00:45.5 ", 45.5)],
)
def test_parse_timecode(raw, expected):
    assert parse_timecode(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1:2:3:4"])
def test_parse_timecode_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_timecode(raw)


# --------------------------------------------------------------------------- #
# Numbers a title promises (SPEC 1.2)
# --------------------------------------------------------------------------- #


def test_a_number_reads_the_same_in_digits_and_in_words():
    from clipper.utils.text import numbers_in

    assert numbers_in("10 mil reais") == numbers_in("dez mil reais") == {10, 1000}


def test_thousands_separators_and_accents_do_not_hide_a_number():
    from clipper.utils.text import numbers_in

    assert 1500 in numbers_in("custou 1.500 pila")
    assert numbers_in("tres anos") == numbers_in("três anos") == {3}


def test_a_title_promising_what_the_clip_never_says_is_caught():
    """The real one: the cut ends 0.1 s before anybody says ten thousand."""
    from clipper.utils.text import unbacked_numbers

    spoken = "eu fui fazer um negocio com o Renato. Dei mais de 4 anos. Quatro anos."

    assert unbacked_numbers("briguei por 10 mil num carro e passei 4 anos", spoken) == [10, 1000]


def test_a_title_backed_by_the_clip_passes():
    from clipper.utils.text import unbacked_numbers

    assert unbacked_numbers("passei 4 anos sem falar", "Dei mais de 4 anos.") == []
    # The clip says it in digits, the title in words: the same promise either way.
    assert unbacked_numbers("um carro em dez mil", "era 10 mil que eu pedi") == []


def test_a_title_with_no_numbers_promises_nothing_to_check():
    from clipper.utils.text import unbacked_numbers

    assert unbacked_numbers("relogio falso diz mais de voce", "qualquer coisa") == []
