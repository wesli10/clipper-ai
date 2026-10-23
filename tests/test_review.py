"""Interactive review edits - SPEC 7.1."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipper.models import (
    ChapterHint,
    Clip,
    ClipSet,
    ClipStatus,
    Segment,
    TimeRange,
    Transcript,
    Word,
)
from clipper.profiles import resolve_profile
from clipper.stages.review import auto_approve, drop_interval, edit_interval


def segment(idx: int, start: float, end: float, text: str) -> Segment:
    tokens = text.split()
    step = (end - start) / len(tokens)
    words = [
        Word(text=t, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
        for i, t in enumerate(tokens)
    ]
    return Segment(id=idx, start=start, end=end, text=text, words=words)


@pytest.fixture
def transcript() -> Transcript:
    return Transcript(
        video_id="vid",
        language="pt",
        duration=600.0,
        segments=[
            segment(i, i * 30.0, i * 30.0 + 28.0, f"assunto {i} explicado com calma aqui.")
            for i in range(20)
        ],
    )


def clip_set(n: int = 4) -> ClipSet:
    return ClipSet(
        video_id="vid",
        profile="short",
        clips=[
            Clip(
                index=i + 1,
                slug=f"c{i}",
                profile="short",
                segments=[TimeRange(start=i * 60.0, end=i * 60.0 + 40.0)],
                score=50 + i * 10,
            )
            for i in range(n)
        ],
    )


def test_auto_approves_the_n_best_and_rejects_the_rest():
    reviewed = auto_approve(clip_set(4), resolve_profile("short", {"short": {"n_clips": 2}}))
    approved = [c.index for c in reviewed.clips if c.status is ClipStatus.APPROVED]
    rejected = [c.index for c in reviewed.clips if c.status is ClipStatus.REJECTED]
    assert approved == [3, 4]  # highest scores
    assert rejected == [1, 2]


def test_auto_approves_everything_when_there_are_fewer_than_n():
    reviewed = auto_approve(clip_set(2), resolve_profile("short"))
    assert all(c.status is ClipStatus.APPROVED for c in reviewed.clips)


def test_editing_an_interval_re_snaps_the_typed_times(transcript):
    clip = clip_set(1).clips[0]
    edit_interval(clip, 1, "01:35", "02:50", transcript, resolve_profile("short"))
    interval = clip.segments[0]
    # 95 s falls inside the segment starting at 90 s; padding of 0.25 s is applied
    assert interval.start == pytest.approx(90.0 - 0.25)
    assert interval.start != 95.0


def test_editing_accepts_plain_seconds(transcript):
    clip = clip_set(1).clips[0]
    edit_interval(clip, 1, "90", "150", transcript, resolve_profile("short"))
    assert clip.segments[0].start == pytest.approx(89.75)


def test_editing_clears_stale_render_offsets(transcript):
    clip = clip_set(1).clips[0]
    clip.offsets = [0.0]
    edit_interval(clip, 1, "90", "150", transcript, resolve_profile("short"))
    assert clip.offsets == []


def test_editing_an_unknown_interval_is_rejected(transcript):
    clip = clip_set(1).clips[0]
    with pytest.raises(ValueError, match="intervalo 3"):
        edit_interval(clip, 3, "90", "150", transcript, resolve_profile("short"))


def test_editing_keeps_intervals_in_chronological_order(transcript):
    clip = Clip(
        index=1,
        slug="c",
        profile="long",
        segments=[TimeRange(start=300, end=400), TimeRange(start=500, end=560)],
    )
    edit_interval(clip, 2, "01:00", "02:00", transcript, resolve_profile("long"))
    assert clip.segments[0].start < clip.segments[1].start
    assert clip.segments[0].start < 300


def test_editing_fuses_intervals_that_end_up_adjacent(transcript):
    """SPEC 7.1: an edit re-runs step 4 of SPEC 6.1, merging included."""
    clip = Clip(
        index=1,
        slug="c",
        profile="long",
        segments=[TimeRange(start=0, end=100), TimeRange(start=300, end=400)],
        chapters=[ChapterHint(offset_hint="a"), ChapterHint(offset_hint="b")],
    )
    edit_interval(clip, 2, "10", "60", transcript, resolve_profile("long"))
    assert len(clip.segments) == 1
    assert len(clip.chapters) == 1


def test_dropping_an_interval_also_drops_its_chapter_hint():
    clip = Clip(
        index=1,
        slug="c",
        profile="long",
        segments=[TimeRange(start=0, end=100), TimeRange(start=300, end=400)],
        chapters=[ChapterHint(offset_hint="a"), ChapterHint(offset_hint="b")],
    )
    drop_interval(clip, 1)
    assert len(clip.segments) == 1
    assert [c.offset_hint for c in clip.chapters] == ["b"]


def test_cannot_drop_the_last_interval():
    clip = clip_set(1).clips[0]
    with pytest.raises(ValueError, match="pelo menos um intervalo"):
        drop_interval(clip, 1)


# --- preview -------------------------------------------------------------- #


def test_mpv_is_used_when_available(monkeypatch):
    from clipper.stages import review as review_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        review_mod.shutil, "which", lambda name: "/usr/bin/mpv" if name == "mpv" else None
    )
    monkeypatch.setattr(review_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    clip = clip_set(1).clips[0]
    review_mod.preview(Path("/tmp/source.mp4"), clip)

    assert calls[0][0] == "mpv"
    assert any(a.startswith("--start=") for a in calls[0])


def test_a_desktop_opener_is_the_fallback(monkeypatch, tmp_path):
    """WSL usually has neither mpv nor ffplay, but does have a Windows player."""
    from clipper.stages import review as review_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        review_mod.shutil,
        "which",
        lambda name: "/usr/bin/wslview" if name == "wslview" else None,
    )
    monkeypatch.setattr(review_mod, "extract_preview", lambda s, c, d: tmp_path / "preview.mp4")
    monkeypatch.setattr(review_mod, "open_with_desktop", lambda p, o: calls.append([o, str(p)]))

    review_mod.preview(tmp_path / "source.mp4", clip_set(1).clips[0])

    assert calls[0][0] == "wslview"
    assert calls[0][1].endswith("preview.mp4")


def test_without_any_player_it_warns_instead_of_failing(monkeypatch, tmp_path):
    from clipper.stages import review as review_mod

    monkeypatch.setattr(review_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(review_mod, "desktop_opener", lambda: None)
    ran: list[object] = []
    monkeypatch.setattr(review_mod.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    review_mod.preview(tmp_path / "source.mp4", clip_set(1).clips[0])
    assert ran == []


# --- non-interactive decisions --------------------------------------------- #


def test_approve_and_reject_by_index_without_a_prompt():
    from clipper.stages.review import apply_decisions

    reviewed = apply_decisions(clip_set(4), approve=[1, 3], reject=[2])
    status = {c.index: str(c.status) for c in reviewed.clips}
    assert status == {1: "approved", 2: "rejected", 3: "approved", 4: "pending"}


def test_an_unknown_index_is_rejected():
    from clipper.stages.review import apply_decisions

    with pytest.raises(ValueError, match="indice 9"):
        apply_decisions(clip_set(2), approve=[9], reject=[])


def test_review_takes_the_decisions_instead_of_prompting(transcript, monkeypatch):
    """Without a tty there is no prompt to take; the flags are the way in."""
    from clipper.profiles import resolve_profile
    from clipper.stages import review as review_mod

    def boom(*_args, **_kwargs):
        raise AssertionError("nao deveria perguntar")

    monkeypatch.setattr(review_mod.Prompt, "ask", boom)
    reviewed = review_mod.review(
        clip_set(3),
        resolve_profile("short"),
        transcript,
        Path("/tmp/source.mp4"),
        approve=[2],
    )
    assert [str(c.status) for c in reviewed.clips] == ["pending", "approved", "pending"]


def test_a_shell_without_a_tty_explains_itself(transcript, monkeypatch):
    from clipper.profiles import resolve_profile
    from clipper.stages import review as review_mod
    from clipper.utils.errors import InputError

    monkeypatch.setattr(review_mod.Prompt, "ask", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
    with pytest.raises(InputError, match="terminal interativo"):
        review_mod.review(clip_set(2), resolve_profile("short"), transcript, Path("/tmp/s.mp4"))


def test_a_wsl_path_is_converted_before_reaching_explorer(monkeypatch, tmp_path):
    """The Windows side cannot read a Linux path; explorer got one and did nothing."""
    from clipper.stages import review as review_mod

    calls: list[list[str]] = []

    class Converted:
        returncode = 0
        stdout = "\\\\\\\\wsl.localhost\\\\Ubuntu\\\\home\\\\u\\\\preview.mp4\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Converted() if cmd[0] == "wslpath" else None

    monkeypatch.setattr(review_mod.shutil, "which", lambda _n: "/usr/bin/wslpath")
    monkeypatch.setattr(review_mod.subprocess, "run", fake_run)

    review_mod.open_with_desktop(tmp_path / "preview.mp4", "/mnt/c/Windows/explorer.exe")

    assert calls[0][0] == "wslpath" and calls[0][1] == "-w"
    assert calls[1][0].endswith("explorer.exe")
    assert calls[1][1].startswith("\\\\")


def test_a_native_opener_gets_the_path_unchanged(monkeypatch, tmp_path):
    from clipper.stages import review as review_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(review_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    review_mod.open_with_desktop(tmp_path / "preview.mp4", "/usr/bin/xdg-open")
    assert calls[0] == ["/usr/bin/xdg-open", str(tmp_path / "preview.mp4")]
