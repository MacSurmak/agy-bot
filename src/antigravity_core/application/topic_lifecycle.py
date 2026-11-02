from pathlib import Path
from uuid import UUID

import structlog
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateForumTopicRequest, EditForumTopicRequest
from telethon.tl.types import Updates

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import EffortLevel, ExecutionMode, TopicKind
from antigravity_core.domain.exceptions import AntigravityError
from antigravity_core.domain.models import ForumTopic
from antigravity_core.infrastructure.agy_runtime.worker import is_effort_supported_for_model
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
        client: TelegramClient,
        static_config: StaticConfig,
        worker_pool: AgyWorkerPool,
        settings_repo: SettingsRepository,
        topic_repo: TopicRepository,
        account_repo: AccountRepository,
    ) -> None:
        self._client = client
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

        resolved_workspace = str(Path(workspace_path).expanduser().resolve())
        Path(resolved_workspace).mkdir(parents=True, exist_ok=True)

        updates = await self._client(
            CreateForumTopicRequest(
                channel=self._cfg.telegram.forum_chat_id,
                title=title,
            )
        )
        topic_id = self._extract_created_topic_id(updates)

        effective_mode = execution_mode if execution_mode is not None else settings.default_execution_mode
        effective_effort = (
            settings.default_effort
            if is_effort_supported_for_model(settings.default_model)
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

    @staticmethod
    def _extract_created_topic_id(updates: Updates) -> int:
        for upd in getattr(updates, "updates", []):
            msg = getattr(upd, "message", None)
            if msg is not None and hasattr(msg, "id"):
                return int(msg.id)
        raise AntigravityError("Failed to extract created topic_id from CreateForumTopicRequest response.")

    async def reset_topic_session(self, topic_id: int) -> ForumTopic:
        """Implements /new without deleting any Telegram messages: evicts warm worker and clears active UUID."""
        await self._pool.evict_topic_worker(topic_id)
        return await self._topic_repo.set_active_conversation(topic_id, None)

    async def resume_past_session(self, topic_id: int, conversation_id: UUID) -> ForumTopic:
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

    async def change_model(
        self, topic_id: int, model_slug: str, effort_level: EffortLevel | None
    ) -> ForumTopic:
        effective_effort = effort_level if is_effort_supported_for_model(model_slug) else None
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

    async def archive_topic(self, topic_id: int) -> ForumTopic:
        topic = await self._topic_repo.get_topic_strict(topic_id)
        settings = await self._settings_repo.load_settings()
        lex = get_lexicon(settings.ui_language)
        await self._pool.evict_topic_worker(topic_id)

        archived_title = (
            topic.title
            if topic.title.startswith("🗄")
            else f"{lex.topic_archive_prefix}{topic.title}"
        )[:120]

        await self._client(
            EditForumTopicRequest(
                channel=self._cfg.telegram.forum_chat_id,
                topic_id=topic_id,
                title=archived_title,
                closed=True,
            )
        )
        return await self._topic_repo.archive_topic(topic_id, archived_title)
