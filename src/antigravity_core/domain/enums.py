from enum import StrEnum


class TopicKind(StrEnum):
    CONTROL_PANEL = "CONTROL_PANEL"
    PERMANENT_WORKSPACE = "PERMANENT_WORKSPACE"
    TASK_TOPIC = "TASK_TOPIC"


class TopicStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ExecutionMode(StrEnum):
    FULL_ACCESS = "FULL_ACCESS"
    READ_ONLY = "READ_ONLY"


class EffortLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class ScheduleKind(StrEnum):
    ONE_SHOT = "ONE_SHOT"
    CRON = "CRON"


class NotifyPolicy(StrEnum):
    ALWAYS = "ALWAYS"
    ALERT_ONLY = "ALERT_ONLY"


class TurnStatus(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"
    INVALID = "INVALID"
    WAITING = "WAITING"
    RUNNING = "RUNNING"


class StepState(StrEnum):
    ACTIVE = "ACTIVE"
    DONE = "DONE"


class StepType(StrEnum):
    USER_INPUT = "user_input"
    TOOL = "tool"
    AGENT_RESPONSE = "agent_response"
    CHECKPOINT = "checkpoint"


class UserRole(StrEnum):
    OWNER = "OWNER"
    MEMBER = "MEMBER"


class SensorKind(StrEnum):
    POLL_COMMAND = "POLL_COMMAND"
    STREAM_WATCH = "STREAM_WATCH"


class SensorTriggerCondition(StrEnum):
    ON_EXIT_ZERO = "ON_EXIT_ZERO"
    ON_EXIT_NONZERO = "ON_EXIT_NONZERO"
    ON_OUTPUT_CHANGE = "ON_OUTPUT_CHANGE"
    ON_REGEX_MATCH = "ON_REGEX_MATCH"


class SensorWakeMode(StrEnum):
    ALWAYS = "ALWAYS"
    ON_ACTION_FAILURE = "ON_ACTION_FAILURE"
    NEVER = "NEVER"
