from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import pwd
import shutil
import sys
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from antigravity_core.config.constants import (
    AGY_AGENT_USER,
    CONTROL_PANEL_TOPIC_ID,
    DB_MAX_OVERFLOW,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT_SEC,
    DEFAULT_POSTGRES_HOST,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_SYSTEM_PATH,
    FHS_APP_DIR,
    FHS_BIN_DIR,
    FHS_ETC_DIR,
    FHS_RUN_DIR,
    FHS_VAR_LIB_DIR,
    LimitsStaticConfig,
    RemotesStaticConfig,
    RuntimeStaticConfig,
)
from antigravity_core.domain.exceptions import StartupValidationError


class _TelegramYamlInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    api_id: int
    api_hash: str
    bot_token: str
    owner_telegram_id: int
    forum_chat_id: int

    @field_validator("api_id", "owner_telegram_id")
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
    model_config = ConfigDict(extra="ignore", frozen=True)

    fernet_master_key: str

    @field_validator("fernet_master_key")
    @classmethod
    def _validate_non_empty_key(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("fernet_master_key cannot be empty.")
        return stripped


class _MinimalYamlConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    telegram: _TelegramYamlInput
    security: SecurityStaticConfig


@dataclass(frozen=True, slots=True)
class TelegramStaticConfig:
    api_id: int
    api_hash: str
    bot_token: str
    session_file_path: Path
    owner_telegram_id: int
    forum_chat_id: int
    control_panel_topic_id: int


@dataclass(frozen=True, slots=True)
class DatabaseStaticConfig:
    dsn: str
    sync_dsn: str
    pool_size: int
    max_overflow: int
    pool_timeout_sec: float


@dataclass(frozen=True, slots=True)
class PathsStaticConfig:
    project_root_dir: Path
    venv_python_path: Path
    agy_binary_path: Path
    rtk_binary_path: Path | None
    agy_home_dir: Path
    hosts_passports_dir: Path
    projects_dir: Path
    agent_user: str
    agent_uid: int
    agent_gid: int
    agent_home_dir: Path
    agent_log_dir: Path
    system_path_env: str
    system_user: str
    system_logname: str
    ssh_config_dir: Path
    ssh_sockets_dir: Path
    runtime_bin_dir: Path
    workspaces_root_dir: Path
    remotes_mount_dir: Path
    inbox_dir: Path
    profiles_data_dir: Path
    runtime_dir: Path
    ipc_socket_path: Path
    log_file_path: Path


@dataclass(frozen=True, slots=True)
class StaticConfig:
    telegram: TelegramStaticConfig
    security: SecurityStaticConfig
    database: DatabaseStaticConfig
    paths: PathsStaticConfig
    remotes: RemotesStaticConfig
    runtime: RuntimeStaticConfig
    limits: LimitsStaticConfig


def resolve_default_config_path() -> Path:
    if "ANTIGRAVITY_CONFIG" in os.environ:
        env_val = os.environ["ANTIGRAVITY_CONFIG"].strip()
        if env_val:
            return Path(env_val).expanduser().resolve()
    return (Path(FHS_ETC_DIR) / "config.yaml").resolve()


def _load_dotenv_map(project_root: Path) -> dict[str, str]:
    dotenv_vars: dict[str, str] = {}
    env_file = project_root / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            dotenv_vars[key.strip()] = val.strip().strip("'\"")
    return dotenv_vars


def _resolve_database_config(
    project_root: Path, parsed_yaml: dict[str, Any]
) -> DatabaseStaticConfig:
    dotenv_map = _load_dotenv_map(project_root)
    pg_user = os.environ.get("POSTGRES_USER") or dotenv_map.get("POSTGRES_USER")
    pg_pass = os.environ.get("POSTGRES_PASSWORD") or dotenv_map.get("POSTGRES_PASSWORD")
    pg_db = os.environ.get("POSTGRES_DB") or dotenv_map.get("POSTGRES_DB")
    pg_host = (
        os.environ.get("POSTGRES_HOST")
        or dotenv_map.get("POSTGRES_HOST")
        or DEFAULT_POSTGRES_HOST
    )
    pg_port_raw = (
        os.environ.get("POSTGRES_PORT")
        or dotenv_map.get("POSTGRES_PORT")
        or str(DEFAULT_POSTGRES_PORT)
    )

    if pg_user and pg_pass and pg_db:
        dsn = (
            f"postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port_raw}/{pg_db}"
        )
        sync_dsn = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port_raw}/{pg_db}"
        return DatabaseStaticConfig(
            dsn=dsn,
            sync_dsn=sync_dsn,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
            pool_timeout_sec=DB_POOL_TIMEOUT_SEC,
        )

    # Fallback if .env is absent and database section was explicitly provided in config.yaml
    db_sec = parsed_yaml.get("database")
    if isinstance(db_sec, dict) and db_sec.get("dsn"):
        raw_dsn = str(db_sec["dsn"]).strip()
        raw_sync = str(
            db_sec.get("sync_dsn") or raw_dsn.replace("+asyncpg", "")
        ).strip()
        return DatabaseStaticConfig(
            dsn=raw_dsn,
            sync_dsn=raw_sync,
            pool_size=DB_POOL_SIZE,
            max_overflow=DB_MAX_OVERFLOW,
            pool_timeout_sec=DB_POOL_TIMEOUT_SEC,
        )

    raise StartupValidationError(
        "Database credentials missing: set POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB in .env."
    )


def _resolve_agy_binary() -> Path:
    found = shutil.which("agy")
    if found:
        return Path(found).resolve()
    for candidate in (
        Path.home() / ".local" / "bin" / "agy",
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
    ):
        if candidate.is_file():
            return candidate.resolve()
    raise StartupValidationError(
        "Could not locate 'agy' binary in PATH or ~/.local/bin/agy."
    )


def _resolve_rtk_binary() -> Path | None:
    found = shutil.which("rtk")
    if found:
        return Path(found).resolve()
    for candidate in (Path("/usr/local/bin/rtk"), Path("/usr/bin/rtk")):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolve_agent_identity() -> tuple[str, int, int, Path]:
    if os.geteuid() == 0:
        try:
            pw = pwd.getpwnam(AGY_AGENT_USER)
        except KeyError as exc:
            raise StartupValidationError(
                f"Dedicated non-root agent user '{AGY_AGENT_USER}' does not exist. "
                "Run 'sudo bash scripts/bootstrap_lxc.sh' first."
            ) from exc
        return pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir).resolve()
    pw = pwd.getpwuid(os.geteuid())
    return pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir).resolve()


def _resolve_paths_config() -> PathsStaticConfig:
    current_user = getpass.getuser()
    raw_path = os.environ["PATH"].strip() if "PATH" in os.environ else ""
    current_path = raw_path if raw_path else DEFAULT_SYSTEM_PATH
    agent_user, agent_uid, agent_gid, agent_home = _resolve_agent_identity()
    agy_home = agent_home / ".gemini"
    run_root = Path(FHS_RUN_DIR).resolve()
    var_lib_root = Path(FHS_VAR_LIB_DIR).resolve()

    return PathsStaticConfig(
        project_root_dir=Path(FHS_APP_DIR).resolve(),
        venv_python_path=Path(sys.executable),
        agy_binary_path=_resolve_agy_binary(),
        rtk_binary_path=_resolve_rtk_binary(),
        agy_home_dir=agy_home,
        hosts_passports_dir=agent_home / "hosts",
        projects_dir=agent_home / "projects",
        agent_user=agent_user,
        agent_uid=agent_uid,
        agent_gid=agent_gid,
        agent_home_dir=agent_home,
        agent_log_dir=agent_home / "logs",
        system_path_env=current_path,
        system_user=current_user,
        system_logname=current_user,
        ssh_config_dir=agent_home / ".ssh" / "config.d",
        ssh_sockets_dir=agent_home / ".ssh" / "sockets",
        runtime_bin_dir=Path(FHS_BIN_DIR).resolve(),
        workspaces_root_dir=agent_home / "workspaces",
        remotes_mount_dir=agent_home / "remotes",
        inbox_dir=agent_home / "inbox",
        profiles_data_dir=agent_home / "profiles",
        runtime_dir=run_root,
        ipc_socket_path=run_root / "core.sock",
        log_file_path=var_lib_root / "logs" / "antigravity_core.jsonl",
    )


def load_static_config(config_path: Path | None = None) -> StaticConfig:
    resolved_cfg_path = (
        config_path.expanduser().resolve()
        if config_path is not None
        else resolve_default_config_path()
    )
    if not resolved_cfg_path.is_file():
        raise StartupValidationError(
            f"Configuration file does not exist: {resolved_cfg_path}"
        )

    try:
        raw_content = resolved_cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartupValidationError(
            f"Failed to read configuration file {resolved_cfg_path}: {exc}"
        ) from exc

    try:
        parsed_yaml: Any = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:
        raise StartupValidationError(
            f"Invalid YAML syntax in {resolved_cfg_path}: {exc}"
        ) from exc

    if not isinstance(parsed_yaml, dict):
        raise StartupValidationError(
            f"Root of {resolved_cfg_path} must be a YAML mapping."
        )

    try:
        minimal = _MinimalYamlConfig.model_validate(parsed_yaml)
    except ValidationError as exc:
        raise StartupValidationError(
            f"Configuration validation failed for {resolved_cfg_path}:\n{exc}"
        ) from exc

    config_dir = resolved_cfg_path.parent
    paths_cfg = _resolve_paths_config()
    db_cfg = _resolve_database_config(config_dir, parsed_yaml)
    session_file = (
        Path(FHS_VAR_LIB_DIR) / "data" / "telethon_bot.session"
    ).resolve()

    tg_cfg = TelegramStaticConfig(
        api_id=minimal.telegram.api_id,
        api_hash=minimal.telegram.api_hash,
        bot_token=minimal.telegram.bot_token,
        session_file_path=session_file,
        owner_telegram_id=minimal.telegram.owner_telegram_id,
        forum_chat_id=minimal.telegram.forum_chat_id,
        control_panel_topic_id=CONTROL_PANEL_TOPIC_ID,
    )

    return StaticConfig(
        telegram=tg_cfg,
        security=minimal.security,
        database=db_cfg,
        paths=paths_cfg,
        remotes=RemotesStaticConfig(),
        runtime=RuntimeStaticConfig(),
        limits=LimitsStaticConfig(),
    )
