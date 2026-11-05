#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Antigravity Telegram Core — Production LXC Provisioner (Debian 13 Trixie / Ubuntu)
# - Installs official Docker CE + Compose v2 plugin from download.docker.com
# - Provisions standalone Python 3.14 environment via `uv`
# - Requires LXC Options -> Features: nesting=1,fuse=1 on the Proxmox host
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: Run this script with sudo: sudo bash scripts/bootstrap_lxc.sh" >&2
    exit 1
fi

if [[ ! -e /dev/fuse ]]; then
    echo "ERROR: /dev/fuse is missing. Enable 'nesting=1,fuse=1' in Proxmox LXC container Options -> Features." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> [1/4] Installing base OS packages..."
apt-get update -y
apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
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
    sqlite3

echo "==> [2/4] Installing official Docker CE & Docker Compose plugin..."
if ! docker compose version >/dev/null 2>&1; then
    # shellcheck source=/dev/null
    source /etc/os-release
    DISTRO_ID="${ID:-debian}"
    CODENAME="${VERSION_CODENAME:-trixie}"

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DISTRO_ID} ${CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -y
    apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

    systemctl enable --now docker.service
fi

echo "==> [3/4] Provisioning Python 3.14 virtual environment via uv..."
if ! command -v uv >/dev/null 2>&1 && [[ ! -x /root/.local/bin/uv ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh
fi

UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
"${UV_BIN}" venv --python 3.14 --clear /srv/antigravity/venv
"${UV_BIN}" pip install --python /srv/antigravity/venv/bin/python -e /srv/antigravity/app

echo "==> [4/4] Preparing runtime directories..."
mkdir -p \
    /srv/antigravity/app/runtime_data \
    /run/agy \
    /root/.gemini \
    /root/.ssh/config.d

chmod 0700 /run/agy /root/.ssh

echo "==> Provisioning complete! Python version in venv: $(/srv/antigravity/venv/bin/python --version)"
