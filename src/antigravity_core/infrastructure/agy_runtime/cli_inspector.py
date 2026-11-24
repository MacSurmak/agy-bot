import asyncio
import contextlib
import json
import re
import time
from typing import Final

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import AgyProcessError
from antigravity_core.domain.models import GoogleAccount, QuotaJsonValue
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import (
    KeyringProfileManager,
)

type JsonValue = (
    str
    | int
    | float
    | bool
    | None
    | list[JsonValue]
    | dict[str, JsonValue]
)

_PERCENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_EMAIL_DETECT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([a-zA-Z0-9_.+-]+@(?:gmail\.com|googlemail\.com|[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+))\b"
)
_TEXT_REFRESH_RE: Final[re.Pattern[str]] = re.compile(
    r"Refreshes\s+in\s+([0-9dhms\s]+)", re.IGNORECASE
)
_COLUMN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\t+|\s{2,}")
_VALID_MODEL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:[A-Za-z0-9]+(?:-[A-Za-z0-9.]+)+|Orchestrator\s*\([A-Za-z0-9_.-]+\))$"
)
_MODELS_CACHE_TTL_SEC: Final[float] = 300.0
_IGNORED_HEADER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "model",
        "models",
        "available",
        "name",
        "slug",
        "id",
        "default",
        "provider",
        "status",
        "description",
    }
)


def is_valid_model_slug_syntax(slug: str) -> bool:
    cleaned = slug.strip()
    return _VALID_MODEL_ID_RE.match(cleaned) is not None


class AgyCliInspector:
    """Executes one-shot CLI queries (`agy models` and `agy -p /quota`) per isolated account profile."""

    def __init__(
        self,
        static_config: StaticConfig,
        keyring_mgr: KeyringProfileManager,
    ) -> None:
        self._cfg = static_config
        self._keyring_mgr = keyring_mgr
        self._models_cache: dict[int, tuple[float, list[str]]] = {}

    async def list_available_models(
        self, account: GoogleAccount
    ) -> list[str]:
        if account.id in self._models_cache:
            cached_ts, cached_models = self._models_cache[account.id]
            if (time.monotonic() - cached_ts) < _MODELS_CACHE_TTL_SEC:
                return list(cached_models)

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
        self._models_cache[account.id] = (time.monotonic(), parsed)
        return list(parsed)

    @staticmethod
    def _extract_models_from_json(raw_output: str) -> list[str]:
        if not (raw_output.startswith("[") and raw_output.endswith("]")) and not (
            raw_output.startswith("{") and raw_output.endswith("}")
        ):
            return []
        parsed_json: JsonValue = json.loads(raw_output)
        items: list[JsonValue] = []
        if isinstance(parsed_json, list):
            items = parsed_json
        elif (
            isinstance(parsed_json, dict)
            and "models" in parsed_json
            and isinstance(parsed_json["models"], list)
        ):
            items = parsed_json["models"]

        models: list[str] = []
        seen: set[str] = set()
        for item in items:
            slug = ""
            if isinstance(item, dict):
                for key in ("slug", "id", "name"):
                    if key in item:
                        val = item[key]
                        if isinstance(val, str) and val.strip():
                            slug = val.strip()
                            break
            elif isinstance(item, str):
                slug = item.strip()
            if slug and slug not in seen:
                seen.add(slug)
                models.append(slug)
        return models

    @classmethod
    def _parse_models_output(cls, raw_output: str) -> list[str]:
        if not raw_output:
            raise AgyProcessError("agy models returned empty output.")

        json_models = cls._extract_models_from_json(raw_output)
        if json_models:
            return json_models

        models: list[str] = []
        seen: set[str] = set()

        for raw_line in raw_output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "=", "-", "─", "┌", "└", "├")):
                continue
            cleaned_line = line.strip("│|*•- \t")
            if not cleaned_line:
                continue

            columns = _COLUMN_SPLIT_RE.split(cleaned_line, maxsplit=1)
            first_col = columns[0].strip("│|,*[]\"'` ")
            if (
                first_col
                and first_col.lower() not in _IGNORED_HEADER_WORDS
                and is_valid_model_slug_syntax(first_col)
                and first_col not in seen
            ):
                seen.add(first_col)
                models.append(first_col)
                continue

            for tok in cleaned_line.split():
                candidate = tok.strip("│|,*()[]\"'`")
                if (
                    candidate
                    and candidate.lower() not in _IGNORED_HEADER_WORDS
                    and is_valid_model_slug_syntax(candidate)
                    and candidate not in seen
                ):
                    seen.add(candidate)
                    models.append(candidate)
                    break

        if not models:
            raise AgyProcessError(
                f"Could not parse any model slugs from agy models output:\n{raw_output}"
            )
        return models

    async def fetch_account_quota(
        self, account: GoogleAccount
    ) -> dict[str, QuotaJsonValue]:
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
                timeout=self._cfg.runtime.agy_quota_exec_timeout_sec,
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

    @staticmethod
    def _extract_dict_str(item: dict[str, JsonValue], keys: tuple[str, ...]) -> str:
        for k in keys:
            if k in item:
                val = item[k]
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    @classmethod
    def _format_single_quota_bucket(
        cls,
        b: dict[str, JsonValue],
        index: int,
    ) -> tuple[str, dict[str, int | float | str], int]:
        b_id = cls._extract_dict_str(b, ("id", "name", "label")).lower()
        b_name = cls._extract_dict_str(b, ("name", "label", "id"))
        window = cls._extract_dict_str(b, ("window",)).lower()
        group_title = cls._extract_dict_str(b, ("group_title",))
        frac = b["remaining_fraction"] if "remaining_fraction" in b else None
        pct = (
            max(0, min(100, round(float(frac) * 100)))
            if isinstance(frac, int | float)
            else 100
        )
        reset_time = cls._extract_dict_str(b, ("reset_time",))

        key = f"{group_title}_{b_id}_{index}".strip("_").lower() if group_title else b_id
        if not key:
            is_5h = "5h" in window or "five" in window or "hour" in window
            key = f"bucket_{'5h' if is_5h else 'weekly'}_{index}"

        entry: dict[str, int | float | str] = {
            "id": b_id,
            "name": b_name if b_name else b_id,
            "group_title": group_title,
            "window": window,
            "remaining_pct": pct,
            "reset_time": reset_time,
        }
        return key, entry, pct

    @staticmethod
    def _parse_text_quota_fallback(
        raw_output: str,
    ) -> dict[str, dict[str, int | float | str]]:
        """Parses plain-text `agy /usage` output dynamically by section headers and progress bars."""
        structured: dict[str, dict[str, int | float | str]] = {}
        current_group = "Models"
        current_limit_name = "Limit"
        lines = raw_output.splitlines()

        for idx, raw_ln in enumerate(lines):
            stripped = raw_ln.strip()
            if not stripped or stripped.startswith(("│", "└", "─", "Account:", "Models within", "For paid")):
                continue
            if stripped.isupper() and "%" not in stripped and "[" not in stripped:
                current_group = stripped.title()
                continue
            if "Limit" in stripped and "[" not in stripped and "%" not in stripped:
                current_limit_name = stripped
                continue
            pct_match = _PERCENT_PATTERN.search(stripped)
            if pct_match is not None and "[" in stripped:
                pct = max(0, min(100, round(float(pct_match.group(1)))))
                rst_str = ""
                if idx + 1 < len(lines):
                    ref_match = _TEXT_REFRESH_RE.search(lines[idx + 1])
                    if ref_match is not None:
                        rst_str = ref_match.group(1).strip()
                b_key = f"text_bucket_{len(structured)}"
                structured[b_key] = {
                    "id": b_key,
                    "name": current_limit_name,
                    "group_title": current_group,
                    "window": current_limit_name.lower(),
                    "remaining_pct": pct,
                    "reset_time": rst_str,
                }
        return structured

    @classmethod
    def _parse_usage_quota_output(
        cls, raw_output: str
    ) -> dict[str, QuotaJsonValue]:
        buckets_list: list[dict[str, JsonValue]] = []

        for candidate_line in reversed([raw_output, *raw_output.splitlines()]):
            stripped = candidate_line.strip()
            if not (stripped.startswith("{") and stripped.endswith("}")):
                continue
            parsed: JsonValue = json.loads(stripped)
            if isinstance(parsed, dict):
                cls._collect_quota_buckets_recursive(parsed, buckets_list, "")
                if buckets_list:
                    break

        structured_buckets: dict[str, dict[str, int | float | str]] = {}
        all_pcts: list[int] = []

        for b in buckets_list:
            key, entry, pct = cls._format_single_quota_bucket(
                b, len(structured_buckets)
            )
            structured_buckets[key] = entry
            all_pcts.append(pct)

        if not structured_buckets:
            structured_buckets = cls._parse_text_quota_fallback(raw_output)
            for b_val in structured_buckets.values():
                raw_p = b_val["remaining_pct"] if "remaining_pct" in b_val else 100
                if isinstance(raw_p, int | float):
                    all_pcts.append(int(raw_p))

        if not all_pcts:
            all_pcts = [
                round(float(m.group(1))) for m in _PERCENT_PATTERN.finditer(raw_output)
            ]

        min_remaining_pct = min(all_pcts) if all_pcts else 100

        email_match = _EMAIL_DETECT_RE.search(raw_output)
        detected_email = email_match.group(1).lower() if email_match is not None else ""

        return {
            "buckets": structured_buckets,
            "min_remaining_pct": min_remaining_pct,
            "parsed_percentages": all_pcts,
            "detected_email": detected_email,
            "raw_report": raw_output[:1500],
        }

    @classmethod
    def _collect_quota_buckets_recursive(
        cls,
        node: JsonValue,
        out: list[dict[str, JsonValue]],
        group_title: str,
    ) -> None:
        if isinstance(node, dict):
            extracted_title = cls._extract_dict_str(node, ("title", "name", "label"))
            current_group = extracted_title if extracted_title else group_title
            raw_buckets = node["buckets"] if "buckets" in node else None
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
