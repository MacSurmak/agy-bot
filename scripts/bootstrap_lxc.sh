#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# STRICT ZERO-LEGACY POLICY (LATE-2026+ BASELINE):
# Target OS: Ubuntu 26.04 LTS / Debian 13+ with Python 3.14 / 3.15+ and PostgreSQL 18.6+.
# Using outdated 2025 / early-2026 runtimes (Python 3.12/3.13, PostgreSQL 17) is FORBIDDEN.
# Requires LXC Options -> Features: nesting=1,fuse=1 enabled on the Proxmox host.
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: bootstrap_lxc.sh must be executed as root inside the LXC container." >&2
    exit 1
fi

if [[ ! -e /dev/fuse ]]; then
    echo "ERROR: /dev/fuse is missing. Enable 'fuse=1' and 'nesting=1' in Proxmox LXC container Options -> Features." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    python3 \
    python3-venv \
    python3-dev \
    build-essential \
    dbus-daemon \
    dbus-user-session \
    gnome-keyring \
    libsecret-tools \
    openssh-client \
    sshfs \
    fuse3 \
    git \
    ripgrep \
    fd-find \
    jq \
    sqlite3 \
    docker.io \
    docker-compose-v2

mkdir -p \
    /srv/antigravity/app \
    /srv/antigravity/data \
    /srv/antigravity/workspaces \
    /srv/antigravity/inbox \
    /srv/antigravity/profiles \
    /srv/antigravity/logs \
    /mnt/remotes \
    /run/agy \
    /root/.ssh/config.d \
    /root/.gemini/config/rules \
    /root/.gemini/config/skills \
    /root/.gemini/antigravity-cli

chmod 0700 /run/agy /srv/antigravity/profiles /root/.ssh

if ! grep -q "Include ~/.ssh/config.d/\*.conf" /root/.ssh/config 2>/dev/null; then
    touch /root/.ssh/config
    chmod 0600 /root/.ssh/config
    printf "\nInclude ~/.ssh/config.d/*.conf\n" >> /root/.ssh/config
fi

if [[ ! -d /srv/antigravity/venv ]]; then
    python3 -m venv /srv/antigravity/venv
fi

/srv/antigravity/venv/bin/pip install --upgrade pip wheel setuptools
/srv/antigravity/venv/bin/pip install -e /srv/antigravity/app

echo "LXC base provisioning completed successfully."
