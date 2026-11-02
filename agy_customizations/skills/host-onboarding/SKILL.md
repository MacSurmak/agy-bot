---
name: host-onboarding
description: "Onboard a new remote SSH server, mount its filesystem via SSHFS under /mnt/remotes/<alias>, inspect its hardware/OS/services, and write a complete infrastructure passport rule in ~/.gemini/config/rules/infra-<alias>.md."
---

# Host Onboarding Skill (`/host-onboarding`)

Follow these exact steps whenever the user asks to add, onboard, or study a remote machine:

1. **Register and Mount via MCP:**
   Call `mcp:telegram-core:register_remote_host` with `alias`, `hostname_or_ip`, `ssh_user`, `ssh_port`, and `remote_root_path`. This writes `~/.ssh/config.d/<alias>.conf` and mounts `/mnt/remotes/<alias>`.
2. **Collect System Telemetry via Read-Only SSH:**
   Run a single diagnostic command via `run_command`:
   `ssh <alias> "uname -a && lsb_release -a 2>/dev/null; lscpu | head -n 20; free -h; df -hT; docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null; systemctl list-units --type=service --state=running --no-pager | head -n 40"`
3. **Write the Infrastructure Rule Passport:**
   Use `write_to_file` to write `/root/.gemini/config/rules/infra-<alias>.md` starting with:
   ```yaml
   ---
   trigger: model_decision
   description: "Infrastructure passport for host <alias> (SSH alias <alias>, mounted at /mnt/remotes/<alias>, OS, Docker containers, services, key directories). Read before performing any task on <alias>."
   ---
   ```
   Include full details of OS, CPU/RAM/Disks, running Docker stacks, systemd services, and key project directories under `/mnt/remotes/<alias>/...`.
