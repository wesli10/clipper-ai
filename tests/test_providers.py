"""Ollama windowing and the `file` provider - SPEC 6.4 / 6.5."""

from __future__ import annotations

import json
import threading
import time

import pytest

from clipper.llm import build_provider
from clipper.llm.file import FileProvider, WaitingForFileError
from clipper.llm.ollama import merge_prompt, split_windows
from clipper.models import SelectionResponse
from clipper.utils.errors import ExternalError, InputError


def transcript_lines(n: int, width: int = 80) -> str:
    return "\n".join(f"[00:00:{i:02d}.0] " + "x" * width for i in range(n))


# --- windowing ------------------------------------------------------------- #


def test_a_short_transcript_is_a_single_window():
    windows = split_windows(transcript_lines(5), window_tokens=6000, overlap=0.10)
    assert len(windows) == 1


def test_a_long_transcript_is_split():
    windows = split_windows(transcript_lines(500), window_tokens=500, overlap=0.10)
    assert len(windows) > 1


def test_windows_overlap():
    windows = split_windows(transcript_lines(200), window_tokens=200, overlap=0.10)
    first_lines = set(windows[0].text.splitlines())
    second_lines = set(windows[1].text.splitlines())
    assert first_lines & second_lines


def test_windows_cover_every_line():
    text = transcript_lines(200)
    windows = split_windows(text, window_tokens=200, overlap=0.10)
    covered = {line for w in windows for line in w.text.splitlines()}
    assert covered == set(text.splitlines())


def test_windows_never_cut_a_line_in_half():
    text = transcript_lines(200)
    windows = split_windows(text, window_tokens=200, overlap=0.10)
    valid = set(text.splitlines())
    for window in windows:
        assert all(line in valid for line in window.text.splitlines())


def test_a_line_longer_than_the_budget_still_makes_progress():
    windows = split_windows(transcript_lines(3, width=10_000), window_tokens=10, overlap=0.10)
    assert len(windows) == 3


def test_empty_text_gives_no_windows():
    assert split_windows("", window_tokens=100, overlap=0.1) == []


def test_merge_prompt_fills_every_placeholder():
    template = (
        "perfil {profile} n {n_clips} min {duration_min} alvo {duration_target} "
        "max {duration_max} seg {max_segments}\n{candidates}"
    )
    prompt = merge_prompt(
        template,
        [{"segments": [{"start": 0, "end": 30}], "score": 80}],
        profile_name="long",
        n_clips=2,
        duration_min=120,
        duration_target=240,
        duration_max=480,
        max_segments=4,
    )
    assert "{" not in prompt.split("\n")[0]
    assert "perfil long n 2" in prompt
    assert '"score": 80' in prompt


# --- file provider --------------------------------------------------------- #


def test_file_provider_writes_the_request(tmp_path):
    provider = FileProvider(tmp_path, wait=False)
    with pytest.raises(WaitingForFileError):
        provider.generate("sistema", "prompt completo")
    text = provider.request_path.read_text(encoding="utf-8")
    assert "<!-- system -->" in text
    assert "sistema" in text
    assert "prompt completo" in text


def test_file_provider_clears_a_stale_response(tmp_path):
    (tmp_path / "select_response.json").write_text("{}", encoding="utf-8")
    provider = FileProvider(tmp_path, wait=False)
    with pytest.raises(WaitingForFileError):
        provider.generate("s", "u")
    assert not provider.response_path.exists()


def test_file_provider_reads_the_answer_once_it_appears(tmp_path):
    provider = FileProvider(tmp_path, wait=True, timeout_s=5.0, poll_interval_s=0.05)
    payload = {"clips": [{"segments": [{"start": 10, "end": 40}], "score": 80}]}

    def answer() -> None:
        time.sleep(0.15)
        provider.response_path.write_text(json.dumps(payload), encoding="utf-8")

    thread = threading.Thread(target=answer)
    thread.start()
    result = provider.complete_json("s", "u", SelectionResponse)
    thread.join()
    assert len(result.clips) == 1


def test_file_provider_times_out(tmp_path):
    provider = FileProvider(tmp_path, wait=True, timeout_s=0.2, poll_interval_s=0.05)
    with pytest.raises(ExternalError, match="nao apareceu"):
        provider.generate("s", "u")


def test_waiting_exits_cleanly():
    assert WaitingForFileError("x").exit_code == 0


# --- factory --------------------------------------------------------------- #


def test_factory_rejects_an_unknown_provider(tmp_path):
    from clipper.config import load_settings

    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path}\n", encoding="utf-8")
    with pytest.raises(InputError, match="desconhecido"):
        build_provider(load_settings(config), "chatgpt")


def test_factory_needs_a_request_dir_for_the_file_provider(tmp_path):
    from clipper.config import load_settings

    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path}\n", encoding="utf-8")
    with pytest.raises(InputError, match="diretorio"):
        build_provider(load_settings(config), "file")


def test_gemini_without_a_key_says_so(tmp_path, monkeypatch):
    from clipper.config import load_settings

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(f"workspace: {tmp_path}\n", encoding="utf-8")
    settings = load_settings(config).model_copy(update={"gemini_api_key": None})
    with pytest.raises(ExternalError, match="GEMINI_API_KEY"):
        build_provider(settings, "gemini")


# --- Claude Code provider --------------------------------------------------- #


def envelope(result: str, *, is_error: bool = False) -> str:
    return json.dumps(
        {
            "result": result,
            "is_error": is_error,
            "usage": {"input_tokens": 5, "output_tokens": 9},
            "type": "result",
        }
    )


def claude_provider(monkeypatch, runner):
    from clipper.config import LLMSettings
    from clipper.llm import claude_code

    monkeypatch.setattr(claude_code.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    return claude_code.ClaudeCodeProvider(LLMSettings())


class Completed:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_claude_provider_unwraps_the_cli_envelope(monkeypatch):
    calls: list[dict] = []

    def runner(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return Completed(envelope('{"clips": []}'))

    provider = claude_provider(monkeypatch, runner)
    assert provider.generate("sistema", "prompt") == '{"clips": []}'

    cmd = calls[0]["cmd"]
    assert "-p" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "sistema"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    # the prompt goes through stdin, so a 2 h transcript never hits ARG_MAX
    assert calls[0]["input"] == "prompt"
    assert "prompt" not in cmd


def test_claude_provider_disables_tools(monkeypatch):
    calls: list[list[str]] = []
    provider = claude_provider(
        monkeypatch, lambda cmd, **kw: (calls.append(cmd), Completed(envelope("{}")))[1]
    )
    provider.generate("s", "u")
    disallowed = calls[0][calls[0].index("--disallowed-tools") + 1]
    for tool in ("Bash", "Write", "WebFetch"):
        assert tool in disallowed


def test_claude_provider_parses_json_through_the_base_class(monkeypatch):
    good = '{"clips": [{"segments": [{"start": 1.0, "end": 40.0}], "score": 80}]}'
    provider = claude_provider(monkeypatch, lambda cmd, **kw: Completed(envelope(good)))
    parsed = provider.complete_json("s", "u", SelectionResponse)
    assert len(parsed.clips) == 1


def test_claude_provider_reports_a_cli_failure(monkeypatch):
    provider = claude_provider(
        monkeypatch, lambda cmd, **kw: Completed("", returncode=1, stderr="not logged in")
    )
    with pytest.raises(ExternalError, match="not logged in"):
        provider.generate("s", "u")


def test_claude_provider_reports_an_error_envelope(monkeypatch):
    provider = claude_provider(
        monkeypatch, lambda cmd, **kw: Completed(envelope("rate limited", is_error=True))
    )
    with pytest.raises(ExternalError, match="rate limited"):
        provider.generate("s", "u")


def test_claude_provider_rejects_an_empty_answer(monkeypatch):
    provider = claude_provider(monkeypatch, lambda cmd, **kw: Completed(envelope("")))
    with pytest.raises(ExternalError, match="vazia"):
        provider.generate("s", "u")


def test_claude_provider_rejects_unparseable_output(monkeypatch):
    provider = claude_provider(monkeypatch, lambda cmd, **kw: Completed("nao e json"))
    with pytest.raises(ExternalError, match="ilegivel"):
        provider.generate("s", "u")


def test_claude_provider_times_out(monkeypatch):
    from clipper.llm import claude_code

    def runner(cmd, **kwargs):
        raise claude_code.subprocess.TimeoutExpired(cmd, 1)

    provider = claude_provider(monkeypatch, runner)
    with pytest.raises(ExternalError, match="nao respondeu"):
        provider.generate("s", "u")


def test_claude_provider_needs_the_cli(monkeypatch):
    from clipper.config import LLMSettings
    from clipper.llm import claude_code

    monkeypatch.setattr(claude_code.shutil, "which", lambda _: None)
    with pytest.raises(ExternalError, match="Claude Code"):
        claude_code.ClaudeCodeProvider(LLMSettings())


def test_claude_provider_passes_a_configured_model(monkeypatch):
    from clipper.config import LLMSettings
    from clipper.llm import claude_code

    calls: list[list[str]] = []
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_code.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(cmd), Completed(envelope("{}")))[1],
    )
    provider = claude_code.ClaudeCodeProvider(LLMSettings(claude_model="claude-opus-5"))
    provider.generate("s", "u")
    assert calls[0][calls[0].index("--model") + 1] == "claude-opus-5"
