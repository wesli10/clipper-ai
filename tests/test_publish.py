"""The publishing plan - SPEC 7.2 metadata, arranged for an upload session."""

from __future__ import annotations

from datetime import date

import pytest

from clipper.models import Chapter, ClipMetadata
from clipper.stages.publish import (
    PublishItem,
    collect,
    publish_order,
    render_plan,
    thumbnail_prompt,
)
from clipper.utils.errors import InputError


def metadata(profile: str, title: str, chapters=()) -> ClipMetadata:
    return ClipMetadata(
        profile=profile,
        title=title,
        hook_line="gancho",
        description="Uma descricao do corte.",
        chapters=list(chapters),
        hashtags=["#um", "#dois"],
        tags=["tag a", "tag b"],
        source_url="https://www.youtube.com/watch?v=abc&t=100s",
        source_segments=[(100.0, 160.0)],
        language="pt",
    )


def delivered_tree(root, entries) -> None:
    """Build the <canal>/<video>/<perfil>/<corte>/metadata.json layout."""
    for channel, video, profile, clip, meta in entries:
        directory = root / channel / video / profile / clip
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(meta.model_dump_json(), encoding="utf-8")


def test_collect_reads_the_delivered_layout(tmp_path):
    delivered_tree(
        tmp_path,
        [
            ("canal", "2026-01-01_video", "shorts", "01_um", metadata("short", "Um")),
            ("canal", "2026-01-01_video", "longs", "01_dois", metadata("long", "Dois")),
        ],
    )
    items = collect(tmp_path)
    assert {i.metadata.title for i in items} == {"Um", "Dois"}
    assert {i.profile_folder for i in items} == {"shorts", "longs"}


def test_collect_can_filter_by_channel(tmp_path):
    delivered_tree(
        tmp_path,
        [
            ("canal-a", "2026-01-01_v", "shorts", "01_a", metadata("short", "A")),
            ("canal-b", "2026-01-01_v", "shorts", "01_b", metadata("short", "B")),
        ],
    )
    assert [i.metadata.title for i in collect(tmp_path, channel="canal-a")] == ["A"]


def test_collect_skips_unreadable_metadata(tmp_path):
    delivered_tree(tmp_path, [("c", "2026-01-01_v", "shorts", "01_a", metadata("short", "A"))])
    broken = tmp_path / "c" / "2026-01-01_v" / "shorts" / "02_b"
    broken.mkdir(parents=True)
    (broken / "metadata.json").write_text("nao e json", encoding="utf-8")

    assert [i.metadata.title for i in collect(tmp_path)] == ["A"]


def item(profile_folder: str, clip: str, title: str) -> PublishItem:
    return PublishItem("2026-01-01_v", profile_folder, clip, metadata("short", title))


def _on(base: PublishItem, when: date) -> PublishItem:
    """The same item, dated - which the queue does, not the plan."""
    return PublishItem(
        base.video_folder,
        base.profile_folder,
        base.clip_folder,
        base.metadata,
        channel=base.channel,
        when=when,
        filename=base.filename,
    )


def test_clips_are_ordered_by_their_number():
    """Clip directories are numbered by score, so name order is score order."""
    items = [item("shorts", "03_c", "terceiro"), item("shorts", "01_c", "primeiro")]
    assert [i.metadata.title for i in publish_order(items)] == ["primeiro", "terceiro"]


def test_shorts_come_before_longs_of_the_same_video():
    items = [item("longs", "01_c", "L1"), item("shorts", "01_c", "S1")]
    assert [i.metadata.title for i in publish_order(items)] == ["S1", "L1"]


def test_the_plan_carries_every_field_to_paste():
    dated = [_on(item("shorts", "01_c", "Meu titulo"), date(2026, 1, 1))]
    plan = render_plan(dated, channel="canal")

    assert "Meu titulo" in plan
    assert "Uma descricao do corte." in plan
    assert "#um #dois" in plan
    assert "tag a, tag b" in plan
    assert "shorts/01_c/clip.mp4" in plan
    assert "2026-01-01 (qui)" in plan
    assert "## [ ] 1." in plan  # a checkbox per clip


def test_long_chapters_appear_in_the_description():
    long_item = PublishItem(
        "2026-01-01_v",
        "longs",
        "01_c",
        metadata(
            "long",
            "Um long",
            [Chapter(offset=0.0, title="Abertura"), Chapter(offset=95.0, title="Fecho")],
        ),
    )
    plan = render_plan([_on(long_item, date(2026, 1, 1))])
    assert "00:00 Abertura" in plan
    assert "01:35 Fecho" in plan


def test_an_empty_queue_is_an_actionable_error():
    with pytest.raises(InputError, match="nenhum corte na fila"):
        render_plan([])


def test_the_source_link_is_kept():
    plan = render_plan([_on(item("shorts", "01_c", "T"), date(2026, 1, 1))])
    assert "https://www.youtube.com/watch?v=abc&t=100s" in plan


def _item(profile_folder: str, clip: str, **fields) -> PublishItem:
    meta = metadata("long", fields.pop("title", "Titulo")).model_copy(update=fields)
    return PublishItem("2026-01-01_video", profile_folder, clip, meta)


def test_thumbnail_prompt_uses_the_short_phrase_not_the_title() -> None:
    item = _item(
        "shorts",
        "01-corte",
        title="O erro que derrubou a rodada inteira no ultimo segundo",
        thumbnail_text="ERRO NO ULTIMO SEGUNDO",
        thumbnail_concept="um cronometro quebrado sobre um tabuleiro",
    )
    prompt = thumbnail_prompt(item)
    assert '"ERRO NO ULTIMO SEGUNDO"' in prompt
    assert "cronometro quebrado" in prompt
    assert "Sem pessoas" in prompt


def test_thumbnail_prompt_falls_back_to_the_title_when_the_fields_are_missing() -> None:
    item = _item("longs", "01-corte", title="Um titulo qualquer")
    assert "Um titulo qualquer" in thumbnail_prompt(item)


def test_plan_puts_the_thumbnail_prompt_on_longs_only() -> None:
    items = [
        _item("shorts", "01-a", title="Curto", thumbnail_text="CURTO"),
        _item("longs", "01-b", title="Longo", thumbnail_text="LONGO"),
    ]
    plan = render_plan(items)
    assert plan.count("Thumbnail** (cole no Gemini)") == 1
    assert "LONGO" in plan
    assert "CURTO" not in plan


def test_plan_can_leave_the_thumbnail_prompt_out() -> None:
    items = [_item("longs", "01-b", title="Longo", thumbnail_text="LONGO")]
    assert "Thumbnail" not in render_plan(items, thumbnails=False)
