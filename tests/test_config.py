from __future__ import annotations

from pathlib import Path

import pytest

from clipper.config import ConfigError, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repo_config_yaml_loads_and_matches_spec():
    settings = load_settings(PROJECT_ROOT / "config.yaml")
    assert settings.default_profiles == ["short", "long"]
    assert settings.llm.provider == "gemini"
    assert settings.llm.gemini_model == "gemini-flash-latest"
    # Measured 3.4x faster than large-v3 on the same audio, with the structure
    # the pipeline depends on unchanged; large-v3 is the fallback.
    assert settings.transcribe.model == "large-v3-turbo"
    assert settings.transcribe.fallback_model == "large-v3"
    assert settings.render.crf == 21
    # intermediates stay high: the final pass re-encodes them
    assert settings.render.intermediate_crf < settings.render.crf
    assert settings.deliver.mode == "rclone"
    assert settings.deliver.rclone_remote == "gdrive:"
    assert settings.deliver.folder_for("short") == "shorts"
    assert settings.deliver.folder_for("long") == "longs"

    short = settings.profile("short")
    assert (short.output_width, short.output_height) == (1080, 1920)
    long_p = settings.profile("long")
    assert (long_p.output_width, long_p.output_height) == (1920, 1080)


def test_missing_config_file_uses_defaults(tmp_path):
    settings = load_settings(None)
    assert settings.default_profiles


def test_explicit_missing_path_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="nao encontrado"):
        load_settings(tmp_path / "absent.yaml")


def test_custom_profile_via_extends(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
profiles:
  default: [long_vertical]
  long_vertical:
    extends: long
    output_width: 1080
    output_height: 1920
    reframe: blur
""",
        encoding="utf-8",
    )
    settings = load_settings(path)
    profile = settings.profile("long_vertical")
    assert profile.reframe == "blur"
    assert profile.boundary_mode == "topic"


def test_bad_default_profile_fails_fast(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("profiles:\n  default: [ghost]\n", encoding="utf-8")
    with pytest.raises(Exception, match="ghost"):
        load_settings(path)


def test_workspace_paths(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.video_dir("abc").name == "abc"
    assert settings.profile_dir("abc", "short").parts[-3:] == ("abc", "profiles", "short")
    assert settings.state_db.name == "state.db"


def test_podcast_profiles_allow_long_single_subject_clips():
    """A 15-minute clip is rejected by `long` (8 min cap) but fits `podcast`."""
    settings = load_settings(PROJECT_ROOT / "config.yaml")

    long_profile = settings.profile("long")
    assert long_profile.duration_max == 480.0

    podcast = settings.profile("podcast")
    assert podcast.duration_min <= 900 <= podcast.duration_max
    assert podcast.n_clips > long_profile.n_clips
    # inherited from `long`
    assert podcast.boundary_mode == "topic"
    assert (podcast.output_width, podcast.output_height) == (1920, 1080)


def test_short_podcast_raises_the_count_and_the_cap():
    settings = load_settings(PROJECT_ROOT / "config.yaml")
    short = settings.profile("short")
    podcast_short = settings.profile("short_podcast")

    assert podcast_short.n_clips > short.n_clips
    assert podcast_short.duration_max > short.duration_max
    assert podcast_short.subtitle_style == "karaoke"


def test_the_podcast_profiles_are_opt_in():
    settings = load_settings(PROJECT_ROOT / "config.yaml")
    assert settings.default_profiles == ["short", "long"]
    assert "podcast" in settings.profile_names()
