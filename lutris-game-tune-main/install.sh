#!/usr/bin/env bash
# =============================================================================
# install.sh — lutris-game-tune installer (v4: CCD isolation re-architected,
#              constrain-in-place instead of sweep-based)
#
# Usage:
#   sudo ./install.sh              # install / update
#   sudo ./install.sh --uninstall  # uninstall
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

readonly LIB_DIR="/usr/local/lib/lutris-game-tune"
readonly SCRIPT_DEST="${LIB_DIR}/lutris-game-tune.sh"
readonly WRAPPER_DEST="/usr/local/bin/lutris-game-tune-wrapper"
readonly CONF_DEST="/etc/lutris-game-tune.conf"

c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        c_red "This script must be run as root: sudo $0 $*"
        exit 1
    fi
}

uninstall() {
    require_root
    local uninstall_sh="${SCRIPT_DIR}/uninstall.sh"
    if [[ -x "${uninstall_sh}" || -f "${uninstall_sh}" ]]; then
        c_yellow "Delegating to uninstall.sh (restores game state, supports --purge)..."
        exec bash "${uninstall_sh}" "${@:2}"
    fi
    # Fallback if uninstall.sh is missing from this directory: minimal removal.
    c_yellow "uninstall.sh not found next to install.sh — doing a minimal removal."
    c_yellow "Note: this fallback does NOT restore game state if still active."
    rm -f "${WRAPPER_DEST}"
    rm -rf "${LIB_DIR}"
    c_yellow "Note: ${CONF_DEST} is kept so you don't lose your settings."
    c_yellow "To remove it entirely: sudo rm -f ${CONF_DEST}"
    c_green "Uninstall complete."
    exit 0
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall "$@"
fi

require_root

# --- 0. Requirement checks --------------------------------------------------
command -v gcc   >/dev/null 2>&1 || { c_red "gcc not found. It's required for the build."; exit 1; }
command -v flock >/dev/null 2>&1 || { c_red "flock not found (part of util-linux)."; exit 1; }

# --- 1. Build the wrapper ----------------------------------------------------
c_yellow "[1/5] Building lutris-game-tune-wrapper (PRE/POST/STATUS + RUN)..."
TMP_BUILD="$(mktemp -d)"
trap 'rm -rf "${TMP_BUILD}"' EXIT
gcc -O2 -Wall -Wextra -o "${TMP_BUILD}/lutris-game-tune-wrapper" "${SCRIPT_DIR}/lutris-game-tune-wrapper.c"
c_green "    Build succeeded."

# --- 2. Install the script ---------------------------------------------------
c_yellow "[2/5] Copying script -> ${SCRIPT_DEST}"
mkdir -p "${LIB_DIR}"
install -o root -g root -m 755 "${SCRIPT_DIR}/lutris-game-tune.sh" "${SCRIPT_DEST}"

# --- 3. Install the config file ---------------------------------------------
c_yellow "[3/5] Checking configuration file..."
if [[ -f "${CONF_DEST}" ]]; then
    c_yellow "    ${CONF_DEST} already exists, NOT overwritten."
    c_yellow "    New sample: ${SCRIPT_DIR}/lutris-game-tune.conf"
    c_yellow "    To see differences: diff ${CONF_DEST} ${SCRIPT_DIR}/lutris-game-tune.conf"
    if grep -q '^THP_MODE=' "${CONF_DEST}" 2>/dev/null; then
        c_yellow "    NOTE: your existing config still has the now-unused THP_MODE key."
        c_yellow "    THP is always 'always' now — this line is ignored by the script,"
        c_yellow "    you may remove it manually if you'd like."
    fi
    if grep -q '^CCD_PROTECTED_CGROUPS=' "${CONF_DEST}" 2>/dev/null; then
        c_yellow "    NOTE: your existing config has CCD_PROTECTED_CGROUPS set."
        c_yellow "    As of v4, CCD isolation no longer sweeps processes out of"
        c_yellow "    existing cgroups (they're constrained in place instead), so"
        c_yellow "    this key has no effect any more — safe to remove, not required."
    fi
else
    install -o root -g root -m 644 "${SCRIPT_DIR}/lutris-game-tune.conf" "${CONF_DEST}"
    c_green "    Configuration file installed: ${CONF_DEST}"
fi

# --- 4. Install the wrapper as setuid root -----------------------------------
c_yellow "[4/5] Installing the wrapper as setuid root..."
install -o root -g root -m 4755 "${TMP_BUILD}/lutris-game-tune-wrapper" "${WRAPPER_DEST}"

# --- 5. Verification ----------------------------------------------------------
c_yellow "[5/5] Verifying..."
if [[ -u "${WRAPPER_DEST}" ]]; then
    c_green "    setuid bit is active: ${WRAPPER_DEST}"
else
    c_red "    WARNING: setuid bit could not be set. The filesystem may be mounted 'nosuid'."
fi

# Traces of an older, separate tasks-redirect/GameTune Suite install
readonly OLD_TASKS_LIB="/usr/local/lib/gametune"
readonly OLD_TASKS_BIN="/usr/local/bin/gametune-wrapper"
if [[ -e "${OLD_TASKS_BIN}" || -d "${OLD_TASKS_LIB}" ]]; then
    echo
    c_yellow "NOTE: found traces of a separate 'tasks-redirect'/GameTune Suite install:"
    [[ -e "${OLD_TASKS_BIN}" ]] && echo "  - ${OLD_TASKS_BIN}"
    [[ -d "${OLD_TASKS_LIB}" ]] && echo "  - ${OLD_TASKS_LIB}"
    c_yellow "CCD/CCX isolation is now integrated directly into lutris-game-tune.sh."
    c_yellow "After updating Lutris's Pre/Post-game script fields to the command below,"
    c_yellow "you can remove the old install manually:"
    echo "  sudo rm -f ${OLD_TASKS_BIN}"
    echo "  sudo rm -rf ${OLD_TASKS_LIB}"
fi

echo
c_green "============================================================"
c_green " Installation complete."
c_green "============================================================"
echo
echo "In Lutris (per-game via Configure > System options, or globally via"
echo "Preferences > System options), set the following fields:"
echo
echo "  Pre-game script:   ${WRAPPER_DEST} PRE"
echo "  Post-game script:  ${WRAPPER_DEST} POST"
echo
echo "Optional: to start the game with a higher priority (lower nice value),"
echo "put this in Lutris's 'Command prefix' field:"
echo
echo "  ${WRAPPER_DEST} RUN -5"
echo
echo "(Lutris prepends this prefix to the actual launch command automatically.)"
echo
echo "Status check:  sudo ${WRAPPER_DEST} STATUS"
echo "Configuration: ${CONF_DEST}"
echo "Log file:      /var/log/lutris-game-tune.log"
