import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog
from telethon import TelegramClient

from antigravity_core.application.batcher import MessageBatcher
from antigravity_core.application.ipc_server import CoreIpcServer
from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.application.turn_orchestrator import TurnOrchestrator
from antigravity_core.config.static_config import StaticConfig, load_static_config
from antigravity_core.domain.exceptions import AntigravityError, StartupValidationError
from antigravity_core.infrastructure.agy_runtime.cli_inspector import AgyCliInspector
from antigravity_core.infrastructure.agy_runtime.environment_bootstrap import (
    AgyEnvironmentBootstrapper,
)
from antigravity_core.infrastructure.agy_runtime.history_reader import AgyHistoryReader
from antigravity_core.infrastructure.agy_runtime.worker_pool import AgyWorkerPool
from antigravity_core.infrastructure.crypto.fernet_vault import FernetVault
from antigravity_core.infrastructure.database.engine import DatabaseManager
from antigravity_core.infrastructure.database.repositories import (
    AccessControlRepository,
    AccountRepository,
    RemoteHostRepository,
    ScheduleRepository,
    SessionRepository,
    SettingsRepository,
    TopicRepository,
)
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import KeyringProfileManager
from antigravity_core.infrastructure.media.mtproto_media import (
    MediaService,
    TelethonForumGateway,
)
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager
from antigravity_core.presentation.control_panel_ui import ControlPanelUI
from antigravity_core.presentation.router import TelegramEventRouter
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI

logger = structlog.get_logger(__name__)


def configure_structlog(log_file_path: Path) -> None:
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(stdout_handler)

    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(file_handler)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


async def run_service(static_config: StaticConfig) -> None:
    # Step 1: Validate Fernet key and bootstrap ~/.gemini environment + WAL mode (Errata Patch #4)
    vault = FernetVault(static_config.security.fernet_master_key)
    bootstrapper = AgyEnvironmentBootstrapper(static_config)
    bootstrapper.bootstrap_and_validate()

    # Step 2: Verify PostgreSQL 18 connectivity and mandatory dynamic settings
    db_mgr = DatabaseManager(static_config.database)
    await db_mgr.verify_connectivity()

    settings_repo = SettingsRepository(db_mgr)
    account_repo = AccountRepository(db_mgr)
    host_repo = RemoteHostRepository(db_mgr)
    topic_repo = TopicRepository(db_mgr)
    session_repo = SessionRepository(db_mgr)
    schedule_repo = ScheduleRepository(db_mgr)
    access_repo = AccessControlRepository(db_mgr)

    await settings_repo.load_settings()
    await access_repo.ensure_owner_registered(
        owner_telegram_id=static_config.telegram.owner_telegram_id,
        username_label="owner",
    )

    # Step 3: Initialize isolated D-Bus & gnome-keyring-daemon buses (Errata Patch #5)
    keyring_mgr = KeyringProfileManager(static_config, vault, account_repo)
    enabled_accounts = await account_repo.list_enabled_accounts()
    for acc in enabled_accounts:
        await keyring_mgr.ensure_account_bus(acc)

    # Step 4: Mount registered SSHFS remote hosts with auto_cache (Errata Patch #3)
    mount_mgr = RemoteMountManager(static_config)
    hosts = await host_repo.list_hosts()
    for host in hosts:
        mount_mgr.ensure_rule_skeleton(host)
        if host.auto_mount_enabled:
            try:
                await mount_mgr.ensure_mounted(host)
            except Exception as exc:
                logger.error("Failed to auto-mount remote host at startup", alias=host.alias, error=str(exc))

    # Step 5: Connect Telethon MTProto client
    static_config.telegram.session_file_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(static_config.telegram.session_file_path),
        static_config.telegram.api_id,
        static_config.telegram.api_hash,
    )
    await client.start(bot_token=static_config.telegram.bot_token)

    # Step 6: Wire application services, WorkerPool, IPC server, and TemporalScheduler
    worker_pool = AgyWorkerPool(static_config, keyring_mgr)
    cli_inspector = AgyCliInspector(static_config, keyring_mgr)
    history_reader = AgyHistoryReader(static_config)
    media_service = MediaService(client, static_config, settings_repo)

    turn_orchestrator = TurnOrchestrator(
        worker_pool=worker_pool,
        cli_inspector=cli_inspector,
        settings_repo=settings_repo,
        topic_repo=topic_repo,
        account_repo=account_repo,
        session_repo=session_repo,
    )
    forum_gateway = TelethonForumGateway(client=client, static_config=static_config)
    topic_lifecycle = TopicLifecycleService(
        forum_gateway=forum_gateway,
        static_config=static_config,
        worker_pool=worker_pool,
        settings_repo=settings_repo,
        topic_repo=topic_repo,
        account_repo=account_repo,
    )

    workspace_ui = WorkspaceTopicUI(
        client=client,
        static_config=static_config,
        worker_pool=worker_pool,
        turn_orchestrator=turn_orchestrator,
        topic_lifecycle=topic_lifecycle,
        cli_inspector=cli_inspector,
        history_reader=history_reader,
        settings_repo=settings_repo,
        topic_repo=topic_repo,
        account_repo=account_repo,
        session_repo=session_repo,
    )

    scheduler_service = TemporalSchedulerService(
        static_config=static_config,
        schedule_repo=schedule_repo,
        settings_repo=settings_repo,
        topic_repo=topic_repo,
        turn_orchestrator=turn_orchestrator,
        deliver_report_cb=workspace_ui.deliver_scheduled_report,
    )

    ipc_server = CoreIpcServer(
        static_config=static_config,
        media_service=media_service,
        scheduler_service=scheduler_service,
        topic_lifecycle=topic_lifecycle,
        topic_repo=topic_repo,
        host_repo=host_repo,
        mount_mgr=mount_mgr,
    )

    control_panel_ui = ControlPanelUI(
        client=client,
        static_config=static_config,
        vault=vault,
        keyring_mgr=keyring_mgr,
        mount_mgr=mount_mgr,
        worker_pool=worker_pool,
        cli_inspector=cli_inspector,
        scheduler_service=scheduler_service,
        topic_lifecycle=topic_lifecycle,
        workspace_ui=workspace_ui,
        settings_repo=settings_repo,
        account_repo=account_repo,
        host_repo=host_repo,
        access_repo=access_repo,
    )

    batcher = MessageBatcher(
        settings_repo=settings_repo,
        on_flush=workspace_ui.execute_batched_prompt,
    )

    router = TelegramEventRouter(
        client=client,
        static_config=static_config,
        settings_repo=settings_repo,
        access_repo=access_repo,
        media_service=media_service,
        batcher=batcher,
        control_panel_ui=control_panel_ui,
        workspace_ui=workspace_ui,
    )
    router.register_handlers()

    await ipc_server.start()
    await scheduler_service.start_and_hydrate()
    await control_panel_ui.render_and_pin_dashboard()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Antigravity Telegram Core is online and ready.")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down Antigravity Telegram Core...")
        await scheduler_service.stop()
        await ipc_server.stop()
        await worker_pool.shutdown_all()
        await keyring_mgr.shutdown_all()
        await client.disconnect()
        await db_mgr.close()


def run() -> None:
    try:
        static_cfg = load_static_config()
        configure_structlog(static_cfg.paths.log_file_path)
        asyncio.run(run_service(static_cfg))
    except (StartupValidationError, AntigravityError) as exc:
        sys.stderr.write(f"FATAL STARTUP VALIDATION ERROR: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    run()
