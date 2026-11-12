import asyncio
import difflib
import os
import secrets
import shlex
import shutil
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode
from antigravity_core.domain.exceptions import RemoteMountError
from antigravity_core.domain.models import RemoteHost, SshCredential
from antigravity_core.infrastructure.crypto.fernet_vault import FernetVault
from antigravity_core.infrastructure.database.repositories import (
    RemoteHostRepository,
    SshCredentialRepository,
    TopicRepository,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_AI_NOTES_MARKER: str = "<!-- AI_NOTES_START -->"


@dataclass(frozen=True, slots=True)
class RemoteFileSpec:
    raw_target: str
    ssh_user: str
    host_alias_or_ip: str
    ssh_port: int
    credential_key: str
    remote_path: str
    local_stage_path: Path
    stage_root_dir: Path
    jump_host: str


@dataclass(frozen=True, slots=True)
class _StagedFileBaseline:
    existed_on_remote: bool
    original_bytes: bytes
    requires_sudo_write: bool


class RemoteMountManager:
    """Manages native OpenSSH multiplexing, key onboarding, PTY sudo auth, and JIT file staging."""

    def __init__(self, static_config: StaticConfig) -> None:
        self._cfg = static_config
        self._vault: FernetVault | None = None
        self._cred_repo: SshCredentialRepository | None = None
        self._host_repo: RemoteHostRepository | None = None
        self._topic_repo: TopicRepository | None = None
        self._on_password_prompt: Callable[[int, str], Awaitable[None]] | None = None
        self._on_sudo_prompt: (
            Callable[[int, str, str, str, str], Awaitable[None]] | None
        ) = None
        self._pending_passwords: dict[int, tuple[str, asyncio.Future[str]]] = {}
        self._pending_sudo: dict[str, tuple[int, asyncio.Future[str]]] = {}
        self._staged_baselines: dict[tuple[int, str], _StagedFileBaseline] = {}
        self._stage_locks: dict[str, asyncio.Lock] = {}
        self._stage_refcounts: dict[str, int] = {}
        self._onboarding_locks: dict[str, asyncio.Lock] = {}

    def bind_security_gateways(
        self,
        vault: FernetVault,
        cred_repo: SshCredentialRepository,
        host_repo: RemoteHostRepository,
        topic_repo: TopicRepository,
        on_password_prompt: Callable[[int, str], Awaitable[None]],
        on_sudo_prompt: Callable[[int, str, str, str, str], Awaitable[None]],
    ) -> None:
        self._vault = vault
        self._cred_repo = cred_repo
        self._host_repo = host_repo
        self._topic_repo = topic_repo
        self._on_password_prompt = on_password_prompt
        self._on_sudo_prompt = on_sudo_prompt

    def has_pending_password_prompt(self, topic_id: int) -> bool:
        if topic_id not in self._pending_passwords:
            return False
        _target, fut = self._pending_passwords[topic_id]
        return not fut.done()

    def submit_password_from_telegram(self, topic_id: int, raw_password: str) -> bool:
        if topic_id not in self._pending_passwords:
            return False
        _target, fut = self._pending_passwords.pop(topic_id)
        if not fut.done():
            fut.set_result(raw_password.strip())
            return True
        return False

    def cancel_topic_prompts(self, topic_id: int) -> None:
        if topic_id in self._pending_passwords:
            _target, fut = self._pending_passwords.pop(topic_id)
            if not fut.done():
                fut.cancel()
        stale_reqs = [
            req_id
            for req_id, (tid, _fut) in self._pending_sudo.items()
            if tid == topic_id
        ]
        for req_id in stale_reqs:
            _tid, s_fut = self._pending_sudo.pop(req_id)
            if not s_fut.done():
                s_fut.cancel()

    def resolve_sudo_approval(self, request_id: str, decision: str) -> tuple[int, bool]:
        if request_id not in self._pending_sudo:
            return 0, False
        topic_id, fut = self._pending_sudo.pop(request_id)
        if not fut.done():
            fut.set_result(decision)
            return topic_id, True
        return topic_id, False

    def install_runtime_wrappers(self) -> None:
        bin_dir = self._cfg.paths.runtime_bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        python_bin = str(self._cfg.paths.venv_python_path)
        app_root = str(self._cfg.paths.workspaces_root_dir.parent.parent.resolve())
        brain_dir = str(
            (self._cfg.paths.agy_home_dir / "antigravity-cli" / "brain").resolve()
        )

        wrapper_src = Path(__file__).with_name("ssh_cli_wrapper.py")
        standalone_wrapper = bin_dir / "ssh_cli_wrapper.py"
        standalone_wrapper.write_text(
            wrapper_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
        os.chmod(standalone_wrapper, 0o755)

        ssh_wrapper = bin_dir / "ssh"
        ssh_script = "#!/bin/sh\n" f'exec "{python_bin}" "{standalone_wrapper}" "$@"\n'
        ssh_wrapper.write_text(ssh_script, encoding="utf-8")
        os.chmod(ssh_wrapper, 0o755)

        bash_wrapper = bin_dir / "bash"
        bash_script = (
            "#!/bin/sh\n"
            'if [ "${AGY_SANDBOX_ACTIVE:-0}" = "1" ]; then\n'
            '    exec /usr/bin/bash "$@"\n'
            "fi\n"
            "export AGY_SANDBOX_ACTIVE=1\n"
            "exec /usr/bin/unshare -m /bin/sh -c '\n"
            f'    mount -t tmpfs -o size=1M tmpfs "{app_root}/src" 2>/dev/null || true\n'
            f'    mount -t tmpfs -o size=1M tmpfs "{app_root}/migrations" 2>/dev/null || true\n'
            f'    mount -t tmpfs -o size=1M tmpfs "{app_root}/runtime_data/logs" 2>/dev/null || true\n'
            f'    mount -t tmpfs -o size=1M tmpfs "{app_root}/runtime_data/data" 2>/dev/null || true\n'
            f'    mount -t tmpfs -o size=1M tmpfs "{app_root}/runtime_data/profiles" 2>/dev/null || true\n'
            f'    mount -t tmpfs -o size=1M tmpfs "{brain_dir}" 2>/dev/null || true\n'
            f'    mount --bind /dev/null "{app_root}/config.yaml" 2>/dev/null || true\n'
            f'    mount --bind /dev/null "{app_root}/.env" 2>/dev/null || true\n'
            '    exec /usr/bin/bash "$@"\n'
            '\' /usr/bin/bash "$@"\n'
        )
        bash_wrapper.write_text(bash_script, encoding="utf-8")
        os.chmod(bash_wrapper, 0o755)

    def _ensure_main_ssh_config_include(self) -> None:
        ssh_dir = self._cfg.paths.ssh_config_dir
        sockets_dir = self._cfg.paths.ssh_sockets_dir
        ssh_dir.mkdir(parents=True, exist_ok=True)
        sockets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(sockets_dir, 0o700)

        multiplex_conf = ssh_dir / "00-antigravity-multiplex.conf"
        multiplex_content = (
            "# Managed by Antigravity Telegram Core\n"
            "Host *\n"
            "    ControlMaster auto\n"
            f"    ControlPath {sockets_dir}/%r@%h-%p\n"
            f"    ControlPersist {self._cfg.remotes.ssh_control_persist_sec}\n"
            "    ServerAliveInterval 15\n"
            "    ServerAliveCountMax 3\n"
            "    StrictHostKeyChecking accept-new\n"
        )
        multiplex_conf.write_text(multiplex_content, encoding="utf-8")
        os.chmod(multiplex_conf, 0o600)

        main_ssh_config = ssh_dir.parent / "config"
        include_line = f"Include {ssh_dir}/*.conf"

        if not main_ssh_config.is_file():
            main_ssh_config.parent.mkdir(parents=True, exist_ok=True)
            main_ssh_config.write_text(f"{include_line}\n\n", encoding="utf-8")
            os.chmod(main_ssh_config, 0o600)
            return

        existing_text = main_ssh_config.read_text(encoding="utf-8")
        first_non_empty = next(
            (
                ln.strip()
                for ln in existing_text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ),
            "",
        )
        if first_non_empty.startswith("Include ") and "config.d/" in first_non_empty:
            return

        cleaned_lines = [
            ln
            for ln in existing_text.splitlines()
            if not (ln.strip().startswith("Include ") and "config.d/" in ln.strip())
        ]
        updated_text = (
            f"{include_line}\n\n" + "\n".join(cleaned_lines).lstrip("\n") + "\n"
        )
        main_ssh_config.write_text(updated_text, encoding="utf-8")
        os.chmod(main_ssh_config, 0o600)

    async def ensure_local_ssh_keypair(self) -> str:
        """Auto-generates ~/.ssh/id_ed25519 if absent and returns the public key string."""
        ssh_root = self._cfg.paths.ssh_config_dir.parent
        ssh_root.mkdir(parents=True, exist_ok=True)
        os.chmod(ssh_root, 0o700)
        self._ensure_main_ssh_config_include()

        priv_key = ssh_root / "id_ed25519"
        pub_key = ssh_root / "id_ed25519.pub"

        if not priv_key.is_file() or not pub_key.is_file():
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "antigravity-core-lxc",
                "-f",
                str(priv_key),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if priv_key.is_file():
                os.chmod(priv_key, 0o600)
            logger.info(
                "Generated new Ed25519 SSH keypair for Antigravity Core",
                path=str(pub_key),
            )

        if pub_key.is_file():
            return pub_key.read_text(encoding="utf-8").strip()
        raise RemoteMountError(
            f"Failed to read or generate SSH public key at {pub_key}"
        )

    def write_ssh_config_entry(self, host: RemoteHost) -> Path:
        ssh_dir = self._cfg.paths.ssh_config_dir
        ssh_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_main_ssh_config_include()
        config_file = ssh_dir / f"{host.alias}.conf"

        clean_jump = (
            host.jump_host.strip().replace("[", "").replace("]", "")
            if host.jump_host and host.jump_host.strip()
            else ""
        )
        if clean_jump.endswith(":22"):
            clean_jump = clean_jump[:-3]
        proxy_jump_line = f"    ProxyJump {clean_jump}\n" if clean_jump else ""
        host_patterns = (
            f"{host.alias} {host.hostname_or_ip}"
            if host.alias != host.hostname_or_ip
            else host.alias
        )
        content = (
            f"# Managed by Antigravity Telegram Core\n"
            f"Host {host_patterns}\n"
            f"    HostName {host.hostname_or_ip}\n"
            f"    User {host.ssh_user}\n"
            f"    Port {host.ssh_port}\n"
            f"{proxy_jump_line}"
            f"    ConnectTimeout {self._cfg.remotes.ssh_connect_timeout_sec}\n"
            f"    ServerAliveInterval 5\n"
            f"    ServerAliveCountMax 2\n"
            f"    StrictHostKeyChecking accept-new\n"
        )
        config_file.write_text(content, encoding="utf-8")
        os.chmod(config_file, 0o600)
        return config_file

    def remove_host_config(self, alias: str) -> None:
        conf_file = self._cfg.paths.ssh_config_dir / f"{alias.strip()}.conf"
        if conf_file.is_file():
            conf_file.unlink()

    async def verify_ssh_connectivity(self, host: RemoteHost) -> None:
        self.write_ssh_config_entry(host)
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
            host.alias,
            "echo AGY_SSH_OK",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ssh_timeout = float(
            self._cfg.remotes.ssh_connect_timeout_sec
            + self._cfg.remotes.ssh_check_grace_sec
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=ssh_timeout,
            )
        except TimeoutError as exc:
            proc.kill()
            raise RemoteMountError(
                f"SSH connectivity check timed out for host '{host.alias}'."
            ) from exc

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or "AGY_SSH_OK" not in stdout_text:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RemoteMountError(
                f"SSH connectivity check failed for host '{host.alias}' (code {proc.returncode}): {stderr_text}"
            )

    def is_mounted(self, mount_path: Path) -> bool:
        return mount_path.exists() and os.path.ismount(mount_path)

    async def ensure_mounted(self, host: RemoteHost) -> None:
        """Ensures SSH config entry exists without mounting persistent FUSE SSHFS."""
        self.write_ssh_config_entry(host)

    async def unmount(self, mount_path: Path) -> None:
        if not self.is_mounted(mount_path):
            return
        proc = await asyncio.create_subprocess_exec(
            "fusermount3",
            "-u",
            "-z",
            str(mount_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    def _internal_ssh_env(self) -> dict[str, str]:
        return {**os.environ, "AGY_INTERNAL_SSH": "1"}

    async def _resolve_ssh_target_details(
        self, raw_target: str, explicit_port: int
    ) -> tuple[str, str, int, str, tuple[str, ...]]:
        user_part = ""
        host_part = raw_target.strip().replace("[", "").replace("]", "")
        if "@" in host_part:
            user_part, host_part = host_part.split("@", 1)
        if ":" in host_part:
            h_only, p_only = host_part.rsplit(":", 1)
            if p_only.isdigit():
                host_part = h_only
                if explicit_port == 22:
                    explicit_port = int(p_only)

        if self._host_repo is not None:
            reg = await self._host_repo.get_by_alias(host_part)
            if reg is None:
                all_hosts = await self._host_repo.list_hosts()
                reg = next(
                    (h for h in all_hosts if h.hostname_or_ip == host_part),
                    None,
                )
            if reg is not None:
                eff_user = user_part if user_part else reg.ssh_user
                eff_port = explicit_port if explicit_port != 22 else reg.ssh_port
                cred_key = f"{eff_user}@{reg.alias}:{eff_port}"
                candidates = (
                    (reg.alias, reg.hostname_or_ip)
                    if reg.alias != reg.hostname_or_ip
                    else (reg.alias,)
                )
                return eff_user, reg.alias, eff_port, cred_key, candidates

        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh",
            "-G",
            raw_target,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._internal_ssh_env(),
        )
        stdout_bytes, _ = await proc.communicate()
        parsed_user = user_part if user_part else self._cfg.paths.system_user
        parsed_port = explicit_port

        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                if parts[0] == "user" and not user_part:
                    parsed_user = parts[1]
                elif parts[0] == "port" and explicit_port == 22 and parts[1].isdigit():
                    parsed_port = int(parts[1])

        cred_key = f"{parsed_user}@{host_part}:{parsed_port}"
        return parsed_user, host_part, parsed_port, cred_key, (host_part,)

    async def _test_batch_key_auth(
        self, resolved_target: str, port: int, jump_host: str
    ) -> bool:
        jump_args = ["-J", jump_host] if jump_host else []
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh",
            *jump_args,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(port),
            resolved_target,
            "true",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._internal_ssh_env(),
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _provision_key_with_password(
        self, resolved_target: str, port: int, password: str, jump_host: str
    ) -> bool:
        pub_key = await self.ensure_local_ssh_keypair()
        self._cfg.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        askpass_script = (
            self._cfg.paths.runtime_dir / f".askpass_{secrets.token_hex(12)}.sh"
        )
        script_body = f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n"
        askpass_script.write_text(script_body, encoding="utf-8")
        os.chmod(askpass_script, 0o700)

        env = {
            **self._internal_ssh_env(),
            "SSH_ASKPASS": str(askpass_script),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": "antigravity:0",
            "LC_ALL": "C",
        }
        remote_install_cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"(grep -qxF {shlex.quote(pub_key)} ~/.ssh/authorized_keys 2>/dev/null || "
            f"printf '%s\\n' {shlex.quote(pub_key)} >> ~/.ssh/authorized_keys) && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        jump_args = ["-J", jump_host] if jump_host else []
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                *jump_args,
                "-o",
                "BatchMode=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "PreferredAuthentications=keyboard-interactive,password",
                "-o",
                "NumberOfPasswordPrompts=1",
                "-o",
                f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-p",
                str(port),
                resolved_target,
                remote_install_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            await proc.communicate()
            return proc.returncode == 0
        finally:
            if askpass_script.exists():
                askpass_script.unlink()

    async def _request_password_from_owner(
        self, topic_id: int, target_label: str
    ) -> str:
        if self._on_password_prompt is None:
            raise RemoteMountError(
                f"SSH key is not provisioned for '{target_label}' and interactive prompt callback is not bound."
            )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_passwords[topic_id] = (target_label, fut)
        try:
            await self._on_password_prompt(topic_id, target_label)
            return await asyncio.wait_for(
                fut,
                timeout=self._cfg.remotes.interactive_approval_timeout_sec,
            )
        except TimeoutError as exc:
            raise RemoteMountError(
                f"Timed out waiting for SSH password for '{target_label}' in Telegram."
            ) from exc
        finally:
            self._pending_passwords.pop(topic_id, None)

    async def ensure_target_access(
        self, topic_id: int, raw_target: str, explicit_port: int
    ) -> tuple[str, SshCredential | None]:
        return await self.ensure_target_access_with_jump(
            topic_id=topic_id,
            raw_target=raw_target,
            explicit_port=explicit_port,
            jump_host="",
        )

    async def ensure_target_access_with_jump(
        self,
        topic_id: int,
        raw_target: str,
        explicit_port: int,
        jump_host: str,
    ) -> tuple[str, SshCredential | None]:
        clean_jump = jump_host.strip().replace("[", "").replace("]", "")
        if clean_jump.endswith(":22"):
            clean_jump = clean_jump[:-3]
        if clean_jump:
            await self.ensure_target_access_with_jump(
                topic_id=topic_id,
                raw_target=clean_jump,
                explicit_port=22,
                jump_host="",
            )

        ssh_user, host_part, port, cred_key, candidate_targets = (
            await self._resolve_ssh_target_details(raw_target, explicit_port)
        )
        resolved_target = f"{ssh_user}@{host_part}"

        if cred_key not in self._onboarding_locks:
            self._onboarding_locks[cred_key] = asyncio.Lock()

        async with self._onboarding_locks[cred_key]:
            cred = (
                await self._cred_repo.get_credential(cred_key)
                if self._cred_repo is not None
                else None
            )
            if cred is None and self._cred_repo is not None:
                matched = await self._cred_repo.find_matching_credential(
                    ssh_user=ssh_user,
                    candidate_targets=candidate_targets,
                    ssh_port=port,
                )
                if matched is not None:
                    cred = await self._cred_repo.upsert_credential(
                        credential_key=cred_key,
                        ssh_user=ssh_user,
                        host_target=host_part,
                        ssh_port=port,
                        encrypted_password=matched.encrypted_password,
                        key_provisioned=matched.key_provisioned,
                    )

            key_ok = await self._test_batch_key_auth(resolved_target, port, clean_jump)
            if key_ok:
                if (
                    cred is not None
                    and not cred.key_provisioned
                    and self._cred_repo is not None
                ):
                    cred = await self._cred_repo.mark_key_provisioned(cred_key, True)
                return resolved_target, cred

            if cred is not None and self._vault is not None:
                saved_pass = self._vault.decrypt_text(cred.encrypted_password)
                if await self._provision_key_with_password(
                    resolved_target, port, saved_pass, clean_jump
                ):
                    if self._cred_repo is not None:
                        cred = await self._cred_repo.mark_key_provisioned(
                            cred_key, True
                        )
                    return resolved_target, cred
                if self._cred_repo is not None:
                    await self._cred_repo.delete_credential(cred_key)
                cred = None

            entered_pass = await self._request_password_from_owner(
                topic_id, resolved_target
            )
            if self._vault is not None and self._cred_repo is not None:
                enc = self._vault.encrypt_text(entered_pass)
                cred = await self._cred_repo.upsert_credential(
                    credential_key=cred_key,
                    ssh_user=ssh_user,
                    host_target=host_part,
                    ssh_port=port,
                    encrypted_password=enc,
                    key_provisioned=False,
                )

            if not await self._provision_key_with_password(
                resolved_target, port, entered_pass, clean_jump
            ):
                if self._cred_repo is not None:
                    await self._cred_repo.delete_credential(cred_key)
                raise RemoteMountError(
                    f"Failed to authenticate and install SSH key on '{resolved_target}' with the provided password."
                )

            if self._cred_repo is not None:
                cred = await self._cred_repo.mark_key_provisioned(cred_key, True)
            return resolved_target, cred

    async def authorize_sudo_operation(
        self,
        topic_id: int,
        raw_target: str,
        explicit_port: int,
        action_summary: str,
        diff_or_cmd_preview: str,
    ) -> tuple[str, str, bool]:
        resolved_target, cred = await self.ensure_target_access(
            topic_id, raw_target, explicit_port
        )
        ssh_user, host_part, port, cred_key, candidate_targets = (
            await self._resolve_ssh_target_details(raw_target, explicit_port)
        )
        if ssh_user == "root":
            return resolved_target, "", True

        carte_blanche = False
        if self._topic_repo is not None:
            topic = await self._topic_repo.get_topic(topic_id)
            if topic is not None:
                carte_blanche = topic.sudo_carte_blanche

        if not carte_blanche:
            if self._on_sudo_prompt is None:
                raise RemoteMountError("Sudo approval callback is not configured.")
            req_id = uuid4().hex[:12]
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._pending_sudo[req_id] = (topic_id, fut)
            await self._on_sudo_prompt(
                topic_id, req_id, resolved_target, action_summary, diff_or_cmd_preview
            )
            try:
                decision = await asyncio.wait_for(
                    fut,
                    timeout=self._cfg.remotes.interactive_approval_timeout_sec,
                )
            except TimeoutError as exc:
                self._pending_sudo.pop(req_id, None)
                raise RemoteMountError(
                    f"Timed out waiting for sudo approval on '{resolved_target}'."
                ) from exc

            if decision == "deny":
                raise RemoteMountError(
                    f"Sudo operation on '{resolved_target}' was rejected by the owner in Telegram."
                )
            if decision == "carte_blanche" and self._topic_repo is not None:
                await self._topic_repo.set_sudo_carte_blanche(topic_id, True)

        if cred is None and self._cred_repo is not None:
            cred = await self._cred_repo.get_credential(cred_key)
        if cred is None and self._cred_repo is not None:
            cred = await self._cred_repo.find_matching_credential(
                ssh_user=ssh_user,
                candidate_targets=candidate_targets,
                ssh_port=port,
            )

        if cred is None:
            entered_pass = await self._request_password_from_owner(
                topic_id, resolved_target
            )
            if self._vault is not None and self._cred_repo is not None:
                enc = self._vault.encrypt_text(entered_pass)
                cred = await self._cred_repo.upsert_credential(
                    credential_key=cred_key,
                    ssh_user=ssh_user,
                    host_target=host_part,
                    ssh_port=port,
                    encrypted_password=enc,
                    key_provisioned=True,
                )
            return resolved_target, entered_pass, False

        if self._vault is None:
            raise RemoteMountError("FernetVault is not initialized.")
        return resolved_target, self._vault.decrypt_text(cred.encrypted_password), False

    def parse_remote_file_spec(
        self, raw_path: str, workspace_dir: Path
    ) -> RemoteFileSpec | None:
        cleaned = raw_path.strip()
        is_root_tramp = False
        jump_host = ""

        if cleaned.startswith("/ssh:"):
            is_root_tramp = True
            remainder = cleaned[len("/ssh:") :]
            if "|ssh:" in remainder:
                jump_part, remainder = remainder.split("|ssh:", 1)
                jump_host = jump_part.strip()
        else:
            ws_str = str(workspace_dir.resolve())
            if cleaned.startswith(ws_str + "/"):
                cleaned = cleaned[len(ws_str) + 1 :]
            if cleaned.startswith("/") or ":" not in cleaned:
                return None
            remainder = cleaned

        if ":" not in remainder:
            return None

        target_part, remote_path = remainder.split(":", 1)
        if not target_part or "/" in target_part or not remote_path:
            return None

        user_part = ""
        host_part = target_part
        if "@" in target_part:
            user_part, host_part = target_part.split("@", 1)
        if not host_part:
            return None

        if not remote_path.startswith(("/", "~/")) and remote_path != "~":
            remote_path = f"~/{remote_path}"

        raw_target = f"{user_part}@{host_part}" if user_part else host_part

        if is_root_tramp:
            local_stage_path = Path(cleaned)
            top_segment = cleaned.lstrip("/").split("/", 1)[0]
            stage_root_dir = Path("/") / top_segment
        else:
            first_segment = cleaned.split("/", 1)[0]
            local_stage_path = (workspace_dir / cleaned).resolve()
            stage_root_dir = (workspace_dir / first_segment).resolve()

        return RemoteFileSpec(
            raw_target=raw_target,
            ssh_user=user_part,
            host_alias_or_ip=host_part,
            ssh_port=22,
            credential_key=f"{raw_target}:22",
            remote_path=remote_path,
            local_stage_path=local_stage_path,
            stage_root_dir=stage_root_dir,
            jump_host=jump_host,
        )

    @staticmethod
    def _quote_remote_path(remote_path: str) -> str:
        if remote_path == "~":
            return '"$HOME"'
        if remote_path.startswith("~/"):
            return f'"$HOME"/{shlex.quote(remote_path[2:])}'
        return shlex.quote(remote_path)

    def _get_stage_lock(self, local_stage_path: Path) -> asyncio.Lock:
        key = str(local_stage_path)
        if key not in self._stage_locks:
            self._stage_locks[key] = asyncio.Lock()
        return self._stage_locks[key]

    @staticmethod
    def _prune_empty_stage_parents(file_path: Path, stage_root_dir: Path) -> None:
        curr = file_path.parent
        root_resolved = stage_root_dir.resolve()
        while curr.exists() and curr.is_dir():
            if any(curr.iterdir()):
                break
            curr.rmdir()
            if curr.resolve() == root_resolved or curr.parent == curr:
                break
            curr = curr.parent

    async def download_remote_file_to_temp(
        self,
        topic_id: int,
        workspace_dir: Path,
        raw_path: str,
    ) -> Path:
        spec = self.parse_remote_file_spec(raw_path, workspace_dir)
        if spec is None:
            raise RemoteMountError(f"Not a remote TRAMP path: {raw_path}")

        resolved_target, _cred = await self.ensure_target_access_with_jump(
            topic_id=topic_id,
            raw_target=spec.raw_target,
            explicit_port=spec.ssh_port,
            jump_host=spec.jump_host,
        )
        qpath = self._quote_remote_path(spec.remote_path)
        jump_args = ["-J", spec.jump_host] if spec.jump_host else []

        tmp_dir = (
            self._cfg.paths.runtime_dir
            / "tg_transfers"
            / f"topic_{topic_id}_{uuid4().hex[:8]}"
        )
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest_file = tmp_dir / Path(spec.remote_path).name

        with dest_file.open("wb") as out_fh:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                *jump_args,
                "-o",
                "BatchMode=yes",
                resolved_target,
                f"cat {qpath}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_fh,
                stderr=asyncio.subprocess.PIPE,
                env=self._internal_ssh_env(),
            )
            _, stderr_bytes = await proc.communicate()

        if proc.returncode != 0:
            dest_file.unlink(missing_ok=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RemoteMountError(
                f"Failed to stream remote file '{spec.remote_path}' from '{resolved_target}': {err_msg}"
            )
        return dest_file

    async def stage_remote_file(
        self,
        topic_id: int,
        workspace_dir: Path,
        raw_path: str,
        for_write: bool,
    ) -> dict[str, str]:
        spec = self.parse_remote_file_spec(raw_path, workspace_dir)
        if spec is None:
            return {"status": "local"}

        stage_key = str(spec.local_stage_path)
        lock = self._get_stage_lock(spec.local_stage_path)
        async with lock:
            self._stage_refcounts[stage_key] = (
                self._stage_refcounts[stage_key] + 1
                if stage_key in self._stage_refcounts
                else 1
            )

        resolved_target, _cred = await self.ensure_target_access_with_jump(
            topic_id=topic_id,
            raw_target=spec.raw_target,
            explicit_port=spec.ssh_port,
            jump_host=spec.jump_host,
        )
        qpath = self._quote_remote_path(spec.remote_path)
        jump_args = ["-J", spec.jump_host] if spec.jump_host else []

        probe_cmd = (
            f"if [ -d {qpath} ]; then echo AGY_KIND_DIR; ls -la {qpath}; "
            f"elif [ -e {qpath} ]; then "
            f"  if [ -r {qpath} ]; then "
            f"    if [ -w {qpath} ]; then echo AGY_FILE_RW; else echo AGY_FILE_RO; fi; "
            f"    cat {qpath}; "
            f"  else echo AGY_NEED_SUDO_READ; fi; "
            f"else "
            f"  parent_dir=$(dirname {qpath}); "
            f'  if [ -w "$parent_dir" ]; then echo AGY_MISSING_RW; else echo AGY_MISSING_SUDO; fi; '
            f"fi"
        )
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh",
            *jump_args,
            "-o",
            "BatchMode=yes",
            resolved_target,
            probe_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._internal_ssh_env(),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode != 0 and not stdout_bytes:
            err_str = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RemoteMountError(
                f"Failed to access '{spec.remote_path}' on '{resolved_target}': {err_str}"
            )

        nl_idx = stdout_bytes.find(b"\n")
        if nl_idx == -1:
            header = stdout_bytes.decode("utf-8", errors="replace").strip()
            body_bytes = b""
        else:
            header = stdout_bytes[:nl_idx].decode("utf-8", errors="replace").strip()
            body_bytes = stdout_bytes[nl_idx + 1 :]

        if header == "AGY_KIND_DIR":
            spec.local_stage_path.mkdir(parents=True, exist_ok=True)
            for raw_entry in body_bytes.decode("utf-8", errors="replace").splitlines():
                parts = raw_entry.split()
                if len(parts) >= 9:
                    entry_name = " ".join(parts[8:])
                    if entry_name not in (".", "..") and "/" not in entry_name:
                        entry_path = spec.local_stage_path / entry_name
                        if parts[0].startswith("d"):
                            entry_path.mkdir(parents=True, exist_ok=True)
                        else:
                            entry_path.write_bytes(b"")
            return {"status": "staged_dir", "local_path": str(spec.local_stage_path)}

        existed = True
        requires_sudo_write = False

        if header == "AGY_NEED_SUDO_READ":
            _t, sudo_pass, is_root = await self.authorize_sudo_operation(
                topic_id=topic_id,
                raw_target=spec.raw_target,
                explicit_port=spec.ssh_port,
                action_summary=f"Чтение защищённого файла {spec.remote_path}",
                diff_or_cmd_preview=f"sudo cat {spec.remote_path}",
            )
            requires_sudo_write = not is_root
            sudo_cmd = (
                f"sudo -k -S -p '' cat {qpath}" if not is_root else f"cat {qpath}"
            )
            s_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                resolved_target,
                sudo_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._internal_ssh_env(),
            )
            stdin_payload = (sudo_pass + "\n").encode("utf-8") if not is_root else b""
            body_bytes, s_err = await s_proc.communicate(input=stdin_payload)
            if s_proc.returncode != 0:
                raise RemoteMountError(
                    f"Failed to read '{spec.remote_path}' with sudo: {s_err.decode('utf-8', errors='replace')}"
                )
        elif header == "AGY_FILE_RO":
            requires_sudo_write = True
        elif header == "AGY_MISSING_RW":
            existed = False
            body_bytes = b""
        elif header == "AGY_MISSING_SUDO":
            existed = False
            requires_sudo_write = True
            body_bytes = b""

        if not existed and not for_write:
            raise RemoteMountError(
                f"REMOTE FILE DOES NOT EXIST: '{spec.remote_path}' was not found on host '{resolved_target}' "
                f"(SSH connection to '{resolved_target}' is healthy and working). "
                f"To create a new file at this path, call 'write_file' or 'write_to_file' with '{raw_path}'."
            )

        spec.local_stage_path.parent.mkdir(parents=True, exist_ok=True)
        if existed or for_write:
            spec.local_stage_path.write_bytes(body_bytes)

        self._staged_baselines[(topic_id, str(spec.local_stage_path))] = (
            _StagedFileBaseline(
                existed_on_remote=existed,
                original_bytes=body_bytes,
                requires_sudo_write=requires_sudo_write,
            )
        )
        return {"status": "staged", "local_path": str(spec.local_stage_path)}

    async def commit_remote_file(
        self,
        topic_id: int,
        workspace_dir: Path,
        raw_path: str,
        cleanup_only: bool,
    ) -> dict[str, str]:
        spec = self.parse_remote_file_spec(raw_path, workspace_dir)
        if spec is None:
            return {"status": "local"}

        baseline_key = (topic_id, str(spec.local_stage_path))
        baseline = (
            self._staged_baselines.pop(baseline_key)
            if baseline_key in self._staged_baselines
            else None
        )

        try:
            if cleanup_only or not spec.local_stage_path.is_file():
                return {"status": "cleaned"}

            new_bytes = spec.local_stage_path.read_bytes()
            if (
                baseline is not None
                and baseline.existed_on_remote
                and new_bytes == baseline.original_bytes
            ):
                return {"status": "unchanged"}

            resolved_target, _cred = await self.ensure_target_access_with_jump(
                topic_id=topic_id,
                raw_target=spec.raw_target,
                explicit_port=spec.ssh_port,
                jump_host=spec.jump_host,
            )
            qpath = self._quote_remote_path(spec.remote_path)
            jump_args = ["-J", spec.jump_host] if spec.jump_host else []
            needs_sudo = baseline.requires_sudo_write if baseline is not None else False

            if not needs_sudo:
                write_cmd = (
                    f"parent_dir=$(dirname {qpath}); "
                    f'mkdir -p "$parent_dir" 2>/dev/null && '
                    f'tmp_f="{qpath}.agy_tmp_$$" && '
                    f'cat > "$tmp_f" && mv -f "$tmp_f" {qpath}'
                )
                w_proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/ssh",
                    *jump_args,
                    "-o",
                    "BatchMode=yes",
                    resolved_target,
                    write_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._internal_ssh_env(),
                )
                await w_proc.communicate(input=new_bytes)
                if w_proc.returncode == 0:
                    return {"status": "committed", "remote_path": spec.remote_path}
                needs_sudo = True

            old_text = (
                baseline.original_bytes.decode("utf-8", errors="replace").splitlines()
                if baseline is not None and baseline.existed_on_remote
                else []
            )
            new_text = new_bytes.decode("utf-8", errors="replace").splitlines()
            diff_lines = list(
                difflib.unified_diff(
                    old_text,
                    new_text,
                    fromfile=f"a:{spec.remote_path}",
                    tofile=f"b:{spec.remote_path}",
                    lineterm="",
                )
            )
            diff_preview = (
                "\n".join(diff_lines[:80])
                if diff_lines
                else f"[Binary or new file: {len(new_bytes)} bytes]"
            )

            _t, sudo_pass, is_root = await self.authorize_sudo_operation(
                topic_id=topic_id,
                raw_target=spec.raw_target,
                explicit_port=spec.ssh_port,
                action_summary=f"Запись системного файла {spec.remote_path}",
                diff_or_cmd_preview=diff_preview,
            )

            stage_upload_cmd = (
                "tmp_stage=$(mktemp /tmp/.agy_stage_XXXXXXXXXX) && "
                'cat > "$tmp_stage" && printf \'%s\' "$tmp_stage"'
            )
            up_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                resolved_target,
                stage_upload_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._internal_ssh_env(),
            )
            up_out, up_err = await up_proc.communicate(input=new_bytes)
            remote_tmp = up_out.decode("utf-8", errors="replace").strip()
            if up_proc.returncode != 0 or not remote_tmp.startswith("/tmp/"):
                raise RemoteMountError(
                    f"Failed to upload staging buffer to '{resolved_target}': {up_err.decode('utf-8', errors='replace')}"
                )

            qtmp = shlex.quote(remote_tmp)
            sudo_prefix = "sudo -k -S -p ''" if not is_root else ""
            apply_cmd = (
                f"{sudo_prefix} sh -c "
                f'\'parent_dir=$(dirname {qpath}); mkdir -p "$parent_dir" && '
                f"if [ -f {qpath} ]; then cp -p {qpath} {qtmp}.perm && cat {qtmp} > {qtmp}.perm && mv -f {qtmp}.perm {qpath}; "
                f"else install -m 0644 {qtmp} {qpath}; fi; "
                f"rc=$?; rm -f {qtmp} {qtmp}.perm; exit $rc'"
            )
            sw_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                resolved_target,
                apply_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._internal_ssh_env(),
            )
            stdin_pass = (sudo_pass + "\n").encode("utf-8") if not is_root else b""
            _, sw_err = await sw_proc.communicate(input=stdin_pass)
            if sw_proc.returncode != 0:
                err_msg = sw_err.decode("utf-8", errors="replace").strip()
                raise RemoteMountError(
                    f"Failed to write '{spec.remote_path}' on '{resolved_target}' via sudo: {err_msg}"
                )
            return {"status": "committed_sudo", "remote_path": spec.remote_path}
        finally:
            stage_key = str(spec.local_stage_path)
            lock = self._get_stage_lock(spec.local_stage_path)
            async with lock:
                remaining = (
                    self._stage_refcounts[stage_key] - 1
                    if stage_key in self._stage_refcounts
                    else 0
                )
                if remaining <= 0:
                    self._stage_refcounts.pop(stage_key, None)
                    if spec.local_stage_path.is_file():
                        spec.local_stage_path.unlink(missing_ok=True)
                    elif spec.local_stage_path.is_dir():
                        shutil.rmtree(spec.local_stage_path, ignore_errors=True)
                    if (
                        spec.stage_root_dir.exists()
                        and spec.stage_root_dir != workspace_dir.resolve()
                    ):
                        self._prune_empty_stage_parents(
                            spec.local_stage_path, spec.stage_root_dir
                        )
                else:
                    self._stage_refcounts[stage_key] = remaining

    def ensure_rule_skeleton(self, host: RemoteHost) -> Path:
        rule_path = Path(host.rule_file_path)
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        if not rule_path.exists():
            skeleton = self._render_two_tier_passport(
                host=host,
                auto_telemetry_md="*(Auto-telemetry scan pending...)*",
                ai_notes_md="- *(Add architectural notes, project paths, and service details here via `update_host_knowledge`.)*",
            )
            rule_path.write_text(skeleton, encoding="utf-8")
        return rule_path

    @staticmethod
    def _render_two_tier_passport(
        host: RemoteHost, auto_telemetry_md: str, ai_notes_md: str
    ) -> str:
        route_note = f" (via `{host.jump_host}`)" if host.jump_host else ""
        tramp_prefix = f"/ssh:{host.ssh_user}@{host.alias}:"
        return (
            "---\n"
            "trigger: model_decision\n"
            f'description: "Infrastructure passport for host {host.alias} '
            f"({host.ssh_user}@{host.hostname_or_ip}{route_note}, TRAMP prefix {tramp_prefix}/path). "
            f'Read whenever working on {host.alias}."\n'
            "---\n\n"
            f"# Infrastructure Passport: `{host.alias}`\n\n"
            f"- **SSH Alias:** `{host.alias}` (`{host.ssh_user}@{host.hostname_or_ip}:{host.ssh_port}`{route_note})\n"
            f"- **Remote Root:** `{host.remote_root_path}`\n"
            f"- **Native File Tools TRAMP Path (`read_file`/`write_file`/`edit_file`):** `{tramp_prefix}/abs/path`\n"
            f'- **Shell Command Execution (`run_command`):** `ssh {host.ssh_user}@{host.alias} "command"`\n\n'
            "## 1. Auto-Synced Hardware & System Telemetry (Managed by Bot)\n\n"
            f"{auto_telemetry_md.strip()}\n\n"
            f"{_AI_NOTES_MARKER}\n"
            "## 2. Architecture & AI Operational Notes\n\n"
            f"{ai_notes_md.strip()}\n"
        )

    def _extract_existing_ai_notes(self, rule_path: Path) -> str:
        if not rule_path.is_file():
            return "- *(Add architectural notes, project paths, and service details here via `update_host_knowledge`.)*"
        raw = rule_path.read_text(encoding="utf-8", errors="replace")
        if _AI_NOTES_MARKER in raw:
            after = raw.split(_AI_NOTES_MARKER, 1)[1]
            lines = [
                ln
                for ln in after.splitlines()
                if not ln.strip().startswith(
                    "## 2. Architecture & AI Operational Notes"
                )
            ]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                return cleaned
        return "- *(Add architectural notes, project paths, and service details here via `update_host_knowledge`.)*"

    async def sync_host_telemetry_and_rules(self, host: RemoteHost) -> Path:
        """Probes live hardware, disks, Docker, Proxmox, Slurm, and key folders over SSH (0 LLM tokens) and updates infra-<alias>.md."""
        self.write_ssh_config_entry(host)
        probe_script = (
            "echo '=== HOST & OS ==='; hostname; uname -sr; uptime -p 2>/dev/null || uptime; "
            "echo '=== CPU & GPU ==='; lscpu 2>/dev/null | grep -E 'Model name|CPU\\(s\\):|Socket' | head -n 5; "
            "nvidia-smi --query-gpu=index,name,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null || true; "
            "echo '=== MEMORY & DISKS ==='; free -h 2>/dev/null | head -n 3; df -hT -x tmpfs -x devtmpfs -x overlay 2>/dev/null | head -n 12; "
            "echo '=== CONTAINERS & VMS ==='; "
            "docker ps --format '{{.Names}} ({{.Image}}) [{{.Status}}]' 2>/dev/null | head -n 15 || true; "
            "pct list 2>/dev/null | head -n 15 || true; qm list 2>/dev/null | head -n 15 || true; "
            "sinfo -s 2>/dev/null | head -n 10 || true; "
            f"echo '=== KEY DIRECTORIES ({host.remote_root_path}) ==='; ls -1 {host.remote_root_path} 2>/dev/null | head -n 30; "
            "if [ -d /shared ]; then echo '=== /shared ==='; ls -1 /shared 2>/dev/null | head -n 25; fi"
        )
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
            host.alias,
            probe_script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._internal_ssh_env(),
        )
        now_str = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        stdout_bytes, _ = await proc.communicate()
        telemetry_out = (
            stdout_bytes.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0
            else "[Telemetry SSH probe failed or timed out]"
        )

        auto_section = (
            f"*Last auto-synced: `{now_str}`*\n\n"
            f"```text\n{telemetry_out[:4000]}\n```"
        )
        rule_path = Path(host.rule_file_path)
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        existing_notes = self._extract_existing_ai_notes(rule_path)
        full_doc = self._render_two_tier_passport(host, auto_section, existing_notes)
        rule_path.write_text(full_doc, encoding="utf-8")
        return rule_path

    def update_host_ai_notes(
        self, host: RemoteHost, notes_markdown: str, append: bool
    ) -> Path:
        rule_path = self.ensure_rule_skeleton(host)
        raw = rule_path.read_text(encoding="utf-8", errors="replace")
        existing_notes = self._extract_existing_ai_notes(rule_path)

        if _AI_NOTES_MARKER in raw:
            top_part = raw.split(_AI_NOTES_MARKER, 1)[0]
            auto_marker = (
                "## 1. Auto-Synced Hardware & System Telemetry (Managed by Bot)"
            )
            if auto_marker in top_part:
                auto_telemetry = top_part.split(auto_marker, 1)[1].strip()
            else:
                auto_telemetry = "*(Auto-telemetry scan pending...)*"
        else:
            auto_telemetry = "*(Auto-telemetry scan pending...)*"

        placeholder = "- *(Add architectural notes, project paths, and service details here via `update_host_knowledge`.)*"
        if append and existing_notes != placeholder:
            updated_notes = f"{existing_notes}\n\n{notes_markdown.strip()}"
        else:
            updated_notes = notes_markdown.strip()

        full_doc = self._render_two_tier_passport(host, auto_telemetry, updated_notes)
        rule_path.write_text(full_doc, encoding="utf-8")
        return rule_path

    async def execute_remote_ssh(
        self,
        host: RemoteHost | None,
        command: str,
        timeout_sec: int,
        execution_mode: ExecutionMode,
        local_scratch_dir: Path,
    ) -> dict[str, str | int]:
        clean_cmd = command.strip()
        if not clean_cmd:
            raise RemoteMountError("Command cannot be empty.")

        effective_timeout = float(max(5, min(3600, timeout_sec)))

        if host is None:
            local_scratch_dir.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                clean_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(local_scratch_dir),
                start_new_session=True,
            )
        else:
            self.write_ssh_config_entry(host)
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
                host.alias,
                clean_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._internal_ssh_env(),
                start_new_session=True,
            )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
            rc = proc.returncode if proc.returncode is not None else 0
            out_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        except TimeoutError as exc:
            if proc.returncode is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            raise RemoteMountError(
                f"Command timed out after {effective_timeout:.0f}s on '{host.alias if host else 'local_scratch'}': {clean_cmd[:120]}"
            ) from exc

        if host is not None:
            # Refresh auto-telemetry in background if command potentially modified services/containers
            if any(
                kw in clean_cmd
                for kw in (
                    "systemctl",
                    "docker",
                    "apt",
                    "pacman",
                    "mount",
                    "mkdir",
                    "pct",
                    "qm",
                )
            ):
                asyncio.create_task(self.sync_host_telemetry_and_rules(host))

        return {
            "host": host.alias if host is not None else "local_scratch",
            "exit_code": rc,
            "output": out_text[:12000] if out_text else "(no output)",
        }
