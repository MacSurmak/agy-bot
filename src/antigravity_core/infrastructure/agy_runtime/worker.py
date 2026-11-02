import asyncio
import contextlib
from dataclasses import replace
import os
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import structlog

from antigravity_core.config.dynamic_settings import DynamicSettings
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode, StepState, TurnStatus
from antigravity_core.domain.exceptions import AgyProcessError, QuotaExhaustedError
from antigravity_core.domain.models import (
    SubagentState,
    ToolCallSummary,
    TurnProgressEvent,
    TurnResult,
    WorkerSignature,
)
from antigravity_core.infrastructure.agy_runtime.stream_protocol import (
    ParsedInitEvent,
    ParsedStepUpdateEvent,
    parse_stream_json_line,
    serialize_user_prompt,
)

logger = structlog.get_logger(__name__)

ProgressCallback = Callable[[TurnProgressEvent], Awaitable[None]]
InitCallback = Callable[[UUID], Awaitable[None]]

_QUOTA_ERROR_MARKERS: tuple[str, ...] = (
    "resourceexhausted",
    "resource_exhausted",
    "quota exceeded",
    "quota_exceeded",
    "rate limit exceeded",
    "429 too many requests",
    "5-hour quota",
    "weekly quota",
)


def is_effort_supported_for_model(
    model_slug: str, supported_prefixes: tuple[str, ...]
) -> bool:
    """Checks whether --effort is supported by matching model_slug against configured prefixes."""
    normalized = model_slug.strip().lower()
    return any(normalized.startswith(prefix) for prefix in supported_prefixes)


def _contains_quota_exhaustion(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _QUOTA_ERROR_MARKERS)


class TopicWorker:
    """Manages a single persistent warm `agy` subprocess for a specific Telegram Forum topic."""

    def __init__(
        self,
        topic_id: int,
        signature: WorkerSignature,
        static_config: StaticConfig,
        account_env: dict[str, str],
        initial_cumulative_tokens: int = 0,
    ) -> None:
        self._topic_id = topic_id
        self._signature = signature
        self._cfg = static_config
        self._account_env = dict(account_env)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []
        self._initial_cumulative_tokens: int = max(0, initial_cumulative_tokens)
        self._cumulative_tokens: int = self._initial_cumulative_tokens
        self._cumulative_duration_sec: float = 0.0
        self._conversation_id: UUID | None = signature.conversation_id

    @property
    def topic_id(self) -> int:
        return self._topic_id

    @property
    def signature(self) -> WorkerSignature:
        return self._signature

    @property
    def conversation_id(self) -> UUID | None:
        return self._conversation_id

    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, settings: DynamicSettings) -> None:
        if self.is_alive():
            return

        workspace_dir = Path(self._signature.workspace_path)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command_args(settings)
        # Errata Patch #1: Inject topic, execution mode, and IPC socket variables for child processes & hooks
        worker_env = {
            **self._account_env,
            "AGY_CURRENT_TOPIC_ID": str(self._topic_id),
            "AGY_EXECUTION_MODE": self._signature.execution_mode.value,
            "AGY_ACCOUNT_SLUG": self._signature.account_slug,
            "AGY_CORE_IPC_SOCKET": str(self._cfg.paths.ipc_socket_path),
        }

        self._stderr_tail.clear()
        self._cumulative_tokens = self._initial_cumulative_tokens
        self._cumulative_duration_sec = 0.0

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace_dir),
                env=worker_env,
                limit=settings.stdout_buffer_limit_bytes,
                start_new_session=True,
            )
        except OSError as exc:
            raise AgyProcessError(
                f"Failed to spawn agy worker for topic {self._topic_id}: {exc}"
            ) from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info(
            "Spawned warm agy worker process",
            topic_id=self._topic_id,
            pid=self._process.pid,
            model=self._signature.model_slug,
            mode=self._signature.execution_mode.value,
            account=self._signature.account_slug,
            conversation_id=str(self._conversation_id) if self._conversation_id else None,
        )

    def _build_command_args(self, settings: DynamicSettings) -> list[str]:
        log_file = self._cfg.paths.log_file_path.parent / f"agy_topic_{self._topic_id}.log"
        cmd: list[str] = [
            str(self._cfg.paths.agy_binary_path),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            self._signature.model_slug,
            "--print-timeout",
            f"{settings.turn_timeout_sec}s",
            "--log-file",
            str(log_file),
        ]

        # Note: agy model slugs already encode reasoning effort (e.g. gemini-3.8-flash-low/medium/high);
        # passing a separate --effort flag causes mismatch errors when switching model variants.

        # Reference #1 Section 5: Deterministic execution profiles
        if self._signature.execution_mode == ExecutionMode.FULL_ACCESS:
            cmd.extend(["--mode", "accept-edits", "--dangerously-skip-permissions"])
        else:
            cmd.extend(["--mode", "plan"])

        if self._conversation_id is not None:
            cmd.extend(["--conversation", str(self._conversation_id)])

        seen_dirs: set[str] = {str(Path(self._signature.workspace_path).resolve())}
        for extra_dir in self._signature.additional_dirs:
            resolved = str(Path(extra_dir).resolve())
            if resolved not in seen_dirs and Path(resolved).exists():
                seen_dirs.add(resolved)
                cmd.extend(["--add-dir", resolved])

        return cmd

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                line_bytes = await self._process.stderr.readline()
                if not line_bytes:
                    break
                text = line_bytes.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > self._cfg.runtime.stderr_tail_max_lines:
                        self._stderr_tail.pop(0)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.debug("Worker stderr reader closed", topic_id=self._topic_id, error=str(exc))
            return

    def get_stderr_summary(self) -> str:
        limit = self._cfg.runtime.stderr_summary_lines
        return "\n".join(self._stderr_tail[-limit:])

    async def execute_turn(
        self,
        prompt: str,
        settings: DynamicSettings,
        on_init: InitCallback,
        on_progress: ProgressCallback,
    ) -> TurnResult:
        if not self.is_alive():
            await self.start(settings)

        proc = self._process
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise AgyProcessError("agy worker process streams are not initialized.")

        encoded_prompt = serialize_user_prompt(prompt)
        try:
            proc.stdin.write(encoded_prompt)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            await self.terminate_forcefully()
            raise AgyProcessError(
                f"Failed to write prompt to agy stdin (stderr: {self.get_stderr_summary()}): {exc}"
            ) from exc

        turn_start_monotonic = time.monotonic()
        accumulated_text = ""
        accumulated_thinking = ""
        tools_by_step: dict[int, ToolCallSummary] = {}
        active_subagents: dict[str, SubagentState] = {}

        watchdog_sec = float(settings.turn_timeout_sec) + self._cfg.runtime.turn_watchdog_grace_sec
        try:
            async with asyncio.timeout(watchdog_sec):
                while True:
                    raw_line_bytes = await proc.stdout.readline()
                    if not raw_line_bytes:
                        stderr_tail = self.get_stderr_summary()
                        await self.terminate_forcefully()
                        if _contains_quota_exhaustion(stderr_tail):
                            raise QuotaExhaustedError(
                                account_slug=self._signature.account_slug,
                                model_slug=self._signature.model_slug,
                                details=stderr_tail,
                            )
                        raise AgyProcessError(
                            f"agy process closed stdout unexpectedly before emitting 'result'. "
                            f"Exit code: {proc.returncode}. Stderr:\n{stderr_tail}"
                        )

                    raw_line = raw_line_bytes.decode("utf-8", errors="replace")
                    event = parse_stream_json_line(
                        raw_line,
                        prev_cumulative_tokens=self._cumulative_tokens,
                        prev_cumulative_duration_sec=self._cumulative_duration_sec,
                        target_hint_max_chars=self._cfg.limits.tool_target_hint_max_chars,
                        fallback_conversation_id=self._conversation_id,
                    )

                    if isinstance(event, ParsedInitEvent):
                        self._conversation_id = event.conversation_id
                        logger.info(
                            "Initialized agy session",
                            topic_id=self._topic_id,
                            conversation_id=str(event.conversation_id),
                            model=event.model,
                            cwd=event.cwd,
                        )
                        await on_init(event.conversation_id)
                        continue

                    if isinstance(event, ParsedStepUpdateEvent):
                        self._conversation_id = event.conversation_id
                        if event.thinking_delta is not None:
                            if accumulated_thinking and not accumulated_thinking.endswith(
                                ("\n", " ")
                            ):
                                accumulated_thinking += "\n"
                            accumulated_thinking += event.thinking_delta
                        if event.text_delta is not None:
                            accumulated_text += event.text_delta
                        if event.tool_summary is not None:
                            prev_tool = tools_by_step.get(event.tool_summary.step_index)
                            if prev_tool is not None:
                                merged_params = (
                                    event.tool_summary.parameters_text
                                    or prev_tool.parameters_text
                                )
                                merged_output = (
                                    event.tool_summary.output_text or prev_tool.output_text
                                )
                                merged_hint = (
                                    event.tool_summary.target_hint or prev_tool.target_hint
                                )
                                tools_by_step[event.tool_summary.step_index] = replace(
                                    event.tool_summary,
                                    target_hint=merged_hint,
                                    parameters_text=merged_params,
                                    output_text=merged_output,
                                )
                            else:
                                tools_by_step[event.tool_summary.step_index] = event.tool_summary
                            t_sum = tools_by_step[event.tool_summary.step_index]
                            logger.info(
                                "Agent tool step",
                                topic_id=self._topic_id,
                                step=t_sum.step_index,
                                tool=t_sum.tool_name,
                                state=t_sum.state.value,
                                target=t_sum.target_hint,
                                duration_sec=t_sum.duration_seconds,
                            )
                        for sub in event.subagents:
                            if sub.conversation_id not in active_subagents:
                                logger.info(
                                    "Agent spawned subagent",
                                    topic_id=self._topic_id,
                                    role=sub.role,
                                    type_name=sub.type_name,
                                    subagent_id=sub.conversation_id,
                                )
                            active_subagents[sub.conversation_id] = sub

                        progress_event = TurnProgressEvent(
                            conversation_id=event.conversation_id,
                            step_index=event.step_index,
                            step_type=event.step_type,
                            state=event.state,
                            thinking_text=accumulated_thinking.strip() or None,
                            text_accumulated=accumulated_text,
                            active_tools=tuple(tools_by_step.values()),
                            active_subagents=tuple(active_subagents.values()),
                        )
                        await on_progress(progress_event)
                        continue

                    if isinstance(event, TurnResult):
                        self._conversation_id = event.conversation_id
                        self._cumulative_tokens = event.cumulative_usage.total_tokens
                        self._cumulative_duration_sec = event.cumulative_duration_sec

                        has_dangling_active_tool = any(
                            t.state == StepState.ACTIVE for t in tools_by_step.values()
                        )
                        if event.status != TurnStatus.SUCCESS or has_dangling_active_tool:
                            # Recycle worker process if turn was interrupted or cut off mid-tool by timeout
                            await self.terminate_forcefully()

                        if event.status == TurnStatus.ERROR:
                            combined_err = (
                                f"{event.error_message or ''}\n{self.get_stderr_summary()}".strip()
                            )
                            if _contains_quota_exhaustion(combined_err):
                                raise QuotaExhaustedError(
                                    account_slug=self._signature.account_slug,
                                    model_slug=self._signature.model_slug,
                                    details=combined_err,
                                )
                        actual_turn_sec = max(0.01, time.monotonic() - turn_start_monotonic)
                        logger.info(
                            "Completed agy turn",
                            topic_id=self._topic_id,
                            conversation_id=str(event.conversation_id),
                            status=event.status.value,
                            turn_sec=round(actual_turn_sec, 2),
                            turn_tokens=event.turn_delta_tokens,
                            session_tokens=event.cumulative_usage.total_tokens,
                            cached_tokens=event.cumulative_usage.cache_read_tokens,
                            tools_used=len(tools_by_step),
                        )
                        return replace(
                            event,
                            turn_delta_duration_sec=actual_turn_sec,
                            thinking_text=accumulated_thinking.strip() or None,
                            tool_calls=tuple(tools_by_step.values()),
                            subagents=tuple(active_subagents.values()),
                        )
        except TimeoutError as exc:
            await self.terminate_forcefully()
            raise AgyProcessError(
                f"Turn timed out after {settings.turn_timeout_sec}s in topic {self._topic_id}."
            ) from exc

    async def close_gracefully(self) -> None:
        proc = self._process
        if proc is None:
            return
        if proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=self._cfg.runtime.process_kill_grace_sec,
                )
            except TimeoutError:
                await self.terminate_forcefully()
                return

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        self._process = None

    async def terminate_forcefully(self) -> None:
        proc = self._process
        if proc is None:
            return
        if proc.returncode is None:
            grace = self._cfg.runtime.process_kill_grace_sec
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    with contextlib.suppress(OSError):
                        await proc.wait()

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        self._process = None
