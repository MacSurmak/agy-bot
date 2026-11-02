import asyncio
import base64
import contextlib
import fcntl
import os
import pty
import re
import signal
import struct
import termios
from dataclasses import dataclass
from pathlib import Path

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import (
    AccountAuthFlowError,
    KeyringInitializationError,
)
from antigravity_core.domain.models import GoogleAccount
from antigravity_core.infrastructure.crypto.fernet_vault import FernetVault
from antigravity_core.infrastructure.database.repositories import AccountRepository

logger = structlog.get_logger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07")
_TUI_NOISE_RE = re.compile(r"[─━═│┃┌┐└┘├┤┬┴┼\ufffd]+")
_OAUTH_URL_PATTERN = re.compile(r"(https://accounts\.google\.com/[^\s\"'<>]+)")


@dataclass(slots=True)
class _ActiveProfileRuntime:
    account_id: int
    slug: str
    dbus_pid: int | None
    env: dict[str, str]


@dataclass(slots=True)
class PendingOAuthSession:
    account: GoogleAccount
    process: asyncio.subprocess.Process
    pty_master_fd: int
    auth_url: str


class KeyringProfileManager:
    """Manages isolated D-Bus sessions and two-phase gnome-keyring-daemon instances per GoogleAccount."""

    def __init__(
        self,
        static_config: StaticConfig,
        vault: FernetVault,
        account_repo: AccountRepository,
    ) -> None:
        self._cfg = static_config
        self._vault = vault
        self._account_repo = account_repo
        self._runtimes: dict[int, _ActiveProfileRuntime] = {}
        self._lock = asyncio.Lock()

    async def ensure_account_bus(self, account: GoogleAccount) -> dict[str, str]:
        async with self._lock:
            existing = self._runtimes.get(account.id)
            socket_path = Path(account.dbus_socket_path)
            if existing is not None and socket_path.exists():
                if await self._verify_secret_service(existing.env):
                    return dict(existing.env)

            return await self._initialize_account_bus_locked(account)

    async def _is_unix_socket_alive(self, socket_path: Path) -> bool:
        if not socket_path.exists():
            return False
        try:
            _reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            return False

    async def _initialize_account_bus_locked(self, account: GoogleAccount) -> dict[str, str]:
        xdg_dir = Path(account.xdg_data_dir)
        keyrings_dir = xdg_dir / "keyrings"
        acc_runtime_dir = xdg_dir / "run"
        keyrings_dir.mkdir(parents=True, exist_ok=True)
        acc_runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(xdg_dir, 0o700)
        os.chmod(keyrings_dir, 0o700)
        os.chmod(acc_runtime_dir, 0o700)

        default_ptr = keyrings_dir / "default"
        if not default_ptr.exists():
            default_ptr.write_text("login\n", encoding="utf-8")
            os.chmod(default_ptr, 0o600)

        socket_path = Path(account.dbus_socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        dbus_pid: int | None = None
        if not await self._is_unix_socket_alive(socket_path):
            if socket_path.exists():
                socket_path.unlink()
            dbus_pid = await self._start_dbus_daemon(socket_path)

        base_env: dict[str, str] = {
            "PATH": self._cfg.paths.system_path_env,
            "HOME": str(self._cfg.paths.agy_home_dir.parent),
            "USER": self._cfg.paths.system_user,
            "LOGNAME": self._cfg.paths.system_logname,
            "XDG_DATA_HOME": str(xdg_dir),
            "XDG_RUNTIME_DIR": str(acc_runtime_dir),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={socket_path}",
        }

        raw_password = self._vault.decrypt_text(account.encrypted_keyring_pass)

        # Errata Patch #5 - Step 1: Unlock keyring via --daemonize --login without trailing newline
        login_env_vars = await self._run_keyring_login_phase(base_env, raw_password)
        profile_env = {**base_env, **login_env_vars}

        # Errata Patch #5 - Step 2: Activate Secret Service component via --start --components=secrets
        start_env_vars = await self._run_keyring_start_phase(profile_env)
        profile_env.update(start_env_vars)

        # Errata Patch #5 - Step 3: Strict Fail-Fast verification via secret-tool
        if not await self._verify_secret_service(profile_env):
            raise KeyringInitializationError(
                f"Secret Service verification failed on account '{account.slug}' "
                f"(socket={socket_path}, xdg={xdg_dir})."
            )

        # Restore OAuth token or login.keyring backup from PostgreSQL if keyring is fresh
        has_token = await self._has_gemini_secret(profile_env)
        keyring_file = keyrings_dir / "login.keyring"
        if not has_token and account.encrypted_token_backup is not None:
            restored_payload = self._vault.decrypt_text(account.encrypted_token_backup)
            if restored_payload.startswith("keyring-b64:"):
                b64_data = restored_payload[len("keyring-b64:") :]
                if not keyring_file.exists():
                    keyring_file.write_bytes(base64.b64decode(b64_data.encode("ascii")))
                    os.chmod(keyring_file, 0o600)
                    logger.info(
                        "Restored login.keyring binary from Fernet backup",
                        account_slug=account.slug,
                    )
            elif not restored_payload.startswith("oauth-verified:"):
                await self._store_gemini_secret(profile_env, restored_payload)
                logger.info(
                    "Restored OAuth token from Fernet backup into keyring",
                    account_slug=account.slug,
                )

        self._runtimes[account.id] = _ActiveProfileRuntime(
            account_id=account.id,
            slug=account.slug,
            dbus_pid=dbus_pid,
            env=profile_env,
        )
        logger.info(
            "Initialized isolated D-Bus & gnome-keyring profile",
            account_slug=account.slug,
            dbus_socket=str(socket_path),
        )
        return dict(profile_env)

    async def _start_dbus_daemon(self, socket_path: Path) -> int:
        proc = await asyncio.create_subprocess_exec(
            "dbus-daemon",
            "--session",
            f"--address=unix:path={socket_path}",
            "--nofork",
            "--nopidfile",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        for _ in range(self._cfg.runtime.dbus_socket_poll_attempts):
            if socket_path.exists():
                return proc.pid
            if proc.returncode is not None:
                stderr_bytes = await proc.stderr.read() if proc.stderr else b""
                raise KeyringInitializationError(
                    f"dbus-daemon exited prematurely with code {proc.returncode}: "
                    f"{stderr_bytes.decode('utf-8', errors='replace')}"
                )
            await asyncio.sleep(self._cfg.runtime.dbus_socket_poll_interval_sec)

        raise KeyringInitializationError(
            f"Timed out waiting for dbus-daemon to create socket at {socket_path}"
        )

    async def _run_keyring_login_phase(
        self, env: dict[str, str], raw_password: str
    ) -> dict[str, str]:
        proc = await asyncio.create_subprocess_exec(
            "gnome-keyring-daemon",
            "--daemonize",
            "--login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise KeyringInitializationError("Failed to open standard pipes for gnome-keyring-daemon --login.")

        # Errata Patch #5: Write password bytes strictly WITHOUT trailing '\n' and close stdin (EOF)
        proc.stdin.write(raw_password.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cfg.runtime.command_exec_timeout_sec,
            )
        except TimeoutError as exc:
            proc.kill()
            raise KeyringInitializationError(
                "gnome-keyring-daemon --daemonize --login timed out."
            ) from exc

        if proc.returncode != 0:
            raise KeyringInitializationError(
                f"gnome-keyring-daemon --login failed (code {proc.returncode}): "
                f"{stderr_bytes.decode('utf-8', errors='replace')}"
            )

        return self._parse_keyring_env_output(stdout_bytes.decode("utf-8", errors="replace"))

    async def _run_keyring_start_phase(self, env: dict[str, str]) -> dict[str, str]:
        proc = await asyncio.create_subprocess_exec(
            "gnome-keyring-daemon",
            "--start",
            "--components=secrets",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cfg.runtime.command_exec_timeout_sec,
            )
        except TimeoutError as exc:
            proc.kill()
            raise KeyringInitializationError(
                "gnome-keyring-daemon --start --components=secrets timed out."
            ) from exc

        if proc.returncode != 0:
            raise KeyringInitializationError(
                f"gnome-keyring-daemon --start --components=secrets failed (code {proc.returncode}): "
                f"{stderr_bytes.decode('utf-8', errors='replace')}"
            )

        return self._parse_keyring_env_output(stdout_bytes.decode("utf-8", errors="replace"))

    @staticmethod
    def _parse_keyring_env_output(output: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" in line:
                key, val = line.split("=", 1)
                val = val.rstrip(";").strip("'\"")
                parsed[key.strip()] = val
        return parsed

    async def _verify_secret_service(self, env: dict[str, str]) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "secret-tool",
            "lookup",
            "service",
            "gemini",
            "account",
            "antigravity",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cfg.runtime.command_exec_timeout_sec,
            )
        except TimeoutError:
            proc.kill()
            return False

        # Return code 0 means secret found; return code 1 with empty stderr means bus is healthy but secret is absent
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode in (0, 1) and not stderr_text:
            return True
        logger.warning(
            "secret-tool lookup reported D-Bus/SecretService error",
            returncode=proc.returncode,
            stderr=stderr_text,
        )
        return False

    async def _has_gemini_secret(self, env: dict[str, str]) -> bool:
        secret = await self._lookup_gemini_secret(env)
        return secret is not None and len(secret) > 0

    async def _lookup_gemini_secret(self, env: dict[str, str]) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "secret-tool",
            "lookup",
            "service",
            "gemini",
            "account",
            "antigravity",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=self._cfg.runtime.command_exec_timeout_sec,
        )
        if proc.returncode != 0:
            return None
        secret = stdout_bytes.decode("utf-8", errors="replace")
        return secret if secret else None

    async def _store_gemini_secret(self, env: dict[str, str], secret_value: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "secret-tool",
            "store",
            "--label=Antigravity",
            "service",
            "gemini",
            "account",
            "antigravity",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if proc.stdin is None:
            raise KeyringInitializationError("Failed to open stdin for secret-tool store.")
        proc.stdin.write(secret_value.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        _, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=self._cfg.runtime.command_exec_timeout_sec,
        )
        if proc.returncode != 0:
            raise KeyringInitializationError(
                f"Failed to store gemini:antigravity secret into keyring: "
                f"{stderr_bytes.decode('utf-8', errors='replace')}"
            )

    async def _clear_gemini_secret(self, env: dict[str, str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            "secret-tool",
            "clear",
            "service",
            "gemini",
            "account",
            "antigravity",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        await asyncio.wait_for(
            proc.wait(),
            timeout=self._cfg.runtime.command_exec_timeout_sec,
        )

    def _sanitize_tui_output(self, raw_text: str) -> str:
        no_ansi = _ANSI_ESCAPE_RE.sub(" ", raw_text)
        no_boxes = _TUI_NOISE_RE.sub(" ", no_ansi)
        collapsed = " ".join(no_boxes.split())
        max_chars = self._cfg.limits.error_summary_max_chars
        return collapsed[-max_chars:] if len(collapsed) > max_chars else collapsed

    async def backup_account_token_to_db(self, account: GoogleAccount) -> bool:
        env = await self.ensure_account_bus(account)
        secret_val = await self._lookup_gemini_secret(env)
        if secret_val:
            encrypted = self._vault.encrypt_text(secret_val)
            await self._account_repo.update_token_backup(account.id, encrypted)
            return True

        keyrings_dir = Path(account.xdg_data_dir) / "keyrings"
        keyring_file = keyrings_dir / "login.keyring"
        if not keyring_file.is_file():
            candidates = sorted(keyrings_dir.glob("*.keyring"))
            if candidates:
                keyring_file = candidates[0]

        if keyring_file.is_file() and keyring_file.stat().st_size > 0:
            b64_str = base64.b64encode(keyring_file.read_bytes()).decode("ascii")
            encrypted = self._vault.encrypt_text(f"keyring-b64:{b64_str}")
            await self._account_repo.update_token_backup(account.id, encrypted)
            return True

        return False

    async def is_account_authenticated(self, env: dict[str, str]) -> bool:
        """Deterministically verifies if agy recognizes the account as signed in via `agy models`."""
        proc = await asyncio.create_subprocess_exec(
            str(self._cfg.paths.agy_binary_path),
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return False

        combined = (
            stdout_bytes.decode("utf-8", errors="replace")
            + "\n"
            + stderr_bytes.decode("utf-8", errors="replace")
        )
        return proc.returncode == 0 and "Please sign in" not in combined

    def _spawn_agy_pty_process(self) -> tuple[int, int]:
        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 50, 2000, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        os.set_blocking(master_fd, False)
        return master_fd, slave_fd

    async def _purge_existing_session_if_signed_in(
        self, account: GoogleAccount, oauth_env: dict[str, str], workspace_cwd: str
    ) -> None:
        await self._clear_gemini_secret(oauth_env)
        if not await self.is_account_authenticated(oauth_env):
            return

        logger.info(
            "Account is currently signed in; running /logout in PTY to reset session cleanly",
            account_slug=account.slug,
        )
        master_fd, slave_fd = self._spawn_agy_pty_process()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._cfg.paths.agy_binary_path),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=workspace_cwd,
                env=oauth_env,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

        try:
            accumulated = ""
            sent_trust = False
            for _ in range(30):
                try:
                    chunk = os.read(master_fd, 4096)
                    if chunk:
                        accumulated += chunk.decode("utf-8", errors="replace")
                except (BlockingIOError, OSError):
                    pass
                clean = _ANSI_ESCAPE_RE.sub("", accumulated)
                if not sent_trust and (
                    "Do you trust the contents" in clean or "Yes, I trust this folder" in clean
                ):
                    os.write(master_fd, b"\r")
                    sent_trust = True
                    await asyncio.sleep(0.5)
                    break
                await asyncio.sleep(0.1)

            await asyncio.sleep(0.6)
            os.write(master_fd, b"/logout\r")
            await asyncio.sleep(1.5)
        finally:
            self._cleanup_oauth_pty(proc, master_fd)

    async def start_remote_oauth_flow(self, account: GoogleAccount) -> PendingOAuthSession:
        env = await self.ensure_account_bus(account)
        oauth_env = {
            **env,
            "SSH_CONNECTION": "1",
            "TERM": "xterm-256color",
            "COLUMNS": "2000",
            "LINES": "50",
            "BROWSER": "/bin/false",
        }
        oauth_env.pop("DISPLAY", None)
        oauth_env.pop("WAYLAND_DISPLAY", None)

        workspace_cwd = str(self._cfg.paths.workspaces_root_dir)
        Path(workspace_cwd).mkdir(parents=True, exist_ok=True)

        await self._purge_existing_session_if_signed_in(account, oauth_env, workspace_cwd)

        master_fd, slave_fd = self._spawn_agy_pty_process()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._cfg.paths.agy_binary_path),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=workspace_cwd,
                env=oauth_env,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

        accumulated = ""
        sent_trust_enter = False
        sent_method_enter = False
        chunk_size = self._cfg.runtime.oauth_stdout_chunk_bytes
        poll_interval = self._cfg.runtime.dbus_socket_poll_interval_sec

        logger.info(
            "Started agy interactive PTY for Remote SSH OAuth flow",
            account_slug=account.slug,
            pid=proc.pid,
        )
        try:
            async with asyncio.timeout(self._cfg.runtime.command_exec_timeout_sec):
                while True:
                    try:
                        chunk = os.read(master_fd, chunk_size)
                    except BlockingIOError:
                        if proc.returncode is not None:
                            break
                        await asyncio.sleep(poll_interval)
                        continue
                    except OSError:
                        break

                    if not chunk:
                        break

                    decoded_chunk = chunk.decode("utf-8", errors="replace")
                    accumulated += decoded_chunk
                    clean_text = _ANSI_ESCAPE_RE.sub("", accumulated)

                    if not sent_trust_enter and (
                        "Do you trust the contents" in clean_text
                        or "Yes, I trust this folder" in clean_text
                    ):
                        await asyncio.sleep(poll_interval * 4)
                        os.write(master_fd, b"\r")
                        sent_trust_enter = True
                        accumulated = ""
                        logger.info(
                            "Confirmed workspace trust dialog in OAuth startup PTY",
                            account_slug=account.slug,
                        )
                        continue

                    if not sent_method_enter and (
                        "Select login method" in clean_text
                        or "Google OAuth" in clean_text
                    ):
                        await asyncio.sleep(poll_interval * 4)
                        os.write(master_fd, b"\r")
                        sent_method_enter = True
                        logger.info(
                            "Selected '1. Google OAuth' in agy TUI prompt",
                            account_slug=account.slug,
                        )

                    match = _OAUTH_URL_PATTERN.search(clean_text)
                    if match is not None and "response_type=" in match.group(1):
                        await asyncio.sleep(poll_interval * 4)
                        with contextlib.suppress(BlockingIOError, OSError):
                            while True:
                                extra = os.read(master_fd, chunk_size)
                                if not extra:
                                    break
                                accumulated += extra.decode("utf-8", errors="replace")
                        clean_text = _ANSI_ESCAPE_RE.sub("", accumulated)
                        full_match = _OAUTH_URL_PATTERN.search(clean_text)
                        if full_match is not None:
                            auth_url = full_match.group(1).strip()
                            logger.info(
                                "Captured complete OAuth URL from agy PTY",
                                account_slug=account.slug,
                                auth_url=auth_url,
                            )
                            return PendingOAuthSession(
                                account=account,
                                process=proc,
                                pty_master_fd=master_fd,
                                auth_url=auth_url,
                            )
        except TimeoutError as exc:
            self._cleanup_oauth_pty(proc, master_fd)
            summary = self._sanitize_tui_output(accumulated)
            logger.error(
                "Timed out waiting for OAuth URL from agy PTY",
                account_slug=account.slug,
                output=summary,
            )
            raise AccountAuthFlowError(
                f"Timed out waiting for OAuth URL from agy PTY. Output: {summary or '<empty>'}"
            ) from exc

        self._cleanup_oauth_pty(proc, master_fd)
        summary = self._sanitize_tui_output(accumulated)
        raise AccountAuthFlowError(
            f"agy PTY exited (code {proc.returncode}) without OAuth URL. Output: {summary or '<empty>'}"
        )

    async def complete_remote_oauth_flow(
        self, pending: PendingOAuthSession, verification_code: str
    ) -> None:
        clean_code = verification_code.strip()
        if not clean_code:
            self._cleanup_oauth_pty(pending.process, pending.pty_master_fd)
            raise AccountAuthFlowError("Verification code cannot be empty.")

        env = await self.ensure_account_bus(pending.account)
        poll_interval = self._cfg.runtime.dbus_socket_poll_interval_sec
        chunk_size = self._cfg.runtime.oauth_stdout_chunk_bytes
        accumulated = ""
        authenticated = False
        sent_post_trust = False
        ticks = 0

        try:
            os.write(pending.pty_master_fd, (clean_code + "\r").encode("utf-8"))
            async with asyncio.timeout(self._cfg.runtime.command_exec_timeout_sec):
                while True:
                    try:
                        chunk = os.read(pending.pty_master_fd, chunk_size)
                        if chunk:
                            accumulated += chunk.decode("utf-8", errors="replace")
                    except (BlockingIOError, OSError):
                        pass

                    clean_text = _ANSI_ESCAPE_RE.sub("", accumulated)
                    if not sent_post_trust and (
                        "Do you trust the contents" in clean_text
                        or "Yes, I trust this folder" in clean_text
                    ):
                        with contextlib.suppress(OSError):
                            os.write(pending.pty_master_fd, b"\r")
                        sent_post_trust = True
                        logger.info(
                            "Confirmed post-login workspace trust prompt",
                            account_slug=pending.account.slug,
                        )

                    ticks += 1
                    # Every ~1.5s after code submission, verify actual auth status via `agy models`
                    if ticks % 30 == 0 and await self.is_account_authenticated(env):
                        await asyncio.sleep(0.5)
                        authenticated = True
                        logger.info(
                            "Verified active OAuth session via agy models",
                            account_slug=pending.account.slug,
                        )
                        break

                    if pending.process.returncode is not None:
                        authenticated = await self.is_account_authenticated(env)
                        break

                    await asyncio.sleep(poll_interval)
        except TimeoutError as exc:
            authenticated = await self.is_account_authenticated(env)
            if not authenticated:
                summary = self._sanitize_tui_output(accumulated)
                raise AccountAuthFlowError(
                    f"Timed out waiting for agy OAuth completion. Output tail: {summary}"
                ) from exc
        finally:
            self._cleanup_oauth_pty(pending.process, pending.pty_master_fd)

        backed_up = await self.backup_account_token_to_db(pending.account)
        if not backed_up and authenticated:
            encrypted_marker = self._vault.encrypt_text(
                f"oauth-verified:{pending.account.slug}"
            )
            await self._account_repo.update_token_backup(
                pending.account.id, encrypted_marker
            )
            backed_up = True

        if not backed_up:
            summary = self._sanitize_tui_output(accumulated)
            raise AccountAuthFlowError(
                f"agy OAuth login did not complete. Output tail: {summary}"
            )

    @staticmethod
    def _cleanup_oauth_pty(proc: asyncio.subprocess.Process, master_fd: int) -> None:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)

    async def shutdown_all(self) -> None:
        async with self._lock:
            for runtime in self._runtimes.values():
                if runtime.dbus_pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(runtime.dbus_pid, signal.SIGTERM)
            self._runtimes.clear()
