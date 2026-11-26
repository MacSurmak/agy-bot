#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Antigravity Telegram Core — Strict Source-to-Install Release Deployer
# Usage (from non-root admin git checkout, e.g. ~/agy-bot):
#   cd ~/agy-bot && git pull && sudo bash scripts/deploy.sh
# Options:
#   --no-restart   Sync release, install wheel, reconcile harness without restarting systemd unit
# ==============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: Run this script with sudo: sudo bash scripts/deploy.sh" >&2
    exit 1
fi

RESTART_SERVICE=1
HARNESS_ARGS=()
for arg in "$@"; do
    case "${arg}" in
        --no-restart)
            RESTART_SERVICE=0
            ;;
        --update-plugins)
            HARNESS_ARGS+=("--update-plugins")
            ;;
        *)
            echo "ERROR: Unknown argument '${arg}'. Supported: --no-restart, --update-plugins" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OPT_DIR="/opt/antigravity"
APP_DIR="${OPT_DIR}/app"
PYTHON_DIR="${OPT_DIR}/python"
VENV_DIR="${OPT_DIR}/venv"
UNIT_SRC="${APP_DIR}/systemd/antigravity-core.service"
UNIT_DST="/etc/systemd/system/antigravity-core.service"

if [[ "$(readlink -f "${SOURCE_DIR}")" == "$(readlink -f "${APP_DIR}")" ]]; then
    echo "ERROR: Do not run deploy.sh from ${APP_DIR}. Clone the repository into a user-owned directory (e.g. ~/agy-bot) and run 'sudo bash ~/agy-bot/scripts/deploy.sh'." >&2
    exit 1
fi

if [[ ! -f "${SOURCE_DIR}/pyproject.toml" || ! -d "${SOURCE_DIR}/src/antigravity_core" ]]; then
    echo "ERROR: Source directory '${SOURCE_DIR}' does not look like the antigravity-telegram-core repository." >&2
    exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "ERROR: Virtualenv not found at ${VENV_DIR}. Run 'sudo bash ${SOURCE_DIR}/scripts/bootstrap_lxc.sh' first." >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "ERROR: 'rsync' is not installed. Install it via 'sudo apt-get install -y rsync' or run bootstrap_lxc.sh." >&2
    exit 1
fi

UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
if [[ ! -x "${UV_BIN}" ]]; then
    echo "ERROR: 'uv' binary not found in PATH or /usr/local/bin/uv." >&2
    exit 1
fi

echo "==> [1/5] Syncing immutable release tree (${SOURCE_DIR} -> ${APP_DIR})..."
install -d -m 0755 -o root -g root "${OPT_DIR}" "${APP_DIR}" "${OPT_DIR}/bin"
rsync -a --delete \
    --exclude='.git/' \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "${SOURCE_DIR}/" "${APP_DIR}/"
chown -R root:root "${APP_DIR}"
chmod -R a+rX "${APP_DIR}"

echo "==> [2/5] Installing non-editable release package into ${VENV_DIR}..."
export UV_PYTHON_INSTALL_DIR="${PYTHON_DIR}"
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
"${UV_BIN}" pip install --python "${VENV_DIR}/bin/python" --reinstall-package antigravity-telegram-core "${APP_DIR}"
chmod -R a+rX "${PYTHON_DIR}" "${VENV_DIR}"

echo "==> [3/5] Installing systemd unit (${UNIT_DST})..."
install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
systemctl daemon-reload

echo "==> [4/5] Reconciling declarative agy harness (RTK + plugins)..."
bash "${APP_DIR}/scripts/setup_harness.sh" "${HARNESS_ARGS[@]}"

if [[ "${RESTART_SERVICE}" -eq 1 ]]; then
    echo "==> [5/5] Restarting antigravity-core.service..."
    systemctl restart antigravity-core.service
    systemctl is-active --quiet antigravity-core.service
    echo "==> Deployment complete! antigravity-core.service is active."
else
    echo "==> [5/5] Skipping service restart (--no-restart)."
fi
