#!/bin/bash
# =============================================================================
# uninstall.sh — lutris-game-tune removal script
#
# Usage:
#   sudo ./uninstall.sh           # remove binary + script (keeps config/log)
#   sudo ./uninstall.sh --purge   # also remove config and log files
#
# What it does:
#   1. If game mode is still active, runs POST first to restore parameters
#   2. Removes the wrapper and script
#   3. (--purge) Also removes /etc/lutris-game-tune.conf and the logs
# =============================================================================

set -euo pipefail

readonly LIB_DIR="/usr/local/lib/lutris-game-tune"
readonly SCRIPT_DST="${LIB_DIR}/lutris-game-tune.sh"
readonly WRAPPER_DST="/usr/local/bin/lutris-game-tune-wrapper"
readonly CONF_DST="/etc/lutris-game-tune.conf"
readonly PROFILE_DIR="/etc/lutris-game-tune.d"
readonly RT_MARKER="/etc/lutris-game-tune.allow-rt"
readonly LOG_FILE="/var/log/lutris-game-tune.log"
readonly STATE_DIR="/run/lutris-game-tune"
readonly LOCK_FILE="/run/lutris-game-tune.lock"

die() { echo "ERROR: $*" >&2; exit 1; }
msg() { echo "==> $*"; }

[[ "${EUID}" -eq 0 ]] || die "Root required. Run with 'sudo ./uninstall.sh'."

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
fi

# --- 1. Restore if game mode is still active -----------------------------------
if [[ -d "${STATE_DIR}" ]] && [[ -n "$(ls -A "${STATE_DIR}" 2>/dev/null)" ]]; then
    msg "Game mode is still ACTIVE — restoring original values before removal..."
    # RESTORE (not POST): the game counter may be stale, and we are removing
    # the tool anyway, so force everything back.
    if [[ -x "${WRAPPER_DST}" ]]; then
        "${WRAPPER_DST}" RESTORE || "${WRAPPER_DST}" POST || echo "WARNING: RESTORE/POST failed, continuing anyway." >&2
    elif [[ -f "${SCRIPT_DST}" ]]; then
        bash "${SCRIPT_DST}" RESTORE || bash "${SCRIPT_DST}" POST || echo "WARNING: RESTORE/POST failed, continuing anyway." >&2
    else
        echo "WARNING: Neither wrapper nor script found; saved state could not be restored." >&2
        echo "         Saved original values: ${STATE_DIR}/ (a reboot also clears everything)" >&2
    fi
fi

# --- 2. Remove files -------------------------------------------------------------
remove() {
    local path="$1"
    if [[ -e "${path}" || -L "${path}" ]]; then
        rm -f "${path}"
        msg "Removed: ${path}"
    fi
}

remove "${WRAPPER_DST}"
remove "${SCRIPT_DST}"
if [[ -d "${LIB_DIR}" ]]; then
    rmdir "${LIB_DIR}" 2>/dev/null && msg "Removed: ${LIB_DIR}" || \
        echo "WARNING: ${LIB_DIR} is not empty, left in place." >&2
fi

# Runtime leftovers (if any)
remove "${LOCK_FILE}"
if [[ -d "${STATE_DIR}" ]]; then
    rm -rf "${STATE_DIR}"
    msg "Removed: ${STATE_DIR}"
fi

# --- 3. Purge ---------------------------------------------------------------------
if (( PURGE )); then
    remove "${CONF_DST}"
    remove "${LOG_FILE}"
    remove "${LOG_FILE}.old"
    remove "${RT_MARKER}"
    if [[ -d "${PROFILE_DIR}" ]]; then
        rm -rf "${PROFILE_DIR}"
        msg "Removed: ${PROFILE_DIR}"
    fi
else
    [[ -e "${CONF_DST}" ]] && echo "Note: ${CONF_DST} was kept (use --purge to remove)."
    [[ -d "${PROFILE_DIR}" ]] && echo "Note: ${PROFILE_DIR} was kept (use --purge to remove)."
    [[ -e "${LOG_FILE}" ]] && echo "Note: ${LOG_FILE} was kept (use --purge to remove)."
fi

msg "Uninstall complete."
echo "Reminder: don't forget to manually clear the Pre/Post-game script fields in Lutris."
