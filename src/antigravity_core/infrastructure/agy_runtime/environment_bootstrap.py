import json
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import StartupValidationError

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
            self._cfg.paths.ssh_config_dir,
            self._cfg.paths.log_file_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self._write_agents_md(agy_home / "AGENTS.md", config_dir / "AGENTS.md")
        self._write_settings_json(cli_dir / "settings.json")
        self._write_hooks_json(config_dir / "hooks.json")
        self._write_mcp_config_json(config_dir / "mcp_config.json")
        self._write_onboarding_skill(skills_dir / "SKILL.md")
        self._configure_agy_internal_sqlite_wal(cli_dir / "conversation_summaries.db")

    def _write_agents_md(self, root_agents: Path, config_agents: Path) -> None:
        remotes_dir = self._cfg.paths.remotes_mount_dir
        rules_dir = self._cfg.paths.agy_home_dir / "config" / "rules"
        ssh_dir = self._cfg.paths.ssh_config_dir

        content = (
            "# Antigravity Telegram Core — Global Agent Constitution & Infrastructure Index\n\n"
            "You are running inside the central **Antigravity Telegram Core** LXC orchestrator on Proxmox VE.\n\n"
            "## 1. Strict Native File Editing & Hybrid Search Law (Zero Shell File Edits)\n\n"
            f"1. **All remote machines are mounted via SSHFS (`auto_cache`) under `{remotes_dir}/<host_alias>/`.**\n"
            "2. **Reading, creating, and modifying files** (both local and on remote servers) "
            "**MUST be performed STRICTLY using your native file tools** (`view_file`, `write_to_file`, "
            "`replace_file_content`, `multi_replace_file_content`, `list_dir`, `find_by_name`).\n"
            "3. **Modifying files via `run_command` (`sed -i`, `awk -i inplace`, `perl -pi`, `cat << EOF >`, "
            "`echo >`, `tee`, `dd of=`, `truncate`) is STRICTLY FORBIDDEN** and blocked by the `PreToolUse` hook.\n"
            "4. **Hybrid Search Rule for Large Remote Directories:**\n"
            "   - For primary full-text search across huge remote directory trees, you MAY run read-only ripgrep via SSH:\n"
            "     `ssh <host> \"rg -n --hidden -g '!.git' '<pattern>' /path 2>/dev/null\"`\n"
            f"   - Then open and edit the target files using native `view_file` and `replace_file_content` via `{remotes_dir}/<host>/...`.\n\n"
            "## 2. Native Telegram & Temporal Scheduler MCP Tools (`telegram-core`)\n\n"
            "Always use MCP tools from `telegram-core` (never use text tags or system `crontab`/`systemctl`):\n"
            "- `send_file_to_telegram(file_path, caption)`\n"
            "- `schedule_task(schedule_kind, trigger_spec, instruction_prompt, notify_policy, keep_conversation_context)`\n"
            "- `list_scheduled_tasks()`\n"
            "- `cancel_scheduled_task(task_id)`\n"
            "- `register_remote_host(alias, hostname_or_ip, ssh_user, ssh_port, remote_root_path)`\n"
            "- `create_workspace_topic(title, workspace_path, execution_mode)`\n\n"
            "## 3. Self-Updating Infrastructure Registry\n\n"
            f"- SSH configs live in `{ssh_dir}/<alias>.conf`.\n"
            f"- Host passports live in `{rules_dir}/infra-<alias>.md` with YAML frontmatter `trigger: model_decision`.\n"
            "- Whenever you learn new facts about a server or configure a new service, update its `infra-<alias>.md` passport.\n"
        )
        root_agents.write_text(content, encoding="utf-8")
        config_agents.write_text(content, encoding="utf-8")

    def _write_settings_json(self, settings_path: Path) -> None:
        workspaces_dir = str(self._cfg.paths.workspaces_root_dir)

        payload: dict[str, Any] = {
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
            "trustedWorkspaces": [workspaces_dir],
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
                    "write_file(/etc/shadow)",
                    "write_file(/root/.ssh/authorized_keys)",
                ],
                "ask": [],
            },
        }
        settings_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_hooks_json(self, hooks_path: Path) -> None:
        python_bin = str(self._cfg.paths.venv_python_path)
        payload: dict[str, Any] = {
            "enforce-native-file-tools": {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": "run_command|write_to_file|replace_file_content|multi_replace_file_content|find_by_name|grep_search",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{python_bin} -m antigravity_core.hooks.enforce_native_edit",
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
