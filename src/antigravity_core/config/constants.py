"""Canonical module of technical, algorithmic, and UI constants (Single Source of Truth)."""

from dataclasses import dataclass

# Telegram Forum & Control Panel
CONTROL_PANEL_TOPIC_ID: int = 1
DASHBOARD_MSG_ID_SETTING_KEY: str = "_control_panel_dashboard_msg_id"

# Database connection pool defaults
DB_POOL_SIZE: int = 10
DB_MAX_OVERFLOW: int = 20
DB_POOL_TIMEOUT_SEC: float = 10.0
DEFAULT_POSTGRES_HOST: str = "127.0.0.1"
DEFAULT_POSTGRES_PORT: int = 5432

# Message batching & Worker lifecycle
DEBOUNCE_WINDOW_SEC: float = 1.8
MAX_BATCH_WINDOW_SEC: float = 10.0
WORKER_IDLE_TTL_SEC: int = 900
TURN_TIMEOUT_SEC: int = 3600
TG_EDIT_THROTTLE_SEC: float = 3.5
TG_MAX_MESSAGE_CHARS: int = 3800
STDOUT_BUFFER_LIMIT_BYTES: int = 33_554_432
QUOTA_AUTO_SWITCH_THRESHOLD_PCT: int = 5

# SSH & SSHFS mount options (Errata Patch #3: safe auto_cache)
SSHFS_MOUNT_OPTIONS: str = (
    "reconnect,ServerAliveInterval=5,ServerAliveCountMax=2,ConnectTimeout=5,"
    "BatchMode=yes,idmap=user,cache=yes,auto_cache,attr_timeout=3,"
    "entry_timeout=5,negative_timeout=2,compression=yes"
)
SSH_CONNECT_TIMEOUT_SEC: int = 10
SSH_CHECK_GRACE_SEC: int = 5
SSHFS_MOUNT_GRACE_SEC: int = 10
SSH_CONTROL_PERSIST_SEC: int = 600
INTERACTIVE_APPROVAL_TIMEOUT_SEC: float = 290.0

# Subprocess, D-Bus, PTY, and Scheduler runtime constants
AGY_SQLITE_BUSY_TIMEOUT_MS: int = 5000
MCP_STDIO_TIMEOUT_SEC: int = 600
HOOK_TIMEOUT_SEC: int = 300
PROCESS_KILL_GRACE_SEC: float = 3.0
IPC_REQUEST_TIMEOUT_SEC: float = 310.0
COMMAND_EXEC_TIMEOUT_SEC: float = 45.0
AGY_QUOTA_EXEC_TIMEOUT_SEC: float = 25.0
AGY_AUTH_CHECK_TIMEOUT_SEC: float = 10.0
SENSOR_REFLEX_TIMEOUT_SEC: float = 60.0
TURN_WATCHDOG_GRACE_SEC: float = 30.0
DBUS_SOCKET_POLL_ATTEMPTS: int = 40
DBUS_SOCKET_POLL_INTERVAL_SEC: float = 0.05
OAUTH_STDOUT_CHUNK_BYTES: int = 4096
STDERR_TAIL_MAX_LINES: int = 50
STDERR_SUMMARY_LINES: int = 15
ACTIVE_TOOLS_WINDOW_SIZE: int = 8
SCHEDULER_MISFIRE_GRACE_SEC: int = 300
INVITE_TTL_HOURS: int = 24
INVITE_TOKEN_BYTES: int = 18
KEYRING_PASSWORD_BYTES: int = 32
EFFORT_SUPPORTED_PREFIXES: tuple[str, ...] = ("gemini",)

# UI formatting & pagination limits
UUID_SHORT_CHARS: int = 8
TOOL_TARGET_HINT_MAX_CHARS: int = 120
TOPIC_TITLE_MAX_CHARS: int = 120
THINKING_SNIPPET_MAX_CHARS: int = 280
DRAFT_PREVIEW_MAX_CHARS: int = 600
ERROR_SUMMARY_MAX_CHARS: int = 350
LIVE_TOOLS_DISPLAY_LIMIT: int = 6
DASHBOARD_TASKS_DISPLAY_LIMIT: int = 5
TASKS_MENU_DISPLAY_LIMIT: int = 10
MODELS_MENU_DISPLAY_LIMIT: int = 12
SESSIONS_HISTORY_DISPLAY_LIMIT: int = 8
WORKSPACE_DIRS_DISPLAY_LIMIT: int = 12

DEFAULT_SYSTEM_PATH: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True, slots=True)
class RemotesStaticConfig:
    sshfs_mount_options: str = SSHFS_MOUNT_OPTIONS
    ssh_connect_timeout_sec: int = SSH_CONNECT_TIMEOUT_SEC
    ssh_check_grace_sec: int = SSH_CHECK_GRACE_SEC
    sshfs_mount_grace_sec: int = SSHFS_MOUNT_GRACE_SEC
    ssh_control_persist_sec: int = SSH_CONTROL_PERSIST_SEC
    interactive_approval_timeout_sec: float = INTERACTIVE_APPROVAL_TIMEOUT_SEC


@dataclass(frozen=True, slots=True)
class RuntimeStaticConfig:
    agy_sqlite_busy_timeout_ms: int = AGY_SQLITE_BUSY_TIMEOUT_MS
    mcp_stdio_timeout_sec: int = MCP_STDIO_TIMEOUT_SEC
    hook_timeout_sec: int = HOOK_TIMEOUT_SEC
    process_kill_grace_sec: float = PROCESS_KILL_GRACE_SEC
    ipc_request_timeout_sec: float = IPC_REQUEST_TIMEOUT_SEC
    command_exec_timeout_sec: float = COMMAND_EXEC_TIMEOUT_SEC
    agy_quota_exec_timeout_sec: float = AGY_QUOTA_EXEC_TIMEOUT_SEC
    agy_auth_check_timeout_sec: float = AGY_AUTH_CHECK_TIMEOUT_SEC
    sensor_reflex_timeout_sec: float = SENSOR_REFLEX_TIMEOUT_SEC
    turn_watchdog_grace_sec: float = TURN_WATCHDOG_GRACE_SEC
    dbus_socket_poll_attempts: int = DBUS_SOCKET_POLL_ATTEMPTS
    dbus_socket_poll_interval_sec: float = DBUS_SOCKET_POLL_INTERVAL_SEC
    oauth_stdout_chunk_bytes: int = OAUTH_STDOUT_CHUNK_BYTES
    stderr_tail_max_lines: int = STDERR_TAIL_MAX_LINES
    stderr_summary_lines: int = STDERR_SUMMARY_LINES
    active_tools_window_size: int = ACTIVE_TOOLS_WINDOW_SIZE
    scheduler_misfire_grace_sec: int = SCHEDULER_MISFIRE_GRACE_SEC
    invite_ttl_hours: int = INVITE_TTL_HOURS
    invite_token_bytes: int = INVITE_TOKEN_BYTES
    keyring_password_bytes: int = KEYRING_PASSWORD_BYTES
    effort_supported_prefixes: tuple[str, ...] = EFFORT_SUPPORTED_PREFIXES


@dataclass(frozen=True, slots=True)
class LimitsStaticConfig:
    uuid_short_chars: int = UUID_SHORT_CHARS
    tool_target_hint_max_chars: int = TOOL_TARGET_HINT_MAX_CHARS
    topic_title_max_chars: int = TOPIC_TITLE_MAX_CHARS
    thinking_snippet_max_chars: int = THINKING_SNIPPET_MAX_CHARS
    draft_preview_max_chars: int = DRAFT_PREVIEW_MAX_CHARS
    error_summary_max_chars: int = ERROR_SUMMARY_MAX_CHARS
    live_tools_display_limit: int = LIVE_TOOLS_DISPLAY_LIMIT
    dashboard_tasks_display_limit: int = DASHBOARD_TASKS_DISPLAY_LIMIT
    tasks_menu_display_limit: int = TASKS_MENU_DISPLAY_LIMIT
    models_menu_display_limit: int = MODELS_MENU_DISPLAY_LIMIT
    sessions_history_display_limit: int = SESSIONS_HISTORY_DISPLAY_LIMIT
    workspace_dirs_display_limit: int = WORKSPACE_DIRS_DISPLAY_LIMIT
