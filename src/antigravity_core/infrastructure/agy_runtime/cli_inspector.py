import asyncio
import json
import re
from typing import Any

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import GoogleAccount
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import KeyringProfileManager

logger = structlog.get_logger(__name__)

_PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")
# Universal model slug pattern: lowercase alphanumeric with hyphens/dots, requiring at least one hyphen and at least one digit
_GENERIC_MODEL_SLUG_RE = re.compile(r"^(?=.*\d)[a-z0-9]+(?:-[a-z0-9.]+)+$")
_IGNORED_HEADER_WORDS: frozenset[str] = frozenset(
    {"model", "models", "available", "name", "slug", "id", "default", "provider", "status"}
)


def is_valid_model_slug_syntax(slug: str) -> bool:
    return _GENERIC_MODEL_SLUG_RE.match(slug.strip().lower()) is not None


class AgyCliInspector:
    """Executes one-shot CLI queries (`agy models` and `agy -p /quota`) per isolated account profile."""

    def __init__(
        self,
        static_config: StaticConfig,
        keyring_mgr: KeyringProfileManager,
    ) -> None:
        self._cfg = static_config
        self._keyring_mgr = keyring_mgr

    async def list_available_models(self, account: GoogleAccount) -> list[str]:
        env = await self._keyring_mgr.ensure_account_bus(account)
        proc = await asyncio.create_subprocess_exec(
            str(self._cfg.paths.agy_binary_path),
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cfg.runtime.command_exec_timeout_sec,
            )
        except TimeoutError as exc:
            proc.kill()
            raise AgyProcessError("agy models command timed out.") from exc

        if proc.returncode != 0:
            raise AgyProcessError(
                f"agy models failed (code {proc.returncode}): "
                f"{stderr_bytes.decode('utf-8', errors='replace')}"
            )

        raw_output = stdout_bytes.decode("utf-8", errors="replace").strip()
        return self._parse_models_output(raw_output)

    @staticmethod
    def _parse_models_output(raw_output: str) -> list[str]:
        if not raw_output:
            raise AgyProcessError("agy models returned empty output.")

        models: list[str] = []
        seen: set[str] = set()

        # Support structured JSON output if emitted by agy
        if raw_output.startswith(("[", "{")):
            try:
                parsed_json: Any = json.loads(raw_output)
                items = (
                    parsed_json
                    if isinstance(parsed_json, list)
                    else parsed_json.get("models", [])
                    if isinstance(parsed_json, dict)
                    else []
                )
                for item in items:
                    slug = (
                        str(item.get("slug") or item.get("id") or item.get("name") or "").strip()
                        if isinstance(item, dict)
                        else str(item).strip()
                    )
                    if slug and slug not in seen:
                        seen.add(slug)
                        models.append(slug)
                if models:
                    return models
            except json.JSONDecodeError as exc:
                logger.debug(
                    "agy models output started with bracket/brace but is not JSON; falling back to line parser",
                    error=str(exc),
                )

        # Universal table / list parser for any model family (no hardcoded gemini/claude prefixes)
        for raw_line in raw_output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "=", "-", "─", "┌", "└", "├")):
                continue
            cleaned_line = line.strip("│|*•- \t")
            if not cleaned_line:
                continue
            tokens = [tok.strip("│|,*()[]\"'`") for tok in cleaned_line.split()]
            for tok in tokens:
                candidate = tok.lower()
                if candidate in _IGNORED_HEADER_WORDS:
                    continue
                if _GENERIC_MODEL_SLUG_RE.match(candidate) and candidate not in seen:
                    seen.add(candidate)
                    models.append(candidate)
                    break

        if not models:
            raise AgyProcessError(f"Could not parse any model slugs from agy models output:\n{raw_output}")
        return models

    async def fetch_account_quota(self, account: GoogleAccount) -> dict[str, Any]:
        # Reference #1 Section 3: /quota must be executed as a standalone `agy -p /quota` invocation
        env = await self._keyring_mgr.ensure_account_bus(account)
        proc = await asyncio.create_subprocess_exec(
            str(self._cfg.paths.agy_binary_path),
            "-p",
            "/quota",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cfg.runtime.command_exec_timeout_sec,
            )
        except TimeoutError as exc:
            proc.kill()
            raise AgyProcessError(f"agy -p /quota timed out for account '{account.slug}'.") from exc

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not raw_stdout:
            raise AgyProcessError(
                f"agy -p /quota failed for account '{account.slug}' (code {proc.returncode}): {raw_stderr}"
            )

        combined = raw_stdout if raw_stdout else raw_stderr
        percentages = [int(m.group(1)) for m in _PERCENT_PATTERN.finditer(combined)]
        min_remaining_pct = min(percentages) if percentages else 100

        return {
            "raw_report": combined,
            "min_remaining_pct": min_remaining_pct,
            "parsed_percentages": percentages,
        }
