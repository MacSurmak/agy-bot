from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import re
from uuid import UUID, uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import NotifyPolicy, ScheduleKind
from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.domain.models import ScheduledTask
from antigravity_core.infrastructure.database.repositories import ScheduleRepository

logger = structlog.get_logger(__name__)

ScheduledWakeAgentCallback = Callable[[int, str, UUID | None], Awaitable[None]]

_RELATIVE_TIME_RE = re.compile(r"^\+?\s*(\d+)\s*([smhd])$", re.IGNORECASE)


class TemporalSchedulerService:
    """APScheduler service backed by PostgreSQL 18 `scheduled_tasks` SSOT table (no pickle serialization)."""

    def __init__(
        self,
        static_config: StaticConfig,
        schedule_repo: ScheduleRepository,
        wake_agent_cb: ScheduledWakeAgentCallback,
    ) -> None:
        self._cfg = static_config
        self._schedule_repo = schedule_repo
        self._wake_agent_cb = wake_agent_cb
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    async def start_and_hydrate(self) -> None:
        self._scheduler.start()
        active_tasks = await self._schedule_repo.list_active_tasks(topic_id=None)
        now = datetime.now(tz=UTC)
        hydrated = 0

        for task in active_tasks:
            if task.schedule_kind == ScheduleKind.ONE_SHOT:
                run_dt = self._parse_iso_datetime(task.trigger_spec)
                if run_dt <= now:
                    await self._schedule_repo.mark_task_executed(
                        task.id, deactivate=True
                    )
                    continue
            self._register_apscheduler_job(task)
            hydrated += 1

        logger.info(
            "Started TemporalScheduler and hydrated tasks from PostgreSQL 18",
            count=hydrated,
        )

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def schedule_new_task(
        self,
        topic_id: int,
        conversation_id: UUID | None,
        schedule_kind: ScheduleKind,
        trigger_spec: str,
        instruction_prompt: str,
        notify_policy: NotifyPolicy,
    ) -> ScheduledTask:
        normalized_spec = trigger_spec.strip()
        if schedule_kind == ScheduleKind.ONE_SHOT:
            run_dt = self._parse_iso_datetime(normalized_spec)
            normalized_spec = run_dt.isoformat()
        else:
            self._build_trigger(schedule_kind, normalized_spec)
        trigger_spec = normalized_spec

        task_id = uuid4()
        task = await self._schedule_repo.create_task(
            task_id=task_id,
            topic_id=topic_id,
            conversation_id=conversation_id,
            schedule_kind=schedule_kind,
            trigger_spec=trigger_spec,
            instruction_prompt=instruction_prompt,
            notify_policy=notify_policy,
        )
        self._register_apscheduler_job(task)
        return task

    async def list_tasks(self, topic_id: int | None) -> list[ScheduledTask]:
        return await self._schedule_repo.list_active_tasks(topic_id=topic_id)

    async def cancel_task(self, task_id: UUID) -> ScheduledTask:
        job_id = str(task_id)
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)
        return await self._schedule_repo.cancel_task(task_id)

    def _register_apscheduler_job(self, task: ScheduledTask) -> None:
        trigger = self._build_trigger(task.schedule_kind, task.trigger_spec)
        self._scheduler.add_job(
            self._execute_scheduled_task,
            trigger=trigger,
            id=str(task.id),
            args=[task.id],
            replace_existing=True,
            misfire_grace_time=self._cfg.runtime.scheduler_misfire_grace_sec,
        )

    def _build_trigger(
        self, kind: ScheduleKind, spec: str
    ) -> DateTrigger | CronTrigger:
        if kind == ScheduleKind.ONE_SHOT:
            run_dt = self._parse_iso_datetime(spec)
            return DateTrigger(run_date=run_dt, timezone=UTC)
        if kind == ScheduleKind.CRON:
            try:
                return CronTrigger.from_crontab(spec.strip(), timezone=UTC)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Invalid 5-field cron expression '{spec}': {exc}"
                ) from exc
        raise ConfigurationError(f"Unsupported schedule_kind: {kind}")

    @staticmethod
    def _parse_iso_datetime(spec: str) -> datetime:
        cleaned = spec.strip()
        rel_match = _RELATIVE_TIME_RE.match(cleaned)
        if rel_match is not None:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2).lower()
            delta_map = {
                "s": timedelta(seconds=amount),
                "m": timedelta(minutes=amount),
                "h": timedelta(hours=amount),
                "d": timedelta(days=amount),
            }
            return datetime.now(tz=UTC) + delta_map[unit]

        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid datetime specification '{spec}' (use ISO-8601 or relative '+60s', '+5m', '+2h'): {exc}"
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    async def _execute_scheduled_task(self, task_id: UUID) -> None:
        task = await self._schedule_repo.get_task(task_id)
        if task is None or not task.is_active:
            return

        now_iso = datetime.now(tz=UTC).isoformat()
        is_one_shot = task.schedule_kind == ScheduleKind.ONE_SHOT
        wake_prompt = (
            f"[SCHEDULED TASK TRIGGER | task_id={task.id} | time={now_iso}]\n"
            f"Execute the scheduled task and report to the user:\n{task.instruction_prompt}"
        )

        logger.info(
            "Firing scheduled task in parallel worker with live trace",
            task_id=str(task.id),
            topic_id=task.topic_id,
            schedule_kind=task.schedule_kind.value,
        )
        await self._schedule_repo.mark_task_executed(task.id, deactivate=is_one_shot)
        await self._wake_agent_cb(
            task.topic_id,
            wake_prompt,
            task.conversation_id,
        )
