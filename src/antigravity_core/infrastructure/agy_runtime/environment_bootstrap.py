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
                "| SSH Host Alias (`host`) | Address & User | Native File Tool Prefix | Detailed Host Passport |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for h in hosts:
                table_lines.append(
                    f"| `{h.alias}` | `{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}` | `{h.ssh_user}@{h.alias}:/path` or `{h.ssh_user}@{h.alias}:~/path` | `{h.rule_file_path}` |"
                )
            hosts_table_md = "\n".join(table_lines)
        else:
            hosts_table_md = (
                "*(Use any host from `~/.ssh/config` or `user@hostname` directly!)*"
            )

        content = (
            "# Antigravity Telegram Core — LXC Brain Constitution & Live Infrastructure Map\n\n"
            "You live inside an isolated **LXC Brain Container** (`Antigravity Telegram Core`).\n"
            "**There are NO user projects inside this LXC container!** Your current working directory (`workspaces/topic_<id>`) "
            "is strictly your private scratchpad for temporary files and generating charts.\n\n"
            "## 1. Live Registered Infrastructure Map\n\n"
            f"{hosts_table_md}\n\n"
            "## 2. Two Iron Laws of Interacting with Remote Computers\n\n"
            "1. **NATIVE FILE TOOLS WITH REMOTE PATHS (`user@host:/path` and `user@host:~/path`):**\n"
            "   - To read, create, or edit ANY file on a remote machine (both `/home/...` and `/etc/...`), "
            "**ALWAYS use your native file tools** (`view_file`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`) "
            "passing the remote path directly: `user@host:/abs/path` or `user@host:~/rel/path` (or `alias:/path`).\n"
            "   - Never edit files via `sed -i`, `awk`, or `cat << EOF >` in shell commands.\n"
            "2. **NATIVE `run_command` WITH `ssh user@host \"...\"`:**\n"
            "   - To inspect directories (`ls -la`, `find`, `rg`), check logs (`cat`, `head`, `tail`), or run workloads (`sbatch`, `gmx`, `docker`, `systemctl`), "
            "use your native `run_command` tool: `ssh user@host \"command\"`.\n"
            "   - When root privileges are needed on a server, simply write `sudo` inside the command as usual (e.g. `ssh user@host \"sudo systemctl restart slurmd\"`).\n"
            "3. **SELF-UPDATING INFRASTRUCTURE MEMORY:**\n"
            f"   - Each known host has a passport in `{rules_dir}/infra-<alias>.md`.\n"
            "   - Whenever you discover project paths, databases, or services on a host, record them using "
            "`mcp:telegram-core:update_host_knowledge(host=\"<alias>\", notes_markdown=\"...\")`.\n\n"
            "## 3. MCP Toolset (`telegram-core`)\n\n"
            "- `send_file_to_telegram(file_path, caption)` — send any file or chart to the current Telegram topic.\n"
            "- `create_sensor(name, command, instruction_prompt, sensor_kind, trigger_condition, poll_interval_sec, match_pattern, action_command, wake_mode, one_shot, cooldown_sec, notify_policy, keep_conversation_context)` — zero-token background event/stream watcher.\n"
            "- `list_sensors()` / `cancel_sensor(sensor_id)`\n"
            "- `schedule_task(schedule_kind, trigger_spec, instruction_prompt, notify_policy, keep_conversation_context)`\n"
            "- `list_scheduled_tasks()` / `cancel_scheduled_task(task_id)`\n"
            "- `register_remote_host(alias, hostname_or_ip, ssh_user, ssh_port, remote_root_path, jump_host)`\n"
            "- `update_host_knowledge(host, notes_markdown, append)`\n"
            "- `create_workspace_topic(title, execution_mode)`\n"
        )
        root_agents.write_text(content, encoding="utf-8")
        config_agents.write_text(content, encoding="utf-8")

    def _write_settings_json(self, settings_path: Path) -> None:
        workspaces_dir = str(self._cfg.paths.workspaces_root_dir)
        remotes_dir = str(self._cfg.paths.remotes_mount_dir)

        payload = {
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
            "trustedWorkspaces": [workspaces_dir, remotes_dir],
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
                        "matcher": "run_command|view_file|read_file|list_dir|write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content|schedule",
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
                        "matcher": "view_file|read_file|list_dir|write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content",
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
        # Errata Patch #1: Connect telegram-core strictly via child stdio_proxy
        python_bin = str(self._cfg.paths.venv_python_path)
        payload: dict[str, Any] = {
            "mcpServers": {
                "telegram-core": {
                    "command": python_bin,
                    "args": ["-m", "antigravity_core.mcp.stdio_proxy"],
                    "timeoutSeconds": self._cfg.runtime.mcp_stdio_timeout_sec,
                }
            }
        }
        mcp_config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_onboarding_skill(self, skill_path: Path) -> None:
        rules_dir = self._cfg.paths.agy_home_dir / "config" / "rules"
        remotes_dir = self._cfg.paths.remotes_mount_dir
        content = (
            "---\n"
            "name: host-onboarding\n"
            'description: "Onboard a new remote SSH host, mount it under /mnt/remotes/<alias>, '
            'inspect its system profile, and create ~/.gemini/config/rules/infra-<alias>.md."\n'
            "---\n\n"
            "# Host Onboarding Procedure\n\n"
            "1. Call `mcp:telegram-core:register_remote_host` with `alias`, `hostname_or_ip`, "
            "`ssh_user`, `ssh_port`, and `remote_root_path`.\n"
            "2. Probe the server via `run_command`: "
            '`ssh <alias> "uname -a && free -h && df -hT && docker ps 2>/dev/null"`.\n'
            f"3. Use `write_to_file` to write `{rules_dir}/infra-<alias>.md` with YAML frontmatter "
            "`trigger: model_decision` and a comprehensive summary of hardware, OS, services, and "
            f"key paths under `{remotes_dir}/<alias>/...`.\n"
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
        logger.info("Configured WAL mode for agy internal conversation_summaries.db", path=str(db_path))
