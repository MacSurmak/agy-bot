import json
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import StartupValidationError
from antigravity_core.domain.models import RemoteHost

logger = structlog.get_logger(__name__)


class AgyEnvironmentBootstrapper:
    """Validates and synchronizes ~/.gemini/ configuration, hooks, MCP stdio_proxy, and SQLite WAL mode."""

    def __init__(self, static_config: StaticConfig) -> None:
        self._cfg = static_config

    def bootstrap_and_validate(self) -> None:
        if not self._cfg.paths.agy_binary_path.is_file():
            raise StartupValidationError(
                f"Google Antigravity CLI binary not found at {self._cfg.paths.agy_binary_path}"
            )
        if not self._cfg.paths.venv_python_path.is_file():
            raise StartupValidationError(
                f"Virtualenv Python binary not found at {self._cfg.paths.venv_python_path}"
            )

        agy_home = self._cfg.paths.agy_home_dir
        cli_dir = agy_home / "antigravity-cli"
        config_dir = agy_home / "config"
        rules_dir = config_dir / "rules"
        skills_dir = config_dir / "skills" / "host-onboarding"

        for directory in (
            cli_dir,
            config_dir,
            rules_dir,
            skills_dir,
            self._cfg.paths.workspaces_root_dir,
            self._cfg.paths.remotes_mount_dir,
            self._cfg.paths.inbox_dir,
            self._cfg.paths.profiles_data_dir,
            self._cfg.paths.runtime_dir,
            self._cfg.paths.runtime_bin_dir,
            self._cfg.paths.ssh_config_dir,
            self._cfg.paths.ssh_sockets_dir,
            self._cfg.paths.log_file_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.sync_global_agents_md_host_map(hosts=[])
        self._write_settings_json(cli_dir / "settings.json")
        self._write_hooks_json(config_dir / "hooks.json")
        self._write_mcp_config_json(config_dir / "mcp_config.json")
        self._write_onboarding_skill(skills_dir / "SKILL.md")
        self._configure_agy_internal_sqlite_wal(cli_dir / "conversation_summaries.db")

    def sync_global_agents_md_host_map(self, hosts: list[RemoteHost]) -> None:
        agy_home = self._cfg.paths.agy_home_dir
        root_agents = agy_home / "AGENTS.md"
        config_agents = agy_home / "config" / "AGENTS.md"
        rules_dir = agy_home / "config" / "rules"

        if hosts:
            table_lines = [
                "| SSH Host Alias (`alias`) | Address & Route | Native File Tool TRAMP Prefix | Detailed Host Passport |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for h in hosts:
                route_note = f" (via `{h.jump_host}`)" if h.jump_host else ""
                table_lines.append(
                    f"| `{h.alias}` | `{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}`{route_note} | `/ssh:{h.ssh_user}@{h.alias}:{h.remote_root_path}` | `{h.rule_file_path}` |"
                )
            hosts_table_md = "\n".join(table_lines)
        else:
            hosts_table_md = "*(No hosts registered yet. Call `mcp:telegram-core:register_remote_host` first when connecting to a new server!)*"

        content = (
            "# Antigravity Telegram Core — LXC Brain Constitution & Live Infrastructure Map\n\n"
            "You live inside an isolated **LXC Brain Container** (`Antigravity Telegram Core`).\n"
            "**There are NO user projects inside this LXC container!** Your current working directory (`workspaces/topic_<id>`) "
            "is strictly your private scratchpad for temporary files and generating charts.\n\n"
            "## 1. Live Registered Infrastructure Map\n\n"
            f"{hosts_table_md}\n\n"
            "## 2. Two-Stage Workflow for Remote Computers\n\n"
            "1. **STAGE 1 — CHECK & ONBOARD NEW HOSTS FIRST (`mcp_telegram-core_register_remote_host`):**\n"
            "   - Before working on a server, check if it is in the **Live Registered Infrastructure Map** above.\n"
            "   - If the host is NOT in the table yet (e.g. a new server IP or an internal LAN/Slurm node discovered behind a gateway), "
            "**IMMEDIATELY call `mcp_telegram-core_register_remote_host`**!\n"
            '   - If the node is behind a gateway, pass `jump_host="user@<gateway_alias>"`. Never manually scrape `/root/.ssh/id_*`, never run interactive `srun` jobs to copy keys, and never inspect local `~/.ssh/config`.\n'
            "2. **STAGE 2 — WORK DIRECTLY BY ALIAS (`/ssh:user@<alias>:/abs/path` & `ssh user@<alias>`):**\n"
            "   - Once a host is registered, **ALWAYS address it directly by its `<alias>`** (OpenSSH handles `ProxyJump` automatically; never use manual `-J` or `|ssh:` pipes):\n"
            "   - Native file tools (`read_file`, `write_file`, `edit_file`): "
            "`/ssh:user@<alias>:/abs/path` (always use full absolute paths like `/home/user/...`, never use `~`).\n"
            "   - If `read_file` returns `REMOTE FILE DOES NOT EXIST`, it simply means the file does not exist on the remote host yet — call `write_file` on `/ssh:user@<alias>:/abs/path` to create it.\n"
            "   - Never edit files via `sed -i`, `awk`, or `perl -pi` in shell commands.\n"
            '   - Shell commands (`run_command`): `ssh user@<alias> "command"` (write plain `sudo` inside the command when root privileges are needed).\n'
            "   - Local container access is restricted strictly to your topic scratchpad and `/tmp`. Never inspect `/srv/antigravity` or `/root/.gemini/antigravity-cli/mcp`.\n"
            "3. **DIRECT REMOTE FILE SENDING TO TELEGRAM:**\n"
            "   - Pass TRAMP paths (`/ssh:user@<alias>:/path/to/file.png`) directly into `mcp_telegram-core_send_file_to_telegram` — never run manual `scp`.\n"
            "4. **SELF-UPDATING INFRASTRUCTURE MEMORY:**\n"
            f"   - Each known host has a passport in `{rules_dir}/infra-<alias>.md`.\n"
            "   - Whenever you discover project paths, databases, or services on a host, record insights using `mcp_telegram-core_update_host_knowledge`.\n\n"
            "## 3. Native Eager MCP Tools (`telegram-core`)\n\n"
            "All tools from MCP server `telegram-core` are pre-registered with `\"eager\": true` in your native tool declarations (`mcp_telegram-core_<tool>`). "
            "Call them directly in a single step without reading schema files from `~/.gemini/antigravity-cli/mcp/telegram-core/`:\n"
            "- `mcp_telegram-core_register_remote_host`\n"
            "- `mcp_telegram-core_update_host_knowledge`\n"
            "- `mcp_telegram-core_send_file_to_telegram`\n"
            "- `mcp_telegram-core_schedule_task` / `mcp_telegram-core_list_scheduled_tasks` / `mcp_telegram-core_cancel_scheduled_task`\n"
            "- `mcp_telegram-core_create_sensor` / `mcp_telegram-core_list_sensors` / `mcp_telegram-core_cancel_sensor`\n"
            "- `mcp_telegram-core_create_workspace_topic`\n"
            "- `mcp_telegram-core_get_bot_ssh_public_key`\n"
        )
        root_agents.write_text(content, encoding="utf-8")
        config_agents.write_text(content, encoding="utf-8")

    def _write_settings_json(self, settings_path: Path) -> None:
        workspaces_dir = str(self._cfg.paths.workspaces_root_dir)
        inbox_dir = str(self._cfg.paths.inbox_dir)

        existing_settings: dict[
            str, str | int | bool | list[str] | dict[str, list[str]]
        ] = {}
        if settings_path.is_file():
            raw_existing = settings_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            if raw_existing.startswith("{"):
                loaded = json.loads(raw_existing)
                if isinstance(loaded, dict):
                    existing_settings = loaded

        payload = {
            **existing_settings,
            "$schema": "https://antigravity.google/schemas/v1/settings.json",
            "agentMode": "accept-edits",
            "toolPermission": "always-proceed",
            "allowNonWorkspaceAccess": True,
            "enableTerminalSandbox": False,
            "artifactReviewPolicy": "always-proceed",
            "disableSlashCommands": False,
            "showTips": False,
            "showFeedbackSurvey": False,
            "notifications": False,
            "historySize": -1,
            "trustedWorkspaces": [workspaces_dir, inbox_dir],
            "permissions": {
                "allow": [
                    "read_file(*)",
                    "write_file(*)",
                    "command(*)",
                    "read_url(*)",
                    "mcp(telegram-core/*)",
                    "mcp(*)",
                ],
                "deny": [
                    "schedule(*)",
                    "write_file(/etc/shadow)",
                    "write_file(/root/.ssh/authorized_keys)",
                    "read_file(/srv/antigravity/app/config.yaml)",
                    "write_file(/srv/antigravity/app/config.yaml)",
                    "read_file(/srv/antigravity/app/.env)",
                    "write_file(/srv/antigravity/app/.env)",
                ],
                "ask": [],
            },
        }
        settings_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_hooks_json(self, hooks_path: Path) -> None:
        python_bin = str(self._cfg.paths.venv_python_path)
        payload = {
            "enforce-native-file-tools": {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": "run_command|read_file|write_file|edit_file|schedule",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{python_bin} -m antigravity_core.hooks.enforce_native_edit",
                                "timeout": self._cfg.runtime.hook_timeout_sec,
                            }
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "read_file|write_file|edit_file",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{python_bin} -m antigravity_core.hooks.enforce_native_edit --post",
                                "timeout": self._cfg.runtime.hook_timeout_sec,
                            }
                        ],
                    }
                ],
            }
        }
        hooks_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_mcp_config_json(self, mcp_config_path: Path) -> None:
        # Errata Patch #1: Connect telegram-core strictly via child stdio_proxy with native eager tool loading
        python_bin = str(self._cfg.paths.venv_python_path)
        eager_tools: dict[str, dict[str, bool]] = {
            name: {"eager": True}
            for name in (
                "register_remote_host",
                "update_host_knowledge",
                "send_file_to_telegram",
                "schedule_task",
                "list_scheduled_tasks",
                "cancel_scheduled_task",
                "create_sensor",
                "list_sensors",
                "cancel_sensor",
                "create_workspace_topic",
                "get_bot_ssh_public_key",
            )
        }
        payload = {
            "mcpServers": {
                "telegram-core": {
                    "command": python_bin,
                    "args": ["-m", "antigravity_core.mcp.stdio_proxy"],
                    "timeoutSeconds": self._cfg.runtime.mcp_stdio_timeout_sec,
                    "tools": eager_tools,
                }
            }
        }
        mcp_config_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def _write_onboarding_skill(self, skill_path: Path) -> None:
        rules_dir = self._cfg.paths.agy_home_dir / "config" / "rules"
        content = (
            "---\n"
            "name: host-onboarding\n"
            'description: "Onboard a new remote SSH host via mcp_telegram-core_register_remote_host, '
            'inspect its system profile, and update ~/.gemini/config/rules/infra-<alias>.md."\n'
            "---\n\n"
            "# Host Onboarding Procedure\n\n"
            "1. Call `mcp_telegram-core_register_remote_host` with `alias`, `hostname_or_ip`, "
            "`ssh_user`, `ssh_port`, `remote_root_path`, and `jump_host` (if behind a gateway).\n"
            "2. Probe the server via `run_command`: "
            '`ssh <alias> "uname -a && free -h && df -hT && docker ps 2>/dev/null"`.\n'
            f"3. Call `mcp_telegram-core_update_host_knowledge` to save architectural notes into `{rules_dir}/infra-<alias>.md`.\n"
        )
        skill_path.write_text(content, encoding="utf-8")

    def _configure_agy_internal_sqlite_wal(self, db_path: Path) -> None:
        # Errata Patch #4: Switch internal agy conversation_summaries.db into non-blocking WAL mode
        busy_timeout_ms = self._cfg.runtime.agy_sqlite_busy_timeout_ms
        try:
            with sqlite3.connect(db_path) as agy_internal_db:
                agy_internal_db.execute("PRAGMA journal_mode=WAL;")
                agy_internal_db.execute("PRAGMA synchronous=NORMAL;")
                agy_internal_db.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")
        except sqlite3.Error as exc:
            raise StartupValidationError(
                f"Failed to configure WAL mode on agy internal SQLite file {db_path}: {exc}"
            ) from exc
        logger.info(
            "Configured WAL mode for agy internal conversation_summaries.db",
            path=str(db_path),
        )
