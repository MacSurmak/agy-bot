---
name: host-onboarding
description: "Onboard a new remote SSH server, mount its filesystem via SSHFS under /mnt/remotes/<alias>, inspect its hardware/OS/services, and write a complete infrastructure passport rule in ~/.gemini/config/rules/infra-<alias>.md."
---

# Host Onboarding Skill (`/host-onboarding`)

Follow these exact steps whenever the user asks to add, onboard, or study a remote machine:

1. **Register Host via MCP (`register_remote_host`):**
   Call `mcp:telegram-core:register_remote_host(alias, hostname_or_ip, ssh_user, ssh_port, remote_root_path, jump_host)`.
   - If the host is an internal LAN/cluster node behind a gateway, pass `jump_host="user@gateway"`.
   - Never manually scrape `/root/.ssh/id_*` or run interactive `srun` jobs to copy keys — `register_remote_host` automatically configures `ProxyJump`, provisions the SSH key, and generates `/root/.gemini/config/rules/infra-<alias>.md`.
2. **Record Architectural Notes via MCP (`update_host_knowledge`):**
   After inspecting services, Slurm partitions, Docker stacks, or project directories via `ssh user@<alias> "..."`, call `mcp:telegram-core:update_host_knowledge(host="<alias>", notes_markdown="...", append=True)` to persist insights in `/root/.gemini/config/rules/infra-<alias>.md`.
