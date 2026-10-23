"""Delivery and INDEX.md - SPEC 8."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipper.config import load_settings
from clipper.models import Chapter, ClipMetadata, ClipSet, SourceInfo
from clipper.stages.deliver import archive_dir, build_index, deliver_folder, video_folder_name
from clipper.utils.errors import InputError


@pytest.fixture
def source():
    return SourceInfo(
        video_id="vid",
        title="Como Ficar Rico: o método",
        channel="Canal do Zé",
        duration=3600.0,
        url="https://www.youtube.com/watch?v=vid",
        upload_date="20260115",
    )


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        f"workspace: {tmp_path / 'ws'}\ndeliver:\n  drive_root: {tmp_path / 'drive'}\n",
        encoding="utf-8",
    )
    return load_settings(config)


def metadata(profile: str, title: str, chapters=()) -> ClipMetadata:
    return ClipMetadata(
        profile=profile,
        title=title,
        hook_line="h",
        description="d",
        chapters=list(chapters),
        hashtags=["#a", "#b"],
        tags=["x"],
        source_url="https://www.youtube.com/watch?v=vid&t=754s",
        source_segments=[(754.0, 809.0)],
        language="pt",
    )


# --- paths ----------------------------------------------------------------- #


def test_folder_name_uses_the_upload_date_and_a_slug(source):
    assert video_folder_name(source) == "2026-01-15_como-ficar-rico-o-metodo"


def test_folder_name_falls_back_to_today_without_an_upload_date(source):
    name = video_folder_name(source.model_copy(update={"upload_date": None}))
    assert name.endswith("_como-ficar-rico-o-metodo")
    assert len(name.split("_")[0]) == 10


def test_archive_dir_nests_root_archive_channel_and_video(settings, source):
    parts = archive_dir(settings, source).parts
    assert parts[0] == "Clipper"
    assert parts[1] == "arquivo"
    assert parts[2] == "canal-do-ze"
    assert parts[3].startswith("2026-01-15_")


# --- folder mode ----------------------------------------------------------- #


def test_folder_mode_copies_clips_under_the_profile_folders(settings, source, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    clip_dir = tmp_path / "01_foco"
    (clip_dir / "intermediates").mkdir(parents=True)
    (clip_dir / "clip.mp4").write_bytes(b"x")
    (clip_dir / "metadata.md").write_text("# t", encoding="utf-8")
    (clip_dir / "thumbnail.jpg").write_bytes(b"j")
    (clip_dir / "intermediates" / "01.mp4").write_bytes(b"y")

    target = deliver_folder(
        [("shorts", clip_dir)], drive, archive_dir(settings, source), index="# INDEX"
    )
    delivered = target / "shorts" / "01_foco"
    assert (delivered / "metadata.md").exists()
    assert (delivered / "thumbnail.jpg").exists()
    assert not (delivered / "intermediates").exists()
    assert (target / "INDEX.md").read_text(encoding="utf-8") == "# INDEX"


def test_no_video_reaches_the_archive(settings, source, tmp_path):
    """The video lives only in the queue (SPEC 8.1); the archive is the history."""
    drive = tmp_path / "drive"
    drive.mkdir()
    clip_dir = tmp_path / "01_foco"
    clip_dir.mkdir()
    (clip_dir / "clip.mp4").write_bytes(b"x")
    (clip_dir / "O melhor corte.mp4").write_bytes(b"x")
    (clip_dir / "metadata.json").write_text("{}", encoding="utf-8")

    target = deliver_folder([("shorts", clip_dir)], drive, archive_dir(settings, source))

    assert list((target / "shorts" / "01_foco").glob("*.mp4")) == []
    assert (target / "shorts" / "01_foco" / "metadata.json").exists()


def test_folder_mode_is_idempotent(settings, source, tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    clip_dir = tmp_path / "01_foco"
    clip_dir.mkdir()
    (clip_dir / "metadata.json").write_text("{}", encoding="utf-8")
    relative = archive_dir(settings, source)
    deliver_folder([("shorts", clip_dir)], drive, relative)
    target = deliver_folder([("shorts", clip_dir)], drive, relative)
    assert (target / "shorts" / "01_foco" / "metadata.json").exists()


def test_missing_drive_folder_is_an_input_error(settings, source, tmp_path):
    with pytest.raises(InputError, match="Google Drive"):
        deliver_folder([("shorts", tmp_path)], tmp_path / "absent", archive_dir(settings, source))


# --- INDEX.md -------------------------------------------------------------- #


def test_index_has_one_section_per_profile(source):
    empty = ClipSet(video_id="vid", profile="short")
    index = build_index(
        source,
        {
            "short": [(empty, metadata("short", "Corte curto"))],
            "long": [
                (
                    empty,
                    metadata(
                        "long",
                        "Corte longo",
                        [Chapter(offset=0, title="A"), Chapter(offset=120, title="B")],
                    ),
                )
            ],
        },
    )
    assert "## short" in index
    assert "## long" in index
    assert "### Corte curto" in index
    assert "### Corte longo" in index
    assert "#a #b" in index
    assert "00:00 A; 02:00 B" in index
    assert "12:34" in index  # source range


def test_index_marks_a_profile_with_nothing_delivered(source):
    index = build_index(source, {"short": [], "long": []})
    assert index.count("_nenhum corte entregue_") == 2


# --- rclone mode ----------------------------------------------------------- #


def test_rclone_without_the_binary_says_how_to_fix(monkeypatch, tmp_path):
    from clipper.stages import deliver as deliver_mod

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: None)
    with pytest.raises(Exception, match="rclone nao encontrado"):
        deliver_mod.deliver_rclone([("shorts", tmp_path)], "gdrive:", Path("Clipper/x"))


def test_rclone_without_any_remote_says_how_to_create_one(monkeypatch, tmp_path):
    from clipper.stages import deliver as deliver_mod

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", list)
    with pytest.raises(Exception, match="rclone config create gdrive"):
        deliver_mod.deliver_rclone([("shorts", tmp_path)], "gdrive:", Path("Clipper/x"))


def test_rclone_with_the_wrong_remote_lists_the_available_ones(monkeypatch, tmp_path):
    from clipper.stages import deliver as deliver_mod

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", lambda: ["dropbox", "onedrive"])
    with pytest.raises(Exception, match="dropbox, onedrive"):
        deliver_mod.deliver_rclone([("shorts", tmp_path)], "gdrive:", Path("Clipper/x"))


def test_rclone_builds_the_destination_path(monkeypatch, tmp_path, source, settings):
    """Each profile goes once into <remote>:<Clipper/arquivo/canal/video>/<pasta>."""
    from clipper.stages import deliver as deliver_mod

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", lambda: ["gdrive"])
    monkeypatch.setattr(
        deliver_mod.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Result())[1]
    )

    clip_dir = tmp_path / "clips" / "01_foco"
    clip_dir.mkdir(parents=True)
    base = deliver_mod.deliver_rclone(
        [("shorts", clip_dir)], "gdrive:", archive_dir(settings, source), index="# INDEX"
    )

    assert base.startswith("gdrive:Clipper/arquivo/canal-do-ze/2026-01-15_")
    copy_call = calls[0]
    assert copy_call[:2] == ["rclone", "copy"]
    assert copy_call[-1].endswith("/shorts")
    assert "- intermediates/**" in copy_call
    assert calls[-1][-1] == base  # the INDEX.md upload targets the video folder


def test_one_rclone_call_per_profile(monkeypatch, tmp_path, source, settings):
    """Drive keys folders by id: concurrent calls each created their own
    `shorts/` in the same parent and split the clips between them."""
    from clipper.stages import deliver as deliver_mod

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", lambda: ["gdrive"])
    monkeypatch.setattr(
        deliver_mod.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Result())[1]
    )

    clips = []
    for i in range(6):
        d = tmp_path / "clips" / f"0{i}_corte"
        d.mkdir(parents=True)
        (d / "clip.mp4").write_bytes(b"x")
        clips.append(("shorts" if i < 4 else "longs", d))

    deliver_mod.deliver_rclone(clips, "gdrive:", archive_dir(settings, source), workers=3)

    assert len(calls) == 2, "deve ser uma chamada por perfil, nao uma por corte"
    shorts = next(c for c in calls if c[-1].endswith("/shorts"))
    assert sum(1 for rule in shorts if rule.startswith("+ /")) == 4
    assert shorts[shorts.index("--transfers") + 1] == "3"


def test_only_the_named_clips_are_uploaded(monkeypatch, tmp_path, source, settings):
    """A rejected clip that was rendered earlier must not be shipped."""
    from clipper.stages import deliver as deliver_mod

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", lambda: ["gdrive"])
    monkeypatch.setattr(
        deliver_mod.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Result())[1]
    )

    root = tmp_path / "clips"
    for name in ("01_bom", "02_rejeitado"):
        (root / name).mkdir(parents=True)
    deliver_mod.deliver_rclone(
        [("shorts", root / "01_bom")], "gdrive:", archive_dir(settings, source)
    )

    assert [a for a in calls[0] if a.startswith("+ /")] == ["+ /01_bom/**"]


def _rclone_calls(monkeypatch, deliver_mod):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    monkeypatch.setattr(deliver_mod, "rclone_remotes", lambda: ["gdrive"])
    monkeypatch.setattr(
        deliver_mod.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Result())[1]
    )
    return calls


def _rules(command: list[str]) -> list[str]:
    return [command[i + 1] for i, arg in enumerate(command) if arg == "--filter"]


def test_no_video_is_uploaded_to_the_archive(monkeypatch, tmp_path, source, settings):
    """One ordered --filter list, never --include mixed with --exclude.

    Given both, rclone parses them in an indeterminate order (it warns about it)
    and the exclusion loses - which is how every clip once went up twice, as
    clip.mp4 and again under its title. The video now travels only to the queue,
    so the archive excludes every mp4, before any include.
    """
    from clipper.stages import deliver as deliver_mod

    calls = _rclone_calls(monkeypatch, deliver_mod)
    clip_dir = tmp_path / "clips" / "01_foco"
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip.mp4").write_bytes(b"x")

    deliver_mod.deliver_rclone([("shorts", clip_dir)], "gdrive:", archive_dir(settings, source))

    command = calls[0]
    assert "--include" not in command and "--exclude" not in command
    rules = _rules(command)
    assert rules.index("- *.mp4") < rules.index("+ /01_foco/**")
    assert rules[-1] == "- *", "o resto da pasta nao pode subir junto"


def test_index_only_delivery_skips_the_remote_check(monkeypatch, settings, source):
    """Writing just the index must not require a configured remote."""
    from clipper.stages import deliver as deliver_mod

    monkeypatch.setattr(deliver_mod.shutil, "which", lambda _: "/usr/bin/rclone")
    base = deliver_mod.deliver_rclone([], "gdrive:", archive_dir(settings, source))
    assert base.startswith("gdrive:Clipper/")


# --------------------------------------------------------------------------- #
# The delivered filename (YouTube pre-fills the title from it)
# --------------------------------------------------------------------------- #


def test_the_clip_is_named_after_its_title():
    from clipper.stages.deliver import delivered_name

    assert (
        delivered_name("Qual é a comida proibida na marmita?")
        == "Qual é a comida proibida na marmita.mp4"
    )


def test_characters_no_filesystem_accepts_are_dropped():
    from clipper.stages.deliver import delivered_name

    assert delivered_name('A/B: teste "com" | tudo*') == "AB teste com tudo.mp4"


def test_a_long_title_is_cut_on_a_word():
    from clipper.stages.deliver import delivered_name

    name = delivered_name("palavra " * 30)
    assert len(name) <= 94
    assert name.endswith("palavra.mp4")


def test_a_title_with_nothing_usable_keeps_the_render_name():
    from clipper.stages.deliver import delivered_name

    assert delivered_name("...") == "clip.mp4"
    assert delivered_name("") == "clip.mp4"
