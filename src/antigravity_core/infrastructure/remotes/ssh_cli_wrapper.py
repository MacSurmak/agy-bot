"""Native OpenSSH Match-exec hook, SSH_ASKPASS helper, and PTY sudo bridge."""

import errno
import json
import os
import pty
import select
import socket
import sys
import termios
from typing import Final

_SUDO_PROMPT_MARKERS: Final[tuple[bytes, ...]] = (
    b"[sudo] password for ",
    b"Password:",
    b"password for ",
)


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


def _contains_sudo_prompt(buffer_tail: bytes, master_fd: int) -> bool:
    if not any(marker in buffer_tail for marker in _SUDO_PROMPT_MARKERS):
        return False
    tty_attrs = termios.tcgetattr(master_fd)
    lflag = int(tty_attrs[3])
    return (lflag & termios.ECHO) == 0


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

    child_env = {
        **os.environ,
        "LC_ALL": "C",
    }

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
            ["/usr/bin/ssh", "-tt", "-o", "LogLevel=ERROR", *ssh_argv],
            child_env,
        )

    os.close(slave_fd)
    cached_sudo_secret: str | None = None
    rolling_tail = b""
    cmd_display = " ".join(ssh_argv)

    try:
        while True:
            rlist, _, _ = select.select([master_fd], [], [], 0.1)
            if not rlist:
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
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

            rolling_tail = (rolling_tail + chunk)[-256:]
            if _contains_sudo_prompt(rolling_tail, master_fd):
                rolling_tail = b""
                if cached_sudo_secret is None:
                    ok, auth_res, err_msg = _ipc_call(
                        "authorize_pty_sudo",
                        {
                            "target": target_label,
                            "port": port,
                            "command": cmd_display,
                        },
                    )
                    if not ok:
                        os.write(master_fd, b"\x03\n")
                        sys.stderr.write(f"\nSudo rejected: {err_msg}\n")
                        continue

                    cached_sudo_secret = (
                        auth_res["sudo_password"] if "sudo_password" in auth_res else ""
                    )

                os.write(master_fd, (cached_sudo_secret + "\n").encode("utf-8"))
            else:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
    finally:
        os.close(master_fd)

    _, final_status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(final_status)


def main() -> int:
    return _run_pty_ssh_bridge(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
