"""Native OpenSSH Match-exec hook, SSH_ASKPASS helper, and PTY sudo bridge."""

import errno
import json
import os
import pty
import select
import socket
import sys
from pathlib import Path

_SUDO_PROMPT_MARKERS: tuple[bytes, ...] = (
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
    if not isinstance(parsed, dict) or not bool(parsed["ok"] if "ok" in parsed else False):
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


def _target_state_file(session_id: str) -> Path:
    if "AGY_CORE_IPC_SOCKET" in os.environ:
        run_dir = Path(os.environ["AGY_CORE_IPC_SOCKET"]).parent
    else:
        run_dir = Path("/tmp")
    return run_dir / f".ssh_target_{session_id}.json"


def _run_askpass_mode() -> int:
    if "AGY_ASKPASS_SECRET_TOKEN" not in os.environ:
        sys.stderr.write("Missing AGY_ASKPASS_SECRET_TOKEN.\n")
        return 1
    token = os.environ["AGY_ASKPASS_SECRET_TOKEN"]
    ok, res, err_msg = _ipc_call("consume_askpass_token", {"token": token})
    if not ok or "secret" not in res:
        sys.stderr.write(f"Askpass error: {err_msg}\n")
        return 1
    sys.stdout.write(res["secret"] + "\n")
    sys.stdout.flush()
    return 0


def _run_match_hook_mode(argv: list[str]) -> int:
    if "AGY_INTERNAL_SSH" in os.environ or "AGY_CORE_IPC_SOCKET" not in os.environ:
        return 0
    if len(argv) < 4:
        return 0

    ssh_user = argv[0].strip()
    hostname = argv[1].strip()
    host_alias = argv[2].strip()
    port = int(argv[3]) if argv[3].strip().isdigit() else 22

    ok, _res, err_msg = _ipc_call(
        "ensure_ssh_target_ready",
        {
            "ssh_user": ssh_user,
            "hostname": hostname,
            "host_alias": host_alias,
            "port": port,
        },
    )
    if not ok:
        sys.stderr.write(f"SSH onboarding aborted: {err_msg}\n")
        return 1

    if "AGY_SSH_WRAPPER_SID" in os.environ:
        sid = os.environ["AGY_SSH_WRAPPER_SID"]
        state_path = _target_state_file(sid)
        state_payload = {
            "ssh_user": ssh_user,
            "hostname": hostname,
            "host_alias": host_alias,
            "port": port,
        }
        state_path.write_text(json.dumps(state_payload), encoding="utf-8")

    return 0


def _contains_sudo_prompt(buffer_tail: bytes) -> bool:
    return any(marker in buffer_tail for marker in _SUDO_PROMPT_MARKERS)


def _run_pty_ssh_bridge(ssh_argv: list[str]) -> int:
    if "AGY_CORE_IPC_SOCKET" not in os.environ:
        os.execv("/usr/bin/ssh", ["/usr/bin/ssh", *ssh_argv])

    sid = f"{os.getpid()}"
    state_file = _target_state_file(sid)
    child_env = {
        **os.environ,
        "AGY_SSH_WRAPPER_SID": sid,
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
            if _contains_sudo_prompt(rolling_tail):
                rolling_tail = b""
                if cached_sudo_secret is None:
                    target_label = "remote"
                    port = 22
                    if state_file.is_file():
                        loaded = json.loads(state_file.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            u_str = str(loaded["ssh_user"]) if "ssh_user" in loaded else ""
                            h_str = str(loaded["host_alias"]) if "host_alias" in loaded else ""
                            target_label = f"{u_str}@{h_str}" if u_str else h_str
                            if "port" in loaded:
                                port = int(loaded["port"])

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
        if state_file.exists():
            state_file.unlink()

    _, final_status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(final_status)


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 1 and args[0] == "--askpass-mode":
        return _run_askpass_mode()
    if len(args) >= 1 and args[0] == "--match-hook":
        return _run_match_hook_mode(args[1:])
    return _run_pty_ssh_bridge(args)


if __name__ == "__main__":
    sys.exit(main())
