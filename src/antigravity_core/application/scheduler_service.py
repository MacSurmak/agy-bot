from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from antigravity_core.application.turn_orchestrator import TurnOrchestrator
from antigravity_core.domain.enums import NotifyPolicy, ScheduleKind, TurnStatus
from antigravity_core.domain.exceptions import ConfigurationError
from antigravity_core.domain.models import ScheduledTask, TurnProgressEvent
from antigravity_core.infrastructure.database.repositories import (
    ScheduleRepository,
    TopicRepository,
)

logger = structlog.get_logger(__name__)

ScheduledReportDeliverCallback = Callable[[int, str], Awaitable[None]]


class TemporalSchedulerService:
    """APScheduler service backed by PostgreSQL 18 `scheduled_tasks` SSOT table (no pickle serialization)."""

    def __init__(
        self,
        schedule_repo: ScheduleRepository,
        topic_repo: TopicRepository,
        turn_orchestrator: TurnOrchestrator,
        deliver_report_cb: ScheduledReportDeliverCallback,
    ) -> None:
        self._schedule_repo = schedule_repo
        self._topic_repo = topic_repo
        self._orchestrator = turn_orchestrator
        self._deliver_cb = deliver_report_cb
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
                    await self._schedule_repo.mark_task_executed(task.id, deactivate=True)
                    continue
            self._register_apscheduler_job(task)
            hydrated += 1

        logger.info("Started TemporalScheduler and hydrated tasks from PostgreSQL 18", count=hydrated)

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
        # Validate trigger specification before writing to database
        self._build_trigger(schedule_kind, trigger_spec)

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
            misfire_grace_time=300,
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
                raise ConfigurationError(f"Invalid 5-field cron expression '{spec}': {exc}") from exc
        raise ConfigurationError(f"Unsupported schedule_kind: {kind}")

    @staticmethod
    def _parse_iso_datetime(spec: str) -> datetime:
        try:
            dt = datetime.fromisoformat(spec.strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid ISO-8601 datetime specification '{spec}': {exc}"
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

        if task.conversation_id is None:
            await self._topic_repo.set_active_conversation(task.topic_id, None)
        else:
            await self._topic_repo.set_active_conversation(task.topic_id, task.conversation_id)

        if task.notify_policy == NotifyPolicy.ALERT_ONLY:
            wake_prompt = (
                f"[SILENT MONITORING TRIGGER | task_id={task.id} | time={now_iso}]\n"
                f"Execute monitoring check: {task.instruction_prompt}\n"
                "If all metrics and services are healthy and no anomaly is detected, "
                "respond with EXACTLY one word: HEARTBEAT_OK (the bot will suppress notification). "
                "If any anomaly, error, or threshold violation is found, provide a detailed alert report."
            )
        else:
            wake_prompt = (
                f"[SCHEDULED TASK TRIGGER | task_id={task.id} | time={now_iso}]\n"
                f"Execute the scheduled task and report to the user:\n{task.instruction_prompt}"
            )

        async def _noop_progress(_ev: TurnProgressEvent) -> None:
            return

        try:
            result = await self._orchestrator.execute_topic_turn(
                topic_id=task.topic_id,
                prompt=wake_prompt,
                on_progress=_noop_progress,
            )
            await self._schedule_repo.mark_task_executed(task.id, deactivate=is_one_shot)

            cleaned_resp = result.response_text.strip()
            if (
                task.notify_policy == NotifyPolicy.ALERT_ONLY
                and result.status == TurnStatus.SUCCESS
                and cleaned_resp == "HEARTBEAT_OK"
            ):
                logger.info("Silent scheduled check returned HEARTBEAT_OK", task_id=str(task.id))
                return

            header = (
                f"⏰ **Scheduled Task Report** (`{task.trigger_spec}`)\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )
            body = cleaned_resp if cleaned_resp else f"Status: `{result.status.value}`"
            await self._deliver_cb(task.topic_id, header + body)
        except Exception as exc:
            logger.error("Scheduled task execution failed", task_id=str(task.id), error=str(exc))
            await self._schedule_repo.mark_task_executed(task.id, deactivate=is_one_shot)
            await self._deliver_cb(
                task.topic_id,
                f"❌ **Scheduled Task Failed** (`{task.id}`):\n`{exc}`",
            )
