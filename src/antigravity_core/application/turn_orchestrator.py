from dataclasses import replace
from uuid import UUID

import structlog

from antigravity_core.config.dynamic_settings import DynamicSettings
from antigravity_core.domain.exceptions import (
    AgyProcessError,
    DatabaseIntegrityError,
    QuotaExhaustedError,
)
from antigravity_core.domain.models import ForumTopic, GoogleAccount, TurnResult
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.worker import ProgressCallback
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.database.repositories import (
    AccountRepository,
    SessionRepository,
    SettingsRepository,
    TopicRepository,
)

logger = structlog.get_logger(__name__)


class TurnOrchestrator:
    """Executes turns in a topic, persists session metrics, and performs seamless quota failover."""

    def __init__(
        self,
        worker_pool: AgyWorkerPool,
        cli_inspector: AgyCliInspector,
        settings_repo: SettingsRepository,
        topic_repo: TopicRepository,
        account_repo: AccountRepository,
        session_repo: SessionRepository,
    ) -> None:
        self._pool = worker_pool
        self._inspector = cli_inspector
        self._settings_repo = settings_repo
        self._topic_repo = topic_repo
        self._account_repo = account_repo
        self._session_repo = session_repo

    async def execute_topic_turn(
        self,
        topic_id: int,
        prompt: str,
        on_progress: ProgressCallback,
    ) -> TurnResult:
        settings = await self._settings_repo.load_settings()
        topic = await self._topic_repo.get_topic_strict(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)

        switched_from: str | None = None
        switched_to: str | None = None

        # Pre-flight proactive quota threshold check if cached quota is below threshold
        if account.last_quota_json is not None:
            min_pct = int(account.last_quota_json.get("min_remaining_pct", 100))
            if min_pct < settings.quota_auto_switch_threshold_pct:
                alt = await self._select_failover_account(
                    exclude_account_id=account.id,
                    threshold_pct=settings.quota_auto_switch_threshold_pct,
                )
                if alt is not None:
                    switched_from = account.slug
                    switched_to = alt.slug
                    topic = await self._topic_repo.update_assigned_account(topic.topic_id, alt.id)
                    account = alt

        try:
            result = await self._run_single_attempt(
                topic=topic,
                account=account,
                prompt=prompt,
                settings=settings,
                on_progress=on_progress,
            )
        except QuotaExhaustedError as exc:
            logger.warning(
                "Quota exhausted during turn; attempting automatic account failover",
                topic_id=topic.topic_id,
                exhausted_account=account.slug,
                error=exc.details,
            )
            alt_account = await self._select_failover_account(
                exclude_account_id=account.id,
                threshold_pct=settings.quota_auto_switch_threshold_pct,
            )
            if alt_account is None:
                raise

            switched_from = account.slug
            switched_to = alt_account.slug
            topic = await self._topic_repo.update_assigned_account(topic.topic_id, alt_account.id)
            result = await self._run_single_attempt(
                topic=topic,
                account=alt_account,
                prompt=prompt,
                settings=settings,
                on_progress=on_progress,
            )

        if switched_from is not None and switched_to is not None:
            result = replace(
                result,
                account_switched_from=switched_from,
                account_switched_to=switched_to,
            )
        return result

    async def execute_scheduled_turn(
        self,
        topic_id: int,
        prompt: str,
        conversation_id: UUID | None,
        on_progress: ProgressCallback | None = None,
    ) -> TurnResult:
        """Runs a scheduled/sensor turn in parallel with full live progress callbacks and session metrics."""
        settings = await self._settings_repo.load_settings()
        topic = await self._topic_repo.get_topic_strict(topic_id)
        account = await self._account_repo.get_by_id(topic.assigned_account_id)

        prev_tokens = 0
        if conversation_id is not None:
            existing_sess = await self._session_repo.get_session(conversation_id)
            if existing_sess is not None:
                prev_tokens = existing_sess.cumulative_tokens

        result = await self._pool.execute_ephemeral_turn(
            topic=topic,
            account=account,
            prompt=prompt,
            settings=settings,
            conversation_id_override=conversation_id,
            on_progress=on_progress,
            initial_cumulative_tokens=prev_tokens,
        )
        await self._session_repo.ensure_session_initialized(
            conversation_id=result.conversation_id,
            topic_id=topic.topic_id,
            account_id=account.id,
            workspace_path=topic.workspace_path,
        )
        await self._session_repo.record_turn_completion(
            conversation_id=result.conversation_id,
            account_id=account.id,
            turn_delta_tokens=result.turn_delta_tokens,
            turn_delta_duration_sec=result.turn_delta_duration_sec,
        )
        return result

    async def _run_single_attempt(
        self,
        topic: ForumTopic,
        account: GoogleAccount,
        prompt: str,
        settings: DynamicSettings,
        on_progress: ProgressCallback,
    ) -> TurnResult:
        async def _on_init(conv_id: UUID) -> None:
            await self._topic_repo.set_active_conversation(topic.topic_id, conv_id)
            await self._session_repo.ensure_session_initialized(
                conversation_id=conv_id,
                topic_id=topic.topic_id,
                account_id=account.id,
                workspace_path=topic.workspace_path,
            )

        prev_tokens = 0
        if topic.active_conversation_id is not None:
            existing_sess = await self._session_repo.get_session(topic.active_conversation_id)
            if existing_sess is not None:
                prev_tokens = existing_sess.cumulative_tokens

        result = await self._pool.execute_turn(
            topic=topic,
            account=account,
            prompt=prompt,
            settings=settings,
            on_init=_on_init,
            on_progress=on_progress,
            initial_cumulative_tokens=prev_tokens,
        )

        # If the 1-hour OAuth access_token expired mid-flight before generating a response,
        # the worker has already been recycled; retry once so the new worker refreshes the token seamlessly.
        if (
            not result.response_text.strip()
            and result.error_message
            and (
                "UNAUTHENTICATED" in result.error_message.upper()
                or "401" in result.error_message
            )
        ):
            logger.info(
                "OAuth access_token expired (401); retrying turn with freshly spawned agy worker",
                topic_id=topic.topic_id,
                account=account.slug,
            )
            result = await self._pool.execute_turn(
                topic=topic,
                account=account,
                prompt=prompt,
                settings=settings,
                on_init=_on_init,
                on_progress=on_progress,
                initial_cumulative_tokens=prev_tokens,
            )

        await self._topic_repo.set_active_conversation(topic.topic_id, result.conversation_id)
        await self._session_repo.ensure_session_initialized(
            conversation_id=result.conversation_id,
            topic_id=topic.topic_id,
            account_id=account.id,
            workspace_path=topic.workspace_path,
        )
        await self._session_repo.record_turn_completion(
            conversation_id=result.conversation_id,
            account_id=account.id,
            turn_delta_tokens=result.turn_delta_tokens,
            turn_delta_duration_sec=result.turn_delta_duration_sec,
        )
        return result

    async def _select_failover_account(
        self, exclude_account_id: int, threshold_pct: int
    ) -> GoogleAccount | None:
        try:
            candidates = await self._account_repo.list_enabled_accounts()
        except DatabaseIntegrityError:
            return None

        for candidate in candidates:
            if candidate.id == exclude_account_id:
                continue
            try:
                quota_info = await self._inspector.fetch_account_quota(candidate)
                await self._account_repo.update_quota_snapshot(candidate.id, quota_info)
                if int(quota_info.get("min_remaining_pct", 100)) >= threshold_pct:
                    return candidate
            except AgyProcessError as exc:
                logger.warning(
                    "Failover candidate quota probe failed",
                    candidate_slug=candidate.slug,
                    error=str(exc),
                )
                continue
        return None
