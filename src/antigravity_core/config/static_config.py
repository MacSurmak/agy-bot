from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from antigravity_core.domain.exceptions import StartupValidationError


class TelegramStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_id: int
    api_hash: str
    bot_token: str
    session_file_path: Path
    owner_telegram_id: int
    forum_chat_id: int
    control_panel_topic_id: int

    @field_validator("api_id", "owner_telegram_id", "control_panel_topic_id")
    @classmethod
    def _validate_positive_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Identifier must be strictly positive, got {value}.")
        return value

    @field_validator("api_hash", "bot_token")
    @classmethod
    def _validate_non_empty_secret(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Secret string cannot be empty.")
        return stripped


class SecurityStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fernet_master_key: str

    @field_validator("fernet_master_key")
    @classmethod
    def _validate_non_empty_key(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("fernet_master_key cannot be empty.")
        return stripped


class DatabaseStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dsn: str
    sync_dsn: str
    pool_size: int
    max_overflow: int
    pool_timeout_sec: float

    @field_validator("dsn", "sync_dsn")
    @classmethod
    def _validate_dsn(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith("postgresql"):
            raise ValueError(f"Database DSN must start with 'postgresql', got '{value}'.")
        return stripped

    @field_validator("pool_size", "max_overflow")
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Pool integer parameter must be positive, got {value}.")
        return value

    @field_validator("pool_timeout_sec")
    @classmethod
    def _validate_positive_float(cls, value: float) -> float:
        if value <= 0.0:
            raise ValueError(f"pool_timeout_sec must be positive, got {value}.")
        return value


class PathsStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    venv_python_path: Path
    agy_binary_path: Path
    agy_home_dir: Path
    system_path_env: str
    system_user: str
    system_logname: str
    ssh_config_dir: Path
    workspaces_root_dir: Path
    remotes_mount_dir: Path
    inbox_dir: Path
    profiles_data_dir: Path
    runtime_dir: Path
    ipc_socket_path: Path
    log_file_path: Path

    @field_validator(
        "venv_python_path",
        "agy_binary_path",
        "agy_home_dir",
        "ssh_config_dir",
        "workspaces_root_dir",
        "remotes_mount_dir",
        "inbox_dir",
        "profiles_data_dir",
        "runtime_dir",
        "ipc_socket_path",
        "log_file_path",
    )
    @classmethod
    def _validate_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"Configured path must be absolute, got '{value}'.")
        return value

    @field_validator("system_path_env", "system_user", "system_logname")
    @classmethod
    def _validate_non_empty_sys_str(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("System environment string cannot be empty.")
        return stripped


class RemotesStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sshfs_mount_options: str
    ssh_connect_timeout_sec: int
    ssh_check_grace_sec: int
    sshfs_mount_grace_sec: int

    @field_validator("sshfs_mount_options")
    @classmethod
    def _validate_sshfs_options(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("sshfs_mount_options cannot be empty.")
        opts = {part.strip() for part in stripped.split(",")}
        if "kernel_cache" in opts:
            raise ValueError(
                "Forbidden 'kernel_cache' in sshfs_mount_options; must use 'auto_cache' (Errata Patch #3)."
            )
        if "auto_cache" not in opts:
            raise ValueError("sshfs_mount_options must include 'auto_cache' (Errata Patch #3).")
        return stripped

    @field_validator(
        "ssh_connect_timeout_sec",
        "ssh_check_grace_sec",
        "sshfs_mount_grace_sec",
    )
    @classmethod
    def _validate_ssh_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"SSH timeout parameter must be positive, got {value}.")
        return value


class RuntimeStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agy_sqlite_busy_timeout_ms: int
    mcp_stdio_timeout_sec: int
    hook_timeout_sec: int
    process_kill_grace_sec: float
    ipc_request_timeout_sec: float
    command_exec_timeout_sec: float
    turn_watchdog_grace_sec: float
    dbus_socket_poll_attempts: int
    dbus_socket_poll_interval_sec: float
    oauth_stdout_chunk_bytes: int
    stderr_tail_max_lines: int
    stderr_summary_lines: int
    active_tools_window_size: int
    scheduler_misfire_grace_sec: int
    invite_ttl_hours: int
    invite_token_bytes: int
    keyring_password_bytes: int
    effort_supported_prefixes: tuple[str, ...]

    @field_validator("effort_supported_prefixes")
    @classmethod
    def _validate_effort_prefixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip().lower() for item in value if item.strip())
        if not cleaned:
            raise ValueError("effort_supported_prefixes must contain at least one prefix.")
        return cleaned

    @field_validator(
        "agy_sqlite_busy_timeout_ms",
        "mcp_stdio_timeout_sec",
        "hook_timeout_sec",
        "dbus_socket_poll_attempts",
        "oauth_stdout_chunk_bytes",
        "stderr_tail_max_lines",
        "stderr_summary_lines",
        "active_tools_window_size",
        "scheduler_misfire_grace_sec",
        "invite_ttl_hours",
        "invite_token_bytes",
        "keyring_password_bytes",
    )
    @classmethod
    def _validate_positive_runtime_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Runtime integer parameter must be positive, got {value}.")
        return value

    @field_validator(
        "process_kill_grace_sec",
        "ipc_request_timeout_sec",
        "command_exec_timeout_sec",
        "turn_watchdog_grace_sec",
        "dbus_socket_poll_interval_sec",
    )
    @classmethod
    def _validate_positive_runtime_float(cls, value: float) -> float:
        if value <= 0.0:
            raise ValueError(f"Runtime timeout parameter must be positive, got {value}.")
        return value


class LimitsStaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid_short_chars: int
    tool_target_hint_max_chars: int
    topic_title_max_chars: int
    thinking_snippet_max_chars: int
    draft_preview_max_chars: int
    error_summary_max_chars: int
    live_tools_display_limit: int
    dashboard_tasks_display_limit: int
    tasks_menu_display_limit: int
    models_menu_display_limit: int
    sessions_history_display_limit: int
    workspace_dirs_display_limit: int

    @field_validator(
        "uuid_short_chars",
        "tool_target_hint_max_chars",
        "topic_title_max_chars",
        "thinking_snippet_max_chars",
        "draft_preview_max_chars",
        "error_summary_max_chars",
        "live_tools_display_limit",
        "dashboard_tasks_display_limit",
        "tasks_menu_display_limit",
        "models_menu_display_limit",
        "sessions_history_display_limit",
        "workspace_dirs_display_limit",
    )
    @classmethod
    def _validate_positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Limit integer parameter must be strictly positive, got {value}.")
        return value


class StaticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    telegram: TelegramStaticConfig
    security: SecurityStaticConfig
    database: DatabaseStaticConfig
    paths: PathsStaticConfig
    remotes: RemotesStaticConfig
    runtime: RuntimeStaticConfig
    limits: LimitsStaticConfig


def load_static_config(config_path: Path) -> StaticConfig:
    if not config_path.is_file():
        raise StartupValidationError(f"Static configuration file does not exist: {config_path}")

    try:
        raw_content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartupValidationError(f"Failed to read configuration file {config_path}: {exc}") from exc

    try:
        parsed_yaml: Any = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:
        raise StartupValidationError(f"Invalid YAML syntax in {config_path}: {exc}") from exc

    if not isinstance(parsed_yaml, dict):
        raise StartupValidationError(
            f"Root of {config_path} must be a YAML mapping, got {type(parsed_yaml).__name__}."
        )

    try:
        return StaticConfig.model_validate(parsed_yaml)
    except ValidationError as exc:
        raise StartupValidationError(
            f"Static configuration validation failed for {config_path}:\n{exc}"
        ) from exc
