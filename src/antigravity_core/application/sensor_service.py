"""Zero-Token Event Sensor & Stream Watcher Engine for waking up agy on real-time infrastructure events."""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import UTC, datetime
import os
import re
import signal
from uuid import UUID, uuid4

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import (
    NotifyPolicy,
    SensorKind,
    SensorTriggerCondition,
    SensorWakeMode,
)
from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.domain.models import EventSensor
from antigravity_core.infrastructure.database.repositories import (
    SensorRepository,
    SettingsRepository,
)
from antigravity_core.lexicon import get_lexicon

logger = structlog.get_logger(__name__)

SensorWakeAgentCallback = Callable[[int, str, str, UUID | None], Awaitable[None]]
SensorReflexDeliverCallback = Callable[[int, str, str], Awaitable[None]]


class EventSensorService:
    """Runs lightweight asyncio watchers (poll commands and live log streams) that consume 0 LLM tokens until triggered."""

    def __init__(
        self,
        static_config: StaticConfig,
        sensor_repo: SensorRepository,
        settings_repo: SettingsRepository,
        wake_agent_cb: SensorWakeAgentCallback,
        deliver_reflex_cb: SensorReflexDeliverCallback,
    ) -> None:
        self._cfg = static_config
        self._sensor_repo = sensor_repo
        self._settings_repo = settings_repo
        self._wake_agent_cb = wake_agent_cb
        self._deliver_reflex_cb = deliver_reflex_cb
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._procs: dict[UUID, asyncio.subprocess.Process] = {}

    async def start_and_hydrate(self) -> None:
        active_sensors = await self._sensor_repo.list_active_sensors(topic_id=None)
        for sensor in active_sensors:
            self._spawn_sensor_task(sensor)
        logger.info(
            "Started EventSensorService and hydrated active sensors",
            count=len(active_sensors),
        )

    async def stop(self) -> None:
        sensor_ids = list(self._tasks.keys())
        for sid in sensor_ids:
            await self._stop_sensor_runtime(sid)

    async def create_new_sensor(
        self,
        topic_id: int,
        conversation_id: UUID | None,
        name: str,
        sensor_kind: SensorKind,
        command: str,
        trigger_condition: SensorTriggerCondition,
        match_pattern: str | None,
        action_command: str | None,
        wake_mode: SensorWakeMode,
        poll_interval_sec: int,
        cooldown_sec: int,
        one_shot: bool,
        instruction_prompt: str,
        notify_policy: NotifyPolicy,
    ) -> EventSensor:
        clean_cmd = command.strip()
        if not clean_cmd:
            raise ConfigurationError("Sensor command cannot be empty.")

        if (
            trigger_condition == SensorTriggerCondition.ON_REGEX_MATCH
            or sensor_kind == SensorKind.STREAM_WATCH
        ) and match_pattern:
            try:
                re.compile(match_pattern)
            except re.error as exc:
                raise ConfigurationError(
                    f"Invalid regex in match_pattern '{match_pattern}': {exc}"
                ) from exc

        effective_poll = max(2, poll_interval_sec)
        effective_cooldown = max(5, cooldown_sec)

        sensor_id = uuid4()
        clean_action = (
            action_command.strip()
            if action_command and action_command.strip()
            else None
        )
        sensor = await self._sensor_repo.create_sensor(
            sensor_id=sensor_id,
            topic_id=topic_id,
            conversation_id=conversation_id,
            name=name.strip() or f"Sensor #{str(sensor_id)[:6]}",
            sensor_kind=sensor_kind,
            command=clean_cmd,
            trigger_condition=trigger_condition,
            match_pattern=match_pattern,
            action_command=clean_action,
            wake_mode=wake_mode,
            poll_interval_sec=effective_poll,
            cooldown_sec=effective_cooldown,
            one_shot=one_shot,
            instruction_prompt=instruction_prompt.strip(),
            notify_policy=notify_policy,
        )
        self._spawn_sensor_task(sensor)
        logger.info(
            "Registered and started new event sensor",
            sensor_id=str(sensor.id),
            name=sensor.name,
            kind=sensor.sensor_kind.value,
            condition=sensor.trigger_condition.value,
            topic_id=sensor.topic_id,
        )
        return sensor

    async def list_sensors(self, topic_id: int | None) -> list[EventSensor]:
        return await self._sensor_repo.list_active_sensors(topic_id=topic_id)

    async def cancel_sensor(self, sensor_id: UUID) -> EventSensor:
        await self._stop_sensor_runtime(sensor_id)
        canceled = await self._sensor_repo.cancel_sensor(sensor_id)
        logger.info(
            "Canceled event sensor", sensor_id=str(sensor_id), name=canceled.name
        )
        return canceled

    def _spawn_sensor_task(self, sensor: EventSensor) -> None:
        existing = self._tasks.get(sensor.id)
        if existing is not None and not existing.done():
            existing.cancel()

        if sensor.sensor_kind == SensorKind.STREAM_WATCH:
            coro = self._run_stream_watch_loop(sensor.id)
        else:
            coro = self._run_poll_command_loop(sensor.id)

        task = asyncio.create_task(coro, name=f"sensor-{sensor.id}")
        self._tasks[sensor.id] = task

    async def _stop_sensor_runtime(self, sensor_id: UUID) -> None:
        proc = self._procs.pop(sensor_id, None)
        if proc is not None and proc.returncode is None:
            self._kill_process_group(proc)

        task = self._tasks.pop(sensor_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)

    async def _run_poll_command_loop(self, sensor_id: UUID) -> None:
        baseline_output: str | None = None
        baseline_captured = False
        was_condition_active = False
        last_fire_monotonic = 0.0
        loop = asyncio.get_running_loop()

        try:
            while True:
                sensor = await self._sensor_repo.get_sensor(sensor_id)
                if sensor is None or not sensor.is_active:
                    return

                returncode, output = await self._exec_probe_command(
                    sensor_id=sensor.id,
                    command=sensor.command,
                    timeout_sec=max(15.0, float(sensor.poll_interval_sec)),
                )

                condition_matched = False
                cond = sensor.trigger_condition

                if cond == SensorTriggerCondition.ON_EXIT_ZERO:
                    condition_matched = returncode == 0
                elif cond == SensorTriggerCondition.ON_EXIT_NONZERO:
                    condition_matched = returncode != 0
                elif cond == SensorTriggerCondition.ON_OUTPUT_CHANGE:
                    if not baseline_captured:
                        baseline_output = output
                        baseline_captured = True
                    elif output != baseline_output:
                        baseline_output = output
                        condition_matched = True
                elif cond == SensorTriggerCondition.ON_REGEX_MATCH:
                    pat = sensor.match_pattern or ".+"
                    condition_matched = (
                        re.search(pat, output, re.IGNORECASE | re.MULTILINE) is not None
                    )

                now_mono = loop.time()
                if condition_matched:
                    # Fire immediately on rising edge (False -> True), or after cooldown_sec if condition stays continuously True
                    cooldown_elapsed = (now_mono - last_fire_monotonic) >= float(
                        sensor.cooldown_sec
                    )
                    if (not was_condition_active) or cooldown_elapsed:
                        was_condition_active = True
                        last_fire_monotonic = now_mono
                        if sensor.one_shot:
                            await self._handle_sensor_trigger(
                                sensor=sensor,
                                captured_output=output
                                or f"[Command exited with code {returncode}]",
                            )
                            return
                        # Run wake-up handler in background task so poll loop keeps watching for state resets
                        asyncio.create_task(
                            self._handle_sensor_trigger(
                                sensor=sensor,
                                captured_output=output
                                or f"[Command exited with code {returncode}]",
                            )
                        )
                else:
                    # Condition cleared (e.g. PyMOL closed): immediately rearm the sensor!
                    was_condition_active = False

                await asyncio.sleep(float(sensor.poll_interval_sec))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Poll sensor loop crashed",
                sensor_id=str(sensor_id),
                error=str(exc),
                exc_info=True,
            )
        finally:
            self._tasks.pop(sensor_id, None)
            self._procs.pop(sensor_id, None)

    async def _run_stream_watch_loop(self, sensor_id: UUID) -> None:
        try:
            while True:
                sensor = await self._sensor_repo.get_sensor(sensor_id)
                if sensor is None or not sensor.is_active:
                    return

                pattern = re.compile(
                    sensor.match_pattern if sensor.match_pattern else ".+",
                    re.IGNORECASE,
                )

                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    "-c",
                    sensor.command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                self._procs[sensor.id] = proc

                try:
                    if proc.stdout is None:
                        break
                    while True:
                        raw_line = await proc.stdout.readline()
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip()
                        if not line:
                            continue

                        if pattern.search(line) is not None:
                            captured_lines = [line]
                            # Drain burst of context lines (e.g. stack trace) over 0.35s
                            with contextlib.suppress(TimeoutError):
                                async with asyncio.timeout(0.35):
                                    while len(captured_lines) < 25:
                                        extra = await proc.stdout.readline()
                                        if not extra:
                                            break
                                        captured_lines.append(
                                            extra.decode(
                                                "utf-8", errors="replace"
                                            ).rstrip()
                                        )

                            await self._handle_sensor_trigger(
                                sensor=sensor,
                                captured_output="\n".join(captured_lines),
                            )
                            if sensor.one_shot:
                                self._kill_process_group(proc)
                                return
                            await asyncio.sleep(float(sensor.cooldown_sec))
                finally:
                    self._procs.pop(sensor.id, None)
                    self._kill_process_group(proc)
                    with contextlib.suppress(OSError):
                        await proc.wait()

                # If stream closed (e.g. remote server rebooted), reconnect after 5s if still active
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Stream sensor loop crashed",
                sensor_id=str(sensor_id),
                error=str(exc),
                exc_info=True,
            )
        finally:
            self._tasks.pop(sensor_id, None)
            proc = self._procs.pop(sensor_id, None)
            if proc is not None:
                self._kill_process_group(proc)

    async def _exec_probe_command(
        self, sensor_id: UUID, command: str, timeout_sec: float
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._procs[sensor_id] = proc
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec,
            )
            rc = proc.returncode if proc.returncode is not None else 1
            out = stdout_bytes.decode("utf-8", errors="replace").strip()
            return rc, out
        except TimeoutError:
            self._kill_process_group(proc)
            with contextlib.suppress(OSError):
                await proc.wait()
            return 124, "[Sensor probe command timed out]"
        finally:
            self._procs.pop(sensor_id, None)

    async def _handle_sensor_trigger(
        self, sensor: EventSensor, captured_output: str
    ) -> None:
        now_iso = datetime.now(tz=UTC).isoformat()
        await self._sensor_repo.mark_sensor_triggered(
            sensor_id=sensor.id,
            deactivate=sensor.one_shot,
        )

        # Stage 1: Immediate millisecond reflex execution (0 LLM tokens)
        action_rc: int | None = None
        action_output: str = ""
        if sensor.action_command:
            logger.info(
                "Executing immediate sensor reflex action_command",
                sensor_id=str(sensor.id),
                name=sensor.name,
                action_command=sensor.action_command,
            )
            action_rc, action_output = await self._exec_probe_command(
                sensor_id=sensor.id,
                command=sensor.action_command,
                timeout_sec=60.0,
            )

        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)

        # Stage 2: Evaluate wake_mode (NEVER or ON_ACTION_FAILURE when reflex succeeded)
        if sensor.wake_mode == SensorWakeMode.NEVER:
            if sensor.notify_policy == NotifyPolicy.ALWAYS or (
                action_rc is not None and action_rc != 0
            ):
                reflex_summary = [
                    lex.sensor_reflex_never_report.format(
                        name=sensor.name, event_output=captured_output[:600]
                    )
                ]
                if sensor.action_command:
                    reflex_summary.append(
                        lex.sensor_reflex_action_line.format(
                            rc=action_rc, action_command=sensor.action_command
                        )
                    )
                    if action_output:
                        reflex_summary.append(f"```\n{action_output[:2000]}\n```")
                await self._deliver_reflex_cb(
                    sensor.topic_id, sensor.name, "\n".join(reflex_summary)
                )
            return

        if (
            sensor.wake_mode == SensorWakeMode.ON_ACTION_FAILURE
            and sensor.action_command
            and action_rc == 0
        ):
            logger.info(
                "Sensor reflex action_command succeeded (exit 0); skipping AI wake-up per ON_ACTION_FAILURE",
                sensor_id=str(sensor.id),
                name=sensor.name,
            )
            if sensor.notify_policy == NotifyPolicy.ALWAYS:
                msg_lines = [
                    lex.sensor_reflex_handled_report.format(
                        name=sensor.name, action_command=sensor.action_command
                    )
                ]
                if action_output:
                    msg_lines.append(f"```\n{action_output[:1500]}\n```")
                await self._deliver_reflex_cb(
                    sensor.topic_id, sensor.name, "\n".join(msg_lines)
                )
            return

        # Stage 3: Wake up agy agent with both sensor event output and reflex dump output
        logger.info(
            "Event sensor TRIGGERED; waking up agy agent",
            sensor_id=str(sensor.id),
            name=sensor.name,
            topic_id=sensor.topic_id,
            condition=sensor.trigger_condition.value,
            one_shot=sensor.one_shot,
            action_rc=action_rc,
        )

        trimmed_capture = captured_output[:2000]
        reflex_section = ""
        if sensor.action_command:
            reflex_section = (
                f"Immediate reflex action_command executed (exit code {action_rc}): {sensor.action_command}\n"
                f"Captured reflex output / dump:\n{action_output[:3000]}\n\n"
            )

        wake_prompt = (
            f"[EVENT SENSOR TRIGGERED | name='{sensor.name}' | condition={sensor.trigger_condition.value} | time={now_iso}]\n"
            f"Sensor command: {sensor.command}\n"
            f"Captured event output:\n{trimmed_capture}\n\n"
            f"{reflex_section}"
            f"Execute instruction and report to the user:\n{sensor.instruction_prompt}"
        )

        await self._wake_agent_cb(
            sensor.topic_id,
            sensor.name,
            wake_prompt,
            sensor.conversation_id,
        )
