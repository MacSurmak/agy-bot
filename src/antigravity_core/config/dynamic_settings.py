from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from antigravity_core.domain.enums import EffortLevel, ExecutionMode
from antigravity_core.domain.exceptions import ConfigurationError

MANDATORY_SYSTEM_SETTING_KEYS: tuple[str, ...] = (
    "default_model",
    "default_effort",
    "default_execution_mode",
    "debounce_window_sec",
    "max_batch_window_sec",
    "worker_idle_ttl_sec",
    "turn_timeout_sec",
    "tg_edit_throttle_sec",
    "tg_max_message_chars",
    "stdout_buffer_limit_bytes",
    "quota_auto_switch_threshold_pct",
)


class DynamicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_model: str
    default_effort: EffortLevel | None
    default_execution_mode: ExecutionMode
    debounce_window_sec: float
    max_batch_window_sec: float
    worker_idle_ttl_sec: int
    turn_timeout_sec: int
    tg_edit_throttle_sec: float
    tg_max_message_chars: int
    stdout_buffer_limit_bytes: int
    quota_auto_switch_threshold_pct: int

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("default_model cannot be empty.")
        return stripped

    @field_validator("debounce_window_sec", "max_batch_window_sec", "tg_edit_throttle_sec")
    @classmethod
    def _validate_positive_float(cls, value: float) -> float:
        if value <= 0.0:
            raise ValueError(f"Setting float value must be strictly positive, got {value}.")
        return value

    @field_validator(
        "worker_idle_ttl_sec",
        "turn_timeout_sec",
        "tg_max_message_chars",
        "stdout_buffer_limit_bytes",
    )
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"Setting integer value must be strictly positive, got {value}.")
        return value

    @field_validator("quota_auto_switch_threshold_pct")
    @classmethod
    def _validate_percentage(cls, value: int) -> int:
        if value < 0 or value > 100:
            raise ValueError(
                f"quota_auto_switch_threshold_pct must be between 0 and 100, got {value}."
            )
        return value


def parse_dynamic_settings(raw_rows: Mapping[str, Any]) -> DynamicSettings:
    missing_keys = [key for key in MANDATORY_SYSTEM_SETTING_KEYS if key not in raw_rows]
    if missing_keys:
        raise ConfigurationError(
            f"Missing mandatory keys in PostgreSQL system_settings table: {missing_keys}"
        )

    try:
        return DynamicSettings.model_validate(dict(raw_rows))
    except ValidationError as exc:
        raise ConfigurationError(
            f"Invalid dynamic settings in PostgreSQL system_settings table:\n{exc}"
        ) from exc
