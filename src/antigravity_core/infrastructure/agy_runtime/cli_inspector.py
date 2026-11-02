import asyncio
import re
from typing import Any

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import GoogleAccount
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import KeyringProfileManager

_PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")
_MODEL_SLUG_PATTERN = re.compile(r"\b((?:gemini|claude)-[a-z0-9.\-]+)\b")


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

        raw_output = stdout_bytes.decode("utf-8", errors="replace")
        models: list[str] = []
        seen: set[str] = set()
        for match in _MODEL_SLUG_PATTERN.finditer(raw_output):
            slug = match.group(1)
            if slug not in seen:
                seen.add(slug)
                models.append(slug)

        if not models:
            for line in raw_output.splitlines():
                token = line.strip().split()[0] if line.strip() else ""
                if token and not token.startswith(("#", "-", "Available", "Model")):
                    if token not in seen:
                        seen.add(token)
                        models.append(token)
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
