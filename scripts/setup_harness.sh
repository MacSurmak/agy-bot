#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Antigravity Telegram Core — Declarative Agent Harness Reconciler
# Declares the exact desired state for agy-agent:
#   1. Native RTK binary (/usr/local/bin/rtk) for PreToolUse output compression
#   2. Exact set of desired Antigravity CLI plugins (reconciled declaratively)
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: Run this script with sudo: sudo bash scripts/setup_harness.sh" >&2
    exit 1
fi

AGENT_USER="agy-agent"
if ! id -u "${AGENT_USER}" >/dev/null 2>&1; then
    echo "ERROR: System user '${AGENT_USER}' not found. Run scripts/bootstrap_lxc.sh first." >&2
    exit 1
fi

AGY_BIN="$(command -v agy || echo /usr/local/bin/agy)"
if [[ ! -x "${AGY_BIN}" ]]; then
    echo "ERROR: 'agy' binary not found in PATH or /usr/local/bin/agy." >&2
    exit 1
fi

echo "==> [1/2] Ensuring native RTK binary (/usr/local/bin/rtk) is installed..."
if [[ ! -x /usr/local/bin/rtk ]]; then
    curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
    if [[ -x /root/.local/bin/rtk ]]; then
        install -m 0755 /root/.local/bin/rtk /usr/local/bin/rtk
    elif [[ -x /root/.cargo/bin/rtk ]]; then
        install -m 0755 /root/.cargo/bin/rtk /usr/local/bin/rtk
    fi
fi
echo "    rtk: $(/usr/local/bin/rtk --version 2>/dev/null || echo 'installed')"

echo "==> [2/2] Reconciling desired Antigravity CLI plugins for ${AGENT_USER}..."
declare -A DESIRED_PLUGINS=(
    ["superpowers"]="https://github.com/obra/superpowers"
    ["ponytail"]="https://github.com/DietrichGebert/ponytail"
)

INSTALLED_LIST="$(su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin list" 2>/dev/null || true)"
while IFS= read -r line; do
    if [[ "${line}" =~ ^-\ ([a-zA-Z0-9._-]+)\ \( ]]; then
        pname="${BASH_REMATCH[1]}"
        if [[ -z "${DESIRED_PLUGINS[${pname}]+x}" ]]; then
            echo "    Removing undeclared plugin: ${pname}"
            su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin uninstall ${pname}" \
                || su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin disable ${pname}"
        fi
    fi
done <<< "${INSTALLED_LIST}"

for pname in "${!DESIRED_PLUGINS[@]}"; do
    repo_url="${DESIRED_PLUGINS[${pname}]}"
    echo "    Ensuring plugin '${pname}' (${repo_url})..."
    su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin install ${repo_url}"
done

echo "==> Final harness state (${AGENT_USER}):"
su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin list"
