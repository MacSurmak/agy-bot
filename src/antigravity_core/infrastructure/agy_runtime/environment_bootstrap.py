import json
import shutil
import sqlite3
from pathlib import Path

from loguru import logger

from antigravity_core.config.static_config import StaticConfig
from antigravity_core.domain.exceptions import StartupValidationError
from antigravity_core.domain.models import RemoteHost
from antigravity_core.infrastructure.process_ownership import as_agent_identity


class AgyEnvironmentBootstrapper:
    """Validates and synchronizes ~/.gemini/ configuration, SSOT skills, hooks, MCP stdio_proxy, and SQLite WAL mode."""

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
        hosts_dir = self._cfg.paths.hosts_passports_dir
        skills_dir = config_dir / "skills"

        for root_dir in (
            self._cfg.paths.runtime_bin_dir,
            self._cfg.paths.runtime_dir,
            self._cfg.paths.log_file_path.parent,
        ):
            root_dir.mkdir(parents=True, exist_ok=True)

        agent_owned_dirs = (
            self._cfg.paths.agent_home_dir,
            agy_home,
            cli_dir,
            config_dir,
            hosts_dir,
            skills_dir,
            self._cfg.paths.projects_dir,
            self._cfg.paths.workspaces_root_dir,
            self._cfg.paths.remotes_mount_dir,
            self._cfg.paths.inbox_dir,
            self._cfg.paths.profiles_data_dir,
            self._cfg.paths.agent_log_dir,
            self._cfg.paths.ssh_config_dir.parent,
            self._cfg.paths.ssh_config_dir,
            self._cfg.paths.ssh_sockets_dir,
        )
        with as_agent_identity(self._cfg.paths):
            for directory in agent_owned_dirs:
                directory.mkdir(parents=True, exist_ok=True)
            self._write_settings_json(cli_dir / "settings.json")
            self._write_hooks_json(config_dir / "hooks.json")
            self._write_mcp_config_json(config_dir / "mcp_config.json")
            self._sync_custom_skills_from_repo(skills_dir)
            self._configure_agy_internal_sqlite_wal(
                cli_dir / "conversation_summaries.db"
            )
        self.sync_global_agents_md_host_map(hosts=[])

    def sync_global_agents_md_host_map(self, hosts: list[RemoteHost]) -> None:
        agy_home = self._cfg.paths.agy_home_dir
        root_agents = agy_home / "AGENTS.md"
        config_agents = agy_home / "config" / "AGENTS.md"
        hosts_dir = self._cfg.paths.hosts_passports_dir
        projects_dir = self._cfg.paths.projects_dir

        if hosts:
            table_lines = [
                "| Alias | Route | TRAMP Prefix | On-Demand Passport |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for h in hosts:
                route_note = f" (via `{h.jump_host}`)" if h.jump_host else ""
                table_lines.append(
                    f"| `{h.alias}` | `{h.ssh_user}@{h.hostname_or_ip}:{h.ssh_port}`{route_note} "
                    f"| `/ssh:{h.ssh_user}@{h.alias}:{h.remote_root_path}` | `{h.rule_file_path}` |"
                )
            hosts_table_md = "\n".join(table_lines)
        else:
            hosts_table_md = "*(No hosts registered yet. Call `mcp_telegram-core_register_remote_host` first.)*"

        content = (
            "# Antigravity Telegram Core — Agent Constitution & Infrastructure Index\n\n"
            "You operate as non-root user `agy-agent` inside the central **Antigravity Telegram Core** LXC workstation.\n"
            f"- **Topic scratchpad (`cwd`):** temporary files and charts for the current Telegram topic.\n"
            f"- **Local GitHub workstation (`{projects_dir}/<repo>`):** clone and edit Git repositories natively here (`view_file`, `replace_file_content`, `rg`, `devtool check`, `git push`).\n\n"
            "## 1. Registered Remote Hosts (On-Demand Passports)\n\n"
            f"{hosts_table_md}\n\n"
            f"- Full host passports live on demand in `{hosts_dir}/infra-<alias>.md`. Read a host's passport with `view_file` only when working on that host.\n"
            "- Whenever you discover a durable host invariant (Python/conda path, `/shared/` mount rule, service, or CLI quirk), immediately persist it via `mcp_telegram-core_update_host_knowledge`.\n\n"
            "## 2. Remote SSH & TRAMP Law\n\n"
            "1. **Onboard unknown hosts first:** If a server is not in the table above, call `mcp_telegram-core_register_remote_host` (pass `jump_host=\"user@<gateway_alias>\"` for internal LAN/cluster nodes).\n"
            "2. **Address registered hosts by alias only:**\n"
            "   - Native file tools (`view_file`, `write_to_file`, `replace_file_content`): `/ssh:user@<alias>:/abs/path` (always full absolute paths, never `~`).\n"
            "   - If `view_file` reports `REMOTE FILE DOES NOT EXIST`, call `write_to_file` on `/ssh:user@<alias>:/abs/path` to create it.\n"
            "   - Never edit files with `sed -i`, `awk -i`, or `perl -pi`.\n"
            '   - Shell commands (`run_command`): `ssh user@<alias> "command"`.\n'
            "   - **Sudo rule:** Write plain `sudo <cmd>` (the PTY gateway requests Telegram approval). NEVER use `sudo -n`.\n"
            "   - **Proxmox containers:** On `proxmox`, always invoke `sudo pct exec <ctid> -- <cmd>`.\n"
            "   - **Multi-line remote scripts:** Always use single-quoted heredoc `ssh user@<alias> \"python3 - << 'PYEOF'\\n...\\nPYEOF\"`.\n"
            "   - **Safe `pkill`:** Bracket the first character (`pkill -f '[p]attern'`) so SSH does not kill itself.\n"
            "3. **Asynchronous Long-Command Law (> 30-60s):** Never block `run_command` on multi-minute jobs. Launch detached with an exit-code marker:\n"
            '   `ssh user@<alias> "setsid bash -c \'<cmd>; echo \\$? > /path/run.exit\' > /path/run.log 2>&1 < /dev/null & echo \\$! > /path/run.pid"`\n'
            "   Then create a dual-condition sensor via `mcp_telegram-core_create_sensor` checking `test -s /path/run.exit || ! kill -0 $(cat /path/run.pid) 2>/dev/null` and finish your turn. Never claim a sensor is active unless `create_sensor` returned a real `sensor_id`.\n"
            "4. **Code exploration & empirical verification:** When `devtool` is available in a repository, run `devtool tree --stdout` before blind file reads. Verify CLI flags and behavior empirically (`--help` or isolated test) before reporting bugs.\n"
            "5. **Deliver files to Telegram:** Pass local or TRAMP paths (`/ssh:user@<alias>:/abs/path`) directly to `mcp_telegram-core_send_file_to_telegram`.\n"
            "6. **Sandbox boundary:** Never inspect or touch `/opt/antigravity/app`, `/etc/antigravity`, `/var/lib/antigravity`, or other topics' directories.\n\n"
            "## 3. Native Eager MCP Tools (`telegram-core`)\n\n"
            "Call `mcp_telegram-core_<tool>` directly without reading schema files:\n"
            "- `register_remote_host`, `update_host_knowledge`, `get_bot_ssh_public_key`\n"
            "- `send_file_to_telegram`\n"
            "- `schedule_task`, `list_scheduled_tasks`, `cancel_scheduled_task`\n"
            "- `create_sensor`, `list_sensors`, `cancel_sensor`\n"
            "- `create_workspace_topic`, `rename_current_topic` (call on turn 1 of a new topic with a concise 3-5 word title in the user's language)\n"
        )
        with as_agent_identity(self._cfg.paths):
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
            "trustedWorkspaces": [
                workspaces_dir,
                inbox_dir,
                str(self._cfg.paths.projects_dir),
            ],
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
                    "read_file(/etc/antigravity/*)",
                    "write_file(/etc/antigravity/*)",
                    "read_file(/var/lib/antigravity/*)",
                    "write_file(/var/lib/antigravity/*)",
                ],
                "ask": [],
            },
        }
        settings_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_hooks_json(self, hooks_path: Path) -> None:
        python_bin = str(self._cfg.paths.venv_python_path)
        pre_matcher = (
            "run_command|command|read_file|write_file|edit_file|view_file|write_to_file|"
            "replace_file_content|multi_replace_file_content|list_dir|find_by_name|grep_search|schedule"
        )
        post_matcher = (
            "read_file|write_file|edit_file|view_file|write_to_file|"
            "replace_file_content|multi_replace_file_content"
        )
        payload: dict[str, dict[str, bool | list[dict[str, str | list[dict[str, str | int]]]]]] = {
            "enforce-native-file-tools": {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": pre_matcher,
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
                        "matcher": post_matcher,
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
        if self._cfg.paths.rtk_binary_path is not None:
            payload["rtk-output-compression"] = {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": "run_command",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{self._cfg.paths.rtk_binary_path} hook antigravity",
                                "timeout": 10,
                            }
                        ],
                    }
                ],
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
                "rename_current_topic",
                "get_bot_ssh_public_key",
            )
        }
        payload: dict[
            str, dict[str, dict[str, str | list[str] | int | dict[str, dict[str, bool]]]]
        ] = {
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

    def _sync_custom_skills_from_repo(self, dest_skills_dir: Path) -> None:
        """Mirrors canonical skills from agy_customizations/skills/ into ~/.gemini/config/skills/ (SSOT)."""
        src_skills_dir = self._cfg.paths.project_root_dir / "agy_customizations" / "skills"
        if not src_skills_dir.is_dir():
            raise StartupValidationError(
                f"Canonical skills directory not found at {src_skills_dir}"
            )

        desired_names: set[str] = set()
        for entry in sorted(src_skills_dir.iterdir()):
            if not entry.is_dir() or not (entry / "SKILL.md").is_file():
                continue
            desired_names.add(entry.name)
            target_dir = dest_skills_dir / entry.name
            if target_dir.exists() or target_dir.is_symlink():
                if target_dir.is_symlink() or target_dir.is_file():
                    target_dir.unlink()
                else:
                    shutil.rmtree(target_dir)
            shutil.copytree(entry, target_dir)

        for existing in list(dest_skills_dir.iterdir()):
            if existing.name not in desired_names:
                if existing.is_symlink() or existing.is_file():
                    existing.unlink()
                else:
                    shutil.rmtree(existing)

        logger.info(
            "Synchronized canonical custom skills from repository",
            count=len(desired_names),
            skills=sorted(desired_names),
        )

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
            "Configured WAL mode for agy internal conversation_summaries.db: path={}",
            db_path,
        )
