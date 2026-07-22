#!/bin/bash
# =============================================================================
# lutris-game-tune.sh — Lutris Pre/Post Game System Tuner (v4.1)
#
# Not meant to be called directly. Invoked by lutris-game-tune-wrapper (a
# setuid root binary). See lutris-game-tune-wrapper.c for wrapper setup.
#
# Lutris settings:
#   Pre-game script:  /usr/local/bin/lutris-game-tune-wrapper PRE
#   Post-game script: /usr/local/bin/lutris-game-tune-wrapper POST
#   Status check:     /usr/local/bin/lutris-game-tune-wrapper STATUS
#   Command prefix:   /usr/local/bin/lutris-game-tune-wrapper RUN <nice> -- <command...>
#                      (goes in Lutris's "Command prefix" field; starts the
#                      game with the given nice value. This mode NEVER enters
#                      this script — it's handled entirely inside the wrapper
#                      (C) binary — see the wrapper source.)
#
# v2 changes:
#   - SECURITY: state directory moved from /var/tmp (world-writable, open to
#     symlink attacks) to /run (root-only tmpfs, cleared on boot)
#   - SECURITY: state directory ownership/permission/symlink verification
#   - flock guards against concurrent PRE/POST runs
#   - log file moved outside STATE_DIR (fixes the rmdir warning bug in POST)
#   - new tunables: CPU governor/EPP, split_lock_mitigate, watchdog,
#     PCIe ASPM, HDA power_save, vm.stat_interval, page-cluster,
#     optional deep C-state disabling
#   - whitelist-based configuration via /etc/lutris-game-tune.conf
#   - deterministic sysfs-based PCI enumeration instead of setpci parsing
#   - STATUS command
#
# v3 changes:
#   - THP (enabled/shmem_enabled/defrag) is now configurable; defaults
#     to "madvise" but can be overridden.
#   - CCD/CCX core isolation (formerly a separate "tasks-redirect" project)
#     has been integrated into this script: on PRE, it moves the
#     launcher/game onto CCD0 and the rest of the system onto CCD1; on POST
#     (once the last game exits) it reverts this. Automatically and silently
#     skipped on single-CCX/CCD processors (see the CCD_* config keys).
#   - Added a RUN <nice> [--] <command...> command-prefix mode to the
#     wrapper: written into Lutris's "Command prefix" field to start the
#     game with a lower (higher-priority) nice value. Root privilege is used
#     only to make the nice() call; it is dropped to the real user
#     immediately afterward — the game itself never runs as root.
#   - CCD revert on POST now retries up to 25 times: if both cgroups fully
#     empty out and are removed before the 25th attempt, the script stops
#     early; if they still aren't empty after 25 attempts, it reports an
#     error (see restore_ccd_isolation()).
#
# v4 changes — CCD/CCX isolation re-architected after a production incident:
#   - PROBLEM: the v3 approach swept every process (except a hand-maintained
#     protected-cgroup list) into a temporary "theUgly" cgroup on PRE and
#     moved everything back on POST. A login-session process was found
#     sitting directly in the cgroup-v2 ROOT at scan time (not yet inside
#     any protected path), got swept and correctly moved back to root — but
#     root is not a safe cgroup for a session leader: D-Bus/PolicyKit's
#     session tracking depends on cgroup membership, and moving a session's
#     processes out (even briefly, even back to the exact same place after)
#     silently broke it. Symptom: pkexec-gated tools (e.g. power-manager
#     GUIs) started failing with "Not authorized", and in one occurrence the
#     login manager's own accounting broke badly enough to wedge the TTY and
#     require a reboot.
#   - FIX: existing cgroups are no longer swept at all. Every cgroup already
#     present under CGROUP_V2_ROOT (login sessions, elogind, dbus, other
#     service cgroups, ...) is left exactly where it is and is instead
#     constrained IN PLACE by writing its own cpuset.cpus to the system CCD
#     (constrain_cgroup_cpus() / restore_constrained_cgroups()) — every
#     process inside it, and all of its descendants, is confined without
#     ever changing cgroup membership. Only processes found sitting
#     directly in the root cgroup's own cgroup.procs (genuinely homeless —
#     mostly stray daemons/kernel threads) are still moved into theUgly,
#     with per-PID origin tracking (CCD_PID_ORIGIN / .ccd_pid_origin) so
#     POST returns each one to the exact cgroup it came from, not to root.
#   - CCD_PROTECTED_CGROUPS is now LEGACY (parsed for config-file
#     compatibility, no longer used by the sweep — there is no sweep to
#     guard any more).
#   - Two independent safety nets remain for the one remaining move path
#     (root-level stray processes): CCD_PROTECTED_PROCS (a static name list
#     covering both OpenRC and systemd daemon/session-manager names) and a
#     dynamic lookup (refresh_protected_session_pids()) that reads current
#     login-session leader PIDs straight from the login manager's own
#     records (/run/systemd/sessions/ or /run/elogind/sessions/) on every
#     PRE run — this is what actually closes the gap that caused the
#     incident, since it doesn't require knowing a process's name or
#     cgroup in advance.
#   - Verified to work unchanged on both OpenRC (elogind) and systemd
#     (systemd-logind) — both write session records in the same LEADER=
#     format the dynamic lookup reads.
#
# v4.1 changes (community improvements):
#   - Removed unused function move_all_procs_under.
#   - Stale theGood/theUgly cgroups from failed restores are cleaned
#     before applying CCD isolation.
#   - Tracking/origin files are reset when the first game starts,
#     preventing stale PID contamination.
#   - save_pid_origins now appends only new PIDs, avoiding excessive I/O.
#   - STATUS output hides internal tracking files.
#   - Added numeric range checks for config values.
#   - Configurable PCI latency tuning (SET_PCI_LATENCY).
#   - Configurable log level (LOG_LEVEL) with DEBUG support.
#   - Micro-optimizations (bash built-in redirections, explicit find -P).
#   - CCD_PROTECTED_CGROUPS deprecated warning.
# =============================================================================

set -euo pipefail

# --- Constants -----------------------------------------------------------------
readonly STATE_DIR="/run/lutris-game-tune"
readonly LOCK_FILE="/run/lutris-game-tune.lock"
readonly LOG_FILE="/var/log/lutris-game-tune.log"
readonly LOG_MAX_BYTES=$((1024 * 1024))
readonly CONFIG_FILE="/etc/lutris-game-tune.conf"

# --- Configuration (defaults; overridable via /etc/lutris-game-tune.conf) ----
# CPU frequency governor (amd-pstate active mode also sets EPP to performance)
CPU_GOVERNOR="performance"
SET_CPU_GOVERNOR=1
# PCIe ASPM policy during gameplay (cuts link wake-up latency)
ASPM_POLICY="performance"
SET_ASPM=1
# Disable deep C-states (1 = disable everything except POLL and C1).
# Reduces DPC/ISR latency and frame-time variance, but increases heat/power.
# Off by default on laptops; set to 1 if you want desktop-like behavior.
DISABLE_DEEP_CSTATES=0
# Highest C-state index to keep enabled when disabling deep C-states (0=POLL, 1=C1)
CSTATE_KEEP_MAX=1
# vm.swappiness value while in game mode
VM_SWAPPINESS=10

THP_ENABLED="madvise"
THP_SHMEM_ENABLED="madvise"
THP_DEFRAG="madvise"

# --- CCD/CCX core isolation ---------------------------------------------------
# On processors with more than one CCD/CCX, moves the launcher/game process
# onto the "performance" core group and the rest of the system onto the
# "system" core group. On single-CCX/CCD processors (i.e. topology detection
# finds only one group), this feature is AUTOMATICALLY and SILENTLY skipped —
# no log lines or warnings are produced and nothing on the system is touched.
CCD_ISOLATION_ENABLED=1
# Launcher/game process name, matched with pgrep -x
CCD_LAUNCHER="lutris"
# Extra process names (space-separated) to add to the theGood (performance) group
CCD_EXTRA_GOOD_PROCS=""
# How many seconds to keep scanning for newly spawned child processes
# (shader compilation, DXVK/VKD3D workers) after the launcher is moved.
# 0 = one-shot move only.
CCD_MONITOR_SECONDS=30
# cpuset.cpus.partition type: "root" or "isolated"
CCD_GOOD_PARTITION_TYPE="root"
CCD_UGLY_PARTITION_TYPE="root"

# --- Logging -------------------------------------------------------------------
LOG_LEVEL="INFO"   # DEBUG, INFO, WARN, ERROR

_log_level_num() {
    case "$1" in
        DEBUG) echo 0 ;; INFO) echo 1 ;; WARN) echo 2 ;; ERROR) echo 3 ;;
        *)     echo 1 ;;
    esac
}

_log_write() {
    local level="$1"; shift
    local msg
    msg="[$(date '+%Y-%m-%d %H:%M:%S')] [${level}] $*"

    # Only write if message level >= configured LOG_LEVEL
    local msg_lvl cfg_lvl
    msg_lvl=$(_log_level_num "${level}")
    cfg_lvl=$(_log_level_num "${LOG_LEVEL}")
    if (( msg_lvl < cfg_lvl )); then
        return
    fi

    echo "${msg}"
    # Append to the log file — skip if it's a symlink (log-path attack prevention)
    if [[ ! -L "${LOG_FILE}" ]]; then
        echo "${msg}" >> "${LOG_FILE}" 2>/dev/null || true
    fi
}
log()       { _log_write "INFO " "$*"; }
warn()      { _log_write "WARN " "$*"; }
err()       { _log_write "ERROR" "$*" >&2; }
log_debug() { _log_write "DEBUG" "$*"; }

rotate_log() {
    local size
    size=$(stat -c '%s' "${LOG_FILE}" 2>/dev/null || echo 0)
    if (( size > LOG_MAX_BYTES )); then
        mv -f "${LOG_FILE}" "${LOG_FILE}.old" 2>/dev/null || true
    fi
}

# --- Security helpers ----------------------------------------------------------
require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        err "This script cannot be run directly. Use lutris-game-tune-wrapper."
        exit 1
    fi
}

# Safely create/verify STATE_DIR:
# /run is tmpfs and only root can write to it, but stay defensive anyway:
# must not be a symlink, must be owned by root, must be 0700.
ensure_state_dir() {
    if [[ -L "${STATE_DIR}" ]]; then
        err "SECURITY: ${STATE_DIR} is a symlink — aborting."
        exit 1
    fi
    if [[ ! -d "${STATE_DIR}" ]]; then
        mkdir -m 0700 "${STATE_DIR}"
    fi
    local owner mode
    owner=$(stat -c '%u' "${STATE_DIR}")
    mode=$(stat -c '%a' "${STATE_DIR}")
    if [[ "${owner}" != "0" ]]; then
        err "SECURITY: ${STATE_DIR} is not owned by root (uid=${owner}) — aborting."
        exit 1
    fi
    if [[ "${mode}" != "700" ]]; then
        chmod 0700 "${STATE_DIR}"
    fi
}

# Read the config file SAFELY: no source (would allow code execution in the
# setuid chain) — only whitelisted KEY=VALUE lines are accepted.
load_config() {
    [[ -f "${CONFIG_FILE}" ]] || return 0
    if [[ -L "${CONFIG_FILE}" ]]; then
        warn "Config is a symlink, ignored: ${CONFIG_FILE}"
        return 0
    fi
    local owner mode
    owner=$(stat -c '%u' "${CONFIG_FILE}")
    mode=$(( 8#$(stat -c '%a' "${CONFIG_FILE}") ))
    if [[ "${owner}" != "0" ]]; then
        warn "Config is not owned by root, ignored: ${CONFIG_FILE}"
        return 0
    fi
    # reject if group (020) or other (002) write bit is set
    if (( mode & 8#022 )); then
        warn "Config is group/other writable, ignored: ${CONFIG_FILE}"
        return 0
    fi

    local line key val
    while IFS= read -r line; do
        # skip comments and blank lines
        [[ "${line}" =~ ^[[:space:]]*(#|$) ]] && continue
        # CCD_EXTRA_GOOD_PROCS may contain spaces (multiple process names);
        # every other key is a single alphanumeric token.
        if [[ "${line}" =~ ^CCD_EXTRA_GOOD_PROCS=(.*)$ ]]; then
            key="CCD_EXTRA_GOOD_PROCS"
            val="${BASH_REMATCH[1]}"
        elif [[ "${line}" =~ ^([A-Z_]+)=([[:alnum:]_.-]+)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
        elif [[ "${line}" =~ ^CCD_PROTECTED_CGROUPS=(.*)$ ]]; then
            key="CCD_PROTECTED_CGROUPS"
            val="${BASH_REMATCH[1]}"
            log_debug "CCD_PROTECTED_CGROUPS is deprecated and ignored"
            continue   # legacy key – silently consumed
        elif [[ "${line}" =~ ^CCD_PROTECTED_PROCS=(.*)$ ]]; then
            key="CCD_PROTECTED_PROCS"
            val="${BASH_REMATCH[1]}"
        else
            warn "Invalid config line, skipped: ${line}"
            continue
        fi
        case "${key}" in
            CPU_GOVERNOR)          CPU_GOVERNOR="${val}" ;;
            SET_CPU_GOVERNOR)      SET_CPU_GOVERNOR="${val}" ;;
            ASPM_POLICY)           ASPM_POLICY="${val}" ;;
            SET_ASPM)              SET_ASPM="${val}" ;;
            DISABLE_DEEP_CSTATES)  DISABLE_DEEP_CSTATES="${val}" ;;
            CSTATE_KEEP_MAX)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 0 )); then
                    CSTATE_KEEP_MAX="${val}"
                else
                    warn "Invalid CSTATE_KEEP_MAX '${val}' (must be >=0), using default ${CSTATE_KEEP_MAX}"
                fi ;;
            VM_SWAPPINESS)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 0 && val <= 100 )); then
                    VM_SWAPPINESS="${val}"
                else
                    warn "Invalid VM_SWAPPINESS '${val}' (0-100), using default ${VM_SWAPPINESS}"
                fi ;;
            CCD_ISOLATION_ENABLED) CCD_ISOLATION_ENABLED="${val}" ;;
            CCD_LAUNCHER)          CCD_LAUNCHER="${val}" ;;
            CCD_EXTRA_GOOD_PROCS)  CCD_EXTRA_GOOD_PROCS="${val}" ;;
            CCD_MONITOR_SECONDS)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 0 )); then
                    CCD_MONITOR_SECONDS="${val}"
                else
                    warn "Invalid CCD_MONITOR_SECONDS '${val}' (must be >=0), using default ${CCD_MONITOR_SECONDS}"
                fi ;;
            CCD_PROTECTED_PROCS)      CCD_PROTECTED_PROCS="${val}" ;;
            CCD_GOOD_PARTITION_TYPE) CCD_GOOD_PARTITION_TYPE="${val}" ;;
            CCD_UGLY_PARTITION_TYPE) CCD_UGLY_PARTITION_TYPE="${val}" ;;
            THP_ENABLED)           THP_ENABLED="${val}" ;;
            THP_SHMEM_ENABLED)     THP_SHMEM_ENABLED="${val}" ;;
            THP_DEFRAG)            THP_DEFRAG="${val}" ;;
            SET_PCI_LATENCY)       SET_PCI_LATENCY="${val}" ;;
            LOG_LEVEL)
                case "${val^^}" in
                    DEBUG|INFO|WARN|ERROR) LOG_LEVEL="${val^^}" ;;
                    *) warn "Invalid LOG_LEVEL '${val}', using default ${LOG_LEVEL}" ;;
                esac ;;
            *) warn "Unknown config key, skipped: ${key}" ;;
        esac
    done < "${CONFIG_FILE}"
    log "Config loaded: ${CONFIG_FILE}"
}

# Check debugfs mount, mount it if needed
ensure_debugfs() {
    if ! mountpoint -q /sys/kernel/debug 2>/dev/null; then
        warn "debugfs not mounted, mounting..."
        if ! mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null; then
            warn "debugfs mount failed — debug/sched parameters will be skipped"
            return 1
        fi
    fi
    return 0
}

# Path -> state file name (unique, collision-free)
_save_name() {
    echo "${STATE_DIR}/$(echo "$1" | tr '/' '_')"
}

# Write a state file SAFELY: reject symlinks
_state_write() {
    local file="$1" content="$2"
    if [[ -L "${file}" ]]; then
        err "SECURITY: state file is a symlink, not written: ${file}"
        return 1
    fi
    printf '%s' "${content}" > "${file}"
}

# --- Parameter helpers -----------------------------------------------------
# Read current value → save it → write new value
# tune_param <path> <new_value> <description>
tune_param() {
    local path="$1" new_val="$2" desc="$3"
    local save_file; save_file="$(_save_name "${path}")"

    if [[ ! -e "${path}" ]]; then
        warn "Does not exist, skipped: ${desc}"
        return 0
    fi

    if [[ ! -f "${save_file}" ]]; then
        local current_val
        if ! current_val="$(cat "${path}" 2>/dev/null)"; then
            warn "Read failed, skipped: ${desc}"
            return 0
        fi
        if [[ -z "${current_val}" ]]; then
            warn "Read empty value, not saving (will retry next run): ${desc}"
            return 0
        fi
        _state_write "${save_file}" "${current_val}" || return 0
        log_debug "Saved       [${desc}]: '${current_val}'"
    else
        log_debug "Already saved [${desc}], not overwriting"
    fi

    if ! printf '%s' "${new_val}" > "${path}" 2>/dev/null; then
        warn "Write failed: ${desc} = ${new_val}"
        return 0
    fi
    log "Set         [${desc}]: ${new_val}"
}

# For "a [b] c"-style choice files (THP, ASPM, io scheduler): extracts the
# current selection from the brackets and saves it.
# tune_choice_param <path> <new_value> <description>
tune_choice_param() {
    local path="$1" new_val="$2" desc="$3"
    local save_file; save_file="$(_save_name "${path}")"

    if [[ ! -e "${path}" ]]; then
        warn "Does not exist, skipped: ${desc}"
        return 0
    fi

    if [[ ! -f "${save_file}" ]]; then
        local raw current_val
        raw="$(cat "${path}" 2>/dev/null)" || { warn "Read failed: ${desc}"; return 0; }
        if [[ "${raw}" =~ \[([^]]+)\] ]]; then
            current_val="${BASH_REMATCH[1]}"
        else
            current_val="${raw}"
        fi
        if [[ -z "${current_val}" ]]; then
            warn "Read empty value, not saving (will retry next run): ${desc}"
            return 0
        fi
        _state_write "${save_file}" "${current_val}" || return 0
        log_debug "Saved       [${desc}]: '${current_val}'"
    else
        log_debug "Already saved [${desc}], not overwriting"
    fi

    if ! printf '%s' "${new_val}" > "${path}" 2>/dev/null; then
        warn "Write failed: ${desc} = ${new_val}"
        return 0
    fi
    log "Set         [${desc}]: ${new_val}"
}

# Restore the saved value → remove the save file
# restore_param <path> <description>
restore_param() {
    local path="$1" desc="$2"
    local save_file; save_file="$(_save_name "${path}")"

    if [[ ! -f "${save_file}" ]]; then
        return 0   # nothing saved, skip quietly (already skipped in PRE)
    fi
    if [[ -L "${save_file}" ]]; then
        err "SECURITY: state file is a symlink, not read: ${save_file}"
        rm -f "${save_file}"
        return 0
    fi
    if [[ ! -e "${path}" ]]; then
        warn "Target no longer exists, skipped: ${desc}"
        rm -f "${save_file}"
        return 0
    fi

    local saved_val
    saved_val="$(cat "${save_file}")"

    if ! printf '%s' "${saved_val}" > "${path}" 2>/dev/null; then
        warn "Restore failed: ${desc} = '${saved_val}'"
    else
        log "Restored    [${desc}]: '${saved_val}'"
    fi
    rm -f "${save_file}"
}

# --- PCI latency timer (deterministic, via sysfs enumeration) ------------------
# Note: on PCIe devices this register is mostly read-only/ineffective; it's
# meaningful for classic PCI bridges. Kept since it's harmless.
PCI_SAVE_FILE=""   # one file with "bdf value" lines

SET_PCI_LATENCY=1

tune_pci_latency() {
    [[ "${SET_PCI_LATENCY}" == "1" ]] || return 0
    PCI_SAVE_FILE="${STATE_DIR}/pci_latency"
    if ! command -v setpci &>/dev/null; then
        warn "setpci not found — PCI latency tuning skipped"
        return 0
    fi

    local dev bdf class cur target
    if [[ ! -f "${PCI_SAVE_FILE}" ]]; then
        : > "${PCI_SAVE_FILE}"
        for dev in /sys/bus/pci/devices/*; do
            bdf="$(basename "${dev}")"
            cur="$(setpci -s "${bdf}" latency_timer 2>/dev/null)" || continue
            echo "${bdf} ${cur}" >> "${PCI_SAVE_FILE}"
        done
        log "PCI latency values saved ($(wc -l < "${PCI_SAVE_FILE}") devices)"
    else
        log_debug "PCI latency already saved, not overwriting"
    fi

    for dev in /sys/bus/pci/devices/*; do
        bdf="$(basename "${dev}")"
        class="$(cat "${dev}/class" 2>/dev/null)" || continue
        case "${class}" in
            0x0600*)  target="00" ;;  # host bridge
            0x0604*)  target="80" ;;  # PCI-PCI bridge
            *)        target="20" ;;  # everything else
        esac
        setpci -s "${bdf}" latency_timer="${target}" 2>/dev/null || true
    done
    log "PCI latency timers set (bridge=80, host=00, other=20)"
}

restore_pci_latency() {
    [[ "${SET_PCI_LATENCY}" == "1" ]] || return 0
    PCI_SAVE_FILE="${STATE_DIR}/pci_latency"
    [[ -f "${PCI_SAVE_FILE}" ]] || return 0
    if ! command -v setpci &>/dev/null; then
        rm -f "${PCI_SAVE_FILE}"
        return 0
    fi
    local bdf val
    while read -r bdf val; do
        [[ -z "${bdf}" || -z "${val}" ]] && continue
        setpci -s "${bdf}" latency_timer="${val}" 2>/dev/null || \
            warn "PCI latency restore failed: ${bdf}=${val}"
    done < "${PCI_SAVE_FILE}"
    rm -f "${PCI_SAVE_FILE}"
    log "PCI latency timers restored"
}

# --- CPU governor / EPP -------------------------------------------------------
tune_cpu_governor() {
    [[ "${SET_CPU_GOVERNOR}" == "1" ]] || return 0
    local pol
    for pol in /sys/devices/system/cpu/cpufreq/policy*; do
        [[ -d "${pol}" ]] || continue
        # Save EPP BEFORE the governor: writing governor=performance
        # automatically pulls EPP to performance on amd-pstate
        if [[ -f "${pol}/energy_performance_preference" ]]; then
            tune_param "${pol}/energy_performance_preference" "performance" \
                "epp.$(basename "${pol}")"
        fi
        tune_param "${pol}/scaling_governor" "${CPU_GOVERNOR}" \
            "governor.$(basename "${pol}")"
    done
}

restore_cpu_governor() {
    local pol
    for pol in /sys/devices/system/cpu/cpufreq/policy*; do
        [[ -d "${pol}" ]] || continue
        # Governor first, then EPP (writing the governor overwrites EPP)
        restore_param "${pol}/scaling_governor" "governor.$(basename "${pol}")"
        if [[ -f "${pol}/energy_performance_preference" ]]; then
            restore_param "${pol}/energy_performance_preference" "epp.$(basename "${pol}")"
        fi
    done
}

# --- Deep C-state control (optional) ----------------------------------------
tune_cstates() {
    [[ "${DISABLE_DEEP_CSTATES}" == "1" ]] || return 0
    log "--- C-states (disabling state>${CSTATE_KEEP_MAX}) ---"
    local st idx
    for st in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do
        [[ -f "${st}" ]] || continue
        idx="${st%/disable}"; idx="${idx##*state}"
        (( idx > CSTATE_KEEP_MAX )) || continue
        local cpu_name; [[ "${st}" =~ /(cpu[0-9]+)/cpuidle/ ]] && cpu_name="${BASH_REMATCH[1]}" || cpu_name="cpu?"
        tune_param "${st}" "1" "cstate.${cpu_name}.state${idx}"
    done
}

restore_cstates() {
    local st idx
    for st in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do
        [[ -f "${st}" ]] || continue
        idx="${st%/disable}"; idx="${idx##*state}"
        local cpu_name; [[ "${st}" =~ /(cpu[0-9]+)/cpuidle/ ]] && cpu_name="${BASH_REMATCH[1]}" || cpu_name="cpu?"
        restore_param "${st}" "cstate.${cpu_name}.state${idx}"
    done
}

# --- CCD/CCX core isolation ----------------------------------------------------
readonly CGROUP_V2_ROOT="/sys/fs/cgroup"
readonly CCD_GOOD_GROUP="theGood"
readonly CCD_UGLY_GROUP="theUgly"
CCD_AVAILABLE=0   # set by detect_ccx_groups()

# N-CCX-aware topology detection. Returns 1 if only a single CCX/CCD is found
# (not an error) — the caller uses this as a "skip silently" signal.
detect_ccx_groups() {
    local cache_dir="/sys/devices/system/cpu/cpu0/cache/index3"
    [[ -d "${cache_dir}" ]] || return 1

    mapfile -t CCX_GROUPS < <(cat /sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list 2>/dev/null | sort -u)
    (( ${#CCX_GROUPS[@]} >= 2 )) || return 1

    if (( ${#CCX_GROUPS[@]} > 2 )); then
        warn "${#CCX_GROUPS[@]} CCX groups found, only the first two (CCX0/CCX1) will be used."
    fi
    return 0
}

detect_mem_node_for_cpus() {
    local cpu_list="$1" node_dir node_cpus first_cpu
    first_cpu="${cpu_list%%[,-]*}"
    for node_dir in /sys/devices/system/node/node*; do
        [[ -f "${node_dir}/cpulist" ]] || continue
        node_cpus=$(cat "${node_dir}/cpulist")
        if [[ ",${node_cpus}," == *",${first_cpu},"* ]]; then
            basename "${node_dir}" | tr -d 'node'
            return 0
        fi
    done
    echo "0"
}

setup_cpuset_group() {
    local name="$1" cpus="$2" partition_type="$3"
    local dir="${CGROUP_V2_ROOT}/${name}"
    local mem_node

    if [[ ! -d "${dir}" ]]; then
        log "Creating: ${name}"
        mkdir "${dir}" || { warn "Failed to create ${dir}."; return 1; }
    fi

    mem_node="$(detect_mem_node_for_cpus "${cpus}")"

    log "${name} -> cpuset.cpus=${cpus}, mems=${mem_node}, partition=${partition_type}"
    echo "${cpus}" > "${dir}/cpuset.cpus" 2>/dev/null || warn "Failed to write cpuset.cpus: ${name}"
    echo "${mem_node}" > "${dir}/cpuset.mems" 2>/dev/null || warn "Failed to write cpuset.mems: ${name}"
    echo "${partition_type}" > "${dir}/cpuset.cpus.partition" 2>/dev/null || warn "Failed to write cpuset.cpus.partition: ${name}"

    if [[ -r "${dir}/cpuset.cpus.effective" ]]; then
        log "${name} effective cpus: $(cat "${dir}/cpuset.cpus.effective")"
    fi
}

get_process_tree() {
    local root_pid="$1"
    local -a queue=("${root_pid}") result=()
    local -A seen=()
    local pid child

    while (( ${#queue[@]} > 0 )); do
        pid="${queue[0]}"
        queue=("${queue[@]:1}")
        [[ -n "${seen[$pid]:-}" ]] && continue
        seen[$pid]=1
        result+=("${pid}")
        while read -r child; do
            [[ -n "${child}" ]] && queue+=("${child}")
        done < <(pgrep -P "${pid}" 2>/dev/null)
    done
    printf '%s\n' "${result[@]}"
}

collect_targets() {
    local pattern="$1"
    local -A all_pids=()
    local root_pid pid

    while read -r root_pid; do
        [[ -z "${root_pid}" ]] && continue
        while read -r pid; do
            [[ -n "${pid}" ]] && all_pids[$pid]=1
        done < <(get_process_tree "${root_pid}")
    done < <(pgrep -x "${pattern}" 2>/dev/null)

    printf '%s\n' "${!all_pids[@]}"
}

# Recursively collect every PID from ALL cgroup.procs files under a root
# directory (the root cgroup's own cgroup.procs, plus every descendant
# cgroup's cgroup.procs) — NOT just the top-level file. On systems where
# most processes live in sub-cgroups (systemd slices, elogind, OpenRC's
# per-service cgroups, user session cgroups, etc.) reading only the
# top-level cgroup.procs sees almost nothing: the vast majority of
# real userspace processes are invisible to it, so they never get moved.
# skip_dirs: cgroup directories (absolute paths) to exclude entirely
# (used to skip theGood/theUgly themselves when scanning from the root).
collect_all_pids_under() {
    local root_dir="$1"; shift
    local -a skip_dirs=("$@")
    local -A pids=()
    local dir skip match pid

    while read -r -d '' dir; do
        match=0
        for skip in "${skip_dirs[@]}"; do
            [[ "${dir}" == "${skip}" || "${dir}" == "${skip}"/* ]] && { match=1; break; }
        done
        (( match )) && continue
        [[ -r "${dir}/cgroup.procs" ]] || continue
        while read -r pid; do
            [[ -n "${pid}" ]] && pids[$pid]=1
        done < "${dir}/cgroup.procs" 2>/dev/null
    done < <(find -P "${root_dir}" -type d -print0 2>/dev/null)

    printf '%s\n' "${!pids[@]}"
}

# Map of pid -> absolute source cgroup dir, populated by
# collect_all_pids_under_map() and consulted by restore_ccd_isolation() so
# each process goes back to the cgroup it actually came from instead of
# being dumped into the root cgroup.
declare -gA CCD_PID_ORIGIN=()

# Set of PIDs to protect this run, populated by
# refresh_protected_session_pids() from the login manager's OWN session
# records (whichever of elogind's or systemd-logind's session directories
# exists) rather than from a static process-name list. This exists because
# a static name list (CCD_PROTECTED_PROCS) cannot know in advance what the
# session leader's binary is called on every setup (openrc-user here, but
# it could be a display manager, xinit, a different init's session helper,
# etc.), and — critically — a session leader is not guaranteed to already
# be sitting inside a "protected" cgroup path (e.g. openrc.user.cian) at
# the moment PRE scans the system: it may still be directly in the root
# cgroup, in which case CCD_PROTECTED_CGROUPS never gets a chance to skip
# it. Moving a session leader even briefly (to theUgly and straight back
# to the exact same place) can make the login manager's cgroup-empty/
# population watch fire, tearing the session into "closing" state — which
# is exactly what broke polkit authentication ("Not authorized") for the
# power-manager GUI in a prior incident. Protecting the leader PID by
# identity, sourced fresh from the login manager's own bookkeeping, closes
# that gap regardless of the leader's cgroup or process name.
declare -gA CCD_PROTECTED_SESSION_PIDS=()

refresh_protected_session_pids() {
    CCD_PROTECTED_SESSION_PIDS=()
    local session_dir leader_pid f line
    for session_dir in /run/systemd/sessions /run/elogind/sessions; do
        [[ -d "${session_dir}" ]] || continue
        for f in "${session_dir}"/*; do
            [[ -f "${f}" ]] || continue
            while IFS= read -r line; do
                if [[ "${line}" =~ ^LEADER=([0-9]+)$ ]]; then
                    CCD_PROTECTED_SESSION_PIDS[${BASH_REMATCH[1]}]=1
                fi
            done < "${f}" 2>/dev/null
        done
    done
}

# Same traversal as collect_all_pids_under(), but also records, for every
# PID found, the absolute path of the cgroup directory it was read from
# (into CCD_PID_ORIGIN). This is what lets restore_ccd_isolation() send
# each process back to its own original cgroup rather than to root.
# Returns 0 (protected) if the given pid's process name (comm) matches one
# of the space-separated names in CCD_PROTECTED_PROCS, OR if the pid is a
# currently-recorded login-session leader (see refresh_protected_session_pids).
# This is a SECOND, INDEPENDENT safety layer on top of cgroup-path
# protection: it protects a process by identity even if it is transiently
# outside its expected protected cgroup (e.g. mid-restart, or read during
# a race window, or — for session leaders — simply because it was never
# inside a protected cgroup to begin with).
is_pid_protected_by_name() {
    local pid="$1"
    [[ -n "${CCD_PROTECTED_SESSION_PIDS[$pid]:-}" ]] && return 0
    [[ -z "${CCD_PROTECTED_PROCS:-}" ]] && return 1
    local comm
    comm="$(cat "/proc/${pid}/comm" 2>/dev/null)" || return 1
    local name
    for name in ${CCD_PROTECTED_PROCS}; do
        [[ "${comm}" == "${name}" ]] && return 0
    done
    return 1
}

collect_all_pids_under_map() {
    local root_dir="$1"; shift
    local -a skip_dirs=("$@")
    local -A pids=()
    local dir skip match pid

    while read -r -d '' dir; do
        match=0
        for skip in "${skip_dirs[@]}"; do
            [[ "${dir}" == "${skip}" || "${dir}" == "${skip}"/* ]] && { match=1; break; }
        done
        (( match )) && continue
        [[ -r "${dir}/cgroup.procs" ]] || continue
        while read -r pid; do
            if [[ -n "${pid}" ]]; then
                if is_pid_protected_by_name "${pid}"; then
                    continue
                fi
                pids[$pid]=1
                CCD_PID_ORIGIN[$pid]="${dir}"
            fi
        done < "${dir}/cgroup.procs" 2>/dev/null
    done < <(find -P "${root_dir}" -type d -print0 2>/dev/null)

    printf '%s\n' "${!pids[@]}"
}

# Append NEW pids (and their origins) to the .ccd_pid_origin file.
# Reads the existing file to avoid duplicate entries.
append_pid_origins() {
    local origin_file="${STATE_DIR}/.ccd_pid_origin"
    local -A existing=()
    local pid origin

    # Load existing entries
    if [[ -f "${origin_file}" ]]; then
        while IFS=$'\t' read -r pid origin; do
            existing["${pid}"]=1
        done < "${origin_file}"
    fi

    # Append only new PIDs
    for pid in "${!CCD_PID_ORIGIN[@]}"; do
        if [[ -z "${existing[$pid]:-}" ]]; then
            printf '%s\t%s\n' "${pid}" "${CCD_PID_ORIGIN[$pid]}" >> "${origin_file}"
        fi
    done
}

# Look up the recorded origin cgroup dir for a pid; echoes it, or nothing
# if unknown (caller falls back to root).
lookup_pid_origin() {
    local pid="$1"
    local origin_file="${STATE_DIR}/.ccd_pid_origin"
    [[ -r "${origin_file}" ]] || return 0
    awk -F'\t' -v p="${pid}" '$1 == p { print $2; exit }' "${origin_file}"
}

# Move every process found ANYWHERE under root_dir (recursively, across all
# sub-cgroups) into dst_procs. This is the general-purpose mover: it does
# not assume processes sit directly in root_dir's own cgroup.procs.
# Also records each moved PID's source cgroup (via collect_all_pids_under_map)
# and persists it to STATE_DIR so POST can send it back home.
# NOTE: Not used in v4.1 architecture; kept for potential future use.
move_all_procs_under() {
    local root_dir="$1" dst_procs="$2"; shift 2
    local -a skip_dirs=("$@")
    local success=0 fail=0 pid
    local -a pids=()
    mapfile -t pids < <(collect_all_pids_under_map "${root_dir}" "${skip_dirs[@]}")
    for pid in "${pids[@]}"; do
        [[ -z "${pid}" ]] && continue
        [[ -d "/proc/${pid}" ]] || continue
        if echo "${pid}" > "${dst_procs}" 2>/dev/null; then
            success=$((success + 1))
        else
            fail=$((fail + 1))
        fi
    done
    append_pid_origins
    echo "${success} ${fail}"
}

move_pid_list_to_group() {
    local group_procs="$1"; shift
    local success=0 fail=0 pid
    for pid in "$@"; do
        [[ -z "${pid}" ]] && continue
        [[ -d "/proc/${pid}" ]] || continue
        if is_pid_protected_by_name "${pid}"; then
            continue
        fi
        if echo "${pid}" > "${group_procs}" 2>/dev/null; then
            success=$((success + 1))
        else
            fail=$((fail + 1))
        fi
    done
    echo "${success} ${fail}"
}

move_launcher_tree_once() {
    local good_procs="$1"
    local -A all_pids=()
    local pid p
    local -a patterns=()
    read -r -a patterns <<< "${CCD_LAUNCHER} ${CCD_EXTRA_GOOD_PROCS}"

    # Step 1: Reload previously tracked live processes (reparenting protection)
    local tracked_file="${STATE_DIR}/.tracked_game_pids"
    if [[ -f "${tracked_file}" ]]; then
        while read -r pid; do
            if [[ -n "${pid}" && -d "/proc/${pid}" ]]; then
                all_pids[$pid]=1
            fi
        done < "${tracked_file}"
    fi

    # Step 2: Find current root PIDs of the launcher and extra processes
    for p in "${patterns[@]}"; do
        [[ -z "${p}" ]] && continue
        while read -r root_pid; do
            [[ -n "${root_pid}" ]] && all_pids[$root_pid]=1
        done < <(pgrep -x "${p}" 2>/dev/null)
    done

    if (( ${#all_pids[@]} == 0 )); then
        echo "0 0 0"
        return
    fi

    # Step 3: Dynamic full-depth process tree scan
    # Recursively includes all children of currently known PIDs
    local -a queue=("${!all_pids[@]}")
    local child
    while (( ${#queue[@]} > 0 )); do
        pid="${queue[0]}"
        queue=("${queue[@]:1}")
        while read -r child; do
            if [[ -n "${child}" && -z "${all_pids[$child]:-}" ]]; then
                all_pids[$child]=1
                queue+=("${child}")   # newly discovered child enters the scan loop
            fi
        done < <(pgrep -P "${pid}" 2>/dev/null)
    done

    # Step 4: Persist the live process list for the next monitoring iteration
    printf '%s\n' "${!all_pids[@]}" > "${tracked_file}"

    # Step 5: Safely move all discovered processes to the performance cgroup
    local s f
    read -r s f <<< "$(move_pid_list_to_group "${good_procs}" "${!all_pids[@]}")"
    echo "${s} ${f} ${#all_pids[@]}"
}

# --- CCD isolation save/restore helpers ----------------------------------------
readonly CPUSET_SAVE_FILE_NAME=".ccd_cpuset_saved"

# Constrain an existing cgroup to the given CPU list IN PLACE by writing its
# cpuset.cpus — no process is moved anywhere. The original cpuset.cpus value
# is saved to STATE_DIR so restore can undo it. This is the key architectural
# change after two incidents: moving a process OUT of its cgroup (even into
# theUgly and back to the exact same place) changes what elogind/logind sees
# as that process's session cgroup, which silently breaks the process's
# polkit session identity ("Not authorized") and can wedge the session into
# "closing". Writing cpuset.cpus on the cgroup constrains every process in
# it (and in all descendants — cpuset is hierarchical) to the system CCD
# without any process ever changing cgroups, so session identity is never
# disturbed.
constrain_cgroup_cpus() {
    local dir="$1" cpus="$2"
    local name saved
    name="$(basename "${dir}")"
    [[ -w "${dir}/cpuset.cpus" ]] || return 1
    saved="$(cat "${dir}/cpuset.cpus" 2>/dev/null)"
    # Record original (possibly empty) value: name<TAB>value
    printf '%s\t%s\n' "${name}" "${saved}" >> "${STATE_DIR}/${CPUSET_SAVE_FILE_NAME}"
    if echo "${cpus}" > "${dir}/cpuset.cpus" 2>/dev/null; then
        return 0
    fi
    return 1
}

restore_constrained_cgroups() {
    local save_file="${STATE_DIR}/${CPUSET_SAVE_FILE_NAME}"
    [[ -f "${save_file}" ]] || return 0
    local name saved dir restored=0 failed=0
    while IFS=$'\t' read -r name saved; do
        [[ -z "${name}" ]] && continue
        dir="${CGROUP_V2_ROOT}/${name}"
        [[ -d "${dir}" && -w "${dir}/cpuset.cpus" ]] || { failed=$((failed+1)); continue; }
        # Empty saved value means "no restriction" — clear the file.
        if echo "${saved}" > "${dir}/cpuset.cpus" 2>/dev/null; then
            restored=$((restored+1))
        else
            failed=$((failed+1))
        fi
    done < "${save_file}"
    log "  cpuset restore: ${restored} cgroup(s) restored, ${failed} failed/vanished."
    rm -f "${save_file}"
}

# Move only the processes sitting DIRECTLY in the root cgroup's own
# cgroup.procs into dst_procs (with origin tracking + protection checks).
# Processes inside existing sub-cgroups are NOT touched — those cgroups are
# constrained in place by constrain_cgroup_cpus() instead.
move_root_level_procs() {
    local dst_procs="$1"
    local success=0 fail=0 pid
    local -a pids=()
    mapfile -t pids < "${CGROUP_V2_ROOT}/cgroup.procs" 2>/dev/null
    for pid in "${pids[@]}"; do
        [[ -z "${pid}" ]] && continue
        [[ -d "/proc/${pid}" ]] || continue
        if is_pid_protected_by_name "${pid}"; then
            continue
        fi
        CCD_PID_ORIGIN[$pid]="${CGROUP_V2_ROOT}"
        if echo "${pid}" > "${dst_procs}" 2>/dev/null; then
            success=$((success + 1))
        else
            fail=$((fail + 1))
        fi
    done
    # Append only new PIDs to the persistent origin file
    append_pid_origins
    echo "${success} ${fail}"
}

# Stale cgroup cleanup: if theGood/theUgly exist from a previous failed
# restore, move their processes back to root and remove the directories.
cleanup_stale_ccd_cgroups() {
    local dir pid
    for grp in "${CCD_GOOD_GROUP}" "${CCD_UGLY_GROUP}"; do
        dir="${CGROUP_V2_ROOT}/${grp}"
        [[ -d "${dir}" ]] || continue
        log "Cleaning up stale cgroup: ${grp}"
        # Move all processes out
        while IFS= read -r pid; do
            [[ -n "${pid}" ]] || continue
            echo "${pid}" > "${CGROUP_V2_ROOT}/cgroup.procs" 2>/dev/null || true
        done < <(collect_all_pids_under "${dir}")
        # Remove directory tree (depth-first)
        find -P "${dir}" -depth -type d -exec rmdir {} + 2>/dev/null || true
        if [[ -d "${dir}" ]]; then
            warn "Could not remove stale cgroup ${grp} — continuing anyway"
        fi
    done
}

apply_ccd_isolation() {
    [[ "${CCD_ISOLATION_ENABLED}" == "1" ]] || return 0
    detect_ccx_groups || return 0   # single CCX/CCD -> silent exit
    CCD_AVAILABLE=1

    if ! mount | grep -q "on ${CGROUP_V2_ROOT} type cgroup2"; then
        warn "cgroup v2 is not mounted on ${CGROUP_V2_ROOT} — CCD isolation skipped."
        return 0
    fi

    log "--- CCD/CCX core isolation ---"
    refresh_protected_session_pids

    # Clean up leftover cgroups from a previous failed restore
    cleanup_stale_ccd_cgroups

    local ccx0="${CCX_GROUPS[0]}" ccx1="${CCX_GROUPS[1]}"
    log "  CCX0 (CCD0 - performance): ${ccx0}"
    log "  CCX1 (CCD1 - system):      ${ccx1}"

    if ! grep -qw "cpuset" "${CGROUP_V2_ROOT}/cgroup.controllers"; then
        warn "Kernel does not support the cpuset controller — CCD isolation skipped."
        return 0
    fi
    if ! grep -qw "cpuset" "${CGROUP_V2_ROOT}/cgroup.subtree_control"; then
        echo "+cpuset" > "${CGROUP_V2_ROOT}/cgroup.subtree_control" 2>/dev/null || \
            warn "Failed to enable the cpuset subtree_control."
    fi

    # Create theUgly first; bail out if it fails
    setup_cpuset_group "${CCD_UGLY_GROUP}" "${ccx1}" "${CCD_UGLY_PARTITION_TYPE}" || return 0
    local ugly_dir="${CGROUP_V2_ROOT}/${CCD_UGLY_GROUP}"

    # Constrain existing top-level cgroups IN PLACE
    rm -f "${STATE_DIR}/${CPUSET_SAVE_FILE_NAME}"
    local dir cname constrained=0 cfailed=0
    for dir in "${CGROUP_V2_ROOT}"/*/; do
        dir="${dir%/}"
        cname="$(basename "${dir}")"
        [[ "${cname}" == "${CCD_GOOD_GROUP}" || "${cname}" == "${CCD_UGLY_GROUP}" ]] && continue
        if constrain_cgroup_cpus "${dir}" "${ccx1}"; then
            constrained=$((constrained+1))
        else
            cfailed=$((cfailed+1))
        fi
    done
    log "  ${constrained} existing cgroup(s) constrained in place to CPUs ${ccx1} (${cfailed} skipped/failed)."
    if (( cfailed > 0 && constrained == 0 )); then
        warn "  No existing cgroups could be constrained — cpuset may not be delegated to them (common on systemd if 'Delegate=cpuset' / subtree_control isn't set for system.slice/user.slice). CCD isolation will only affect the launcher/game tree and stray root-level processes; system-side isolation is reduced but nothing is unsafe."
    fi

    # Move genuine root-level strays into theUgly
    local us uf
    read -r us uf <<< "$(move_root_level_procs "${ugly_dir}/cgroup.procs")"
    log "  root-level: ${us} process(es) moved to ${CCD_UGLY_GROUP}, ${uf} failed (kernel threads are expected)."

    # Create theGood group
    setup_cpuset_group "${CCD_GOOD_GROUP}" "${ccx0}" "${CCD_GOOD_PARTITION_TYPE}" || {
        # If theGood creation fails, remove theUgly and abort
        warn "Failed to create theGood cgroup — cleaning up theUgly"
        cleanup_stale_ccd_cgroups
        return 0
    }
    local good_procs="${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}/cgroup.procs"

    local success fail total
    read -r success fail total <<< "$(move_launcher_tree_once "${good_procs}")"
    if (( total == 0 )); then
        log "  No running process found for '${CCD_LAUNCHER}' (it may not have started yet)."
    else
        log "  Process tree: ${total} processes found -> ${success} moved, ${fail} failed."
    fi

    if (( CCD_MONITOR_SECONDS > 0 )); then
        log "  Watching for newly spawned child processes for ${CCD_MONITOR_SECONDS} seconds..."
        local end_time s f t rs rf
        end_time=$(( $(date +%s) + CCD_MONITOR_SECONDS ))
        while (( $(date +%s) < end_time )); do
            sleep 2
            read -r s f t <<< "$(move_launcher_tree_once "${good_procs}")"
            (( t > 0 )) && log "  [watch] ${t} processes scanned (${s} newly/re-moved)."
            # Also re-sweep the root cgroup: wine/proton workers that are
            # reparented or setsid'd away from the launcher's pgrep -P chain
            # never show up in move_launcher_tree_once's targets, and would
            # otherwise sit unconstrained in root for the rest of the
            # session (letting the scheduler park them wherever it likes,
            # usually next to the hot CCD0 workload) until the one-shot
            # root-level move_root_level_procs() call at the top of this
            # function is repeated here every 2s instead of just once.
            read -r rs rf <<< "$(move_root_level_procs "${ugly_dir}/cgroup.procs")"
            (( rs > 0 )) && log "  [watch] ${rs} new stray root-level process(es) moved to ${CCD_UGLY_GROUP}."
        done
        log "  Monitoring finished."
    fi
}

# Called from POST: moves all processes from theGood/theUgly back to the
# root cgroup and removes both cgroups. Retries up to 25 times.
# - If both groups are fully emptied and successfully removed before the
#   25th attempt, the function stops early and returns success (0).
# - If, after 25 attempts, at least one group still isn't empty/removed,
#   the function logs an error and returns failure (1).
# If neither cgroup exists to begin with (single CCX/CCD, or isolation was
# never applied / already reverted), it returns silently.
restore_ccd_isolation() {
    local ugly_dir="${CGROUP_V2_ROOT}/${CCD_UGLY_GROUP}"
    local good_dir="${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}"
    # Run if our groups exist OR if we constrained existing cgroups in place
    # (the cpuset save-file may exist even when theGood/theUgly don't).
    [[ -d "${ugly_dir}" || -d "${good_dir}" || -f "${STATE_DIR}/${CPUSET_SAVE_FILE_NAME}" ]] || return 0

    log "--- Reverting CCD/CCX core isolation ---"

    # Undo the in-place cpuset constraints on existing cgroups FIRST, so
    # session/service processes get their full CPU range back immediately,
    # independent of how long the theGood/theUgly teardown below takes.
    restore_constrained_cgroups

    local max_attempts=25
    local attempt success fail pid remaining
    local ugly_done=0 good_done=0

    # Groups already gone from a previous run count as already-reverted.
    [[ -d "${ugly_dir}" ]] || ugly_done=1
    [[ -d "${good_dir}" ]] || good_done=1

    for ((attempt = 1; attempt <= max_attempts; attempt++)); do
        if (( !ugly_done )); then
            success=0; fail=0
            local -a ugly_pids=()
            # Recursively collect all processes under theUgly hierarchy
            mapfile -t ugly_pids < <(collect_all_pids_under "${ugly_dir}")
            for pid in "${ugly_pids[@]}"; do
                [[ -z "${pid}" ]] && continue
                if restore_pid_to_origin "${pid}"; then success=$((success + 1)); else fail=$((fail + 1)); fi
            done
            (( attempt == 1 )) && log "  ${CCD_UGLY_GROUP}: ${success} processes moved back, ${fail} failed."

            # Hierarchical removal: deepest subdirectories first
            find -P "${ugly_dir}" -depth -type d -exec rmdir {} + 2>/dev/null
            [[ -d "${ugly_dir}" ]] || ugly_done=1
        fi

        if (( !good_done )); then
            success=0; fail=0
            local -a good_pids=()
            mapfile -t good_pids < <(collect_all_pids_under "${good_dir}")
            for pid in "${good_pids[@]}"; do
                [[ -z "${pid}" ]] && continue
                if restore_pid_to_origin "${pid}"; then success=$((success + 1)); else fail=$((fail + 1)); fi
            done
            (( attempt == 1 )) && log "  ${CCD_GOOD_GROUP}: ${success} processes moved back, ${fail} failed."

            find -P "${good_dir}" -depth -type d -exec rmdir {} + 2>/dev/null
            [[ -d "${good_dir}" ]] || good_done=1
        fi

        if (( ugly_done && good_done )); then
            log "  CCD/CCX isolation fully reverted after ${attempt} attempt(s)."
            rm -f "${STATE_DIR}/.ccd_pid_origin"
            rm -f "${STATE_DIR}/.tracked_game_pids"   # clean tracking file
            return 0
        fi

        (( attempt < max_attempts )) && sleep 0.2
    done

    # Exhausted all 25 attempts and at least one group is still not empty/removed.
    err "CCD/CCX isolation could not be fully reverted after ${max_attempts} attempts."
    (( !ugly_done )) && err "  ${CCD_UGLY_GROUP} still has $(wc -l < "${ugly_dir}/cgroup.procs" 2>/dev/null || echo '?') process(es) or could not be removed."
    (( !good_done )) && err "  ${CCD_GOOD_GROUP} still has $(wc -l < "${good_dir}/cgroup.procs" 2>/dev/null || echo '?') process(es) or could not be removed."
    # Origin map is left in place on failure so a subsequent manual retry
    # (or the next POST run) can still use it.
    return 1
}

# Moves a single pid back to the cgroup it was recorded as having come
# from (via lookup_pid_origin / CCD_PID_ORIGIN). Falls back to the root
# cgroup if no origin was recorded (unknown process, or origin file
# missing/corrupt) or if the recorded origin directory no longer exists
# (its owning service was stopped/restarted while game mode was active).
restore_pid_to_origin() {
    local pid="$1"
    local origin
    origin="$(lookup_pid_origin "${pid}")"
    if [[ -n "${origin}" && -d "${origin}" && -w "${origin}/cgroup.procs" ]]; then
        if echo "${pid}" > "${origin}/cgroup.procs" 2>/dev/null; then
            return 0
        fi
        # Origin dir exists but refused the write (e.g. process no longer
        # eligible, or origin is itself mid-removal) — fall through to root.
    fi
    echo "${pid}" > "${CGROUP_V2_ROOT}/cgroup.procs" 2>/dev/null
}

# --- Reference counter -------------------------------------------------------
# Tracks how many games are currently running. PRE increments by 1 on every
# call, POST decrements by 1. Once the counter reaches zero (the last game
# has exited), the restore actually happens.
# Updates are atomic via flock — no race under concurrent PRE/POST.
readonly REFCOUNT_FILE="${STATE_DIR}/.refcount"

# Read the counter (returns 0 if STATE_DIR or the file doesn't exist)
_refcount_read() {
    local n
    if [[ ! -f "${REFCOUNT_FILE}" ]]; then echo 0; return; fi
    if [[ -L "${REFCOUNT_FILE}" ]]; then
        err "SECURITY: refcount file is a symlink — treating as 0"
        echo 0; return
    fi
    n="$(cat "${REFCOUNT_FILE}" 2>/dev/null)"
    if [[ "${n}" =~ ^[0-9]+$ ]]; then echo "${n}"; else echo 0; fi
}

# Called by PRE: increments the counter, returns the new value
refcount_inc() {
    ensure_state_dir
    local n
    n=$(( $(_refcount_read) + 1 ))
    printf '%s' "${n}" > "${REFCOUNT_FILE}"
    echo "${n}"
}

# Called by POST: decrements the counter (never below 0), returns the new value
refcount_dec() {
    local n
    n=$(( $(_refcount_read) - 1 ))
    (( n < 0 )) && n=0
    if (( n == 0 )); then rm -f "${REFCOUNT_FILE}"; else printf '%s' "${n}" > "${REFCOUNT_FILE}"; fi
    echo "${n}"
}

# =============================================================================
# PRE — apply settings before the game starts
# =============================================================================
apply_game_settings() {
    log "========== ENTERING GAME MODE =========="
    ensure_state_dir
    local count
    count="$(refcount_inc)"
    if (( count > 1 )); then
        log "Reference count: ${count} (parameters already set, not rewriting)"
        log "========== GAME MODE ALREADY ACTIVE (${count} games running) =========="
        return 0
    fi
    log "Reference count: ${count} (first game — applying parameters)"

    # First game — reset stale CCD tracking files
    rm -f "${STATE_DIR}/.tracked_game_pids" "${STATE_DIR}/.ccd_pid_origin" "${STATE_DIR}/.ccd_cpuset_saved"

    log "--- VM Parameters ---"
    tune_param "/proc/sys/vm/compaction_proactiveness"        "0"                 "vm.compaction_proactiveness"
    tune_param "/proc/sys/vm/watermark_boost_factor"          "1"                 "vm.watermark_boost_factor"
    tune_param "/proc/sys/vm/min_free_kbytes"                 "262144"            "vm.min_free_kbytes"
    tune_param "/proc/sys/vm/watermark_scale_factor"          "50"                "vm.watermark_scale_factor"
    tune_param "/proc/sys/vm/swappiness"                      "${VM_SWAPPINESS}"  "vm.swappiness"
    tune_param "/proc/sys/vm/zone_reclaim_mode"               "0"                 "vm.zone_reclaim_mode"
    tune_param "/proc/sys/vm/page_lock_unfairness"            "1"                 "vm.page_lock_unfairness"
    # extend the vmstat update interval -> fewer periodic per-cpu timer wakeups
    tune_param "/proc/sys/vm/stat_interval"                   "20"                "vm.stat_interval"
    # disable swap readahead -> single-page swap-in, lower latency (ideal with zram)
    tune_param "/proc/sys/vm/page-cluster"                    "0"                 "vm.page-cluster"

    log "--- LRU Gen ---"
    tune_param "/sys/kernel/mm/lru_gen/enabled"               "5"       "lru_gen.enabled"

    log "--- Transparent HugePage ---"
    tune_choice_param "/sys/kernel/mm/transparent_hugepage/enabled"       "${THP_ENABLED}" "thp.enabled"
    tune_choice_param "/sys/kernel/mm/transparent_hugepage/shmem_enabled" "${THP_SHMEM_ENABLED}" "thp.shmem_enabled"
    tune_choice_param "/sys/kernel/mm/transparent_hugepage/defrag"        "${THP_DEFRAG}" "thp.defrag"

    log "--- Kernel / Latency ---"
    # Split lock penalty: some Windows games trigger split locks; the kernel
    # default penalizes the core by slowing it down ~1000x -> massive stutter.
    # Disable the penalty during gameplay. (kernel >= 6.0)
    tune_param "/proc/sys/kernel/split_lock_mitigate"         "0"       "kernel.split_lock_mitigate"
    # Disable soft/NMI lockup watchdogs -> fewer periodic per-cpu interrupts
    tune_param "/proc/sys/kernel/watchdog"                    "0"       "kernel.watchdog"

    log "--- Scheduler (procfs) ---"
    tune_param "/proc/sys/kernel/sched_autogroup_enabled"     "1"       "sched.autogroup_enabled"
    tune_param "/proc/sys/kernel/sched_cfs_bandwidth_slice_us" "3000"   "sched.cfs_bandwidth_slice_us"

    log "--- Scheduler (debugfs) ---"
    if ensure_debugfs; then
        tune_param "/sys/kernel/debug/sched/min_base_slice_ns" "3000000" "sched_debug.min_base_slice_ns"
        tune_param "/sys/kernel/debug/sched/migration_cost_ns" "500000"  "sched_debug.migration_cost_ns"
        tune_param "/sys/kernel/debug/sched/nr_migrate"        "8"       "sched_debug.nr_migrate"
    else
        warn "debugfs not accessible — sched debug parameters skipped"
    fi

    log "--- CPU Governor / EPP ---"
    tune_cpu_governor

    tune_cstates

    log "--- PCIe ASPM ---"
    if [[ "${SET_ASPM}" == "1" ]]; then
        # Disables link power-state transition delays (L0s/L1 wake-up).
        # Reduces GPU and NVMe latency jitter.
        tune_choice_param "/sys/module/pcie_aspm/parameters/policy" "${ASPM_POLICY}" "pcie_aspm.policy"
    fi

    log "--- Audio (HDA power save) ---"
    # Codec sleep/wake cycling causes pops and latency at the start of audio
    tune_param "/sys/module/snd_hda_intel/parameters/power_save"            "0" "snd_hda.power_save"
    tune_param "/sys/module/snd_hda_intel/parameters/power_save_controller" "N" "snd_hda.power_save_controller"

    log "--- PCI Latency Timer ---"
    tune_pci_latency

    apply_ccd_isolation

    log "========== GAME MODE ACTIVE =========="
}

# =============================================================================
# POST — restore original settings after the game exits
# =============================================================================
restore_game_settings() {
    log "========== EXITING GAME MODE =========="

    if [[ ! -d "${STATE_DIR}" ]]; then
        warn "State directory not found (${STATE_DIR}) — did PRE ever run?"
        exit 0
    fi

    local count
    count="$(refcount_dec)"
    if (( count > 0 )); then
        log "Reference count: ${count} (${count} game(s) still running — restore deferred)"
        log "========== RESTORE DEFERRED =========="
        return 0
    fi
    log "Reference count: 0 (last game exited — starting restore)"
    log "--- RESTORE ---"

    local ccd_restore_failed=0
    restore_ccd_isolation || ccd_restore_failed=1

    log "--- VM Parameters ---"
    restore_param "/proc/sys/vm/compaction_proactiveness"        "vm.compaction_proactiveness"
    restore_param "/proc/sys/vm/watermark_boost_factor"          "vm.watermark_boost_factor"
    restore_param "/proc/sys/vm/min_free_kbytes"                 "vm.min_free_kbytes"
    restore_param "/proc/sys/vm/watermark_scale_factor"          "vm.watermark_scale_factor"
    restore_param "/proc/sys/vm/swappiness"                      "vm.swappiness"
    restore_param "/proc/sys/vm/zone_reclaim_mode"               "vm.zone_reclaim_mode"
    restore_param "/proc/sys/vm/page_lock_unfairness"            "vm.page_lock_unfairness"
    restore_param "/proc/sys/vm/stat_interval"                   "vm.stat_interval"
    restore_param "/proc/sys/vm/page-cluster"                    "vm.page-cluster"

    log "--- LRU Gen ---"
    restore_param "/sys/kernel/mm/lru_gen/enabled"               "lru_gen.enabled"

    log "--- Transparent HugePage ---"
    restore_param "/sys/kernel/mm/transparent_hugepage/enabled"       "thp.enabled"
    restore_param "/sys/kernel/mm/transparent_hugepage/shmem_enabled" "thp.shmem_enabled"
    restore_param "/sys/kernel/mm/transparent_hugepage/defrag"        "thp.defrag"

    log "--- Kernel / Latency ---"
    restore_param "/proc/sys/kernel/split_lock_mitigate"         "kernel.split_lock_mitigate"
    restore_param "/proc/sys/kernel/watchdog"                    "kernel.watchdog"

    log "--- Scheduler (procfs) ---"
    restore_param "/proc/sys/kernel/sched_autogroup_enabled"     "sched.autogroup_enabled"
    restore_param "/proc/sys/kernel/sched_cfs_bandwidth_slice_us" "sched.cfs_bandwidth_slice_us"

    log "--- Scheduler (debugfs) ---"
    if ensure_debugfs; then
        restore_param "/sys/kernel/debug/sched/min_base_slice_ns" "sched_debug.min_base_slice_ns"
        restore_param "/sys/kernel/debug/sched/migration_cost_ns" "sched_debug.migration_cost_ns"
        restore_param "/sys/kernel/debug/sched/nr_migrate"        "sched_debug.nr_migrate"
    fi

    log "--- CPU Governor / EPP ---"
    restore_cpu_governor

    restore_cstates

    log "--- PCIe ASPM ---"
    restore_param "/sys/module/pcie_aspm/parameters/policy"     "pcie_aspm.policy"

    log "--- Audio (HDA power save) ---"
    restore_param "/sys/module/snd_hda_intel/parameters/power_save"            "snd_hda.power_save"
    restore_param "/sys/module/snd_hda_intel/parameters/power_save_controller" "snd_hda.power_save_controller"

    log "--- PCI Latency Timer ---"
    restore_pci_latency

    # Clean up the state directory
    if rmdir "${STATE_DIR}" 2>/dev/null; then
        log "State directory cleaned up."
    else
        warn "Could not clean up STATE_DIR — unexpected files remain: ${STATE_DIR}"
        ls -la "${STATE_DIR}" 2>/dev/null | while IFS= read -r l; do warn "  ${l}"; done
    fi

    log "========== RESTORE COMPLETE =========="

    if (( ccd_restore_failed )); then
        err "CCD/CCX isolation failed to fully revert — see errors above. All other settings were restored successfully."
        return 1
    fi
}

# =============================================================================
# STATUS — is game mode active, and what values are saved?
# =============================================================================
show_status() {
    if [[ -d "${STATE_DIR}" ]] && compgen -G "${STATE_DIR}/*" >/dev/null 2>&1; then
        local game_count
        game_count="$(_refcount_read)"
        local param_count
        param_count="$(find -P "${STATE_DIR}" -maxdepth 1 -type f -not -name '.refcount' -not -name '.ccd_*' -not -name '.tracked_game_pids' | wc -l)"
        echo "Game mode: ACTIVE (${game_count} game(s) running, ${param_count} parameter(s) saved)"
        echo
        printf '%-55s %s\n' "PARAMETER (save file)" "ORIGINAL VALUE"
        printf '%-55s %s\n' "----------------------" "--------------"
        local f
        for f in "${STATE_DIR}"/*; do
            [[ -f "${f}" ]] || continue
            local fname
            fname="$(basename "${f}")"
            [[ "${fname}" == ".refcount" || "${fname}" == .ccd_* || "${fname}" == ".tracked_game_pids" ]] && continue
            printf '%-55s %s\n' "${fname}" "$(head -c 120 "${f}" | tr '\n' ' ')"
        done
    else
        echo "Game mode: OFF (no saved state)"
    fi
    echo
    if [[ -d "${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}" ]]; then
        echo "CCD isolation: ACTIVE"
        echo "  ${CCD_GOOD_GROUP} (performance): $(cat "${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}/cpuset.cpus.effective" 2>/dev/null)"
        echo "  ${CCD_UGLY_GROUP} (system):      $(cat "${CGROUP_V2_ROOT}/${CCD_UGLY_GROUP}/cpuset.cpus.effective" 2>/dev/null)"
    else
        echo "CCD isolation: OFF (may be single CCX/CCD, disabled, or game mode is not active)"
    fi
    echo
    echo "Current values:"
    local p
    for p in /proc/sys/vm/swappiness \
             /proc/sys/kernel/split_lock_mitigate \
             /proc/sys/kernel/watchdog \
             /sys/kernel/mm/transparent_hugepage/enabled \
             /sys/module/pcie_aspm/parameters/policy \
             /sys/devices/system/cpu/cpufreq/policy0/scaling_governor; do
        [[ -e "${p}" ]] && printf '  %-60s %s\n' "${p}" "$(cat "${p}" 2>/dev/null)"
    done
    return 0
}

# =============================================================================
# Entry point
# =============================================================================
# Bash version check
if (( BASH_VERSINFO[0] < 4 )); then
    echo "FATAL: Bash 4.0 or higher is required." >&2
    exit 1
fi

require_root
rotate_log
load_config

ACTION="${1:-}"
case "${ACTION}" in
    PRE|pre|POST|post)
        # Lock against concurrent PRE/POST runs (Lutris fast-restart scenario).
        # PRE can hold this lock for up to CCD_MONITOR_SECONDS while it scans
        # for newly spawned child processes — if a game is closed quickly
        # (shorter than that window), a fixed short wait here would make
        # POST time out and exit BEFORE it ever restores anything, leaving
        # the system stuck in game-mode settings with no visible error.
        # Scale the wait with CCD_MONITOR_SECONDS (already loaded from
        # config above) plus a safety margin.
        LOCK_WAIT=$(( CCD_MONITOR_SECONDS + 20 ))
        (( LOCK_WAIT < 15 )) && LOCK_WAIT=15
        exec 9>"${LOCK_FILE}"
        if ! flock -w "${LOCK_WAIT}" 9; then
            err "Could not acquire lock after ${LOCK_WAIT}s (another instance is running) — exiting"
            exit 1
        fi
        case "${ACTION}" in
            PRE|pre)   apply_game_settings ;;
            POST|post) restore_game_settings ;;
        esac
        ;;
    STATUS|status)
        show_status
        ;;
    *)
        err "Invalid argument: '${ACTION}'. Expected: PRE, POST, or STATUS"
        exit 1
        ;;
esac
