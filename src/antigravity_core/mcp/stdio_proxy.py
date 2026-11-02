import asyncio
import json
import os
from typing import Any

from mcp.server import MCPServer

from antigravity_core.domain.exceptions import IpcError

# STRICT LATE-2026+ MCP SDK v2.3+ SYNTAX: Use `MCPServer` from `mcp.server`
mcp = MCPServer("telegram-core")


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise IpcError(
            f"Missing mandatory environment variable '{name}' in agy MCP stdio_proxy process."
        )
    return val


def _get_inherited_topic_id() -> int:
    raw_topic_id = _require_env("AGY_CURRENT_TOPIC_ID")
    try:
        topic_id = int(raw_topic_id)
    except ValueError as exc:
        raise IpcError(f"Invalid integer in AGY_CURRENT_TOPIC_ID: {raw_topic_id!r}") from exc
    if topic_id <= 0:
        raise IpcError(f"AGY_CURRENT_TOPIC_ID must be positive, got {topic_id}")
    return topic_id


async def _call_bot_ipc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    socket_path = _require_env("AGY_CORE_IPC_SOCKET")
    topic_id = _get_inherited_topic_id()
    account_slug = os.environ.get("AGY_ACCOUNT_SLUG", "unknown")

    request_envelope = {
        "method": method,
        "topic_id": topic_id,
        "account_slug": account_slug,
        "params": params,
    }

    try:
        reader, writer = await asyncio.open_unix_connection(path=socket_path)
    except OSError as exc:
        raise IpcError(
            f"Failed to connect to bot IPC socket at '{socket_path}': {exc}"
        ) from exc

    try:
        payload_bytes = (json.dumps(request_envelope, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(payload_bytes)
        await writer.drain()

        raw_response = await reader.readline()
        if not raw_response:
            raise IpcError("Bot IPC server closed connection without returning a response.")

        decoded: Any = json.loads(raw_response.decode("utf-8", errors="replace"))
        if not isinstance(decoded, dict):
            raise IpcError("Malformed non-object JSON response from bot IPC server.")

        if not decoded.get("ok", False):
            err_msg = str(decoded.get("error", "Unknown IPC error"))
            raise IpcError(err_msg)

        result = decoded.get("result")
        return dict(result) if isinstance(result, dict) else {"value": result}
    finally:
        writer.close()
        await writer.wait_closed()


# Errata Patch #1: `topic_id` is completely omitted from tool signatures exposed to the LLM!
@mcp.tool()
async def send_file_to_telegram(file_path: str, caption: str) -> dict[str, Any]:
    """Send a local or /mnt/remotes/<host>/... file (up to 2 GB) to the current Telegram topic."""
    return await _call_bot_ipc(
        "send_file_to_telegram",
        {"file_path": file_path, "caption": caption},
    )


@mcp.tool()
async def schedule_task(
    schedule_kind: str,
    trigger_spec: str,
    instruction_prompt: str,
    notify_policy: str,
    keep_conversation_context: bool,
) -> dict[str, Any]:
    """Schedule a ONE_SHOT (ISO-8601 date) or CRON (5-field cron) task in the current topic."""
    return await _call_bot_ipc(
        "schedule_task",
        {
            "schedule_kind": schedule_kind,
            "trigger_spec": trigger_spec,
            "instruction_prompt": instruction_prompt,
            "notify_policy": notify_policy,
            "keep_conversation_context": keep_conversation_context,
        },
    )


@mcp.tool()
async def list_scheduled_tasks() -> dict[str, Any]:
    """List all active scheduled tasks and cron jobs for the current Telegram topic."""
    return await _call_bot_ipc("list_scheduled_tasks", {})


@mcp.tool()
async def cancel_scheduled_task(task_id: str) -> dict[str, Any]:
    """Cancel and deactivate a scheduled task by its UUID."""
    return await _call_bot_ipc("cancel_scheduled_task", {"task_id": task_id})


@mcp.tool()
async def register_remote_host(
    alias: str,
    hostname_or_ip: str,
    ssh_user: str,
    ssh_port: int,
    remote_root_path: str,
) -> dict[str, Any]:
    """Register a remote SSH host, mount it via SSHFS under /mnt/remotes/<alias>, and create its rule passport."""
    return await _call_bot_ipc(
        "register_remote_host",
        {
            "alias": alias,
            "hostname_or_ip": hostname_or_ip,
            "ssh_user": ssh_user,
            "ssh_port": ssh_port,
            "remote_root_path": remote_root_path,
        },
    )


@mcp.tool()
async def create_workspace_topic(
    title: str,
    workspace_path: str,
    execution_mode: str,
) -> dict[str, Any]:
    """Create a new Telegram Forum Topic bound to the given workspace directory."""
    return await _call_bot_ipc(
        "create_workspace_topic",
        {
            "title": title,
            "workspace_path": workspace_path,
            "execution_mode": execution_mode,
        },
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
