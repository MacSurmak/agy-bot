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

UPDATE_PLUGINS=0
for arg in "$@"; do
    case "${arg}" in
        --update-plugins)
            UPDATE_PLUGINS=1
            ;;
        *)
            echo "ERROR: Unknown argument '${arg}'. Supported: --update-plugins" >&2
            exit 1
            ;;
    esac
done

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

echo "==> [1/3] Ensuring native RTK binary (/usr/local/bin/rtk) is installed..."
if [[ ! -x /usr/local/bin/rtk ]]; then
    curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
    if [[ -x /root/.local/bin/rtk ]]; then
        install -m 0755 /root/.local/bin/rtk /usr/local/bin/rtk
    elif [[ -x /root/.cargo/bin/rtk ]]; then
        install -m 0755 /root/.cargo/bin/rtk /usr/local/bin/rtk
    fi
fi
echo "    rtk: $(/usr/local/bin/rtk --version 2>/dev/null || echo 'installed')"

echo "==> [2/3] Configuring RTK policy and native Antigravity plugin for ${AGENT_USER}..."
install -d -m 0700 -o "${AGENT_USER}" -g "${AGENT_USER}" "/home/${AGENT_USER}/.config"
install -d -m 0755 -o root -g "${AGENT_USER}" "/home/${AGENT_USER}/.config/rtk"
cat << 'EOF' > "/home/${AGENT_USER}/.config/rtk/config.toml"
[hooks]
exclude_commands = ["ssh"]
EOF
chown root:"${AGENT_USER}" "/home/${AGENT_USER}/.config/rtk/config.toml"
chmod 0644 "/home/${AGENT_USER}/.config/rtk/config.toml"

if [[ ! -f "/home/${AGENT_USER}/.gemini/config/plugins/rtk/plugin.json" || "${UPDATE_PLUGINS}" -eq 1 ]]; then
    echo "    Installing native RTK Antigravity plugin..."
    su -s /bin/bash - "${AGENT_USER}" -c "/usr/local/bin/rtk init -g --agent antigravity"
else
    echo "    Native RTK Antigravity plugin already installed (skip; pass --update-plugins to refresh)."
fi

echo "==> [3/3] Reconciling desired Antigravity CLI plugins for ${AGENT_USER}..."
declare -A DESIRED_PLUGINS=(
    ["superpowers"]="https://github.com/obra/superpowers"
)
declare -A PRESERVED_NATIVE_PLUGINS=(
    ["rtk"]=1
)

declare -A INSTALLED_PLUGINS=()
INSTALLED_LIST="$(su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin list" 2>/dev/null || true)"
while IFS= read -r line; do
    if [[ "${line}" =~ ^-\ ([a-zA-Z0-9._-]+)\ \( ]]; then
        pname="${BASH_REMATCH[1]}"
        INSTALLED_PLUGINS["${pname}"]=1
        if [[ -z "${DESIRED_PLUGINS[${pname}]+x}" && -z "${PRESERVED_NATIVE_PLUGINS[${pname}]+x}" ]]; then
            echo "    Removing undeclared plugin: ${pname}"
            su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin uninstall ${pname}" \
                || su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin disable ${pname}"
        fi
    fi
done <<< "${INSTALLED_LIST}"

for pname in "${!DESIRED_PLUGINS[@]}"; do
    repo_url="${DESIRED_PLUGINS[${pname}]}"
    if [[ -n "${INSTALLED_PLUGINS[${pname}]+x}" && "${UPDATE_PLUGINS}" -eq 0 ]]; then
        echo "    Plugin '${pname}' already installed (skip; pass --update-plugins to refresh)."
        continue
    fi
    echo "    Installing plugin '${pname}' (${repo_url})..."
    su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin install ${repo_url}"
done

echo "==> Final harness state (${AGENT_USER}):"
su -s /bin/bash - "${AGENT_USER}" -c "${AGY_BIN} plugin list"
