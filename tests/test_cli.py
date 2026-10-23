from __future__ import annotations

import pytest
from typer.testing import CliRunner

from clipper.cli import app
from clipper.utils.errors import EXIT_INPUT

runner = CliRunner()

COMMANDS = [
    "run",
    "ingest",
    "transcribe",
    "select",
    "review",
    "render",
    "package",
    "deliver",
    "status",
    "profiles",
    "clean",
    "doctor",
]


def test_root_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_builds_its_parameters(command):
    """Guards against malformed typer.Option declarations."""
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


def test_profiles_prints_both_builtins():
    result = runner.invoke(app, ["profiles"])
    assert result.exit_code == 0
    assert "short" in result.output
    assert "long" in result.output


def test_profiles_accepts_a_single_name(tmp_path):
    result = runner.invoke(app, ["profiles", "short"])
    assert result.exit_code == 0
    assert "karao" in result.output


def test_unknown_profile_exits_with_input_code():
    result = runner.invoke(app, ["profiles", "fantasma"])
    assert result.exit_code == EXIT_INPUT


def test_status_without_videos(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "--config", str(config)])
    assert result.exit_code == 0
    assert "nenhum video" in result.output


def test_status_for_unknown_video_exits_with_input_code(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = runner.invoke(app, ["status", "xyz", "--config", str(config)])
    assert result.exit_code == EXIT_INPUT


def test_status_warns_when_the_source_is_smaller_than_the_long_profile(tmp_path):
    from clipper.config import load_settings
    from clipper.models import SourceInfo
    from clipper.state import State

    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    settings = load_settings(config)
    State(settings.state_db).upsert_video(
        SourceInfo(
            video_id="small",
            title="Fonte pequena",
            channel="C",
            duration=100.0,
            url="u",
            width=1280,
            height=720,
        )
    )
    result = runner.invoke(app, ["status", "--config", str(config)])
    assert result.exit_code == 0
    assert "nunca fazemos upscale" in result.output


def test_status_does_not_warn_for_a_full_hd_source(tmp_path):
    from clipper.config import load_settings
    from clipper.models import SourceInfo
    from clipper.state import State

    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    settings = load_settings(config)
    State(settings.state_db).upsert_video(
        SourceInfo(
            video_id="hd",
            title="Fonte HD",
            channel="C",
            duration=100.0,
            url="u",
            width=1920,
            height=1080,
        )
    )
    result = runner.invoke(app, ["status", "--config", str(config)])
    assert "upscale" not in result.output


# --- agent (SPEC-agent 13, fase A) ----------------------------------------- #


def test_agent_tools_lists_the_toolbox():
    result = runner.invoke(app, ["agent-tools"])
    assert result.exit_code == 0
    assert "find_candidates" in result.stdout and "publish_clips" in result.stdout


def test_agent_tools_shows_one_schema():
    result = runner.invoke(app, ["agent-tools", "snap_clip"])
    assert result.exit_code == 0
    assert "segments" in result.stdout


def test_agent_tool_runs_a_tool_and_prints_what_the_model_would_see(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = runner.invoke(
        app, ["agent-tool", "list_profiles", "--profile", "short", "-c", str(config)]
    )
    assert result.exit_code == 0
    assert '"name":"short"' in result.stdout.replace(" ", "")


def test_agent_tool_exits_nonzero_when_the_tool_answers_with_an_error(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = runner.invoke(
        app, ["agent-tool", "get_outline", '{"video_id": "naoexiste"}', "-c", str(config)]
    )
    assert result.exit_code == EXIT_INPUT


def test_agent_tool_refuses_arguments_that_are_not_json(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = runner.invoke(app, ["agent-tool", "list_profiles", "[1,2]", "-c", str(config)])
    assert result.exit_code == EXIT_INPUT
