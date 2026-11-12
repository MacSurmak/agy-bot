from pathlib import Path
from uuid import UUID

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import EffortLevel, ExecutionMode, TopicKind
from antigravity_core.domain.models import ForumTopic, ForumTopicGateway
from antigravity_core.infrastructure.agy_runtime.worker import (
    is_effort_supported_for_model,
)
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.database.repositories import (
    AccountRepository,
    SettingsRepository,
    TopicRepository,
)
from antigravity_core.lexicon import get_lexicon

logger = structlog.get_logger(__name__)


class TopicLifecycleService:
    """Manages creation, session reset (/new), mode toggling, and archiving of Forum Topics."""

    def __init__(
        self,
        forum_gateway: ForumTopicGateway,
        static_config: StaticConfig,
        worker_pool: AgyWorkerPool,
        settings_repo: SettingsRepository,
        topic_repo: TopicRepository,
        account_repo: AccountRepository,
    ) -> None:
        self._forum_gateway = forum_gateway
        self._cfg = static_config
        self._pool = worker_pool
        self._settings_repo = settings_repo
        self._topic_repo = topic_repo
        self._account_repo = account_repo

    async def create_forum_topic_with_card(
        self,
        title: str,
        kind: TopicKind,
        workspace_path: str,
        execution_mode: ExecutionMode | None,
    ) -> ForumTopic:
        settings = await self._settings_repo.load_settings()
        default_account = await self._account_repo.get_default_account()

        # Do not create the directory on disk yet so switching folders leaves no empty directories
        resolved_workspace = str(Path(workspace_path).expanduser().resolve())

        topic_id = await self._forum_gateway.create_forum_topic(title=title)

        effective_mode = (
            execution_mode
            if execution_mode is not None
            else settings.default_execution_mode
        )
        effective_effort = (
            settings.default_effort
            if is_effort_supported_for_model(
                settings.default_model,
                self._cfg.runtime.effort_supported_prefixes,
            )
            else None
        )

        topic = await self._topic_repo.create_topic(
            topic_id=topic_id,
            title=title,
            kind=kind,
            workspace_path=resolved_workspace,
            additional_dirs=(),
            model_slug=settings.default_model,
            effort_level=effective_effort,
            execution_mode=effective_mode,
            assigned_account_id=default_account.id,
            pinned_card_msg_id=None,
        )
        logger.info("Created new Telegram Forum topic", topic_id=topic_id, title=title)
        return topic

    async def reset_topic_session(self, topic_id: int) -> ForumTopic:
        """Implements /new without deleting any Telegram messages: evicts warm worker and clears active UUID."""
        await self._pool.evict_topic_worker(topic_id)
        return await self._topic_repo.set_active_conversation(topic_id, None)

    async def resume_past_session(
        self, topic_id: int, conversation_id: UUID
    ) -> ForumTopic:
        await self._pool.evict_topic_worker(topic_id)
        return await self._topic_repo.set_active_conversation(topic_id, conversation_id)

    async def toggle_execution_mode(self, topic_id: int) -> ForumTopic:
        topic = await self._topic_repo.get_topic_strict(topic_id)
        new_mode = (
            ExecutionMode.READ_ONLY
            if topic.execution_mode == ExecutionMode.FULL_ACCESS
            else ExecutionMode.FULL_ACCESS
        )
        updated = await self._topic_repo.update_execution_mode(topic_id, new_mode)
        await self._pool.evict_topic_worker(topic_id)
        return updated

    async def toggle_sudo_carte_blanche(self, topic_id: int) -> ForumTopic:
        topic = await self._topic_repo.get_topic_strict(topic_id)
        return await self._topic_repo.set_sudo_carte_blanche(
            topic_id, not topic.sudo_carte_blanche
        )

    async def set_sudo_carte_blanche(self, topic_id: int, enabled: bool) -> ForumTopic:
        return await self._topic_repo.set_sudo_carte_blanche(topic_id, enabled)

    async def change_model(
        self, topic_id: int, model_slug: str, effort_level: EffortLevel | None
    ) -> ForumTopic:
        effective_effort = (
            effort_level
            if is_effort_supported_for_model(
                model_slug,
                self._cfg.runtime.effort_supported_prefixes,
            )
            else None
        )
        updated = await self._topic_repo.update_model_and_effort(
            topic_id=topic_id,
            model_slug=model_slug,
            effort_level=effective_effort,
        )
        await self._pool.evict_topic_worker(topic_id)
        return updated

    async def switch_topic_account(self, topic_id: int, account_id: int) -> ForumTopic:
        updated = await self._topic_repo.update_assigned_account(topic_id, account_id)
        await self._pool.evict_topic_worker(topic_id)
        return updated

    async def rename_topic(self, topic_id: int, new_title: str) -> ForumTopic:
        max_len = self._cfg.limits.topic_title_max_chars
        clean_title = new_title.strip()[:max_len]
        if not clean_title:
            return await self._topic_repo.get_topic_strict(topic_id)
        await self._forum_gateway.rename_forum_topic(
            topic_id=topic_id, new_title=clean_title
        )
        return await self._topic_repo.update_topic_title(
            topic_id=topic_id, new_title=clean_title
        )

    async def archive_topic(self, topic_id: int) -> ForumTopic:
        topic = await self._topic_repo.get_topic_strict(topic_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        await self._pool.evict_topic_worker(topic_id)

        max_len = self._cfg.limits.topic_title_max_chars
        archived_title = (
            topic.title
            if topic.title.startswith("🗄")
            else f"{lex.topic_archive_prefix}{topic.title}"
        )[:max_len]

        await self._forum_gateway.close_and_rename_forum_topic(
            topic_id=topic_id,
            new_title=archived_title,
        )
        return await self._topic_repo.archive_topic(topic_id, archived_title)
