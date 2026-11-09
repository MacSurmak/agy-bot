import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path

_SAFE_REDIRECT_RE: re.Pattern[str] = re.compile(
    r"(?:\b\d*>&\d+\b|\b\d*>>?\s*/dev/null\b|&>>?\s*/dev/null\b)"
)
_SHELL_FILE_EDIT_RE: re.Pattern[str] = re.compile(
    r"(?:\bsed\s+(?:-[a-zA-Z0-9]*i\b|--in-place\b)"
    r"|\bawk\b[^\n;|]*-i\s+inplace\b"
    r"|\bperl\s+-[a-zA-Z0-9]*i\b"
    r"|\bcat\s*<<"
    r"|\bpython[23]?\s+-c\s+.*(?:\.write\(|\.writelines\(|open\([^\n)]*['\"][wa]['\"]))"
)
_SELF_INSPECT_CMD_RE: re.Pattern[str] = re.compile(
    r"(?:\bantigravity_core\b"
    r"|/srv/antigravity/app/src\b"
    r"|/srv/antigravity/app/config\.yaml\b"
    r"|/srv/antigravity/app/\.env\b"
    r"|bot_ipc\.sock\b"
    r"|telethon_bot\.session\b)"
)
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    }
)
_ALL_FILE_TOOLS: frozenset[str] = frozenset(
    {
        "view_file",
        "read_file",
        "list_dir",
        "find_by_name",
        "grep_search",
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


def _ipc_call(
    method: str, params: dict[str, str | int | bool]
) -> tuple[bool, str]:
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
        sock.settimeout(305.0)
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
    if not isinstance(parsed, dict) or not bool(parsed["ok"] if "ok" in parsed else False):
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


def _is_remote_tramp_or_scp_path(target_path: str) -> bool:
    if target_path.startswith("/ssh:"):
        return True
    return ":" in target_path and not target_path.startswith("/")


def _pre_onboard_ssh_command(cmd_line: str) -> tuple[bool, str]:
    """Resolves ssh targets in PreToolUse before run_command starts its background timer."""
    if "ssh " not in f" {cmd_line} ":
        return True, ""

    segments = [
        seg.strip()
        for seg in re.split(r"(?:&&|\|\||;|\|)", cmd_line)
        if seg.strip()
    ]
    for seg in segments:
        tokens = (
            shlex.split(seg, posix=True)
            if "'" not in seg and '"' not in seg
            else seg.split()
        )
        if not tokens:
            continue
        first_bin = Path(tokens[0]).name
        if first_bin != "ssh" or len(tokens) < 2:
            continue

        ssh_args = tokens[1:]
        proc = subprocess.run(
            ["/usr/bin/ssh", "-G", *ssh_args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            continue

        ssh_user = ""
        hostname = ""
        port = 22
        proxy_jump = ""
        for line in proc.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                if parts[0] == "user":
                    ssh_user = parts[1]
                elif parts[0] == "hostname":
                    hostname = parts[1]
                elif parts[0] == "port" and parts[1].isdigit():
                    port = int(parts[1])
                elif parts[0] == "proxyjump" and parts[1].lower() != "none":
                    proxy_jump = parts[1]

        if hostname:
            ok, err_msg = _ipc_call(
                "ensure_ssh_target_ready",
                {
                    "ssh_user": ssh_user,
                    "hostname": hostname,
                    "host_alias": hostname,
                    "port": port,
                    "jump_host": proxy_jump,
                },
            )
            if not ok:
                return False, err_msg
    return True, ""


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
    if target_path and _is_remote_tramp_or_scp_path(target_path):
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
                    "reason": "Use MCP tool 'schedule_task' or 'create_sensor' from server 'telegram-core'.",
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
        if _SELF_INSPECT_CMD_RE.search(cmd_line) is not None:
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": "Access to internal Antigravity Core orchestrator files, source code, and secrets is strictly forbidden.",
                    }
                )
                + "\n"
            )
            return

        cleaned_cmd = _SAFE_REDIRECT_RE.sub("", cmd_line)
        if _SHELL_FILE_EDIT_RE.search(cleaned_cmd) is not None:
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": (
                            "Editing or creating files via shell commands or python -c is forbidden. "
                            "Use your native file tools (view_file, read_file, write_to_file, replace_file_content, multi_replace_file_content) "
                            "passing remote paths in TRAMP format: '/ssh:user@host:/path' or '/ssh:user@host:~/path'."
                        ),
                    }
                )
                + "\n"
            )
            return

        ok_ssh, err_ssh = _pre_onboard_ssh_command(cmd_line)
        if not ok_ssh:
            sys.stdout.write(
                json.dumps({"decision": "deny", "reason": err_ssh}) + "\n"
            )
            return

        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    if tool_name in _ALL_FILE_TOOLS:
        target_path = _extract_file_path(str_args)
        if target_path and _is_remote_tramp_or_scp_path(target_path):
            if tool_name in ("find_by_name", "grep_search"):
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "deny",
                            "reason": "For searching across remote directories, run 'ssh user@host \"rg ...\"' via run_command, then open specific files with '/ssh:user@host:/path'.",
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
        elif target_path:
            resolved_local = str(Path(target_path).expanduser().resolve())
            allowed_local_prefixes = (
                "/srv/antigravity/app/runtime_data/workspaces",
                "/srv/antigravity/app/runtime_data/inbox",
                "/root/.gemini/config/rules",
                "/tmp",
            )
            if resolved_local.startswith("/srv/antigravity") and not resolved_local.startswith(
                allowed_local_prefixes
            ):
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "deny",
                            "reason": "Access to internal Antigravity Core application code and configuration (/srv/antigravity) is strictly forbidden.",
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
