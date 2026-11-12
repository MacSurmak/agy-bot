import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.sensor_service import EventSensorService
from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import (
    ExecutionMode,
    NotifyPolicy,
    ScheduleKind,
    SensorKind,
    SensorTriggerCondition,
    SensorWakeMode,
    TopicKind,
)
from antigravity_core.infrastructure.agy_runtime.environment_bootstrap import (
    AgyEnvironmentBootstrapper,
)
from antigravity_core.infrastructure.database.repositories import (
    RemoteHostRepository,
    TopicRepository,
)
from antigravity_core.infrastructure.media.mtproto_media import MediaService
from antigravity_core.infrastructure.remotes.sshfs_mount_mgr import RemoteMountManager

logger = structlog.get_logger(__name__)


class CoreIpcServer:
    """Local UNIX domain socket RPC server handling requests from child `stdio_proxy` MCP processes."""

    def __init__(
        self,
        static_config: StaticConfig,
        bootstrapper: AgyEnvironmentBootstrapper,
        media_service: MediaService,
        scheduler_service: TemporalSchedulerService,
        sensor_service: EventSensorService,
        topic_lifecycle: TopicLifecycleService,
        topic_repo: TopicRepository,
        host_repo: RemoteHostRepository,
        mount_mgr: RemoteMountManager,
    ) -> None:
        self._cfg = static_config
        self._bootstrapper = bootstrapper
        self._media = media_service
        self._scheduler = scheduler_service
        self._sensors = sensor_service
        self._topic_lifecycle = topic_lifecycle
        self._topic_repo = topic_repo
        self._host_repo = host_repo
        self._mount_mgr = mount_mgr
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        sock_path = self._cfg.paths.ipc_socket_path
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(sock_path),
        )
        os.chmod(sock_path, 0o600)
        logger.info(
            "Started local UNIX IPC server for MCP stdio_proxy", socket=str(sock_path)
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        sock_path = self._cfg.paths.ipc_socket_path
        if sock_path.exists():
            sock_path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_line = await asyncio.wait_for(
                reader.readline(),
                timeout=self._cfg.runtime.ipc_request_timeout_sec,
            )
            if not raw_line:
                return

            payload: Any = json.loads(raw_line.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise ValueError("IPC request must be a JSON object.")

            method = str(payload.get("method", ""))
            topic_id = int(payload.get("topic_id", 0))
            account_slug = str(payload.get("account_slug", ""))
            params = payload.get("params")
            if not isinstance(params, dict) or topic_id <= 0:
                raise ValueError("Invalid IPC request parameters or topic_id.")

            logger.info(
                "Executing MCP tool call via IPC",
                method=method,
                topic_id=topic_id,
                account_slug=account_slug,
            )
            result = await self._dispatch(
                method=method, topic_id=topic_id, params=params
            )
            response = {"ok": True, "result": result}
        except Exception as exc:
            logger.error("IPC tool execution failed", error=str(exc))
            response = {"ok": False, "error": str(exc)}

        try:
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                writer.write(
                    (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                await writer.wait_closed()

    async def _dispatch(
        self, method: str, topic_id: int, params: dict[str, str | int | bool]
    ) -> dict[str, str | int | bool | list[dict[str, str | int | bool | None]]]:
        if method == "ensure_ssh_target_ready":
            ssh_user = str(params["ssh_user"]) if "ssh_user" in params else ""
            host_alias = str(params["host_alias"]) if "host_alias" in params else ""
            port = int(params["port"]) if "port" in params else 22
            jump_host = (
                str(params["jump_host"]).strip() if "jump_host" in params else ""
            )
            raw_target = f"{ssh_user}@{host_alias}" if ssh_user else host_alias
            resolved_target, _cred = (
                await self._mount_mgr.ensure_target_access_with_jump(
                    topic_id=topic_id,
                    raw_target=raw_target,
                    explicit_port=port,
                    jump_host=jump_host,
                )
            )
            return {"resolved_target": resolved_target}

        if method == "authorize_pty_sudo":
            target = str(params["target"]) if "target" in params else ""
            port = int(params["port"]) if "port" in params else 22
            cmd_str = str(params["command"]) if "command" in params else ""
            resolved_target, sudo_pass, is_root = (
                await self._mount_mgr.authorize_sudo_operation(
                    topic_id=topic_id,
                    raw_target=target,
                    explicit_port=port,
                    action_summary=f"Выполнение команды sudo на {target}",
                    diff_or_cmd_preview=cmd_str,
                )
            )
            return {
                "resolved_target": resolved_target,
                "sudo_password": sudo_pass,
                "is_root": str(is_root),
            }

        if method == "stage_remote_file":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            raw_path = str(params["path"]) if "path" in params else ""
            for_write = bool(params["for_write"]) if "for_write" in params else False
            return await self._mount_mgr.stage_remote_file(
                topic_id=topic_id,
                workspace_dir=Path(topic.workspace_path),
                raw_path=raw_path,
                for_write=for_write,
            )

        if method == "commit_remote_file":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            raw_path = str(params["path"]) if "path" in params else ""
            cleanup_only = (
                bool(params["cleanup_only"]) if "cleanup_only" in params else False
            )
            return await self._mount_mgr.commit_remote_file(
                topic_id=topic_id,
                workspace_dir=Path(topic.workspace_path),
                raw_path=raw_path,
                cleanup_only=cleanup_only,
            )

        if method == "ssh_exec":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            host_alias = str(params["host"]).strip()
            command_str = str(params["command"])
            timeout_sec = int(params["timeout_sec"]) if "timeout_sec" in params else 60
            scratch_dir = Path(topic.workspace_path)

            if host_alias.lower() in ("local", "local_scratch", "scratch", "lxc"):
                return await self._mount_mgr.execute_remote_ssh(
                    host=None,
                    command=command_str,
                    timeout_sec=timeout_sec,
                    execution_mode=topic.execution_mode,
                    local_scratch_dir=scratch_dir,
                )

            remote_host = await self._host_repo.get_by_alias(host_alias)
            if remote_host is None:
                all_hosts = await self._host_repo.list_hosts()
                known = ", ".join(h.alias for h in all_hosts) or "none"
                raise ValueError(
                    f"Unknown remote host alias '{host_alias}'. Registered hosts: [{known}]. "
                    f"Call 'register_remote_host' first or use host='local_scratch' for local plotting scripts."
                )

            return await self._mount_mgr.execute_remote_ssh(
                host=remote_host,
                command=command_str,
                timeout_sec=timeout_sec,
                execution_mode=topic.execution_mode,
                local_scratch_dir=scratch_dir,
            )

        if method == "update_host_knowledge":
            host_alias = str(params["host"]).strip()
            remote_host = await self._host_repo.get_by_alias(host_alias)
            if remote_host is None:
                raise ValueError(
                    f"Host '{host_alias}' is not registered. Call 'register_remote_host' first."
                )
            notes_md = str(params["notes_markdown"])
            append_flag = bool(params.get("append", True))
            rule_path = self._mount_mgr.update_host_ai_notes(
                remote_host, notes_markdown=notes_md, append=append_flag
            )
            return {
                "host": remote_host.alias,
                "rule_file_path": str(rule_path),
                "status": "updated",
            }

        if method == "send_file_to_telegram":
            raw_file_path = str(params["file_path"]).strip()
            caption_str = str(params["caption"]) if "caption" in params else ""
            if raw_file_path.startswith("/ssh:"):
                topic = await self._topic_repo.get_topic_strict(topic_id)
                ws_dir = Path(topic.workspace_path)
                streamed_tmp_path = await self._mount_mgr.download_remote_file_to_temp(
                    topic_id=topic_id,
                    workspace_dir=ws_dir,
                    raw_path=raw_file_path,
                )
                try:
                    return await self._media.send_file_to_topic(
                        topic_id=topic_id,
                        file_path_str=str(streamed_tmp_path),
                        caption=caption_str,
                    )
                finally:
                    streamed_tmp_path.unlink(missing_ok=True)
                    if streamed_tmp_path.parent.exists():
                        with contextlib.suppress(OSError):
                            streamed_tmp_path.parent.rmdir()
            return await self._media.send_file_to_topic(
                topic_id=topic_id,
                file_path_str=raw_file_path,
                caption=caption_str,
            )

        if method == "schedule_task":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            keep_ctx = bool(params.get("keep_conversation_context", False))
            conv_id = topic.active_conversation_id if keep_ctx else None
            raw_kind = str(params["schedule_kind"]).strip().upper()
            if raw_kind in ("ONCE", "ONE-SHOT", "SINGLE", "TIMER"):
                kind = ScheduleKind.ONE_SHOT
            else:
                kind = ScheduleKind(raw_kind)

            raw_policy = str(params.get("notify_policy", "ALWAYS")).strip().upper()
            policy = (
                NotifyPolicy.ALERT_ONLY
                if "ALERT" in raw_policy
                else NotifyPolicy.ALWAYS
            )
            task = await self._scheduler.schedule_new_task(
                topic_id=topic_id,
                conversation_id=conv_id,
                schedule_kind=kind,
                trigger_spec=str(params["trigger_spec"]),
                instruction_prompt=str(params["instruction_prompt"]),
                notify_policy=policy,
            )
            return {
                "task_id": str(task.id),
                "topic_id": task.topic_id,
                "schedule_kind": task.schedule_kind.value,
                "trigger_spec": task.trigger_spec,
                "notify_policy": task.notify_policy.value,
            }

        if method == "list_scheduled_tasks":
            tasks = await self._scheduler.list_tasks(topic_id=topic_id)
            return {
                "topic_id": topic_id,
                "tasks": [
                    {
                        "task_id": str(t.id),
                        "schedule_kind": t.schedule_kind.value,
                        "trigger_spec": t.trigger_spec,
                        "instruction_prompt": t.instruction_prompt,
                        "notify_policy": t.notify_policy.value,
                        "last_run_at": (
                            t.last_run_at.isoformat() if t.last_run_at else None
                        ),
                    }
                    for t in tasks
                ],
            }

        if method == "cancel_scheduled_task":
            task_uuid = UUID(str(params["task_id"]))
            canceled = await self._scheduler.cancel_task(task_uuid)
            return {"task_id": str(canceled.id), "status": "canceled"}

        if method == "create_sensor":
            topic = await self._topic_repo.get_topic_strict(topic_id)
            keep_ctx = bool(params.get("keep_conversation_context", True))
            conv_id = topic.active_conversation_id if keep_ctx else None

            raw_s_kind = str(params.get("sensor_kind", "POLL_COMMAND")).strip().upper()
            s_kind = (
                SensorKind.STREAM_WATCH
                if "STREAM" in raw_s_kind
                else SensorKind.POLL_COMMAND
            )

            raw_cond = (
                str(params.get("trigger_condition", "ON_EXIT_ZERO")).strip().upper()
            )
            if "NONZERO" in raw_cond or "FAIL" in raw_cond or "ERROR" in raw_cond:
                t_cond = SensorTriggerCondition.ON_EXIT_NONZERO
            elif "CHANGE" in raw_cond or "DIFF" in raw_cond:
                t_cond = SensorTriggerCondition.ON_OUTPUT_CHANGE
            elif (
                "REGEX" in raw_cond
                or "MATCH" in raw_cond
                or s_kind == SensorKind.STREAM_WATCH
            ):
                t_cond = SensorTriggerCondition.ON_REGEX_MATCH
            else:
                t_cond = SensorTriggerCondition.ON_EXIT_ZERO

            raw_policy = str(params.get("notify_policy", "ALWAYS")).strip().upper()
            policy = (
                NotifyPolicy.ALERT_ONLY
                if "ALERT" in raw_policy
                else NotifyPolicy.ALWAYS
            )
            match_pat_raw = str(params.get("match_pattern") or "").strip()
            action_cmd_raw = str(params.get("action_command") or "").strip()
            raw_wake = str(params.get("wake_mode", "ALWAYS")).strip().upper()
            if "FAIL" in raw_wake:
                w_mode = SensorWakeMode.ON_ACTION_FAILURE
            elif "NEVER" in raw_wake or "NONE" in raw_wake:
                w_mode = SensorWakeMode.NEVER
            else:
                w_mode = SensorWakeMode.ALWAYS

            sensor = await self._sensors.create_new_sensor(
                topic_id=topic_id,
                conversation_id=conv_id,
                name=str(params.get("name", "Event Sensor")),
                sensor_kind=s_kind,
                command=str(params["command"]),
                trigger_condition=t_cond,
                match_pattern=match_pat_raw if match_pat_raw else None,
                action_command=action_cmd_raw if action_cmd_raw else None,
                wake_mode=w_mode,
                poll_interval_sec=int(params.get("poll_interval_sec", 15)),
                cooldown_sec=int(params.get("cooldown_sec", 300)),
                one_shot=bool(params.get("one_shot", True)),
                instruction_prompt=str(params["instruction_prompt"]),
                notify_policy=policy,
            )
            return {
                "sensor_id": str(sensor.id),
                "name": sensor.name,
                "sensor_kind": sensor.sensor_kind.value,
                "trigger_condition": sensor.trigger_condition.value,
                "action_command": sensor.action_command,
                "wake_mode": sensor.wake_mode.value,
                "one_shot": sensor.one_shot,
                "status": "watching",
            }

        if method == "list_sensors":
            sensors = await self._sensors.list_sensors(topic_id=topic_id)
            return {
                "topic_id": topic_id,
                "sensors": [
                    {
                        "sensor_id": str(s.id),
                        "name": s.name,
                        "sensor_kind": s.sensor_kind.value,
                        "command": s.command,
                        "trigger_condition": s.trigger_condition.value,
                        "action_command": s.action_command,
                        "wake_mode": s.wake_mode.value,
                        "poll_interval_sec": s.poll_interval_sec,
                        "one_shot": s.one_shot,
                        "triggers_count": s.triggers_count,
                        "last_triggered_at": (
                            s.last_triggered_at.isoformat()
                            if s.last_triggered_at
                            else None
                        ),
                    }
                    for s in sensors
                ],
            }

        if method == "cancel_sensor":
            sensor_uuid = UUID(str(params["sensor_id"]))
            canceled_s = await self._sensors.cancel_sensor(sensor_uuid)
            return {
                "sensor_id": str(canceled_s.id),
                "name": canceled_s.name,
                "status": "canceled",
            }

        if method == "get_bot_ssh_public_key":
            pub_key = await self._mount_mgr.ensure_local_ssh_keypair()
            return {
                "public_key": pub_key,
                "authorize_one_liner": (
                    f'mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo "{pub_key}" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
                ),
            }

        if method == "register_remote_host":
            alias = str(params["alias"]).strip()
            hostname_or_ip = str(params["hostname_or_ip"]).strip()
            ssh_user = str(params["ssh_user"]).strip()
            ssh_port = int(params["ssh_port"]) if "ssh_port" in params else 22
            remote_root_raw = (
                str(params["remote_root_path"]).strip()
                if "remote_root_path" in params
                else "/"
            )
            remote_root_path = remote_root_raw if remote_root_raw else "/"
            jump_host_raw = (
                str(params["jump_host"]).strip() if "jump_host" in params else ""
            )
            jump_host = jump_host_raw if jump_host_raw else None

            local_mount = f"{ssh_user}@{alias}:{remote_root_path}"
            rule_file = str(
                (
                    self._cfg.paths.agy_home_dir
                    / "config"
                    / "rules"
                    / f"infra-{alias}.md"
                ).resolve()
            )
            host = await self._host_repo.upsert_host(
                alias=alias,
                hostname_or_ip=hostname_or_ip,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                remote_root_path=remote_root_path,
                local_mount_path=local_mount,
                rule_file_path=rule_file,
                auto_mount_enabled=False,
                jump_host=jump_host,
            )
            self._mount_mgr.write_ssh_config_entry(host)
            await self._mount_mgr.ensure_target_access_with_jump(
                topic_id=topic_id,
                raw_target=f"{ssh_user}@{alias}",
                explicit_port=ssh_port,
                jump_host=jump_host if jump_host is not None else "",
            )
            await self._mount_mgr.sync_host_telemetry_and_rules(host)
            all_hosts = await self._host_repo.list_hosts()
            self._bootstrapper.sync_global_agents_md_host_map(all_hosts)
            return {
                "alias": host.alias,
                "remote_prefix": local_mount,
                "rule_file_path": host.rule_file_path,
                "status": "onboarded_and_indexed",
            }

        if method == "create_workspace_topic":
            title = str(params["title"]).strip()
            mode = ExecutionMode(
                str(params.get("execution_mode", "FULL_ACCESS")).upper()
            )
            placeholder_dir = str(self._cfg.paths.workspaces_root_dir / "pending_topic")
            created = await self._topic_lifecycle.create_forum_topic_with_card(
                title=title,
                kind=TopicKind.PERMANENT_WORKSPACE,
                workspace_path=placeholder_dir,
                execution_mode=mode,
            )
            scratch_dir = (
                self._cfg.paths.workspaces_root_dir / f"topic_{created.topic_id}"
            ).resolve()
            scratch_dir.mkdir(parents=True, exist_ok=True)
            updated = await self._topic_repo.update_workspace_path(
                created.topic_id, str(scratch_dir)
            )
            return {
                "topic_id": updated.topic_id,
                "title": updated.title,
                "workspace_path": updated.workspace_path,
                "execution_mode": updated.execution_mode.value,
            }

        raise ValueError(f"Unsupported IPC method: '{method}'")
