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
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
"${UV_BIN}" pip install --python /srv/antigravity/venv/bin/python -e /srv/antigravity/app

echo "==> [4/5] Installing Google Antigravity CLI (agy)..."
if ! command -v agy >/dev/null 2>&1 && [[ ! -x /usr/local/bin/agy ]]; then
    curl -fsSL https://antigravity.google/cli/install.sh | bash
    if [[ -x /root/.local/bin/agy ]]; then
        install -m 0755 /root/.local/bin/agy /usr/local/bin/agy
    elif [[ -x "${HOME}/.local/bin/agy" ]]; then
        install -m 0755 "${HOME}/.local/bin/agy" /usr/local/bin/agy
    fi
fi

echo "==> [5/6] Syncing external skill repositories and token optimization tools..."
EXT_REPOS_DIR="/srv/antigravity/app/runtime_data/external_repos"
mkdir -p "${EXT_REPOS_DIR}"

sync_git_repo() {
    local repo_url="$1"
    local target_dir="$2"
    if [[ -d "${target_dir}/.git" ]]; then
        git -C "${target_dir}" pull --ff-only || true
    else
        rm -rf "${target_dir}"
        git clone --depth 1 "${repo_url}" "${target_dir}"
    fi
}

sync_git_repo "https://github.com/addyosmani/agent-skills.git" "${EXT_REPOS_DIR}/agent-skills"
sync_git_repo "https://github.com/DietrichGebert/ponytail.git" "${EXT_REPOS_DIR}/ponytail"
sync_git_repo "https://github.com/juliusbrussee/caveman.git" "${EXT_REPOS_DIR}/caveman"
sync_git_repo "https://github.com/mksglu/context-mode.git" "${EXT_REPOS_DIR}/context-mode"

if ! command -v rtk >/dev/null 2>&1 && [[ ! -x /usr/local/bin/rtk ]]; then
    curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh | sh || true
    if [[ -x "${HOME}/.local/bin/rtk" ]]; then
        install -m 0755 "${HOME}/.local/bin/rtk" /usr/local/bin/rtk
    fi
fi

echo "==> [6/6] Preparing runtime directories..."
mkdir -p \
    /srv/antigravity/app/runtime_data \
    /run/agy \
    /root/.gemini \
    /root/.ssh/config.d

chmod 0700 /run/agy /root/.ssh

echo "==> Provisioning complete! Python version in venv: $(/srv/antigravity/venv/bin/python --version)"
