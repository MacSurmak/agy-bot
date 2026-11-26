---
name: host-onboarding
description: "Use when onboarding, registering, or auditing a remote SSH host via telegram-core MCP. Do NOT use for local files or already-registered hosts."
metadata:
  author: MacSurmak
  tags:
    - ssh
    - onboarding
    - security
    - infrastructure
---

# Remote Host Onboarding & Security Hardening

## Purpose

Standardizes remote SSH host registration, read-only security posture inspection, owner-approved lockout-safe SSH hardening, and persistent host passport recording inside Antigravity Telegram Core.

## Prerequisites

- The `telegram-core` MCP server exposes `mcp_telegram-core_register_remote_host`, `mcp_telegram-core_update_host_knowledge`, and `mcp_telegram-core_get_bot_ssh_public_key`.
- Target host alias, IP or hostname, SSH owner username (connect as the host file owner or `root` on minimal LXC/VPS/router nodes), SSH port, and optional jump host are known.

## Instructions

### 1. Register Host and Verify Key-Based Transport
1. Call `mcp_telegram-core_register_remote_host` with `alias`, `hostname_or_ip`, `ssh_user`, `ssh_port`, `remote_root_path`, and `jump_host` (empty string if directly reachable).
2. Verify non-interactive SSH connectivity with `ssh user@alias "whoami && hostname"`.
3. Confirm the remote user SSH directory and key-authorization file use strict permissions (`700` for the directory, `600` for the key list).

### 2. Read-Only Security Audit Before Any Mutations
Run individual read-only inspection commands over SSH without chaining elevated commands:
- Inspect effective OpenSSH settings (`sshd -T`) for `passwordauthentication`, `permitrootlogin`, `pubkeyauthentication`, `permitemptypasswords`, and `kbdinteractiveauthentication`.
- Inspect listening sockets (`ss -tulpn`).
- Inspect firewall status (`ufw status verbose` or `nft list ruleset`) and `systemctl is-active fail2ban`.
- Inform the user before invoking any elevated command; all privileged operations require explicit owner approval through the Telegram approval gateway.
- Report open ports, authentication policy, and firewall findings to the user before making changes.

### 3. Lockout-Safe SSH Hardening (Explicit Owner Approval Required)
1. Present the proposed SSH hardening settings to the user and wait for explicit confirmation before modifying any host configuration.
2. Write a drop-in configuration file under `/etc/ssh/sshd_config.d/` via native TRAMP edit tools (`/ssh:user@alias:/etc/ssh/sshd_config.d/99-hardening.conf`) enabling `PubkeyAuthentication yes` and disabling `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, and `PermitEmptyPasswords no`. Set `PermitRootLogin prohibit-password` on key-only root nodes or `PermitRootLogin no` on standard workstations.
3. Validate configuration syntax as a standalone command (`sshd -t`) before reloading the SSH daemon.
4. Only after syntax validation succeeds, reload the SSH service as a separate standalone command, then verify a fresh non-interactive SSH connection succeeds.

### 4. Inventory and Persist Host Passport
1. Collect OS version, CPU/GPU hardware, disk layout, Docker/Slurm/Conda environments, and key project directories over SSH.
2. Call `mcp_telegram-core_update_host_knowledge` with `append=True` to persist verified paths, services, security posture, and quirks into the host's on-demand passport under `/home/agy-agent/hosts/`.

## Examples

### Example 1: LAN GPU Node Behind a Gateway
1. Call `mcp_telegram-core_register_remote_host` for alias `bigcomp` (`192.168.1.100`, user `user`, port `22`, root `/home/user`, `jump_host="user@b650mt"`).
2. Run the read-only security audit and inspect GPU/Slurm state (`nvidia-smi`, `sinfo -s`).
3. Persist verified findings via `mcp_telegram-core_update_host_knowledge`.

### Example 2: Key-Only VPS
1. Call `mcp_telegram-core_get_bot_ssh_public_key` when password login is disabled and give the owner the public key to authorize on the VPS.
2. After owner confirmation, call `mcp_telegram-core_register_remote_host` and complete the read-only audit and inventory.

## Limitations

- Embedded routers (such as Keenetic Entware/Dropbear) do not use OpenSSH drop-in configs or `systemd`; skip OpenSSH hardening and record router constraints in the passport.
- Never use non-interactive elevation flags (`-n`); elevated commands must pass through the interactive Telegram approval gateway.

## Troubleshooting

- **Public key rejected on a key-only host:** Retrieve the public key via `mcp_telegram-core_get_bot_ssh_public_key`, ask the owner to authorize it on the host, then retry registration.
- **Unreachable hostname or no route to host:** Re-register the host with the appropriate `jump_host` gateway alias.
- **SSH daemon syntax check fails after hardening:** Do not reload the SSH service; fix or remove the drop-in configuration file first so active SSH access remains intact.
