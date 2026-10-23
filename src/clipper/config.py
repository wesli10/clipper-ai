"""User configuration: ``config.yaml`` for behaviour, ``.env`` for secrets (SPEC 10)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from clipper.profiles import Profile, ProfileError, available_profiles, resolve_profile

ProviderName = Literal["gemini", "claude", "ollama", "file"]
DeliverMode = Literal["folder", "rclone"]
#: ask = perguntar no terminal; auto = aprovar os melhores; web = decidir no monitor.
ReviewMode = Literal["ask", "auto", "web"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

#: A name that is safe as a folder in the Drive and on every filesystem.
_SAFE_FOLDER = re.compile(r"[a-z0-9][a-z0-9-]*")


class ConfigError(ValueError):
    """Invalid or unusable configuration."""


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderName = "gemini"
    #: Per-stage overrides. Selection is the editorial call and repays a stronger
    #: model; metadata is short and formulaic. Empty means "use `provider`".
    select_provider: ProviderName | None = None
    metadata_provider: ProviderName | None = None
    gemini_model: str = "gemini-flash-latest"
    gemini_fallback_model: str = "gemini-flash-lite-latest"
    ollama_model: str = "qwen2.5:7b-instruct-q4_K_M"
    #: `claude -p`, authenticated with the user's Claude subscription (SPEC 2/6.5).
    claude_binary: str = "claude"
    #: Empty means whatever the CLI is configured to use.
    claude_model: str = ""
    ollama_host: str = "http://localhost:11434"
    ollama_window_tokens: int = 6000
    ollama_window_overlap: float = 0.10
    max_retries: int = 3
    timeout_s: float = 300.0

    def for_stage(self, stage: str) -> ProviderName:
        """Provider for ``select`` or ``package``, falling back to the default."""
        chosen = {
            "select": self.select_provider,
            "package": self.metadata_provider,
        }.get(stage)
        return chosen or self.provider


class TranscribeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "large-v3"
    fallback_model: str = "medium"
    compute_type: str = "int8_float16"
    device: str = "cuda"
    language: str = "auto"
    beam_size: int = 5
    vad_filter: bool = True


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Quality of the delivered file.
    crf: int = 18
    #: Quality of the cut and concat passes, which the final encode re-encodes.
    #:
    #: Every clip is encoded up to three times (cut -> join -> final). Using the
    #: delivery CRF for all three compounds the loss: measured against the source,
    #: 21/21/21 scores SSIM 0.9946 while 16/16/21 scores 0.9960, for 3% more bytes
    #: in the delivered file. Intermediates are deleted after the render.
    intermediate_crf: int = 16
    preset: str = "medium"
    audio_bitrate: str = "192k"
    transition_s: float = 0.3
    pad_before_s: float = 0.25
    pad_after_s: float = 0.4
    thumbnail_at_s: float = 1.0
    #: Clips rendered at once. x264 does not saturate a modern CPU on one job,
    #: and rendering is the largest single stage of a run.
    workers: int = 3


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Default for `run`; `batch` still defaults to `auto` unless asked otherwise.
    mode: ReviewMode = "ask"
    #: How long the pipeline waits for a decision in `web` mode before giving up.
    timeout_s: float = 3600.0


class AgentSettings(BaseModel):
    """Limits and model for the agent loop (SPEC-agent 8.2).

    Fuses, not targets: they exist so a run that goes wrong stops costing, and
    they are meant to be retuned against a real run's `usage`, not guessed at
    twice.
    """

    model_config = ConfigDict(extra="forbid")

    #: Empty means whatever the Claude Code CLI is configured to use.
    model: str = "claude-opus-5"
    #: Hard stops: the loop is interrupted and the runtime closes the run itself.
    #: Measured over five real runs: a video needs 39 to 54 tool calls, so the
    #: old 40 sat on the median of the need and cut two runs out of five - both
    #: during the wrap-up, after every clip was already rendered and shipped.
    max_turns: int = 80
    #: Calls left when the runtime tells the agent to stop starting things and
    #: land: publish what is ready and write its own report (SPEC-agent 8.2).
    landing_margin: int = 12
    max_output_tokens: int = 60_000
    #: Cache reads do not count - only tokens the run actually paid to send.
    max_input_tokens: int = 150_000
    wall_clock_s: float = 90 * 60
    #: Soft stops: the tool is refused, the loop carries on and can still finish.
    max_renders: int = 10
    max_clip_renders: int = 2
    max_scans_per_profile: int = 2
    max_publishes: int = 2
    #: Per-tool timeouts. A render and a transcription are minutes; the rest is not.
    tool_timeout_s: float = 60.0
    transcribe_timeout_s: float = 30 * 60
    render_timeout_s: float = 20 * 60
    publish_timeout_s: float = 30 * 60
    #: Formats the agent may choose from. Empty falls back to `profiles.default`.
    profiles: list[str] = Field(default_factory=list)


class MonitorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8787

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


class DeliverSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: DeliverMode = "folder"
    drive_root: str = "~/Google Drive/My Drive"
    rclone_remote: str = "gdrive:"
    #: Google Drive uploads in chunks; the rclone default of 8M is slow. Measured
    #: on a real connection: 114 MB took 422 s at 8M and 177 s at 128M.
    rclone_chunk_size: str = "128M"
    rclone_transfers: int = 4
    #: Clip directories uploaded at once. Upload is the other large stage, and
    #: one rclone call per clip in sequence uses none of the available bandwidth
    #: concurrency.
    workers: int = 3
    root_folder: str = "Clipper"
    folder_names: dict[str, str] = Field(
        default_factory=lambda: {"short": "shorts", "long": "longs"}
    )

    def folder_for(self, profile_name: str) -> str:
        return self.folder_names.get(profile_name, profile_name)

    def profile_for(self, folder: str) -> str:
        """The profile a delivery folder belongs to - `shorts` back to `short`."""
        for profile_name, name in self.folder_names.items():
            if name == folder:
                return profile_name
        return folder

    @property
    def drive_path(self) -> Path:
        return Path(self.drive_root).expanduser()


class PublishSettings(BaseModel):
    """The publishing queue inside the delivery (SPEC 8.1).

    The delivery has two halves and they never mix. ``archive_folder`` keeps the
    history of every video - metadata, thumbnail, INDEX.md - and nothing is
    published from it. ``queue_folder`` is the only half an upload session
    touches: one folder per day, the videos of that day inside it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Both live inside ``deliver.root_folder``.
    queue_folder: str = "publicar"
    archive_folder: str = "arquivo"
    #: Shorts queued per day, and the gap in days between one long and the next.
    per_day: int = Field(default=4, ge=1)
    long_every_days: int = Field(default=7, ge=1)
    #: Profiles that go out every day and fill a day up to `per_day`. Anything
    #: else is the weekly piece and is spaced by `long_every_days`.
    daily_profiles: list[str] = Field(default_factory=lambda: ["short"])
    #: Where a clip goes, looked up as `<canal>/<perfil>`, `<canal>`,
    #: `default/<perfil>`, `default` - the most specific key wins. An empty list
    #: means the clip is not published anywhere and stays only in the archive.
    destinations: dict[str, list[str]] = Field(
        default_factory=lambda: {"default": ["tiktok"]},
    )
    #: The per-day file that carries title, description and tags.
    plan_filename: str = "PUBLICAR.md"

    @field_validator("destinations")
    @classmethod
    def _check_destinations(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """A destination becomes a folder name in the Drive, so it must be one."""
        for key, names in value.items():
            for name in names:
                if not _SAFE_FOLDER.fullmatch(name):
                    raise ValueError(
                        f"destino invalido em publish.destinations[{key!r}]: {name!r} "
                        "(use so minusculas, numeros e hifen, ex.: youtube, tiktok)"
                    )
            if len(set(names)) != len(names):
                raise ValueError(f"destino repetido em publish.destinations[{key!r}]")
        return value

    def destinations_for(self, channel: str, profile_name: str) -> list[str]:
        """The destinations of one clip, most specific key first."""
        for key in (f"{channel}/{profile_name}", channel, f"default/{profile_name}", "default"):
            if key in self.destinations:
                return list(self.destinations[key])
        return []


class Settings(BaseModel):
    """Effective configuration for one run."""

    model_config = ConfigDict(extra="forbid")

    workspace: Path = Path("./workspace")
    max_source_duration: float = 3 * 60 * 60
    llm: LLMSettings = Field(default_factory=LLMSettings)
    transcribe: TranscribeSettings = Field(default_factory=TranscribeSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    deliver: DeliverSettings = Field(default_factory=DeliverSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    default_profiles: list[str] = Field(default_factory=lambda: ["short", "long"])
    profile_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    #: Where the publishing channels live (SPEC-agent 7), one YAML each.
    channels_dir: Path = PROJECT_ROOT / "channels"
    #: Where the eval rounds live (SPEC-agent 11), one directory each.
    evals_dir: Path = PROJECT_ROOT / "evals"
    config_path: Path | None = None

    # secrets (from .env, never from config.yaml)
    gemini_api_key: str | None = None

    def profile(self, name: str) -> Profile:
        return resolve_profile(name, self.profile_overrides, self.prompts_dir)

    def profile_names(self) -> list[str]:
        return available_profiles(self.profile_overrides)

    def video_dir(self, video_id: str) -> Path:
        return self.workspace.expanduser() / video_id

    def profile_dir(self, video_id: str, profile_name: str) -> Path:
        return self.video_dir(video_id) / "profiles" / profile_name

    def logs_dir(self, video_id: str) -> Path:
        return self.video_dir(video_id) / "logs"

    def agent_dir(self, video_id: str) -> Path:
        return self.video_dir(video_id) / "agent"

    def agent_profiles(self) -> list[str]:
        """Formats the agent may choose from (SPEC-agent 2.1)."""
        return list(self.agent.profiles) or list(self.default_profiles)

    @property
    def state_db(self) -> Path:
        return self.workspace.expanduser() / "state.db"


def _split_profiles_block(block: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    defaults_raw = block.get("default", ["short", "long"])
    if isinstance(defaults_raw, str):
        defaults = [p.strip() for p in defaults_raw.split(",") if p.strip()]
    elif isinstance(defaults_raw, list):
        defaults = [str(p) for p in defaults_raw]
    else:
        raise ConfigError("profiles.default deve ser uma lista ou string separada por virgulas")

    overrides: dict[str, dict[str, Any]] = {}
    for key, value in block.items():
        if key == "default":
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"profiles.{key} deve ser um mapa de campos do perfil")
        overrides[key] = value
    return defaults, overrides


def load_settings(config_path: Path | None = None, *, env_file: Path | None = None) -> Settings:
    """Load ``config.yaml`` (optional) plus ``.env`` into a :class:`Settings`."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    path = config_path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: o arquivo deve conter um mapa no nivel raiz")
        raw = loaded
    elif config_path is not None:
        raise ConfigError(f"arquivo de configuracao nao encontrado: {path}")

    profiles_block = raw.pop("profiles", {}) or {}
    if not isinstance(profiles_block, dict):
        raise ConfigError("a chave 'profiles' deve ser um mapa")
    defaults, overrides = _split_profiles_block(profiles_block)

    data: dict[str, Any] = dict(raw)
    data["default_profiles"] = defaults
    data["profile_overrides"] = overrides
    data["config_path"] = path if path.exists() else None
    data.setdefault("prompts_dir", PROJECT_ROOT / "prompts")
    data.setdefault("channels_dir", PROJECT_ROOT / "channels")
    data.setdefault("evals_dir", PROJECT_ROOT / "evals")
    data["gemini_api_key"] = os.environ.get("GEMINI_API_KEY") or None

    try:
        settings = Settings.model_validate(data)
    except ProfileError:
        raise
    except ValueError as exc:
        raise ConfigError(f"configuracao invalida ({path}): {exc}") from exc

    # Fail fast on profiles that cannot be resolved at all.
    for name in settings.default_profiles:
        settings.profile(name)
    return settings
