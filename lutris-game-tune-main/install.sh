#!/usr/bin/env bash
# =============================================================================
# install.sh — lutris-game-tune installer (v4: CCD isolation re-architected,
#              constrain-in-place instead of sweep-based)
#
# Usage:
#   sudo ./install.sh              # install / update (setuid wrapper, classic)
#   sudo ./install.sh --polkit     # install / update WITHOUT setuid:
#                                  #   PRE/POST through pkexec + polkit action,
#                                  #   RUN through the cap_sys_nice file capability
#   sudo ./install.sh --uninstall  # uninstall
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

readonly LIB_DIR="/usr/local/lib/lutris-game-tune"
readonly SCRIPT_DEST="${LIB_DIR}/lutris-game-tune.sh"
readonly WRAPPER_DEST="/usr/local/bin/lutris-game-tune-wrapper"
readonly CONF_DEST="/etc/lutris-game-tune.conf"
readonly PROFILE_DIR="/etc/lutris-game-tune.d"
readonly POLICY_NAME="org.drstool.lutris-game-tune.policy"
readonly POLICY_DEST="/usr/share/polkit-1/actions/${POLICY_NAME}"

c_green()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red()    { printf '\033[31m%s\033[0m\n' "$*"; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        c_red "This script must be run as root: sudo $0"
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

MODE="setuid"
case "${1:-}" in
    --uninstall) uninstall "$@" ;;
    --polkit)    MODE="polkit" ;;
    --setuid|"") MODE="setuid" ;;
    *) c_red "Unknown option: $1 (use --polkit, --setuid or --uninstall)"; exit 1 ;;
esac

require_root

# --- 0. Requirement checks --------------------------------------------------
command -v gcc   >/dev/null 2>&1 || { c_red "gcc not found. It's required for the build."; exit 1; }
command -v flock >/dev/null 2>&1 || { c_red "flock not found (part of util-linux)."; exit 1; }
if [[ "${MODE}" == "polkit" ]] && ! command -v pkexec >/dev/null 2>&1; then
    c_red "pkexec not found — install polkit first (or use the default setuid mode)."
    exit 1
fi

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
# DRSTool's "Uninstall" button runs the copy in the lib dir
install -o root -g root -m 755 "${SCRIPT_DIR}/uninstall.sh" "${LIB_DIR}/uninstall.sh"

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

# Per-game profile directory (PRE <profile>); must be root-owned, not g/o-writable
install -d -o root -g root -m 755 "${PROFILE_DIR}"
if [[ -d "${SCRIPT_DIR}/profiles" ]]; then
    for _p in "${SCRIPT_DIR}"/profiles/*.example; do
        [[ -f "${_p}" ]] || continue
        _d="${PROFILE_DIR}/$(basename "${_p}")"
        [[ -e "${_d}" ]] || install -o root -g root -m 644 "${_p}" "${_d}"
    done
fi
c_green "    Profile directory ready: ${PROFILE_DIR}"

# --- 4. Install the wrapper ---------------------------------------------------
if [[ "${MODE}" == "polkit" ]]; then
    c_yellow "[4/5] Installing the wrapper WITHOUT setuid (polkit + file capability)..."
    install -o root -g root -m 755 "${TMP_BUILD}/lutris-game-tune-wrapper" "${WRAPPER_DEST}"
    install -d -o root -g root -m 755 "$(dirname "${POLICY_DEST}")"
    install -o root -g root -m 644 "${SCRIPT_DIR}/${POLICY_NAME}" "${POLICY_DEST}"
    if command -v setcap >/dev/null 2>&1 && setcap cap_sys_nice=ep "${WRAPPER_DEST}"; then
        CAP_OK=1
    else
        CAP_OK=0
    fi
else
    c_yellow "[4/5] Installing the wrapper as setuid root..."
    install -o root -g root -m 4755 "${TMP_BUILD}/lutris-game-tune-wrapper" "${WRAPPER_DEST}"
    # switching back from polkit mode: the action would otherwise stay installed
    rm -f "${POLICY_DEST}"
fi

# --- 5. Verification ----------------------------------------------------------
c_yellow "[5/5] Verifying..."
if [[ "${MODE}" == "polkit" ]]; then
    if [[ -u "${WRAPPER_DEST}" ]]; then
        c_red "    ERROR: the wrapper still has the setuid bit."
        exit 1
    fi
    c_green "    no setuid bit: ${WRAPPER_DEST}"
    c_green "    polkit action:  ${POLICY_DEST}"
    if [[ "${CAP_OK}" == 1 ]]; then
        c_green "    file capability: $(getcap "${WRAPPER_DEST}" 2>/dev/null || echo cap_sys_nice=ep)"
    else
        c_red "    WARNING: could not set cap_sys_nice on the wrapper (setcap missing, or the"
        c_red "    filesystem has no extended attributes). PRE/POST work; RUN starts games"
        c_red "    without the priority boost."
    fi
elif [[ -u "${WRAPPER_DEST}" ]]; then
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
if [[ "${MODE}" == "polkit" ]]; then
    PFX="pkexec "
else
    PFX=""
fi
echo "  Pre-game script:   ${PFX}${WRAPPER_DEST} PRE"
echo "  Post-game script:  ${PFX}${WRAPPER_DEST} POST"
echo
if [[ "${MODE}" == "polkit" ]]; then
    echo "(polkit mode: games whose Lutris config still says '${WRAPPER_DEST} PRE' without"
    echo " 'pkexec' must be updated — DRSTool's Lutris tab rewrites them on the next Apply.)"
    echo
fi
echo "Optional: to start the game with a higher priority (lower nice value),"
echo "put this in Lutris's 'Command prefix' field:"
echo
echo "  ${WRAPPER_DEST} RUN -5"
echo
echo "(Lutris prepends this prefix to the actual launch command automatically.)"
echo
echo "Per-game profile (optional): ${PFX}${WRAPPER_DEST} PRE <profile>"
echo "  -> loads ${PROFILE_DIR}/<profile>.conf on top of the global config"
echo "Extra RUN options: --io idle|be:0-7  --sched other|batch|idle"
echo "  (real-time: rt:N / rr:N / fifo:N need: sudo install -o root -g root -m 644 /dev/null /etc/lutris-game-tune.allow-rt)"
echo
echo "Install mode: ${MODE}"
echo "Preview PRE without changing anything: sudo ${WRAPPER_DEST} DRYRUN [profile]"
echo "Force a restore if game mode got stuck: sudo ${WRAPPER_DEST} RESTORE"
echo "Status check:  sudo ${WRAPPER_DEST} STATUS"
echo "Configuration: ${CONF_DEST}"
echo "Log file:      /var/log/lutris-game-tune.log"
