from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from antigravity_core.domain.enums import (
    EffortLevel,
    ExecutionMode,
    NotifyPolicy,
    ScheduleKind,
    SensorKind,
    SensorTriggerCondition,
    SensorWakeMode,
    StepState,
    TopicKind,
    TopicStatus,
    TurnStatus,
    UserRole,
)


@dataclass(frozen=True, slots=True)
class GoogleAccount:
    id: int
    slug: str
    email_label: str
    xdg_data_dir: str
    dbus_socket_path: str
    encrypted_keyring_pass: bytes
    encrypted_token_backup: bytes | None
    is_enabled: bool
    is_default: bool
    priority: int
    last_quota_json: dict[str, object] | None
    last_quota_check_at: datetime | None


@dataclass(frozen=True, slots=True)
class RemoteHost:
    id: int
    alias: str
    hostname_or_ip: str
    ssh_port: int
    ssh_user: str
    remote_root_path: str
    local_mount_path: str
    rule_file_path: str
    auto_mount_enabled: bool
    jump_host: str | None = None


@dataclass(frozen=True, slots=True)
class ForumTopic:
    topic_id: int
    title: str
    kind: TopicKind
    status: TopicStatus
    workspace_path: str
    additional_dirs: tuple[str, ...]
    model_slug: str
    effort_level: EffortLevel | None
    execution_mode: ExecutionMode
    assigned_account_id: int
    active_conversation_id: UUID | None
    pinned_card_msg_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TopicSession:
    conversation_id: UUID
    topic_id: int
    account_id: int
    workspace_path: str
    turns_count: int
    cumulative_tokens: int
    cumulative_duration_sec: float
    created_at: datetime
    last_turn_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    id: UUID
    topic_id: int
    conversation_id: UUID | None
    schedule_kind: ScheduleKind
    trigger_spec: str
    instruction_prompt: str
    notify_policy: NotifyPolicy
    is_active: bool
    last_run_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EventSensor:
    id: UUID
    topic_id: int
    conversation_id: UUID | None
    name: str
    sensor_kind: SensorKind
    command: str
    trigger_condition: SensorTriggerCondition
    match_pattern: str | None
    action_command: str | None
    wake_mode: SensorWakeMode
    poll_interval_sec: int
    cooldown_sec: int
    one_shot: bool
    instruction_prompt: str
    notify_policy: NotifyPolicy
    is_active: bool
    triggers_count: int
    last_triggered_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedUser:
    telegram_user_id: int
    username_label: str
    role: UserRole
    allowed_topic_ids: tuple[int, ...]
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InviteToken:
    token: str
    role: UserRole
    allowed_topic_ids: tuple[int, ...]
    created_by_user_id: int
    redeemed_by_user_id: int | None
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerSignature:
    conversation_id: UUID | None
    model_slug: str
    effort_level: EffortLevel | None
    execution_mode: ExecutionMode
    account_id: int
    account_slug: str
    dbus_socket_path: str
    xdg_data_dir: str
    workspace_path: str
    additional_dirs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubagentState:
    type_name: str
    role: str
    conversation_id: str
    log_uri: str
    workspace_uris: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    step_index: int
    tool_name: str
    state: StepState
    duration_seconds: float | None
    target_hint: str | None
    parameters_text: str | None
    output_text: str | None


@dataclass(frozen=True, slots=True)
class TurnProgressEvent:
    conversation_id: UUID
    step_index: int
    step_type: str
    state: StepState
    thinking_text: str | None
    text_accumulated: str
    active_tools: tuple[ToolCallSummary, ...]
    active_subagents: tuple[SubagentState, ...]


@dataclass(frozen=True, slots=True)
class TurnUsage:
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class TurnResult:
    conversation_id: UUID
    status: TurnStatus
    response_text: str
    error_message: str | None
    cumulative_duration_sec: float
    cumulative_turns: int
    cumulative_usage: TurnUsage
    turn_delta_duration_sec: float
    turn_delta_tokens: int
    account_switched_from: str | None
    account_switched_to: str | None
    thinking_text: str | None = None
    tool_calls: tuple[ToolCallSummary, ...] = ()
    subagents: tuple[SubagentState, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerTopicSnapshot:
    topic_id: int
    is_warm: bool
    is_running_turn: bool
    model_slug: str | None
    account_slug: str | None


class ForumTopicGateway(Protocol):
    """Domain boundary protocol for creating, renaming, and closing Telegram Forum Topics."""

    async def create_forum_topic(self, title: str) -> int: ...

    async def rename_forum_topic(self, topic_id: int, new_title: str) -> None: ...

    async def close_and_rename_forum_topic(self, topic_id: int, new_title: str) -> None: ...
