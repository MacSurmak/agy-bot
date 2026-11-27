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

echo "==> [1/6] Installing base OS packages..."
apt-get update -y
apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    build-essential \
    rustc \
    cargo \
    dbus-daemon \
    dbus-user-session \
    gnome-keyring \
    libsecret-tools \
    openssh-client \
    sshfs \
    fuse3 \
    git \
    rsync \
    ripgrep \
    fd-find \
    tree \
    jq \
    sqlite3

echo "==> [2/6] Installing official Docker CE & Docker Compose plugin..."
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

echo "==> [3/6] Provisioning Python 3.14 virtual environment via uv..."
if ! command -v uv >/dev/null 2>&1 && [[ ! -x /root/.local/bin/uv ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh
fi

UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
export UV_PYTHON_INSTALL_DIR="/opt/antigravity/python"
export UV_TOOL_DIR="/opt/antigravity/tools"
export UV_TOOL_BIN_DIR="/usr/local/bin"
install -d -m 0755 -o root -g root /opt/antigravity /opt/antigravity/python /opt/antigravity/bin /opt/antigravity/tools
"${UV_BIN}" python install 3.14
"${UV_BIN}" venv --python 3.14 --clear /opt/antigravity/venv
"${UV_BIN}" tool install --python 3.14 --upgrade ruff
"${UV_BIN}" tool install --python 3.14 --upgrade pyright
"${UV_BIN}" tool install --python 3.14 --upgrade code-review-graph
chmod -R a+rX /opt/antigravity/python /opt/antigravity/tools

echo "==> [4/6] Installing Google Antigravity CLI (agy)..."
if ! command -v agy >/dev/null 2>&1 && [[ ! -x /usr/local/bin/agy ]]; then
    curl -fsSL https://antigravity.google/cli/install.sh | bash
    if [[ -x /root/.local/bin/agy ]]; then
        install -m 0755 /root/.local/bin/agy /usr/local/bin/agy
    elif [[ -x "${HOME}/.local/bin/agy" ]]; then
        install -m 0755 "${HOME}/.local/bin/agy" /usr/local/bin/agy
    fi
fi

echo "==> [5/6] Provisioning dedicated non-root agy-agent user and directories..."
if ! id -u agy-agent >/dev/null 2>&1; then
    useradd -m -s /bin/bash -d /home/agy-agent agy-agent
fi

install -d -m 0700 -o root -g root /etc/antigravity-core /var/lib/antigravity /var/lib/antigravity/data /var/lib/antigravity/logs
install -d -m 0750 -o root -g agy-agent /run/agy
install -d -m 0700 -o agy-agent -g agy-agent \
    /home/agy-agent \
    /home/agy-agent/.gemini \
    /home/agy-agent/.gemini/config \
    /home/agy-agent/.gemini/config/skills \
    /home/agy-agent/.gemini/antigravity-cli \
    /home/agy-agent/.ssh \
    /home/agy-agent/.ssh/config.d \
    /home/agy-agent/.ssh/sockets \
    /home/agy-agent/hosts \
    /home/agy-agent/projects \
    /home/agy-agent/workspaces \
    /home/agy-agent/inbox \
    /home/agy-agent/profiles \
    /home/agy-agent/logs

echo "==> [6/6] Deploying release into /opt/antigravity/app and reconciling harness..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "${SCRIPT_DIR}/deploy.sh" --no-restart

echo "==> Provisioning complete! Python version in venv: $(/opt/antigravity/venv/bin/python --version)"
echo "==> Next: place .env and config.yaml in /etc/antigravity-core (chmod 0600, root:root), then run: sudo systemctl enable --now antigravity-core"
