import json
import os
import re
import sys
from typing import Any

# Errata Patch #2 - Phase 1: Safe descriptor redirects and /dev/null sinks to strip before analysis
_SAFE_FD_DUP_RE = re.compile(r"(?:^|(?<=[\s;|&()])|\b\d+)>&\d+\b")
_SAFE_DEV_NULL_FD_RE = re.compile(r"(?:^|(?<=[\s;|&()])|\b\d+)>>?\s*/dev/null\b")
_SAFE_DEV_NULL_AMP_RE = re.compile(r"&>>?\s*/dev/null\b")

# Errata Patch #2 - Phase 2: Forbidden file modification and stream editing patterns
_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsed\s+(?:-[a-zA-Z0-9]*i\b|--in-place\b)"), "sed -i in-place file editing"),
    (re.compile(r"\bawk\b[^\n;|]*-i\s+inplace\b"), "awk -i inplace file editing"),
    (re.compile(r"\bperl\s+-[a-zA-Z0-9]*p[a-zA-Z0-9]*i\b|\bperl\s+-[a-zA-Z0-9]*i[a-zA-Z0-9]*p\b"), "perl -pi in-place file editing"),
    (re.compile(r"(?:^|[|;&\s])tee\s+"), "tee file writing"),
    (re.compile(r"\bdd\b[^\n;|]*\bof="), "dd of= file writing"),
    (re.compile(r"\btruncate\s+"), "truncate file modification"),
    (re.compile(r"\bcat\s*<<"), "cat heredoc file creation"),
    (re.compile(r"<<\s*['\"]?\w+['\"]?[^\n]*>"), "heredoc redirection to file"),
    (re.compile(r"<<<[^\n]*>"), "herestring redirection to file"),
    (re.compile(r">>"), "append redirection (>>)"),
    # Catch single '>' preceded by whitespace, delimiter, or descriptor digit, excluding '->', '=>', '>='
    (
        re.compile(r"(?:^|[\s;|&()]|\b\d)(?<![-=>])>\s*(?![&\s=>]*$)[^\s;|&>]+"),
        "output redirection (>) to file",
    ),
    # Block inline python one-liners attempting file writes via run_command
    (
        re.compile(
            r"\bpython3?\b[^\n]*-c\b[^\n]*(?:\.write(?:_text|_bytes)?\s*\(|open\s*\([^)]*['\"][wa+])"
        ),
        "inline python file write",
    ),
)

# Block unguarded recursive grep -r / grep -rn (unless bounded by both timeout and --include)
_RECURSIVE_GREP_RE = re.compile(
    r"\bgrep\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b"
)
_INLINE_SCRIPT_BODY_RE = re.compile(
    r"\b(?:python3?|awk|perl)\b[^\n]*?(?:-c\s+(['\"])[\s\S]*?\1|'[\s\S]*?')"
)

# Block local `cat <file>` (when command is not an ssh invocation and not reading /proc or /sys)
_LOCAL_CAT_READ_RE = re.compile(
    r"(?:^|[;&|]\s*)cat\s+(?!/proc/|/sys/|/etc/os-release)[^\s|;&><]+"
)
_SSH_COMMAND_RE = re.compile(r"(?:^|[;&|]\s*)ssh\b")

# Errata Patch #3: Block recursive traversal (find, rg, grep, fd, du, tree, ls -R) over local SSHFS mounts (/remotes/<host>)
_SSHFS_TRAVERSAL_RE = re.compile(
    r"\b(?:find|rg|ripgrep|grep|fd|fdfind|du|tree|ls\s+-[a-zA-Z]*R)\b[^\n;|]*?(?:/mnt/remotes/|/runtime_data/remotes/)"
)
_SSHFS_PATH_RE = re.compile(r"(?:/mnt/remotes/|/runtime_data/remotes/)")

_DENY_REASON = (
    "Use native file tools (read_file, view_file, write_to_file, replace_file_content, "
    "multi_replace_file_content) on local or /mnt/remotes/<host> paths."
)

_DENY_RECURSIVE_GREP_REASON = (
    "Recursive 'grep -r / -rn' is forbidden because it hangs on binary/cache/virtual directories. "
    "Locally, use native grep_search / find_by_name tools or 'rg --max-filesize 1M'. "
    "Over SSH, always wrap searches with 'timeout 10 rg -n --max-filesize 1M' and exclude /var, /proc, /sys."
)

_DENY_LOCAL_CAT_REASON = (
    "Reading local files via 'cat' in run_command is forbidden because it floods stdout. "
    "Use your native read_file / view_file tool instead."
)

_DENY_SSHFS_TRAVERSAL_REASON = (
    "FORBIDDEN (Errata Patch #3): Running recursive search/traversal (find, rg, grep, fd, du, tree) "
    "over a local SSHFS FUSE mount (/remotes/<host>/...) causes hundreds of thousands of slow SFTP "
    "round-trips and hangs! Run searches directly on the remote CPU via 'ssh <host> \"timeout 10 find /path -maxdepth 4 ...\"' "
    "or 'ssh <host> \"timeout 10 rg ...\"', and use /remotes/<host>/... ONLY to read/edit specific known files."
)


def normalize_and_strip_safe_redirects(command_line: str) -> str:
    cleaned = _SAFE_DEV_NULL_AMP_RE.sub(" ", command_line)
    cleaned = _SAFE_DEV_NULL_FD_RE.sub(" ", cleaned)
    cleaned = _SAFE_FD_DUP_RE.sub(" ", cleaned)
    return cleaned


def evaluate_command_line(command_line: str) -> dict[str, str]:
    normalized = normalize_and_strip_safe_redirects(command_line)
    # Strip inline script bodies before checking shell '>' redirection so 'if x > 10' in python -c is not mistaken for file redirection
    shell_only = _INLINE_SCRIPT_BODY_RE.sub(" ", normalized)

    # Allow standard daemon stdout/stderr log redirection when launching background processes via nohup
    is_nohup_daemon = bool(re.search(r"\bnohup\b", normalized))

    for pattern, label in _FORBIDDEN_PATTERNS:
        if is_nohup_daemon and label in (
            "output redirection (>) to file",
            "append redirection (>>)",
        ):
            continue
        target_str = normalized if label == "inline python file write" else shell_only
        if pattern.search(target_str) is not None:
            return {
                "decision": "deny",
                "reason": _DENY_REASON,
            }

    if _RECURSIVE_GREP_RE.search(normalized) is not None:
        has_timeout = "timeout " in normalized
        has_include = "--include=" in normalized
        if not (has_timeout and has_include):
            return {
                "decision": "deny",
                "reason": _DENY_RECURSIVE_GREP_REASON,
            }

    if _SSHFS_TRAVERSAL_RE.search(normalized) is not None:
        return {
            "decision": "deny",
            "reason": _DENY_SSHFS_TRAVERSAL_REASON,
        }

    if (
        _SSH_COMMAND_RE.search(normalized) is None
        and _LOCAL_CAT_READ_RE.search(normalized) is not None
    ):
        return {
            "decision": "deny",
            "reason": _DENY_LOCAL_CAT_REASON,
        }

    return {"decision": "allow"}


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

    args = tool_call.get("args")
    if not isinstance(args, dict):
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    if tool_name == "schedule":
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        "FORBIDDEN: Do NOT use the built-in 'schedule' tool because it blocks the current turn. "
                        "You MUST use the non-blocking MCP tool 'schedule_task' from server 'telegram-core' "
                        "(call_mcp_tool with ServerName='telegram-core', ToolName='schedule_task', "
                        "schedule_kind='ONE_SHOT', trigger_spec='+5m')."
                    ),
                }
            )
            + "\n"
        )
        return

    if tool_name in ("find_by_name", "grep_search"):
        search_target = str(
            args.get("SearchDirectory")
            or args.get("SearchPath")
            or args.get("DirectoryPath")
            or ""
        )
        if _SSHFS_PATH_RE.search(search_target) is not None:
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": _DENY_SSHFS_TRAVERSAL_REASON,
                    }
                )
                + "\n"
            )
            return
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    if tool_name != "run_command":
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    command_line = str(args.get("CommandLine", ""))
    decision = evaluate_command_line(command_line)
    sys.stdout.write(json.dumps(decision) + "\n")


if __name__ == "__main__":
    main()
