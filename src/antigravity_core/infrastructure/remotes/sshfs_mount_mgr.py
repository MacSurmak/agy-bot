import asyncio
import os
from pathlib import Path

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import RemoteMountError
from antigravity_core.domain.models import RemoteHost

logger = structlog.get_logger(__name__)


class RemoteMountManager:
    """Manages ~/.ssh/config.d/<alias>.conf and FUSE SSHFS mounts under /mnt/remotes/<alias>."""

    def __init__(self, static_config: StaticConfig) -> None:
        self._cfg = static_config

    def write_ssh_config_entry(self, host: RemoteHost) -> Path:
        ssh_dir = self._cfg.paths.ssh_config_dir
        ssh_dir.mkdir(parents=True, exist_ok=True)
        config_file = ssh_dir / f"{host.alias}.conf"

        content = (
            f"# Managed by Antigravity Telegram Core\n"
            f"Host {host.alias}\n"
            f"    HostName {host.hostname_or_ip}\n"
            f"    User {host.ssh_user}\n"
            f"    Port {host.ssh_port}\n"
            f"    BatchMode yes\n"
            f"    ConnectTimeout {self._cfg.remotes.ssh_connect_timeout_sec}\n"
            f"    ServerAliveInterval 5\n"
            f"    ServerAliveCountMax 2\n"
            f"    StrictHostKeyChecking accept-new\n"
        )
        config_file.write_text(content, encoding="utf-8")
        os.chmod(config_file, 0o600)
        return config_file

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
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self._cfg.remotes.ssh_connect_timeout_sec + 5),
            )
        except TimeoutError as exc:
            proc.kill()
            raise RemoteMountError(f"SSH connectivity check timed out for host '{host.alias}'.") from exc

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or "AGY_SSH_OK" not in stdout_text:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RemoteMountError(
                f"SSH connectivity check failed for host '{host.alias}' (code {proc.returncode}): {stderr_text}"
            )

    def is_mounted(self, mount_path: Path) -> bool:
        if not mount_path.exists():
            return False
        return os.path.ismount(mount_path)

    async def ensure_mounted(self, host: RemoteHost) -> None:
        self.write_ssh_config_entry(host)
        mount_path = Path(host.local_mount_path)
        mount_path.mkdir(parents=True, exist_ok=True)

        if self.is_mounted(mount_path):
            return

        # Errata Patch #3: Use configured sshfs_mount_options with auto_cache and attr_timeout=3
        remote_spec = f"{host.alias}:{host.remote_root_path}"
        proc = await asyncio.create_subprocess_exec(
            "sshfs",
            remote_spec,
            str(mount_path),
            "-o",
            self._cfg.remotes.sshfs_mount_options,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self._cfg.remotes.ssh_connect_timeout_sec + 10),
            )
        except TimeoutError as exc:
            proc.kill()
            raise RemoteMountError(
                f"SSHFS mount timed out for '{remote_spec}' -> '{mount_path}'."
            ) from exc

        if proc.returncode != 0 or not self.is_mounted(mount_path):
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RemoteMountError(
                f"SSHFS mount failed for '{remote_spec}' -> '{mount_path}' "
                f"(code {proc.returncode}): {stderr_text}"
            )

        logger.info(
            "Mounted remote host via SSHFS (auto_cache)",
            alias=host.alias,
            remote_spec=remote_spec,
            local_mount=str(mount_path),
        )

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

    def ensure_rule_skeleton(self, host: RemoteHost) -> Path:
        rule_path = Path(host.rule_file_path)
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        if not rule_path.exists():
            skeleton = (
                "---\n"
                "trigger: model_decision\n"
                f'description: "Infrastructure passport for remote host {host.alias} '
                f"(SSH alias {host.alias}, mounted at {host.local_mount_path}). "
                f'Read whenever working on {host.alias}."\n'
                "---\n\n"
                f"# Infrastructure Passport: `{host.alias}`\n\n"
                f"- **SSH Alias:** `{host.alias}` (`{host.ssh_user}@{host.hostname_or_ip}:{host.ssh_port}`)\n"
                f"- **Remote Root:** `{host.remote_root_path}`\n"
                f"- **Local SSHFS Mount:** `{host.local_mount_path}`\n"
                f"- **File Editing Policy:** Strictly use native tools (`view_file`, `write_to_file`, "
                f"`replace_file_content`, `multi_replace_file_content`) on `{host.local_mount_path}/...`.\n"
            )
            rule_path.write_text(skeleton, encoding="utf-8")
        return rule_path
