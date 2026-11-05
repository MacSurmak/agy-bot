import json
import os
import re
import sys
from typing import Any

_SSHFS_PATH_RE = re.compile(r"(?:/mnt/remotes/|/runtime_data/remotes/)")

_DENY_LOCAL_RUN_COMMAND_REASON = (
    "Local bash execution ('run_command') inside the LXC brain container is disabled! "
    "1. To run commands on a remote server/workstation, call MCP tool 'mcp:telegram-core:ssh_exec(host=\"<alias>\", command=\"...\")'. "
    "2. To run a local Python plotting/helper script in your topic scratchpad, call 'mcp:telegram-core:ssh_exec(host=\"local_scratch\", command=\"...\")'. "
    "3. To read, create, or edit files, use native file tools (read_file, write_file, edit_file, list_dir) on /remotes/<host>/... or your scratch folder."
)

_DENY_SSHFS_TRAVERSAL_REASON = (
    "FORBIDDEN (Errata Patch #3): Running local find_by_name/grep_search over a mounted SSHFS path "
    "(/remotes/<host>/...) causes slow SFTP round-trips. Run searches directly on the remote host via "
    "MCP tool 'mcp:telegram-core:ssh_exec(host=\"<alias>\", command=\"timeout 10 rg -n ...\")', "
    "then open specific files with read_file/edit_file on /remotes/<host>/..."
)


def main() -> None:
    raw_stdin = sys.stdin.read()
    if not raw_stdin.strip():
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    try:
        payload: dict[str, Any] = json.loads(raw_stdin)
    except json.JSONDecodeError:
        sys.stdout.write(
            json.dumps({"decision": "deny", "reason": "Malformed PreToolUse JSON input."}) + "\n"
        )
        return

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    tool_name = str(tool_call.get("name", ""))
    if os.environ.get("AGY_EXECUTION_MODE") == "READ_ONLY" and tool_name in (
        "write_file",
        "edit_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    ):
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": "Topic is currently in READ_ONLY (Plan) mode. File modifications are disabled.",
                }
            )
            + "\n"
        )
        return

    if tool_name == "run_command":
        sys.stdout.write(
            json.dumps({"decision": "deny", "reason": _DENY_LOCAL_RUN_COMMAND_REASON})
            + "\n"
        )
        return

    if tool_name == "schedule":
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": "Use MCP tool 'schedule_task' or 'create_sensor' from server 'telegram-core'.",
                }
            )
            + "\n"
        )
        return

    args = tool_call.get("args")
    if isinstance(args, dict) and tool_name in ("find_by_name", "grep_search"):
        search_target = str(
            args.get("SearchDirectory")
            or args.get("SearchPath")
            or args.get("DirectoryPath")
            or ""
        )
        if _SSHFS_PATH_RE.search(search_target) is not None:
            sys.stdout.write(
                json.dumps({"decision": "deny", "reason": _DENY_SSHFS_TRAVERSAL_REASON})
                + "\n"
            )
            return

    sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")


if __name__ == "__main__":
    main()
