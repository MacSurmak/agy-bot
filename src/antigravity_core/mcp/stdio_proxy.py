import asyncio
import json
import os
from typing import Annotated, Final, Literal

from mcp.server import MCPServer
from pydantic import Field

from antigravity_core.domain.exceptions import IpcError

type IpcScalar = str | int | float | bool | None
type IpcPayload = dict[str, IpcScalar | list[dict[str, IpcScalar]]]

_SERVER_INSTRUCTIONS: Final[str] = (
    "Native control plane for Antigravity Telegram Core. "
    "Use register_remote_host to onboard SSH servers (with jump_host for internal cluster nodes), "
    "update_host_knowledge to record infrastructure notes in ~/.gemini/config/rules/infra-<host>.md, "
    "send_file_to_telegram to deliver local or remote TRAMP (/ssh:user@alias:/abs/path) files to Telegram, "
    "schedule_task for persistent timers/cron jobs, and create_sensor for zero-token background event watchers."
)

# STRICT LATE-2026+ MCP SDK v2.3+ SYNTAX: Use `MCPServer` from `mcp.server`
mcp: Final[MCPServer] = MCPServer("telegram-core", instructions=_SERVER_INSTRUCTIONS)


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
        raise IpcError(
            f"Invalid integer in AGY_CURRENT_TOPIC_ID: {raw_topic_id!r}"
        ) from exc
    if topic_id <= 0:
        raise IpcError(f"AGY_CURRENT_TOPIC_ID must be positive, got {topic_id}")
    return topic_id


async def _call_bot_ipc(method: str, params: dict[str, IpcScalar]) -> IpcPayload:
    socket_path = _require_env("AGY_CORE_IPC_SOCKET")
    topic_id = _get_inherited_topic_id()
    account_slug = _require_env("AGY_ACCOUNT_SLUG")

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
        payload_bytes = (
            json.dumps(request_envelope, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        writer.write(payload_bytes)
        await writer.drain()

        raw_response = await reader.readline()
        if not raw_response:
            raise IpcError(
                "Bot IPC server closed connection without returning a response."
            )

        decoded = json.loads(raw_response.decode("utf-8", errors="replace"))
        if not isinstance(decoded, dict):
            raise IpcError("Malformed non-object JSON response from bot IPC server.")

        ok_flag = bool(decoded["ok"]) if "ok" in decoded else False
        if not ok_flag:
            err_msg = (
                str(decoded["error"]) if "error" in decoded else "Unknown IPC error"
            )
            raise IpcError(err_msg)

        result = decoded["result"] if "result" in decoded else None
        return dict(result) if isinstance(result, dict) else {"value": str(result)}
    finally:
        writer.close()
        await writer.wait_closed()


@mcp.tool()
async def update_host_knowledge(
    host: Annotated[
        str,
        Field(description="Registered SSH host alias (e.g. 'bigcomp', 'b650mt')."),
    ],
    notes_markdown: Annotated[
        str,
        Field(
            description="Markdown architectural notes, cluster topology, GPU specs, or key directories to store in ~/.gemini/config/rules/infra-<host>.md."
        ),
    ],
    append: Annotated[
        bool,
        Field(
            description="Pass True to append to existing notes or False to replace Section 2."
        ),
    ],
) -> IpcPayload:
    """Save architectural notes, project paths, database locations, or operational insights into the permanent passport ~/.gemini/config/rules/infra-<host>.md."""
    return await _call_bot_ipc(
        "update_host_knowledge",
        {
            "host": host,
            "notes_markdown": notes_markdown,
            "append": append,
        },
    )


@mcp.tool()
async def send_file_to_telegram(
    file_path: Annotated[
        str,
        Field(
            description="Absolute local path or remote TRAMP path ('/ssh:user@<alias>:/abs/path') of the file to send (up to 2 GB)."
        ),
    ],
    caption: Annotated[
        str,
        Field(description="Caption text displayed under the file in Telegram."),
    ],
) -> IpcPayload:
    """Send any local file or remote TRAMP file ('/ssh:user@<alias>:/abs/path', up to 2 GB) directly to the current Telegram topic without scp."""
    return await _call_bot_ipc(
        "send_file_to_telegram",
        {"file_path": file_path, "caption": caption},
    )


@mcp.tool()
async def schedule_task(
    schedule_kind: Annotated[
        Literal["ONE_SHOT", "CRON"],
        Field(
            description="'ONE_SHOT' for a single timer or 'CRON' for a recurring 5-field cron schedule."
        ),
    ],
    trigger_spec: Annotated[
        str,
        Field(
            description="For ONE_SHOT: relative delay ('+60s', '+1m', '+2h') or ISO-8601 datetime. For CRON: 5-field cron expression ('0 7 * * *')."
        ),
    ],
    instruction_prompt: Annotated[
        str,
        Field(description="Exact task instructions to execute when woken up."),
    ],
    notify_policy: Annotated[
        Literal["ALWAYS", "ALERT_ONLY"],
        Field(
            description="Pass 'ALWAYS' to report every run or 'ALERT_ONLY' to report only on anomalies."
        ),
    ],
    keep_conversation_context: Annotated[
        bool,
        Field(
            description="Pass False for a clean session or True to resume the current conversation UUID."
        ),
    ],
) -> IpcPayload:
    """Schedule a persistent time-based ONE_SHOT or CRON task in PostgreSQL for the current Telegram topic."""
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
async def list_scheduled_tasks() -> IpcPayload:
    """List all active scheduled tasks and cron jobs for the current Telegram topic."""
    return await _call_bot_ipc("list_scheduled_tasks", {})


@mcp.tool()
async def cancel_scheduled_task(
    task_id: Annotated[str, Field(description="UUID string of the scheduled task.")],
) -> IpcPayload:
    """Cancel and deactivate a scheduled task by its UUID."""
    return await _call_bot_ipc("cancel_scheduled_task", {"task_id": task_id})


@mcp.tool()
async def create_sensor(
    name: Annotated[str, Field(description="Human-readable sensor name.")],
    command: Annotated[
        str,
        Field(
            description="Shell probe command or stream command (e.g. 'ssh user@bigcomp \"nvidia-smi ...\"')."
        ),
    ],
    instruction_prompt: Annotated[
        str,
        Field(description="Instructions for the AI agent when woken up by the sensor."),
    ],
    sensor_kind: Annotated[
        Literal["POLL_COMMAND", "STREAM_WATCH"],
        Field(
            description="'POLL_COMMAND' for periodic command polling or 'STREAM_WATCH' for live log tailing."
        ),
    ],
    trigger_condition: Annotated[
        Literal[
            "ON_EXIT_ZERO", "ON_EXIT_NONZERO", "ON_OUTPUT_CHANGE", "ON_REGEX_MATCH"
        ],
        Field(description="Condition that fires the sensor."),
    ],
    poll_interval_sec: Annotated[
        int, Field(description="Polling interval in seconds (e.g. 15).")
    ],
    match_pattern: Annotated[
        str,
        Field(
            description="Regex pattern for ON_REGEX_MATCH or STREAM_WATCH, or '' if unused."
        ),
    ],
    action_command: Annotated[
        str,
        Field(
            description="Optional millisecond shell reflex command executed before waking AI, or '' if unused."
        ),
    ],
    wake_mode: Annotated[
        Literal["ALWAYS", "ON_ACTION_FAILURE", "NEVER"],
        Field(
            description="'ALWAYS' to wake AI, 'ON_ACTION_FAILURE' to wake AI only if action_command fails, or 'NEVER'."
        ),
    ],
    one_shot: Annotated[
        bool,
        Field(
            description="Pass True to deactivate after first trigger or False to keep watching."
        ),
    ],
    cooldown_sec: Annotated[
        int,
        Field(description="Minimum seconds between repeated triggers (e.g. 300)."),
    ],
    notify_policy: Annotated[
        Literal["ALWAYS", "ALERT_ONLY"],
        Field(description="Notification policy ('ALWAYS' or 'ALERT_ONLY')."),
    ],
    keep_conversation_context: Annotated[
        bool,
        Field(description="Pass True to keep the current conversation context."),
    ],
) -> IpcPayload:
    """Create a zero-token background event sensor with optional immediate reflex action_command."""
    return await _call_bot_ipc(
        "create_sensor",
        {
            "name": name,
            "command": command,
            "instruction_prompt": instruction_prompt,
            "sensor_kind": sensor_kind,
            "trigger_condition": trigger_condition,
            "poll_interval_sec": poll_interval_sec,
            "match_pattern": match_pattern,
            "action_command": action_command,
            "wake_mode": wake_mode,
            "one_shot": one_shot,
            "cooldown_sec": cooldown_sec,
            "notify_policy": notify_policy,
            "keep_conversation_context": keep_conversation_context,
        },
    )


@mcp.tool()
async def list_sensors() -> IpcPayload:
    """List all active event sensors for the current Telegram topic."""
    return await _call_bot_ipc("list_sensors", {})


@mcp.tool()
async def cancel_sensor(
    sensor_id: Annotated[str, Field(description="UUID string of the sensor.")],
) -> IpcPayload:
    """Cancel and stop an active event sensor by its UUID."""
    return await _call_bot_ipc("cancel_sensor", {"sensor_id": sensor_id})


@mcp.tool()
async def get_bot_ssh_public_key() -> IpcPayload:
    """Return the bot's Ed25519 SSH public key."""
    return await _call_bot_ipc("get_bot_ssh_public_key", {})


@mcp.tool()
async def register_remote_host(
    alias: Annotated[
        str,
        Field(description="Short SSH alias for the host (e.g. 'b650mt', 'bigcomp', 'rtx4060')."),
    ],
    hostname_or_ip: Annotated[
        str,
        Field(description="IP address or hostname of the target server (e.g. '172.16.6.6', '192.168.1.100')."),
    ],
    ssh_user: Annotated[
        str,
        Field(description="SSH login username on the target server (e.g. 'user', 'andrey')."),
    ],
    ssh_port: Annotated[
        int,
        Field(description="SSH port number (pass 22 for standard SSH)."),
    ],
    remote_root_path: Annotated[
        str,
        Field(description="Default remote root directory (e.g. '/home/user' or '/')."),
    ],
    jump_host: Annotated[
        str,
        Field(
            description="Pass 'user@<gateway_alias>' (e.g. 'user@b650mt') if the host is an internal LAN/cluster node behind a gateway, or '' if directly reachable."
        ),
    ],
) -> IpcPayload:
    """Register a remote SSH host (with optional jump_host ProxyJump for internal LAN nodes), provision the SSH key, and generate its infrastructure passport ~/.gemini/config/rules/infra-<alias>.md."""
    return await _call_bot_ipc(
        "register_remote_host",
        {
            "alias": alias,
            "hostname_or_ip": hostname_or_ip,
            "ssh_user": ssh_user,
            "ssh_port": ssh_port,
            "remote_root_path": remote_root_path,
            "jump_host": jump_host,
        },
    )


@mcp.tool()
async def create_workspace_topic(
    title: Annotated[str, Field(description="Title for the new Telegram Forum Topic.")],
    execution_mode: Annotated[
        Literal["FULL_ACCESS", "READ_ONLY"],
        Field(description="'FULL_ACCESS' or 'READ_ONLY'."),
    ],
) -> IpcPayload:
    """Create a new Telegram Forum Topic with its own isolated scratch directory."""
    return await _call_bot_ipc(
        "create_workspace_topic",
        {
            "title": title,
            "execution_mode": execution_mode,
        },
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
