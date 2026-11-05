import asyncio
import contextlib
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import signal
from typing import Any

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.enums import ExecutionMode
from antigravity_core.domain.exceptions import RemoteMountError
from antigravity_core.domain.models import RemoteHost

logger = structlog.get_logger(__name__)

_AI_NOTES_MARKER: str = "<!-- AI_NOTES_START -->"
_FILE_EDIT_IN_SSH_RE = re.compile(
    r"\bsed\s+(?:-[a-zA-Z0-9]*i\b|--in-place\b)|\bawk\b[^\n;|]*-i\s+inplace\b|\bcat\s*<<"
)


class RemoteMountManager:
    """Manages ~/.ssh/config.d/<alias>.conf and FUSE SSHFS mounts under /mnt/remotes/<alias>."""

    def __init__(self, static_config: StaticConfig) -> None:
        self._cfg = static_config

    def _ensure_main_ssh_config_include(self) -> None:
        ssh_dir = self._cfg.paths.ssh_config_dir
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
        updated_text = f"{include_line}\n\n" + "\n".join(cleaned_lines).lstrip("\n") + "\n"
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
            logger.info("Generated new Ed25519 SSH keypair for Antigravity Core", path=str(pub_key))

        if pub_key.is_file():
            return pub_key.read_text(encoding="utf-8").strip()
        raise RemoteMountError(f"Failed to read or generate SSH public key at {pub_key}")

    def write_ssh_config_entry(self, host: RemoteHost) -> Path:
        ssh_dir = self._cfg.paths.ssh_config_dir
        ssh_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_main_ssh_config_include()
        config_file = ssh_dir / f"{host.alias}.conf"

        proxy_jump_line = (
            f"    ProxyJump {host.jump_host.strip()}\n"
            if host.jump_host and host.jump_host.strip()
            else ""
        )
        content = (
            f"# Managed by Antigravity Telegram Core\n"
            f"Host {host.alias}\n"
            f"    HostName {host.hostname_or_ip}\n"
            f"    User {host.ssh_user}\n"
            f"    Port {host.ssh_port}\n"
            f"{proxy_jump_line}"
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
        ssh_timeout = float(
            self._cfg.remotes.ssh_connect_timeout_sec + self._cfg.remotes.ssh_check_grace_sec
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=ssh_timeout,
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
        mount_timeout = float(
            self._cfg.remotes.ssh_connect_timeout_sec + self._cfg.remotes.sshfs_mount_grace_sec
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=mount_timeout,
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
        return (
            "---\n"
            "trigger: model_decision\n"
            f'description: "Infrastructure passport for host {host.alias} '
            f"({host.ssh_user}@{host.hostname_or_ip}, mounted at {host.local_mount_path}). "
            f'Read whenever working on {host.alias}."\n'
            "---\n\n"
            f"# Infrastructure Passport: `{host.alias}`\n\n"
            f"- **SSH Alias:** `{host.alias}` (`{host.ssh_user}@{host.hostname_or_ip}:{host.ssh_port}`)\n"
            f"- **Remote Root:** `{host.remote_root_path}`\n"
            f"- **Local VFS Mount (for `read_file`/`write_file`/`edit_file`):** `{host.local_mount_path}`\n"
            f"- **Command Execution:** `mcp:telegram-core:ssh_exec(host=\"{host.alias}\", command=\"...\")`\n\n"
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
                if not ln.strip().startswith("## 2. Architecture & AI Operational Notes")
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
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
            host.alias,
            probe_script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        now_str = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            telemetry_out = stdout_bytes.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            telemetry_out = "[Telemetry SSH probe timed out]"

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
            auto_marker = "## 1. Auto-Synced Hardware & System Telemetry (Managed by Bot)"
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
    ) -> dict[str, Any]:
        clean_cmd = command.strip()
        if not clean_cmd:
            raise RemoteMountError("Command cannot be empty.")

        if _FILE_EDIT_IN_SSH_RE.search(clean_cmd) is not None:
            mount_hint = host.local_mount_path if host is not None else str(local_scratch_dir)
            raise RemoteMountError(
                f"Editing or creating files via sed -i / awk -i / cat << EOF inside ssh_exec is forbidden. "
                f"Use your native file tools (write_file, edit_file) directly on '{mount_hint}/...'."
            )

        effective_timeout = float(max(5, min(3600, timeout_sec)))

        if host is None:
            # Local scratch execution (e.g. running matplotlib plot script inside workspaces/topic_<id>)
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
            if host.auto_mount_enabled and not self.is_mounted(Path(host.local_mount_path)):
                with contextlib.suppress( Exception):
                    await self.ensure_mounted(host)

            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self._cfg.remotes.ssh_connect_timeout_sec}",
                host.alias,
                clean_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
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
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            raise RemoteMountError(
                f"Command timed out after {effective_timeout:.0f}s on '{host.alias if host else 'local_scratch'}': {clean_cmd[:120]}"
            ) from exc

        if host is not None:
            # Refresh auto-telemetry in background if command potentially modified services/containers
            if any(
                kw in clean_cmd
                for kw in ("systemctl", "docker", "apt", "pacman", "mount", "mkdir", "pct", "qm")
            ):
                asyncio.create_task(self.sync_host_telemetry_and_rules(host))

        return {
            "host": host.alias if host is not None else "local_scratch",
            "exit_code": rc,
            "output": out_text[:12000] if out_text else "(no output)",
        }
