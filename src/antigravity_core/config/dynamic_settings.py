from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from antigravity_core.config.constants import (
    DEBOUNCE_WINDOW_SEC,
    MAX_BATCH_WINDOW_SEC,
    QUOTA_AUTO_SWITCH_THRESHOLD_PCT,
    STDOUT_BUFFER_LIMIT_BYTES,
    TG_EDIT_THROTTLE_SEC,
    TG_MAX_MESSAGE_CHARS,
    TURN_TIMEOUT_SEC,
    WORKER_IDLE_TTL_SEC,
)
from antigravity_core.domain.enums import EffortLevel, ExecutionMode
from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.lexicon import SUPPORTED_LANGUAGES

MANDATORY_SYSTEM_SETTING_KEYS: tuple[str, ...] = (
    "ui_language",
    "default_model",
    "default_effort",
    "default_execution_mode",
)


class DynamicSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ui_language: str
    default_model: str
    default_effort: EffortLevel | None
    default_execution_mode: ExecutionMode

    @field_validator("ui_language")
    @classmethod
    def _validate_ui_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Unsupported ui_language '{value}'. Must be one of {SUPPORTED_LANGUAGES}."
            )
        return normalized

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("default_model cannot be empty.")
        return stripped

    @property
    def debounce_window_sec(self) -> float:
        return DEBOUNCE_WINDOW_SEC

    @property
    def max_batch_window_sec(self) -> float:
        return MAX_BATCH_WINDOW_SEC

    @property
    def worker_idle_ttl_sec(self) -> int:
        return WORKER_IDLE_TTL_SEC

    @property
    def turn_timeout_sec(self) -> int:
        return TURN_TIMEOUT_SEC

    @property
    def tg_edit_throttle_sec(self) -> float:
        return TG_EDIT_THROTTLE_SEC

    @property
    def tg_max_message_chars(self) -> int:
        return TG_MAX_MESSAGE_CHARS

    @property
    def stdout_buffer_limit_bytes(self) -> int:
        return STDOUT_BUFFER_LIMIT_BYTES

    @property
    def quota_auto_switch_threshold_pct(self) -> int:
        return QUOTA_AUTO_SWITCH_THRESHOLD_PCT


def parse_dynamic_settings(raw_rows: Mapping[str, Any]) -> DynamicSettings:
    missing_keys = [key for key in MANDATORY_SYSTEM_SETTING_KEYS if key not in raw_rows]
    if missing_keys:
        raise ConfigurationError(
            f"Missing mandatory keys in PostgreSQL system_settings table: {missing_keys}"
        )

    filtered = {k: raw_rows[k] for k in MANDATORY_SYSTEM_SETTING_KEYS}
    try:
        return DynamicSettings.model_validate(filtered)
    except ValidationError as exc:
        raise ConfigurationError(
            f"Invalid dynamic settings in PostgreSQL system_settings table:\n{exc}"
        ) from exc
