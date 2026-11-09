import asyncio
from dataclasses import dataclass
from uuid import UUID

import structlog

from antigravity_core.config.dynamic_settings import DynamicSettings
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.models import (
    ForumTopic,
    GoogleAccount,
    TurnResult,
    WorkerSignature,
    WorkerTopicSnapshot,
)
from antigravity_core.infrastructure.agy_runtime.worker import (
    InitCallback,
    ProgressCallback,
    TopicWorker,
)
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import KeyringProfileManager

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class WorkerPoolSlot:
    topic_id: int
    turn_lock: asyncio.Lock
    worker: TopicWorker | None
    idle_timer_task: asyncio.Task[None] | None
    is_running_turn: bool


class AgyWorkerPool:
    """Manages warm persistent agy subprocesses indexed strictly by topic_id with TTL eviction."""

    def __init__(
        self,
        static_config: StaticConfig,
        keyring_mgr: KeyringProfileManager,
    ) -> None:
        self._cfg = static_config
        self._keyring_mgr = keyring_mgr
        self._slots: dict[int, WorkerPoolSlot] = {}
        self._registry_lock = asyncio.Lock()
        self._busy_conversations: set[UUID] = set()

    async def _get_or_create_slot(self, topic_id: int) -> WorkerPoolSlot:
        async with self._registry_lock:
            slot = self._slots.get(topic_id)
            if slot is None:
                slot = WorkerPoolSlot(
                    topic_id=topic_id,
                    turn_lock=asyncio.Lock(),
                    worker=None,
                    idle_timer_task=None,
                    is_running_turn=False,
                )
                self._slots[topic_id] = slot
            return slot

    @staticmethod
    def _build_signature(topic: ForumTopic, account: GoogleAccount) -> WorkerSignature:
        return WorkerSignature(
            conversation_id=topic.active_conversation_id,
            model_slug=topic.model_slug,
            effort_level=topic.effort_level,
            execution_mode=topic.execution_mode,
            account_id=account.id,
            account_slug=account.slug,
            dbus_socket_path=account.dbus_socket_path,
            xdg_data_dir=account.xdg_data_dir,
            workspace_path=topic.workspace_path,
            additional_dirs=topic.additional_dirs,
        )

    @staticmethod
    def _signatures_compatible(current: WorkerSignature, desired: WorkerSignature) -> bool:
        # Conversation ID may transition from None -> UUID on the first turn without requiring a restart
        conv_ok = (
            current.conversation_id == desired.conversation_id
            or (current.conversation_id is None and desired.conversation_id is not None)
        )
        return (
            conv_ok
            and current.model_slug == desired.model_slug
            and current.effort_level == desired.effort_level
            and current.execution_mode == desired.execution_mode
            and current.account_id == desired.account_id
            and current.workspace_path == desired.workspace_path
            and current.additional_dirs == desired.additional_dirs
        )

    async def execute_turn(
        self,
        topic: ForumTopic,
        account: GoogleAccount,
        prompt: str,
        settings: DynamicSettings,
        on_init: InitCallback,
        on_progress: ProgressCallback,
        initial_cumulative_tokens: int = 0,
    ) -> TurnResult:
        slot = await self._get_or_create_slot(topic.topic_id)

        async with slot.turn_lock:
            self._cancel_idle_timer(slot)
            slot.is_running_turn = True
            try:
                desired_sig = self._build_signature(topic, account)
                if (
                    slot.worker is not None
                    and slot.worker.is_alive()
                    and not self._signatures_compatible(slot.worker.signature, desired_sig)
                ):
                    logger.info(
                        "Topic configuration changed; cycling warm agy worker",
                        topic_id=topic.topic_id,
                    )
                    await slot.worker.close_gracefully()
                    slot.worker = None

                if slot.worker is None or not slot.worker.is_alive():
                    account_env = await self._keyring_mgr.ensure_account_bus(account)
                    slot.worker = TopicWorker(
                        topic_id=topic.topic_id,
                        signature=desired_sig,
                        static_config=self._cfg,
                        account_env=account_env,
                        initial_cumulative_tokens=initial_cumulative_tokens,
                    )
                    await slot.worker.start(settings)

                active_cid = desired_sig.conversation_id
                if active_cid is not None:
                    self._busy_conversations.add(active_cid)
                try:
                    result = await slot.worker.execute_turn(
                        prompt=prompt,
                        settings=settings,
                        on_init=on_init,
                        on_progress=on_progress,
                    )
                    return result
                finally:
                    if active_cid is not None:
                        self._busy_conversations.discard(active_cid)
            finally:
                slot.is_running_turn = False
                if slot.worker is not None and slot.worker.is_alive():
                    slot.idle_timer_task = asyncio.create_task(
                        self._run_idle_ttl_timer(topic.topic_id, settings.worker_idle_ttl_sec)
                    )

    def _cancel_idle_timer(self, slot: WorkerPoolSlot) -> None:
        if slot.idle_timer_task is not None and not slot.idle_timer_task.done():
            slot.idle_timer_task.cancel()
        slot.idle_timer_task = None

    async def _run_idle_ttl_timer(self, topic_id: int, ttl_sec: int) -> None:
        try:
            await asyncio.sleep(float(ttl_sec))
            slot = await self._get_or_create_slot(topic_id)
            async with slot.turn_lock:
                if not slot.is_running_turn and slot.worker is not None:
                    logger.info(
                        "Evicting idle warm agy worker after TTL expiration",
                        topic_id=topic_id,
                        ttl_sec=ttl_sec,
                    )
                    await slot.worker.close_gracefully()
                    slot.worker = None
        except asyncio.CancelledError:
            return

    async def execute_ephemeral_turn(
        self,
        topic: ForumTopic,
        account: GoogleAccount,
        prompt: str,
        settings: DynamicSettings,
        conversation_id_override: UUID | None,
        on_progress: ProgressCallback | None,
        initial_cumulative_tokens: int,
    ) -> TurnResult:
        """Executes an isolated background/scheduled turn in parallel with full live progress streaming."""
        conv_id = (
            conversation_id_override
            if conversation_id_override is not None
            and conversation_id_override not in self._busy_conversations
            else None
        )
        if conv_id is not None:
            self._busy_conversations.add(conv_id)
        sig = WorkerSignature(
            conversation_id=conv_id,
            model_slug=topic.model_slug,
            effort_level=None,
            execution_mode=topic.execution_mode,
            account_id=account.id,
            account_slug=account.slug,
            dbus_socket_path=account.dbus_socket_path,
            xdg_data_dir=account.xdg_data_dir,
            workspace_path=topic.workspace_path,
            additional_dirs=topic.additional_dirs,
        )
        account_env = await self._keyring_mgr.ensure_account_bus(account)
        ephemeral_worker = TopicWorker(
            topic_id=topic.topic_id,
            signature=sig,
            static_config=self._cfg,
            account_env=account_env,
            initial_cumulative_tokens=initial_cumulative_tokens,
        )

        async def _noop_init(_cid: UUID) -> None:
            return

        async def _noop_prog(_ev: object) -> None:
            return

        effective_prog: ProgressCallback = (
            on_progress if on_progress is not None else _noop_prog
        )

        try:
            await ephemeral_worker.start(settings)
            return await ephemeral_worker.execute_turn(
                prompt=prompt,
                settings=settings,
                on_init=_noop_init,
                on_progress=effective_prog,
            )
        finally:
            if conv_id is not None:
                self._busy_conversations.discard(conv_id)
            await ephemeral_worker.close_gracefully()

    async def cancel_active_turn(self, topic_id: int) -> bool:
        slot = await self._get_or_create_slot(topic_id)
        self._cancel_idle_timer(slot)
        if slot.worker is not None and slot.worker.is_alive():
            await slot.worker.terminate_forcefully()
            slot.worker = None
            return True
        return False

    async def evict_topic_worker(self, topic_id: int) -> None:
        slot = await self._get_or_create_slot(topic_id)
        self._cancel_idle_timer(slot)
        async with slot.turn_lock:
            if slot.worker is not None:
                await slot.worker.close_gracefully()
                slot.worker = None

    async def get_pool_snapshots(self) -> list[WorkerTopicSnapshot]:
        async with self._registry_lock:
            snapshots: list[WorkerTopicSnapshot] = []
            for topic_id, slot in sorted(self._slots.items()):
                worker = slot.worker
                is_warm = worker is not None and worker.is_alive()
                snapshots.append(
                    WorkerTopicSnapshot(
                        topic_id=topic_id,
                        is_warm=is_warm,
                        is_running_turn=slot.is_running_turn,
                        model_slug=worker.signature.model_slug if is_warm and worker else None,
                        account_slug=worker.signature.account_slug if is_warm and worker else None,
                    )
                )
            return snapshots

    async def shutdown_all(self) -> None:
        async with self._registry_lock:
            slots = list(self._slots.values())
        for slot in slots:
            self._cancel_idle_timer(slot)
            if slot.worker is not None:
                try:
                    await slot.worker.terminate_forcefully()
                except OSError as exc:
                    logger.warning(
                        "Error terminating worker during pool shutdown",
                        topic_id=slot.topic_id,
                        error=str(exc),
                    )
                slot.worker = None
