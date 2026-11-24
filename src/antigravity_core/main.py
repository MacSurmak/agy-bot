import asyncio
import contextlib
import signal
import sys
from typing import Final

from loguru import logger
from telethon import TelegramClient

from antigravity_core.application.batcher import MessageBatcher
from antigravity_core.application.ipc_server import CoreIpcServer
from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.sensor_service import EventSensorService
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
    SensorRepository,
    SessionRepository,
    SettingsRepository,
    SshCredentialRepository,
    TopicRepository,
)
from antigravity_core.infrastructure.keyring.dbus_profile_mgr import (
    KeyringProfileManager,
)
from antigravity_core.infrastructure.media.mtproto_media import (
    MediaService,
    TelethonForumGateway,
)
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager
from antigravity_core.logging_setup import configure_loguru
from antigravity_core.presentation.control_panel_ui import ControlPanelUI
from antigravity_core.presentation.router import TelegramEventRouter
from antigravity_core.presentation.workspace_topic_ui import WorkspaceTopicUI

_MTPROTO_CONNECTION_RETRIES: Final[int] = 1_000_000
_MTPROTO_RETRY_DELAY_SEC: Final[int] = 3
_WATCHDOG_CHECK_INTERVAL_SEC: Final[float] = 15.0
_MAX_CONSECUTIVE_DISCONNECT_CHECKS: Final[int] = 6


async def _wait_with_mtproto_watchdog(
    client: TelegramClient, stop_event: asyncio.Event
) -> None:
    """Monitors MTProto connection health and triggers reconnect or fatal restart if permanently wedged."""
    disconnected_ticks = 0
    while not stop_event.is_set():
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            [stop_task],
            timeout=_WATCHDOG_CHECK_INTERVAL_SEC,
        )
        if done:
            break
        for pending_task in pending:
            pending_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_task
        if client.is_connected():
            disconnected_ticks = 0
            continue
        disconnected_ticks += 1
        logger.warning(
            "MTProto watchdog detected disconnected TelegramClient (tick {}/{}); attempting reconnect",
            disconnected_ticks,
            _MAX_CONSECUTIVE_DISCONNECT_CHECKS,
        )
        with contextlib.suppress(OSError, ConnectionError):
            await client.connect()
        if client.is_connected():
            logger.info("MTProto watchdog successfully reconnected TelegramClient")
            disconnected_ticks = 0
        elif disconnected_ticks >= _MAX_CONSECUTIVE_DISCONNECT_CHECKS:
            raise AntigravityError(
                "TelegramClient remained disconnected across 90s watchdog window; exiting for systemd restart."
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
    cred_repo = SshCredentialRepository(db_mgr)
    topic_repo = TopicRepository(db_mgr)
    session_repo = SessionRepository(db_mgr)
    schedule_repo = ScheduleRepository(db_mgr)
    sensor_repo = SensorRepository(db_mgr)
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

    # Step 4: Ensure local SSH keypair, install native SSH/PTY wrappers, and sync AGENTS.md host map
    mount_mgr = RemoteMountManager(static_config)
    mount_mgr.install_runtime_wrappers()
    await mount_mgr.ensure_local_ssh_keypair()
    hosts = await host_repo.list_hosts()
    bootstrapper.sync_global_agents_md_host_map(hosts)
    for host in hosts:
        mount_mgr.write_ssh_config_entry(host)
        mount_mgr.ensure_rule_skeleton(host)

    # Step 5: Connect Telethon MTProto client with infinite auto-reconnect resilience
    static_config.telegram.session_file_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(static_config.telegram.session_file_path),
        static_config.telegram.api_id,
        static_config.telegram.api_hash,
        connection_retries=_MTPROTO_CONNECTION_RETRIES,
        retry_delay=_MTPROTO_RETRY_DELAY_SEC,
        auto_reconnect=True,
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
        mount_mgr=mount_mgr,
        settings_repo=settings_repo,
        topic_repo=topic_repo,
        account_repo=account_repo,
        session_repo=session_repo,
    )
    mount_mgr.bind_security_gateways(
        vault=vault,
        cred_repo=cred_repo,
        host_repo=host_repo,
        topic_repo=topic_repo,
        on_password_prompt=workspace_ui.send_ssh_password_prompt,
        on_sudo_prompt=workspace_ui.send_sudo_approval_prompt,
    )

    scheduler_service = TemporalSchedulerService(
        static_config=static_config,
        schedule_repo=schedule_repo,
        wake_agent_cb=workspace_ui.execute_scheduled_wake_turn,
    )

    sensor_service = EventSensorService(
        static_config=static_config,
        sensor_repo=sensor_repo,
        settings_repo=settings_repo,
        wake_agent_cb=workspace_ui.execute_sensor_wake_turn,
        deliver_reflex_cb=workspace_ui.deliver_sensor_reflex_report,
    )

    ipc_server = CoreIpcServer(
        static_config=static_config,
        bootstrapper=bootstrapper,
        media_service=media_service,
        scheduler_service=scheduler_service,
        sensor_service=sensor_service,
        topic_lifecycle=topic_lifecycle,
        topic_repo=topic_repo,
        host_repo=host_repo,
        mount_mgr=mount_mgr,
    )
    workspace_ui.bind_active_turn_message_callback(
        ipc_server.set_active_user_message_id
    )

    control_panel_ui = ControlPanelUI(
        client=client,
        static_config=static_config,
        bootstrapper=bootstrapper,
        vault=vault,
        keyring_mgr=keyring_mgr,
        mount_mgr=mount_mgr,
        worker_pool=worker_pool,
        cli_inspector=cli_inspector,
        scheduler_service=scheduler_service,
        sensor_service=sensor_service,
        topic_lifecycle=topic_lifecycle,
        workspace_ui=workspace_ui,
        settings_repo=settings_repo,
        account_repo=account_repo,
        host_repo=host_repo,
        cred_repo=cred_repo,
        access_repo=access_repo,
    )

    batcher = MessageBatcher(
        inbox_dir=static_config.paths.inbox_dir,
        settings_repo=settings_repo,
        on_flush=workspace_ui.execute_batched_prompt,
        on_staged_media=workspace_ui.notify_staged_media_held,
    )
    workspace_ui.bind_staged_media_callbacks(
        flush_cb=batcher.flush_staged_files_now,
        clear_cb=batcher.clear_staged_files,
    )
    TelegramEventRouter(
        client=client,
        static_config=static_config,
        settings_repo=settings_repo,
        access_repo=access_repo,
        media_service=media_service,
        mount_mgr=mount_mgr,
        batcher=batcher,
        control_panel_ui=control_panel_ui,
        workspace_ui=workspace_ui,
    ).register_handlers()

    await ipc_server.start()
    await scheduler_service.start_and_hydrate()
    await sensor_service.start_and_hydrate()
    await control_panel_ui.render_and_pin_dashboard(recreate_at_bottom=False)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Antigravity Telegram Core is online and ready.")
    try:
        await _wait_with_mtproto_watchdog(client, stop_event)
    finally:
        logger.info("Shutting down Antigravity Telegram Core...")
        await sensor_service.stop()
        await scheduler_service.stop()
        await ipc_server.stop()
        await worker_pool.shutdown_all()
        await keyring_mgr.shutdown_all()
        await client.disconnect()
        await db_mgr.close()


def main() -> None:
    try:
        static_cfg = load_static_config()
        configure_loguru(static_cfg.paths.log_file_path)
        asyncio.run(run_service(static_cfg))
    except (StartupValidationError, AntigravityError) as exc:
        sys.stderr.write(f"FATAL STARTUP VALIDATION ERROR: {exc}\n")
        raise


def run() -> None:
    main()


if __name__ == "__main__":
    run()
