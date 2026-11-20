#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Antigravity Telegram Core — Idempotent Agent Harness Provisioner
# - Installs Node.js 24 LTS (required by context-mode >= 24.15.0)
# - Removes legacy symlinked skills from runtime_data/external_repos and rtk
# - Installs official plugins strictly via `agy plugin install`
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: Run this script with sudo: sudo bash scripts/setup_harness.sh" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/bin:/root/.local/bin:${PATH}"

echo "==> [1/4] Ensuring Node.js 24 LTS is installed..."
NEED_NODE=1
if command -v node >/dev/null 2>&1; then
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [[ "${NODE_MAJOR}" -ge 24 ]]; then
        NEED_NODE=0
    fi
fi

if [[ "${NEED_NODE}" -eq 1 ]]; then
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
    apt-get install -y nodejs
fi
echo "    Node.js version: $(node --version)"

echo "==> [2/4] Cleaning legacy external_repos symlinks and unused rtk binary..."
SKILLS_DIR="/root/.gemini/config/skills"
EXT_REPOS_DIR="/srv/antigravity/app/runtime_data/external_repos"

if [[ -d "${SKILLS_DIR}" ]]; then
    for entry in "${SKILLS_DIR}"/*; do
        if [[ -L "${entry}" ]]; then
            target="$(readlink -f "${entry}" || true)"
            if [[ "${target}" == "${EXT_REPOS_DIR}"* ]] || [[ ! -e "${entry}" ]]; then
                rm -f "${entry}"
            fi
        fi
    done
fi

rm -rf "${EXT_REPOS_DIR}"
rm -f /usr/local/bin/rtk /root/.local/bin/rtk

echo "==> [3/4] Installing context-mode CLI/MCP package globally..."
npm install -g context-mode

echo "==> [4/4] Installing official Antigravity CLI plugins..."
AGY_BIN="$(command -v agy || echo /usr/local/bin/agy)"
if [[ ! -x "${AGY_BIN}" ]]; then
    echo "ERROR: 'agy' binary not found in PATH or /usr/local/bin/agy." >&2
    exit 1
fi

install_agy_plugin() {
    local repo_url="$1"
    echo "    Installing plugin: ${repo_url}"
    "${AGY_BIN}" plugin install "${repo_url}"
}

install_agy_plugin "https://github.com/addyosmani/agent-skills.git"
install_agy_plugin "https://github.com/DietrichGebert/ponytail"
install_agy_plugin "https://github.com/obra/superpowers"
install_agy_plugin "https://github.com/mksglu/context-mode"

echo "==> Harness verification:"
echo "    node: $(node --version)"
echo "    context-mode: $(command -v context-mode || echo 'not found')"
"${AGY_BIN}" plugin list
