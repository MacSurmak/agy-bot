import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from antigravity_core.application.scheduler_service import TemporalSchedulerService
from antigravity_core.application.topic_lifecycle import TopicLifecycleService
from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode, NotifyPolicy, ScheduleKind, TopicKind
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
        media_service: MediaService,
        scheduler_service: TemporalSchedulerService,
        topic_lifecycle: TopicLifecycleService,
        topic_repo: TopicRepository,
        host_repo: RemoteHostRepository,
        mount_mgr: RemoteMountManager,
    ) -> None:
        self._cfg = static_config
        self._media = media_service
        self._scheduler = scheduler_service
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
        logger.info("Started local UNIX IPC server for MCP stdio_proxy", socket=str(sock_path))

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
            result = await self._dispatch(method=method, topic_id=topic_id, params=params)
            response = {"ok": True, "result": result}
        except Exception as exc:
            logger.error("IPC tool execution failed", error=str(exc))
            response = {"ok": False, "error": str(exc)}

        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(
        self, method: str, topic_id: int, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "send_file_to_telegram":
            return await self._media.send_file_to_topic(
                topic_id=topic_id,
                file_path_str=str(params["file_path"]),
                caption=str(params.get("caption", "")),
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
                        "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
                    }
                    for t in tasks
                ],
            }

        if method == "cancel_scheduled_task":
            task_uuid = UUID(str(params["task_id"]))
            canceled = await self._scheduler.cancel_task(task_uuid)
            return {"task_id": str(canceled.id), "status": "canceled"}

        if method == "register_remote_host":
            alias = str(params["alias"]).strip()
            hostname_or_ip = str(params["hostname_or_ip"]).strip()
            ssh_user = str(params["ssh_user"]).strip()
            ssh_port = int(params["ssh_port"])
            remote_root_path = str(params["remote_root_path"]).strip()

            local_mount = str((self._cfg.paths.remotes_mount_dir / alias).resolve())
            rule_file = str(
                (
                    self._cfg.paths.agy_home_dir / "config" / "rules" / f"infra-{alias}.md"
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
                auto_mount_enabled=True,
            )
            await self._mount_mgr.verify_ssh_connectivity(host)
            await self._mount_mgr.ensure_mounted(host)
            self._mount_mgr.ensure_rule_skeleton(host)
            return {
                "alias": host.alias,
                "local_mount_path": host.local_mount_path,
                "rule_file_path": host.rule_file_path,
                "status": "mounted",
            }

        if method == "create_workspace_topic":
            title = str(params["title"]).strip()
            workspace_path = str(Path(str(params["workspace_path"])).resolve())
            mode = ExecutionMode(str(params["execution_mode"]).upper())
            created = await self._topic_lifecycle.create_forum_topic_with_card(
                title=title,
                kind=TopicKind.TASK_TOPIC,
                workspace_path=workspace_path,
                execution_mode=mode,
            )
            return {
                "topic_id": created.topic_id,
                "title": created.title,
                "workspace_path": created.workspace_path,
                "execution_mode": created.execution_mode.value,
            }

        raise ValueError(f"Unsupported IPC method: '{method}'")
