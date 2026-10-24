"""Strict YAML configuration.

Project rule: **every parameter lives in the config file** — no hidden
business defaults in Python code. This loader only expands environment
references and validates the declared shape; absent required keys are a
clear error instead of a silent fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from kernel.agent_framework.protocol import DEFAULT_SILENCE_MARKER


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


def load_dotenv(path: str | Path, override: bool = False) -> None:
    """Load ``KEY=VALUE`` lines from a local ``.env`` into os.environ.

    Minimal by design: ignores blank/comment lines, strips surrounding
    quotes, does no variable interpolation, and by default never overrides
    an already-set variable. ``override=True`` replaces existing values —
    the console uses it before spawning the kernel so that editing ``.env``
    takes effect on kernel restart (the child otherwise inherits stale
    variables from the console's start-time environment). The file is meant
    to be git-ignored (local secrets).
    """
    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        value = raw.split("#", 1)[0].strip()  # 行尾注释（# 后）去掉
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]  # 引号包裹：剥掉成对引号
        if key and value and (override or key not in os.environ):
            os.environ[key] = value


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(
                    f"missing required environment variable: {name}")
            return os.environ[name]
        return _ENV_RE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    expanded = _expand(raw or {})
    if not isinstance(expanded, dict):
        raise ConfigError(f"config root must be a mapping: {config_path}")
    return expanded


def require(mapping: dict[str, Any], path: str) -> Any:
    """Read a required dotted key without supplying an implicit default."""
    value: Any = mapping
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ConfigError(f"missing required config key: {path}")
        value = value[part]
    if value is None or value == "":
        raise ConfigError(f"required config key is empty: {path}")
    return value


def get(mapping: dict[str, Any], path: str, default: Any) -> Any:
    """Read an optional dotted key with an explicit caller-supplied default."""
    value: Any = mapping
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


SUPPORTED_INPUT_MODALITIES = ("text", "image")


def parse_input_modalities(value: Any, path: str = "llm.input") -> list[str]:
    """Validate a declared modality list (e.g. ['text', 'image'])."""
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{path} must be a non-empty list of "
            f"{'/'.join(SUPPORTED_INPUT_MODALITIES)}")
    result: list[str] = []
    for item in value:
        modality = str(item).strip().lower()
        if modality not in SUPPORTED_INPUT_MODALITIES:
            raise ConfigError(
                f"unsupported modality in {path}: {item!r}; expected "
                f"{'/'.join(SUPPORTED_INPUT_MODALITIES)}")
        if modality not in result:
            result.append(modality)
    return result


def validate_runtime_config(config: dict[str, Any]) -> None:
    """Validate the framework foundation keys used by the bootstrap."""
    for key in (
        "runtime.name",
        "runtime.data_dir",
        "runtime.log_level",
        "database.path",
        "todos.file",
        "messages_tools.query_max_rows",
        "messages_tools.list_latest",
        "messages_tools.content_max_chars",
        "messages_tools.unread_latest",
        "messages_tools.list_threads_limit",
        "messages_tools.range_max_chars",
        "messages_tools.range_limit",
        "messages_tools.detail_max_chars",
        "messages_tools.clear_show",
        "messages_tools.download_dir",
        "skills.enabled",
        "skills.directory",
        "skills.install_max_zip_bytes",
        "skills.install_max_total_bytes",
        "skills.install_max_files",
        "skills.detail_max_chars",
        "skills.backup_dir",
        "skills.trash_dir",
        "email.enabled",
        "prompt.directory",
        "prompt.files",
        "workspace.host_root",
        "workspace.container_path",
        "workspace_tools.enabled",
        "http.host",
        "http.port",
        "http.api_key",
        "http.max_body_bytes",
        "agent.tool_timeout_seconds",
        "agent.tool_result_max_chars",
        "agent.event_batch_wait_seconds",
        "agent.repair_latest_n",
        "agent.user_merge.enabled",
        "agent.user_merge.separator",
        "agent.user_merge.notice",
        "agent.model_output_threshold_chars",
        "agent.model_output_line_max_chars",
        "agent.unread_window_hours",
        "agent.unread_max_per_session",
        "agent.llm_failed_backoff_seconds",
        "agent.llm_permanent_fail_threshold",
        "agent.llm_permanent_fail_backoff_seconds",
        "agent.context_max_bytes",
        "readimage.enabled",
        "readimage.mode",
        "readimage.max_input_bytes",
        "readimage.max_edge",
        "readimage.jpeg_quality",
        "readimage.quality_floor",
        "readimage.max_base64_bytes",
        "readimage.min_base64_bytes",
        "readimage.block_overhead_bytes",
        "readimage.allow_url",
        "readimage.allow_private_hosts",
        "readimage.url_timeout_seconds",
        "imgbed.enabled",
        "imgbed.retries",
        "wake.initial_probability",
        "wake.min_probability",
        "wake.unread_weight",
        "wake.wake_x_convs",
        "wake.wake_max_chars",
        "wake.inject_latest",
        "wake.free_preview_enabled",
        "wake.free_preview_sessions",
        "wake.wake_dedup",
        "wake.wake_cooldown_seconds",
        "wake.wake_notice_prefix",
        "wake.wake_supplement_prefix",
        "wake.no_message_prompt_file",
        "beat.enabled",
        "beat.interval_seconds",
        "beat.archive_interval_hours",
        "beat.archive_days",
        "events_port.enabled",
        "llm.protocol",
        "llm.variant",
        "llm.model",
        "llm.base_url",
        "llm.api_key",
        "llm.user_agent",
        "llm.input",
        "llm.timeout_seconds",
        "llm.trailing_empty_user",
        "llm.retries",
        "llm.retry_delay_seconds",
    ):
        require(config, key)


class RuntimeConfig:
    """Typed view over the validated config dict (fields mirror YAML)."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        validate_runtime_config(raw)
        self.runtime_name: str = str(require(raw, "runtime.name"))
        self.data_dir: str = str(require(raw, "runtime.data_dir"))
        self.log_level: str = str(require(raw, "runtime.log_level"))
        self.db_path: str = str(require(raw, "database.path"))
        self.todos_file: str = str(require(raw, "todos.file"))
        self.messages_tools: dict = raw["messages_tools"]
        self.skills_enabled: bool = bool(require(raw, "skills.enabled"))
        self.skills_directory: str = str(require(raw, "skills.directory"))
        self.skills_install_max_zip_bytes: int = int(
            require(raw, "skills.install_max_zip_bytes"))
        self.skills_install_max_total_bytes: int = int(
            require(raw, "skills.install_max_total_bytes"))
        self.skills_install_max_files: int = int(
            require(raw, "skills.install_max_files"))
        self.skills_detail_max_chars: int = int(
            require(raw, "skills.detail_max_chars"))
        self.skills_backup_dir: str = str(
            require(raw, "skills.backup_dir"))
        self.skills_trash_dir: str = str(
            require(raw, "skills.trash_dir"))
        self.prompt_dir: str = str(require(raw, "prompt.directory"))
        self.prompt_files: list[str] = [
            str(f) for f in require(raw, "prompt.files")]
        self.workspace_host_root: str = str(
            require(raw, "workspace.host_root"))
        self.workspace_container_path: str = str(
            require(raw, "workspace.container_path"))
        self.http_host: str = str(require(raw, "http.host"))
        self.http_port: int = int(require(raw, "http.port"))
        self.http_api_key: str = str(require(raw, "http.api_key"))
        self.http_max_body_bytes: int = int(
            require(raw, "http.max_body_bytes"))
        self.stream_history: int = int(get(raw, "http.stream_history", 200))
        self.stream_heartbeat: float = float(
            get(raw, "http.stream_heartbeat", 15.0))
        self.http_shutdown_timeout_seconds: float = float(
            require(raw, "http.shutdown_timeout_seconds"))
        self.tool_timeout_seconds: float = float(
            require(raw, "agent.tool_timeout_seconds"))
        self.tool_result_max_chars: int = int(
            require(raw, "agent.tool_result_max_chars"))
        self.event_batch_wait_seconds: float = float(
            require(raw, "agent.event_batch_wait_seconds"))
        self.repair_latest_n: int = int(
            require(raw, "agent.repair_latest_n"))
        self.user_merge_enabled: bool = bool(
            require(raw, "agent.user_merge.enabled"))
        self.user_merge_separator: str = str(
            require(raw, "agent.user_merge.separator"))
        self.user_merge_notice: str = str(
            require(raw, "agent.user_merge.notice"))
        self.model_output_threshold_chars: int = int(
            require(raw, "agent.model_output_threshold_chars"))
        self.model_output_line_max_chars: int = int(
            require(raw, "agent.model_output_line_max_chars"))
        # 静默标记（可配置常量；提示词 {{silence_marker}} 自动替换）
        self.silence_marker: str = str(
            get(raw, "agent.silence_marker", DEFAULT_SILENCE_MARKER))
        self.unread_window_hours: float = float(
            require(raw, "agent.unread_window_hours"))
        self.unread_max_per_session: int = int(
            require(raw, "agent.unread_max_per_session"))
        self.llm_failed_backoff_seconds: float = float(
            require(raw, "agent.llm_failed_backoff_seconds"))
        self.llm_permanent_fail_threshold: int = int(
            require(raw, "agent.llm_permanent_fail_threshold"))
        self.llm_permanent_fail_backoff_seconds: float = float(
            require(raw, "agent.llm_permanent_fail_backoff_seconds"))
        self.context_max_bytes: int = int(
            require(raw, "agent.context_max_bytes"))
        self.readimage_enabled: bool = bool(
            require(raw, "readimage.enabled"))
        self.readimage_max_input_bytes: int = int(
            require(raw, "readimage.max_input_bytes"))
        self.readimage_max_edge: int = int(
            require(raw, "readimage.max_edge"))
        self.readimage_jpeg_quality: int = int(
            require(raw, "readimage.jpeg_quality"))
        self.readimage_quality_floor: int = int(
            require(raw, "readimage.quality_floor"))
        self.readimage_max_base64_bytes: int = int(
            require(raw, "readimage.max_base64_bytes"))
        self.readimage_min_base64_bytes: int = int(
            require(raw, "readimage.min_base64_bytes"))
        self.readimage_block_overhead_bytes: int = int(
            require(raw, "readimage.block_overhead_bytes"))
        self.readimage_allow_url: bool = bool(
            require(raw, "readimage.allow_url"))
        self.readimage_allow_private_hosts: bool = bool(
            require(raw, "readimage.allow_private_hosts"))
        self.readimage_url_timeout_seconds: float = float(
            require(raw, "readimage.url_timeout_seconds"))
        self.readimage_mode: str = str(
            require(raw, "readimage.mode")).strip().lower()
        if self.readimage_mode not in ("local", "imgbed"):
            raise ConfigError(
                "readimage.mode must be 'local' or 'imgbed'")
        self.imgbed_enabled: bool = bool(require(raw, "imgbed.enabled"))
        if self.readimage_mode == "imgbed" and not self.imgbed_enabled:
            raise ConfigError(
                "readimage.mode=imgbed requires imgbed.enabled=true")
        if self.imgbed_enabled:
            self.imgbed_upload_url = str(require(raw, "imgbed.upload_url"))
            self.imgbed_api_key = str(require(raw, "imgbed.api_key"))
            self.imgbed_api_key_header = str(
                require(raw, "imgbed.api_key_header"))
            self.imgbed_form_field = str(require(raw, "imgbed.form_field"))
            self.imgbed_response_url_field = str(
                require(raw, "imgbed.response_url_field"))
            self.imgbed_timeout_seconds = float(
                require(raw, "imgbed.timeout_seconds"))
            self.imgbed_retries = int(require(raw, "imgbed.retries"))
        else:
            self.imgbed_upload_url = ""
            self.imgbed_api_key = ""
            self.imgbed_api_key_header = ""
            self.imgbed_form_field = ""
            self.imgbed_response_url_field = ""
            self.imgbed_timeout_seconds = 0.0
            self.imgbed_retries = 0
        self.wake_initial_probability: float = float(
            require(raw, "wake.initial_probability"))
        self.wake_min_probability: float = float(
            require(raw, "wake.min_probability"))
        self.wake_notice_prefix: str = str(
            require(raw, "wake.wake_notice_prefix"))
        self.no_message_prompt_file: str = str(
            require(raw, "wake.no_message_prompt_file"))
        self.beat_enabled: bool = bool(require(raw, "beat.enabled"))
        self.beat_interval_seconds: float = float(
            require(raw, "beat.interval_seconds"))
        self.beat_archive_interval_hours: float = float(
            require(raw, "beat.archive_interval_hours"))
        self.beat_archive_days: int = int(
            require(raw, "beat.archive_days"))
        self.events_port_enabled: bool = bool(
            require(raw, "events_port.enabled"))
        self.events_port_host: str = str(
            require(raw, "events_port.host"))
        self.events_port_port: int = int(require(raw, "events_port.port"))
        self.events_port_api_key: str = str(
            require(raw, "events_port.api_key"))
        self.model: str = str(require(raw, "llm.model"))
        self.trailing_empty_user: bool = bool(
            require(raw, "llm.trailing_empty_user"))
        self.llm_user_agent: str = str(require(raw, "llm.user_agent"))
        self.llm_input: list[str] = parse_input_modalities(
            require(raw, "llm.input"), "llm.input")
        self.llm = raw["llm"]

    @property
    def context_file(self) -> str:
        return str(get(self.raw, "runtime.context_file",
                       str(Path(self.data_dir) / "context.jsonl")))

    @property
    def compressed_dir(self) -> str:
        return str(get(self.raw, "runtime.compressed_dir",
                       str(Path(self.data_dir) / "compressed")))

    @property
    def container_section(self) -> dict[str, Any]:
        return dict(get(self.raw, "compression", {}))
