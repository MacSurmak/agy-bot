"""Native OpenSSH Match-exec hook, SSH_ASKPASS helper, and PTY sudo bridge."""

import errno
import json
import os
import pty
import secrets
import select
import shlex
import socket
import sys
from typing import Final

_REMOTE_OPTIONS_WITH_ARG: Final[frozenset[str]] = frozenset(
    {
        "-b",
        "-c",
        "-D",
        "-E",
        "-e",
        "-F",
        "-I",
        "-i",
        "-J",
        "-L",
        "-l",
        "-m",
        "-O",
        "-o",
        "-p",
        "-Q",
        "-R",
        "-S",
        "-W",
        "-w",
    }
)


def _prepare_ssh_command(
    ssh_argv: list[str], prompt_marker: str
) -> tuple[bool, list[str]]:
    """Inject non-interactive pager env vars and detect whether PTY allocation for sudo is needed."""
    idx = 0
    n = len(ssh_argv)
    while idx < n:
        arg = ssh_argv[idx]
        if arg == "--":
            idx += 1
            break
        if arg.startswith("-"):
            if arg in _REMOTE_OPTIONS_WITH_ARG:
                idx += 2
            else:
                idx += 1
            continue
        break

    if idx >= n - 1:
        return False, list(ssh_argv)

    host_and_opts = ssh_argv[: idx + 1]
    remote_cmd_parts = ssh_argv[idx + 1 :]
    raw_remote_cmd = " ".join(remote_cmd_parts)
    needs_pty = "sudo" in raw_remote_cmd

    path_export = 'export GIT_PAGER=cat PAGER=cat PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"'
    if needs_pty:
        wrapped_cmd = (
            f"{path_export} SUDO_PROMPT={shlex.quote(prompt_marker)}; "
            f"{raw_remote_cmd}"
        )
    elif "\n" in raw_remote_cmd or "setsid" in raw_remote_cmd:
        wrapped_cmd = f"{path_export}; {raw_remote_cmd}"
    else:
        quoted_cmd = shlex.quote(raw_remote_cmd)
        wrapped_cmd = (
            f"{path_export}; "
            "if command -v rtk >/dev/null 2>&1; then "
            f'__agy_rw="$(rtk rewrite {quoted_cmd} 2>/dev/null || true)"; '
            f'if [ -n "$__agy_rw" ]; then eval "$__agy_rw"; else {raw_remote_cmd}; fi; '
            f"else {raw_remote_cmd}; fi"
        )
    return needs_pty, [*host_and_opts, wrapped_cmd]


def _request_sudo_secret(
    target_label: str,
    port: int,
    cmd_display: str,
    invalidate_existing: bool,
) -> tuple[bool, str, str]:
    method = (
        "invalidate_and_reprompt_sudo"
        if invalidate_existing
        else "authorize_pty_sudo"
    )
    if invalidate_existing:
        sys.stderr.write(
            f"\n[Antigravity Sudo Gateway]: Saved sudo password for {target_label} was rejected. "
            "Waiting for owner to provide the updated password in Telegram...\n"
        )
    else:
        sys.stderr.write(
            f"\n[Antigravity Sudo Gateway]: Waiting for owner to click [Approve Sudo] in Telegram for {target_label}... "
            "DO NOT kill or cancel this task!\n"
        )
    sys.stderr.flush()
    ok, auth_res, err_msg = _ipc_call(
        method,
        {
            "target": target_label,
            "port": port,
            "command": cmd_display,
        },
    )
    if not ok:
        return False, "", err_msg
    sys.stderr.write(
        f"[Antigravity Sudo Gateway]: Owner approved sudo on {target_label}. Executing command...\n"
    )
    sys.stderr.flush()
    secret = auth_res["sudo_password"] if "sudo_password" in auth_res else ""
    return True, secret, ""


def _ipc_call(
    method: str, params: dict[str, str | int | bool]
) -> tuple[bool, dict[str, str], str]:
    if "AGY_CORE_IPC_SOCKET" not in os.environ:
        return False, {}, "Missing AGY_CORE_IPC_SOCKET in environment."
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
        return False, {}, "Empty response from CoreIpcServer."

    parsed = json.loads(raw_resp)
    if not isinstance(parsed, dict) or not bool(
        parsed["ok"] if "ok" in parsed else False
    ):
        err_text = (
            str(parsed["error"])
            if isinstance(parsed, dict) and "error" in parsed
            else "IPC request failed"
        )
        return False, {}, err_text

    res = parsed["result"] if "result" in parsed else {}
    if not isinstance(res, dict):
        return True, {}, ""
    return True, {str(k): str(v) for k, v in res.items() if v is not None}, ""


def _resolve_target_via_openssh(
    ssh_argv: list[str],
) -> tuple[str, str, int, str] | None:
    if not ssh_argv:
        return None
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(w_fd, 1)
        os.dup2(devnull, 2)
        if w_fd > 2:
            os.close(w_fd)
        if devnull > 2:
            os.close(devnull)
        os.execv("/usr/bin/ssh", ["/usr/bin/ssh", "-G", *ssh_argv])

    os.close(w_fd)
    chunks: list[bytes] = []
    while True:
        data = os.read(r_fd, 16384)
        if not data:
            break
        chunks.append(data)
    os.close(r_fd)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        return None

    out_text = b"".join(chunks).decode("utf-8", errors="replace")
    ssh_user = ""
    hostname = ""
    port = 22
    proxy_jump = ""
    for line in out_text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            if parts[0] == "user":
                ssh_user = parts[1]
            elif parts[0] == "hostname":
                hostname = parts[1]
            elif parts[0] == "port" and parts[1].isdigit():
                port = int(parts[1])
            elif parts[0] == "proxyjump" and parts[1].lower() != "none":
                raw_pj = parts[1].replace("[", "").replace("]", "")
                if raw_pj.endswith(":22"):
                    raw_pj = raw_pj[:-3]
                proxy_jump = raw_pj

    clean_host = hostname.replace("[", "").replace("]", "")
    if not clean_host:
        return None
    return ssh_user, clean_host, port, proxy_jump


def _run_pty_ssh_bridge(ssh_argv: list[str]) -> int:
    if "AGY_CORE_IPC_SOCKET" not in os.environ:
        os.execv("/usr/bin/ssh", ["/usr/bin/ssh", *ssh_argv])

    resolved = _resolve_target_via_openssh(ssh_argv)
    if resolved is None:
        os.execv("/usr/bin/ssh", ["/usr/bin/ssh", *ssh_argv])

    ssh_user, hostname, port, proxy_jump = resolved
    target_label = f"{ssh_user}@{hostname}" if ssh_user else hostname

    ok_ready, _res_ready, err_ready = _ipc_call(
        "ensure_ssh_target_ready",
        {
            "ssh_user": ssh_user,
            "hostname": hostname,
            "host_alias": hostname,
            "port": port,
            "jump_host": proxy_jump,
        },
    )
    if not ok_ready:
        sys.stderr.write(f"SSH onboarding failed for {target_label}: {err_ready}\n")
        return 255

    prompt_marker_str = f"__AGY_SUDO_{secrets.token_hex(8)}__:"
    prompt_marker_bytes = prompt_marker_str.encode("ascii")
    needs_pty, effective_argv = _prepare_ssh_command(ssh_argv, prompt_marker_str)

    child_env = {
        **os.environ,
        "LC_ALL": "C",
    }

    if not needs_pty:
        os.execve(
            "/usr/bin/ssh",
            ["/usr/bin/ssh", "-o", "LogLevel=ERROR", *effective_argv],
            child_env,
        )

    master_fd, slave_fd = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execve(
            "/usr/bin/ssh",
            ["/usr/bin/ssh", "-tt", "-o", "LogLevel=ERROR", *effective_argv],
            child_env,
        )

    os.close(slave_fd)
    cached_sudo_secret: str | None = None
    sudo_prompt_count = 0
    pending_buf = b""
    cmd_display = " ".join(ssh_argv)
    keep_tail_len = len(prompt_marker_bytes) - 1

    try:
        while True:
            rlist, _, _ = select.select([master_fd], [], [], 0.1)
            if not rlist:
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    if pending_buf:
                        sys.stdout.buffer.write(pending_buf)
                        sys.stdout.buffer.flush()
                    return os.waitstatus_to_exitcode(status)
                continue

            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise

            if not chunk:
                break

            pending_buf += chunk
            if prompt_marker_bytes in pending_buf:
                before_marker, after_marker = pending_buf.split(prompt_marker_bytes, 1)
                if before_marker:
                    sys.stdout.buffer.write(before_marker)
                    sys.stdout.buffer.flush()
                pending_buf = after_marker

                rejected_retry = (
                    sudo_prompt_count > 0 and b"try again" in before_marker.lower()
                )
                if cached_sudo_secret is None or rejected_retry:
                    ok, new_secret, err_msg = _request_sudo_secret(
                        target_label=target_label,
                        port=port,
                        cmd_display=cmd_display,
                        invalidate_existing=rejected_retry,
                    )
                    if not ok:
                        os.write(master_fd, b"\x03\n")
                        sys.stderr.write(f"\nSudo rejected: {err_msg}\n")
                        sys.stderr.flush()
                        break
                    cached_sudo_secret = new_secret

                sudo_prompt_count += 1
                os.write(master_fd, (cached_sudo_secret + "\n").encode("utf-8"))
            elif len(pending_buf) > keep_tail_len:
                flush_part = pending_buf[:-keep_tail_len]
                pending_buf = pending_buf[-keep_tail_len:]
                sys.stdout.buffer.write(flush_part)
                sys.stdout.buffer.flush()

        if pending_buf:
            sys.stdout.buffer.write(pending_buf)
            sys.stdout.buffer.flush()
    finally:
        os.close(master_fd)

    _, final_status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(final_status)


def _with_shared_ssh_config(ssh_argv: list[str]) -> list[str]:
    if "AGY_SSH_CONFIG" not in os.environ or "-F" in ssh_argv:
        return list(ssh_argv)
    ssh_cfg = os.environ["AGY_SSH_CONFIG"].strip()
    if not ssh_cfg:
        return list(ssh_argv)
    return ["-F", ssh_cfg, *ssh_argv]


def main() -> int:
    return _run_pty_ssh_bridge(_with_shared_ssh_config(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
