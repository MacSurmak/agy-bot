import asyncio
import contextlib
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
        self._models_cache: dict[int, list[str]] = {}

    async def list_available_models(
        self, account: GoogleAccount, force_refresh: bool = False
    ) -> list[str]:
        if not force_refresh and account.id in self._models_cache:
            return list(self._models_cache[account.id])

        env = await self._keyring_mgr.ensure_account_bus(account)
        workspace_cwd = str(self._cfg.paths.workspaces_root_dir)
        self._cfg.paths.workspaces_root_dir.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            str(self._cfg.paths.agy_binary_path),
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_cwd,
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
        parsed = self._parse_models_output(raw_output)
        self._models_cache[account.id] = parsed
        return list(parsed)

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
        """Queries structured 5-hour and weekly quota buckets via `agy --print /usage --output-format json`."""
        env = await self._keyring_mgr.ensure_account_bus(account)
        workspace_cwd = str(self._cfg.paths.workspaces_root_dir)
        self._cfg.paths.workspaces_root_dir.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            str(self._cfg.paths.agy_binary_path),
            "--print",
            "/usage",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_cwd,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=25.0,
            )
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise AgyProcessError(
                f"agy --print /usage timed out for account '{account.slug}'."
            ) from exc

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not raw_stdout:
            raise AgyProcessError(
                f"agy --print /usage failed for account '{account.slug}' (code {proc.returncode}): {raw_stderr}"
            )

        return self._parse_usage_quota_output(raw_stdout if raw_stdout else raw_stderr)

    @classmethod
    def _parse_usage_quota_output(cls, raw_output: str) -> dict[str, Any]:
        buckets_list: list[dict[str, Any]] = []

        # Extract JSON object from stdout (in case multiple NDJSON lines or logs were emitted)
        for candidate_line in reversed([raw_output, *raw_output.splitlines()]):
            stripped = candidate_line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    cls._collect_quota_buckets_recursive(parsed, buckets_list)
                    if buckets_list:
                        break
            except json.JSONDecodeError:
                continue

        structured_buckets: dict[str, dict[str, Any]] = {}
        gemini_pcts: list[int] = []
        all_pcts: list[int] = []

        for b in buckets_list:
            b_id = str(b.get("id") or b.get("name") or b.get("label") or "").strip().lower()
            window = str(b.get("window") or "").strip().lower()
            frac = b.get("remaining_fraction")
            if isinstance(frac, int | float):
                pct = max(0, min(100, round(float(frac) * 100)))
            else:
                pct = 100
            reset_time = str(b.get("reset_time") or "").strip()

            key = b_id
            if not key:
                is_5h = "5h" in window or "five" in window or "hour" in window
                key = f"bucket_{'5h' if is_5h else 'weekly'}_{len(structured_buckets)}"

            structured_buckets[key] = {
                "id": b_id,
                "name": str(b.get("name") or b.get("label") or b_id),
                "window": window,
                "remaining_pct": pct,
                "reset_time": reset_time,
            }
            all_pcts.append(pct)
            if "gemini" in key or "gemini" in str(b.get("group_title", "")).lower():
                gemini_pcts.append(pct)

        if not all_pcts:
            all_pcts = [int(m.group(1)) for m in _PERCENT_PATTERN.finditer(raw_output)]

        min_remaining_pct = (
            min(gemini_pcts)
            if gemini_pcts
            else (min(all_pcts) if all_pcts else 100)
        )

        return {
            "buckets": structured_buckets,
            "min_remaining_pct": min_remaining_pct,
            "parsed_percentages": all_pcts,
            "raw_report": raw_output[:1500],
        }

    @classmethod
    def _collect_quota_buckets_recursive(
        cls,
        node: Any,
        out: list[dict[str, Any]],
        group_title: str = "",
    ) -> None:
        if isinstance(node, dict):
            current_group = str(
                node.get("title") or node.get("name") or node.get("label") or group_title
            )
            raw_buckets = node.get("buckets")
            if isinstance(raw_buckets, list):
                for item in raw_buckets:
                    if isinstance(item, dict) and (
                        "remaining_fraction" in item or "window" in item
                    ):
                        out.append({**item, "group_title": current_group})
            for val in node.values():
                if isinstance(val, dict | list):
                    cls._collect_quota_buckets_recursive(val, out, current_group)
        elif isinstance(node, list):
            for elem in node:
                if isinstance(elem, dict | list):
                    cls._collect_quota_buckets_recursive(elem, out, group_title)
