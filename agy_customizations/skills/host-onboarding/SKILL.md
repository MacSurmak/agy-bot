---
name: host-onboarding
description: "Use when onboarding, registering, or security-auditing a remote SSH host in Antigravity Telegram Core. Registers the host via mcp_telegram-core_register_remote_host, runs a read-only SSH/firewall/sudo security audit, applies safe lockout-proof hardening, and persists the on-demand passport in ~/.gemini/config/hosts/infra-<alias>.md."
---

# Remote Host Onboarding & Security Hardening

## Prerequisites

- The `telegram-core` MCP server is available with `mcp_telegram-core_register_remote_host`, `mcp_telegram-core_update_host_knowledge`, and `mcp_telegram-core_get_bot_ssh_public_key`.
- You know the target host alias, IP/hostname, SSH owner username (never create a separate bot user; connect as the owner of the host's files or `root` on minimal LXC/router/VPS nodes), SSH port, and optional `jump_host`.

## Workflow

### Step 1: Register Host & Verify Key-Based Transport
1. Call `mcp_telegram-core_register_remote_host` with `alias`, `hostname_or_ip`, `ssh_user`, `ssh_port`, `remote_root_path`, and `jump_host` (pass `""` if directly reachable).
2. Verify non-interactive key authentication succeeds:
   ```bash
   ssh <ssh_user>@<alias> "whoami && hostname && stat -c '%a %n' ~/.ssh ~/.ssh/authorized_keys"
   ```
3. Ensure `~/.ssh` has mode `700` and `~/.ssh/authorized_keys` has mode `600`.

### Step 2: Read-Only Security Audit (Before Any Mutations)
Run a read-only security posture probe over SSH:
```bash
ssh <ssh_user>@<alias> "
echo '=== SSHD ==='
(sshd -T 2>/dev/null || sudo sshd -T 2>/dev/null) | grep -E '^(passwordauthentication|permitrootlogin|pubkeyauthentication|permitemptypasswords|kbdinteractiveauthentication) ' || true
echo '=== LISTENING PORTS ==='
ss -tulpn 2>/dev/null | head -n 30
echo '=== FIREWALL ==='
(sudo ufw status verbose 2>/dev/null || sudo nft list ruleset 2>/dev/null | head -n 30 || echo 'no ufw/nftables detected')
echo '=== FAIL2BAN ==='
systemctl is-active fail2ban 2>/dev/null || echo 'fail2ban inactive'
"
```
Report all findings (open ports, password auth status, root login policy, firewall state) clearly to the user.

### Step 3: Lockout-Safe SSH Hardening (On Confirmation)
Before modifying SSH or firewall rules on production, Proxmox, VPS, or router hosts, present the exact diff and wait for explicit owner approval.
1. Never edit `/etc/ssh/sshd_config` in place; write a drop-in file `/etc/ssh/sshd_config.d/99-hardening.conf` via `/ssh:<ssh_user>@<alias>:/etc/ssh/sshd_config.d/99-hardening.conf`:
   ```text
   PubkeyAuthentication yes
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitEmptyPasswords no
   ```
   Set `PermitRootLogin prohibit-password` on key-only root hosts (LXC/VPS) or `PermitRootLogin no` on non-root workstations.
2. Validate syntax BEFORE reloading (mandatory lockout guard):
   ```bash
   ssh <ssh_user>@<alias> "sudo sshd -t && sudo systemctl reload sshd || sudo systemctl reload ssh"
   ```
3. Verify a fresh non-interactive SSH connection succeeds before closing the turn.

### Step 4: Inventory & Persist On-Demand Passport
1. Inspect OS, CPU/GPU, disks, Docker, Slurm, Conda, and active project directories via `ssh <ssh_user>@<alias> "..."`.
2. Call `mcp_telegram-core_update_host_knowledge(host="<alias>", notes_markdown="...", append=True)` to store verified paths, services, security posture, and operational quirks in `~/.gemini/config/hosts/infra-<alias>.md`.

## Examples

### Example 1: Onboarding a LAN GPU Node Behind a Gateway
1. Call `mcp_telegram-core_register_remote_host(alias="bigcomp", hostname_or_ip="192.168.1.100", ssh_user="user", ssh_port=22, remote_root_path="/home/user", jump_host="user@b650mt")`.
2. Run Step 2 security audit and GPU/Slurm inventory (`nvidia-smi`, `sinfo -s`).
3. Persist findings via `mcp_telegram-core_update_host_knowledge(host="bigcomp", notes_markdown="...", append=True)`.

### Example 2: Onboarding a Key-Only VPS
1. Call `mcp_telegram-core_get_bot_ssh_public_key` if the VPS rejects password authentication, and provide the one-liner for the owner to authorize the key.
2. Once authorized, call `mcp_telegram-core_register_remote_host` and complete Steps 2–4.

## Limitations

- Embedded routers (such as Keenetic Entware/Dropbear) do not use OpenSSH `sshd_config.d` or `systemd`; skip Step 3 OpenSSH hardening and document router-specific constraints in the passport.
- Never run `sudo -n` during onboarding; plain `sudo` routes through the Telegram approval gateway.

## Troubleshooting

- **`Permission denied (publickey)` on a key-only host:** Call `mcp_telegram-core_get_bot_ssh_public_key`, give the owner the `authorized_keys` command, and retry `register_remote_host` once confirmed.
- **`Could not resolve hostname` or `No route to host`:** The target is likely behind a jump host; re-run `register_remote_host` with `jump_host="user@<gateway_alias>"`.
- **`sshd -t` fails after editing `99-hardening.conf`:** Do NOT reload `sshd`; remove or fix the drop-in file first so existing SSH access is preserved.
