import json
import os
import re
import socket
import sys
from typing import Final
from pathlib import Path

_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write_file",
        "edit_file",
    }
)
_ALL_FILE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "read_file",
        *_WRITE_TOOLS,
    }
)
_PATH_ARG_KEYS: tuple[str, ...] = (
    "TargetFile",
    "AbsolutePath",
    "Path",
    "FilePath",
    "DirectoryPath",
    "SearchPath",
    "SearchDirectory",
)
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_RUNTIME_DATA_DIR: Final[Path] = (_REPO_ROOT / "runtime_data").resolve()

_ALLOWED_LOCAL_WRITE_PREFIXES: Final[tuple[str, ...]] = (
    str(_RUNTIME_DATA_DIR / "workspaces"),
    str(_RUNTIME_DATA_DIR / "inbox"),
    "/srv/antigravity/app/runtime_data/workspaces",
    "/srv/antigravity/app/runtime_data/inbox",
    "/root/.gemini/config/rules",
    "/tmp",
)

_ALLOWED_LOCAL_READ_ONLY_SEGMENTS: Final[tuple[str, ...]] = (
    "/.gemini/antigravity-cli/mcp/",
    "/.system_generated/tasks/",
    "/.gemini/config/rules/",
    "/.gemini/config/skills/",
    "/.gemini/AGENTS.md",
)

_SAFE_FD_REDIRECT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b\d*>&\d+\b|\b\d*>>?\s*/dev/null\b|&>>?\s*/dev/null\b)"
)
_FORBIDDEN_INPLACE_EDIT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:sed\s+(?:-[a-zA-Z]*i|--in-place)|awk\s+-i\s+inplace|perl\s+-[a-zA-Z]*p[a-zA-Z]*i)\b"
)


def _ipc_call(method: str, params: dict[str, str | int | bool]) -> tuple[bool, str]:
    if "AGY_CORE_IPC_SOCKET" not in os.environ:
        return True, ""
    socket_path = os.environ["AGY_CORE_IPC_SOCKET"].strip()
    topic_id = (
        int(os.environ["AGY_CURRENT_TOPIC_ID"])
        if "AGY_CURRENT_TOPIC_ID" in os.environ
        else 1
    )
    account_slug = (
        os.environ["AGY_ACCOUNT_SLUG"].strip()
        if "AGY_ACCOUNT_SLUG" in os.environ
        else "default"
    )
    envelope = {
        "method": method,
        "topic_id": topic_id,
        "account_slug": account_slug,
        "params": params,
    }
    raw_req = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(960.0)
        sock.connect(socket_path)
        sock.sendall(raw_req)

        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break

    raw_resp = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw_resp:
        return False, "Empty response from CoreIpcServer."

    parsed = json.loads(raw_resp)
    if not isinstance(parsed, dict) or not bool(
        parsed["ok"] if "ok" in parsed else False
    ):
        err_text = (
            str(parsed["error"])
            if isinstance(parsed, dict) and "error" in parsed
            else "IPC request failed"
        )
        return False, err_text
    return True, ""


def _extract_file_path(args: dict[str, str]) -> str:
    for key in _PATH_ARG_KEYS:
        if key in args and args[key]:
            return str(args[key]).strip()
    return ""


def _handle_post_tool_use(raw_stdin: str) -> None:
    if not raw_stdin.strip():
        sys.stdout.write("{}\n")
        return

    parsed = json.loads(raw_stdin)
    if not isinstance(parsed, dict) or "toolCall" not in parsed:
        sys.stdout.write("{}\n")
        return

    tool_call = parsed["toolCall"]
    if not isinstance(tool_call, dict):
        sys.stdout.write("{}\n")
        return

    tool_name = str(tool_call["name"]) if "name" in tool_call else ""
    raw_args = tool_call["args"] if "args" in tool_call else {}
    if not isinstance(raw_args, dict) or tool_name not in _ALL_FILE_TOOLS:
        sys.stdout.write("{}\n")
        return

    str_args = {str(k): str(v) for k, v in raw_args.items() if v is not None}
    target_path = _extract_file_path(str_args)
    if target_path.startswith("/ssh:"):
        _ipc_call(
            "commit_remote_file",
            {
                "path": target_path,
                "cleanup_only": tool_name not in _WRITE_TOOLS,
            },
        )

    sys.stdout.write("{}\n")


def _handle_pre_tool_use(raw_stdin: str) -> None:
    if not raw_stdin.strip():
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    parsed = json.loads(raw_stdin)
    if not isinstance(parsed, dict) or "toolCall" not in parsed:
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    tool_call = parsed["toolCall"]
    if not isinstance(tool_call, dict):
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    tool_name = str(tool_call["name"]) if "name" in tool_call else ""
    exec_mode = (
        os.environ["AGY_EXECUTION_MODE"] if "AGY_EXECUTION_MODE" in os.environ else ""
    )

    if exec_mode == "READ_ONLY" and tool_name in _WRITE_TOOLS:
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

    if tool_name == "schedule":
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": "Use native eager MCP tool 'mcp_telegram-core_schedule_task' or 'mcp_telegram-core_create_sensor'.",
                }
            )
            + "\n"
        )
        return

    raw_args = tool_call["args"] if "args" in tool_call else {}
    str_args = (
        {str(k): str(v) for k, v in raw_args.items() if v is not None}
        if isinstance(raw_args, dict)
        else {}
    )

    if tool_name == "run_command":
        cmd_line = str_args["CommandLine"] if "CommandLine" in str_args else ""
        cleaned_cmd = _SAFE_FD_REDIRECT_RE.sub("", cmd_line)
        if _FORBIDDEN_INPLACE_EDIT_RE.search(cleaned_cmd) is not None:
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": (
                            "POLICY VIOLATION: In-place file editing via sed -i / awk -i / perl -pi in run_command is forbidden. "
                            "Use native file tools (read_file, edit_file, write_file) "
                            "on local scratchpad files or remote TRAMP paths (/ssh:user@<alias>:/abs/path)."
                        ),
                    }
                )
                + "\n"
            )
            return
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    if tool_name in _ALL_FILE_TOOLS:
        target_path = _extract_file_path(str_args)
        if not target_path:
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return

        if ":" in target_path and not target_path.startswith("/"):
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": (
                            f"INVALID REMOTE PATH FORMAT: '{target_path}'. "
                            f"Native file tools require an absolute TRAMP path starting with '/ssh:'. "
                            f"Call this tool with '/ssh:{target_path}' instead."
                        ),
                    }
                )
                + "\n"
            )
            return

        if target_path.startswith("/ssh:"):
            if tool_name in ("find_by_name", "grep_search"):
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "deny",
                            "reason": (
                                "SEARCH ON REMOTE HOST: Do not run local find_by_name/grep_search on '/ssh:...' paths. "
                                "Run 'ssh user@<alias> \"rg -n ...\"' or 'ssh user@<alias> \"find ...\"' via run_command, "
                                "then open the specific files with view_file/read_file/edit_file on '/ssh:user@<alias>:/path'."
                            ),
                        }
                    )
                    + "\n"
                )
                return

            ok, err_msg = _ipc_call(
                "stage_remote_file",
                {
                    "path": target_path,
                    "for_write": tool_name in _WRITE_TOOLS,
                },
            )
            if not ok:
                sys.stdout.write(
                    json.dumps({"decision": "deny", "reason": err_msg}) + "\n"
                )
                return
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return

        resolved_local = str(Path(target_path).expanduser().resolve())
        is_allowed_write = resolved_local.startswith(_ALLOWED_LOCAL_WRITE_PREFIXES)
        is_allowed_ro = (tool_name not in _WRITE_TOOLS) and any(
            seg in resolved_local or seg in target_path
            for seg in _ALLOWED_LOCAL_READ_ONLY_SEGMENTS
        )
        if not is_allowed_write and not is_allowed_ro:
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": (
                            f"LOCAL LXC SANDBOX RESTRICTION: Access to '{resolved_local}' inside the LXC brain container is forbidden. "
                            "You may only access your topic workspace (/srv/antigravity/app/runtime_data/workspaces/...), "
                            "inbox (/srv/antigravity/app/runtime_data/inbox/...), host passports (/root/.gemini/config/rules/...), or /tmp. "
                            "To access files on a remote machine, use '/ssh:user@<alias>:/abs/path'."
                        ),
                    }
                )
                + "\n"
            )
            return

    sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")


def main() -> None:
    raw_stdin = sys.stdin.read()
    if len(sys.argv) > 1 and sys.argv[1] == "--post":
        _handle_post_tool_use(raw_stdin)
    else:
        _handle_pre_tool_use(raw_stdin)


if __name__ == "__main__":
    main()
