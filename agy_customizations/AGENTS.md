# Antigravity Telegram Core — Global Agent Constitution & Infrastructure Index

You are running inside the central **Antigravity Telegram Core** LXC orchestrator on Proxmox VE.
Every conversation topic in Telegram corresponds to an isolated workspace.

## 1. Native Remote File Tools & Direct SSH Execution Law

1. **Direct Remote Paths in Native File Tools (`user@host:/path` and `user@host:~/path`):**
   - To read, create, or edit ANY file on a remote server (both user project files in `~/...` and system files in `/etc/...`), **ALWAYS pass the remote path directly to your native file tools**:
     - `view_file` (e.g. `user@bigcomp:~/md_run/md.mdp` or `user@bigcomp:/etc/slurm/slurm.conf`)
     - `write_to_file`
     - `replace_file_content`
     - `multi_replace_file_content`
   - The `PreToolUse` and `PostToolUse` hooks automatically stage that single file just-in-time, apply your native edit, push it back (handling `sudo` automatically if needed), and clean up local temporary files.
2. **Zero Shell File Edits:**
   - Editing or creating files via `run_command` (`sed -i`, `awk -i inplace`, `perl -pi`, `cat << EOF >`, `echo >`, `tee`, `dd of=`, `truncate`) is **STRICTLY FORBIDDEN** and blocked by `PreToolUse`.
3. **Executing Commands & Inspection on Remote Hosts (`run_command`):**
   - To inspect directories (`ls -la`, `find`, `rg`), check logs (`cat`, `head`, `tail`), or run workloads (`sbatch`, `gmx`, `docker`, `systemctl`, `pytest`), use your native `run_command` tool with standard `ssh`:
     `ssh user@host "command"` (or `ssh <alias> "command"`).
   - If a command requires administrator privileges, simply write `sudo` inside the command as usual (e.g. `ssh user@host "sudo systemctl restart slurmd"`).

## 2. Native Telegram, Event Sensor & Temporal Scheduler MCP Tools (`telegram-core`)

Never invent text tags like `[SEND_FILE: ...]`, never use the built-in `schedule` tool, and never create system timers via `crontab`, `systemctl`, or long `sleep`. Always call your native MCP tools from server `telegram-core`:
- `send_file_to_telegram(file_path, caption)` — sends any local or `/mnt/remotes/...` file (up to 2 GB) directly into the current Telegram topic.
- `create_sensor(name, command, instruction_prompt, sensor_kind, trigger_condition, poll_interval_sec, match_pattern, action_command, wake_mode, one_shot, cooldown_sec, notify_policy, keep_conversation_context)` — **Two-Stage Zero-Token Event Watcher & Reflex Engine!**
  - Stage 1 (Detector): Monitors `command` (`POLL_COMMAND` or real-time `STREAM_WATCH`) consuming 0 LLM tokens.
  - Stage 2 (Optional Millisecond Reflex `action_command`): If `action_command` is provided, the bot executes it within 10ms of the trigger (e.g. to capture an instant crash dump, `nvidia-smi`, `/proc/interrupts`, or restart a service) *before* waking you up!
  - `wake_mode`: `"ALWAYS"` (execute `action_command` and wake you up with its output), `"ON_ACTION_FAILURE"` (only wake you up if `action_command` exits non-zero), or `"NEVER"` (pure autopilot reflex without waking AI).
- `list_sensors()` / `cancel_sensor(sensor_id)` — list or stop active event sensors.
- `schedule_task(schedule_kind, trigger_spec, instruction_prompt, notify_policy, keep_conversation_context)` — schedules a time-based `ONE_SHOT` (`+5m`, `+2h`, or ISO-8601) or `CRON` (5-field cron) wake-up task.
- `list_scheduled_tasks()` / `cancel_scheduled_task(task_id)` — list or cancel time-based tasks.
- `register_remote_host(alias, hostname_or_ip, ssh_user, ssh_port, remote_root_path)` — registers a new SSH host, mounts it at `/mnt/remotes/<alias>`, and initializes its infrastructure rule file.
- `create_workspace_topic(title, workspace_path, execution_mode)` — creates a new Forum Topic in Telegram bound to `workspace_path`.

## 3. Self-Updating Infrastructure Memory (`~/.gemini/config/rules/infra-*.md`)

- Host passports are stored as modular rules in `~/.gemini/config/rules/infra-<alias>.md` with YAML frontmatter `trigger: model_decision`.
- Whenever you discover a new host, service, Docker stack, or path change on a machine, immediately update its `~/.gemini/config/rules/infra-<alias>.md` file using `replace_file_content` or `write_to_file` so future sessions know the exact topology without re-probing.
