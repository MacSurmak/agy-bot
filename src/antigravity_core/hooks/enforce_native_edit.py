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

_DENY_REASON = (
    "Use native file tools (view_file, write_to_file, replace_file_content, "
    "multi_replace_file_content) on local or /mnt/remotes/<host> paths."
)


def normalize_and_strip_safe_redirects(command_line: str) -> str:
    cleaned = _SAFE_DEV_NULL_AMP_RE.sub(" ", command_line)
    cleaned = _SAFE_DEV_NULL_FD_RE.sub(" ", cleaned)
    cleaned = _SAFE_FD_DUP_RE.sub(" ", cleaned)
    return cleaned


def evaluate_command_line(command_line: str) -> dict[str, str]:
    normalized = normalize_and_strip_safe_redirects(command_line)
    for pattern, _label in _FORBIDDEN_PATTERNS:
        if pattern.search(normalized) is not None:
            return {
                "decision": "deny",
                "reason": _DENY_REASON,
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

    if tool_name != "run_command":
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    args = tool_call.get("args")
    if not isinstance(args, dict):
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    command_line = str(args.get("CommandLine", ""))
    decision = evaluate_command_line(command_line)
    sys.stdout.write(json.dumps(decision) + "\n")


if __name__ == "__main__":
    main()
