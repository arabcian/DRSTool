#!/bin/bash
# =============================================================================
# lutris-game-tune.sh — Lutris Pre/Post Game System Tuner (v4.6)
#
# Not meant to be called directly. Invoked by lutris-game-tune-wrapper (a
# setuid root binary). See lutris-game-tune-wrapper.c for wrapper setup.
#
# Lutris settings:
#   Pre-game script:  /usr/local/bin/lutris-game-tune-wrapper PRE
#   Post-game script: /usr/local/bin/lutris-game-tune-wrapper POST
#   Status check:     /usr/local/bin/lutris-game-tune-wrapper STATUS
#   Per-game profile: /usr/local/bin/lutris-game-tune-wrapper PRE <profile>
#   Preview (no writes): lutris-game-tune-wrapper DRYRUN [profile]
#   Stuck state:      lutris-game-tune-wrapper RESTORE   (forces a full restore)
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
#
# v4.2 changes — "smooth at first, stutters after a few minutes" fixes:
#   - PERF (main cause): move_pid_list_to_group() used to write EVERY tracked
#     PID to cgroup.procs on every monitor pass, even PIDs that were already
#     in the target cgroup. Each such write takes cgroup_threadgroup_rwsem
#     for WRITE (a percpu_rwsem => rcu_sync_enter => synchronize_rcu), which
#     blocks fork/exec/clone system-wide for the duration. With a game tree
#     that keeps growing (Wine services, DXVK/VKD3D compile pools, overlays,
#     anti-cheat) this cost grows over the session and shows up as periodic
#     system-wide micro-stalls that a present-to-present frametime graph
#     cannot see. Now every PID's current cgroup is checked first and only
#     genuine migrations are performed.
#   - PERF: process-tree discovery no longer forks `pgrep -P` once per PID
#     (hundreds of forks every pass, growing with the tree). A single pass
#     over /proc builds the whole PPID map instead.
#   - CORRECTNESS: theUgly now defaults to partition type "member" instead of
#     "root". With both groups as partition roots the root cgroup's effective
#     cpuset could end up empty, which makes the kernel silently mark
#     theGood's partition "root invalid" — i.e. no isolation at all, with no
#     error anywhere. With only theGood as a partition root, CCD0 is genuinely
#     exclusive and processes left in the root cgroup physically cannot be
#     scheduled on it. Partition state is now verified and reported.
#   - CCD monitoring can now run for the WHOLE game session
#     (CCD_MONITOR_MODE=session, default) via a detached background watcher
#     instead of stopping after 30s. Late-spawning helpers (launcher overlays,
#     anti-cheat, Wine services, shader workers) previously landed in the root
#     cgroup unconstrained several minutes in — exactly when the stutters
#     start. The watcher runs at nice 19 inside theUgly, closes the flock fd,
#     and is stopped by POST.
#   - VM: watermark_boost_factor is no longer forced to 1 and
#     compaction_proactiveness is no longer forced to 0. Disabling BOTH
#     fragmentation defences means free memory fragments monotonically during
#     a session; the first higher-order allocation that fails then hits
#     synchronous direct compaction, often in a driver/worker thread rather
#     than the render thread — again invisible to a frametime graph. Both are
#     now configurable with sane defaults.
#   - VM: vm.stat_interval default lowered 20 -> 10 (zone stats used by
#     watermark checks were going stale for up to 20s).
#   - MM: lru_gen.enabled default 5 -> 7. Bit 0x2 (batched leaf-PTE aging via
#     page-table walks) was being cleared, forcing MGLRU back onto rmap
#     scanning, whose cost scales with the game's mapped address space — so
#     reclaim gets more expensive the longer the session runs.
#   - SCHED: nr_migrate default 8 -> 32 (kernel default; 8 slows down
#     load-balance correction as the runnable thread count grows) and
#     min_base_slice_ns default 3000000 -> 1000000, both now configurable.
#
# v4.3 changes:
#   - Added support for amd-pstate's per-core "epp_boost" module parameter
#     (/sys/module/amd_pstate/parameters/epp_boost). Not yet upstream as of
#     this writing — only present on kernels built with the epp_boost patch
#     series. Enabled on PRE / restored on POST like every other tunable;
#     configurable via SET_EPP_BOOST (default 1). Silently skipped (debug-
#     logged only) on kernels where the sysfs knob doesn't exist, so this
#     is safe to leave on even without a patched kernel.
#
# v4.4 changes:
#   - Added support for the amd_x3d_vcache driver's per-CPU core preference
#     (.../drivers/amd_x3d_vcache/<instance>/amd_x3d_mode, "frequency" or
#     "cache"). AMD 3D V-Cache CPUs only; the ACPI instance directory is
#     discovered at runtime (not hardcoded as AMDI0101:00) since it isn't
#     guaranteed to be the same on every board/BIOS. Configurable via
#     SET_X3D_VCACHE_MODE (default 1) and X3D_VCACHE_MODE (default
#     "frequency"). Silently skipped, like epp_boost, when the driver isn't
#     bound.
#
# v4.5 changes — Intel CPU support:
#   - CPU vendor is detected once per run (/proc/cpuinfo vendor_id). AMD-only
#     tunables (amd-pstate epp_boost, amd_x3d_vcache) are skipped on Intel, so
#     Intel systems no longer get an "amd_x3d_vcache: driver not bound" warning
#     on every PRE.
#   - New Intel tunables, each saved on PRE and restored on POST like every
#     other parameter and each skipped when its kernel interface is missing:
#       * intel_pstate/no_turbo            (SET_INTEL_TURBO)      -> 0
#       * intel_pstate/hwp_dynamic_boost   (SET_INTEL_HWP_BOOST)  -> 1
#       * intel_pstate/max_perf_pct, min_perf_pct (INTEL_MAX/MIN_PERF_PCT)
#       * cpuN/power/energy_perf_bias      (SET_INTEL_EPB)        -> 0
#       * kernel.sched_itmt_enabled        (SET_INTEL_ITMT)       -> 1
#     The existing governor/EPP handling already works with intel_pstate
#     (active+HWP exposes energy_performance_preference per policy).
#   - Hybrid P/E-core isolation (Alder Lake and newer): the AMD CCD logic
#     looks for two L3 domains, which never exist on Intel (one shared L3), so
#     it silently did nothing there. With HYBRID_CORE_ISOLATION=1 the same
#     theGood/theUgly machinery now uses P-cores as the performance group and
#     E-cores (+ LP-E cores) as the system group. Opt-in, and automatically
#     skipped when the P-cores expose fewer than HYBRID_MIN_P_THREADS threads.
#
# v4.6 changes:
#   - RELIABILITY: the plain PRE/POST counter is replaced by per-game entries
#     (STATE_DIR/.games/). Each entry remembers its launcher process (anchor +
#     start time), so entries left behind by a crashed/killed launcher are
#     dropped on the next PRE instead of keeping game mode "active" forever.
#     New RESTORE command forces a full restore regardless of the count.
#   - PRE <profile> / DRYRUN [profile]: per-game config overrides loaded from
#     /etc/lutris-game-tune.d/<profile>.conf (same safe whitelist parser;
#     root-owned, not group/other writable). DRYRUN prints what PRE would do
#     without writing, saving or moving anything.
#   - VERIFY: every tune_param/tune_choice_param write is read back; a PRE
#     summary (applied / read back differently / failed / skipped) is logged
#     and shown by STATUS.
#   - New tunables: vm.max_map_count (raise-only), dirty-page limits, NUMA
#     balancing, IRQ affinity (opt-in), NVIDIA persistence mode, amdgpu
#     power_dpm_force_performance_level (opt-in), Wi-Fi power save, NVMe I/O
#     scheduler.
#   - tests/run-tests.sh sources this file (GT_TEST_DIR, non-root only).
# =============================================================================

set -euo pipefail

# --- Constants -----------------------------------------------------------------
readonly LOG_MAX_BYTES=$((1024 * 1024))
# Test hook (tests/run-tests.sh): honored ONLY for non-root callers, so it can
# never influence the setuid-root chain (the wrapper also clears the env).
if (( EUID != 0 )) && [[ -n "${GT_TEST_DIR:-}" ]]; then
    GT_TEST_MODE=1
    STATE_DIR="${GT_TEST_DIR}/run"
    LOCK_FILE="${GT_TEST_DIR}/lock"
    LOG_FILE="${GT_TEST_DIR}/log"
    CONFIG_FILE="${GT_TEST_DIR}/lutris-game-tune.conf"
    PROFILE_DIR="${GT_TEST_DIR}/profiles"
    OWNER_UID="${EUID}"
else
    GT_TEST_MODE=0
    readonly STATE_DIR="/run/lutris-game-tune"
    readonly LOCK_FILE="/run/lutris-game-tune.lock"
    readonly LOG_FILE="/var/log/lutris-game-tune.log"
    readonly CONFIG_FILE="/etc/lutris-game-tune.conf"
    readonly PROFILE_DIR="/etc/lutris-game-tune.d"
    readonly OWNER_UID=0
fi

# --- Configuration (defaults; overridable via /etc/lutris-game-tune.conf) ----
# CPU frequency governor (amd-pstate active mode also sets EPP to performance)
CPU_GOVERNOR="performance"
SET_CPU_GOVERNOR=1
# amd-pstate per-core EPP boost (kernel module param; not upstream yet as of
# this writing — only present on patched kernels). Silently skipped if the
# sysfs knob doesn't exist, so this is safe to leave enabled across kernels
# that don't have it.
SET_EPP_BOOST=1
# AMD 3D V-Cache core preference (amd_x3d_vcache driver; AMD 3D V-Cache CPUs
# only). "frequency" prefers the higher-clocking CCD, "cache" prefers cores
# on the CCD with the larger L3. Silently skipped if the driver isn't bound
# (non-X3D CPU or kernel without the driver), so safe to leave enabled.
SET_X3D_VCACHE_MODE=1
X3D_VCACHE_MODE="frequency"
# --- Intel CPUs ---------------------------------------------------------------
# Used only when /proc/cpuinfo reports GenuineIntel (the AMD-only tunables above
# are skipped on Intel). Each knob is skipped when its kernel interface is
# missing, so leaving them enabled is safe on any Intel system.
# Force turbo on while gaming (intel_pstate/no_turbo=0).
SET_INTEL_TURBO=1
# intel_pstate hwp_dynamic_boost=1 (active mode + HWP only).
SET_INTEL_HWP_BOOST=1
# intel_pstate performance limits in percent; 0 = leave unchanged.
INTEL_MAX_PERF_PCT=100
INTEL_MIN_PERF_PCT=0
# Energy/performance bias (cpuN/power/energy_perf_bias): 0 = max performance.
SET_INTEL_EPB=1
# ITMT favored-core scheduling (Turbo Boost Max 3.0 / hybrid): sched_itmt_enabled=1.
SET_INTEL_ITMT=1
# Hybrid (P+E core) CPUs only: pin the game to the P-cores and the rest of the
# system to the E-cores, reusing the CCD isolation machinery. Opt-in, because
# confining a game to P-core threads only pays off when there are enough of them.
HYBRID_CORE_ISOLATION=0
# Skip hybrid isolation when the P-cores expose fewer logical CPUs than this.
HYBRID_MIN_P_THREADS=8
CPU_VENDOR="unknown"   # set by detect_cpu_vendor(): intel | amd | unknown
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
# Proactive background compaction (0 = off). Setting this to 0 removes the
# only mechanism that keeps free memory defragmented during a long session.
# Low but non-zero is the safe default: kcompactd works in the background
# instead of the game hitting synchronous direct compaction later.
VM_COMPACTION_PROACTIVENESS=5
# Watermark boost on external fragmentation events (kernel default 15000,
# 0 = disabled). Do NOT set this to 0/1 together with
# VM_COMPACTION_PROACTIVENESS=0 — that disables both defences at once.
VM_WATERMARK_BOOST_FACTOR=15000
# vmstat refresh interval in seconds. Larger = fewer per-cpu timer wakeups,
# but zone statistics used by watermark checks go stale for that long.
VM_STAT_INTERVAL=10
# MGLRU feature bitmask (0x1 core, 0x2 batched leaf-PTE aging, 0x4 non-leaf).
# 7 = kernel default. Clearing 0x2 forces rmap-based aging, whose cost grows
# with the process's mapped address space.
LRU_GEN_ENABLED=7

# --- Scheduler ----------------------------------------------------------------
SCHED_MIN_BASE_SLICE_NS=1000000
SCHED_MIGRATION_COST_NS=500000
SCHED_NR_MIGRATE=32

# --- Proton / Wine friendliness -----------------------------------------------
# vm.max_map_count: some Proton titles crash on the stock 65530. Only ever
# RAISED, never lowered. 0 = leave alone. (SteamOS uses 2147483642.)
VM_MAX_MAP_COUNT=2147483642
# Smaller dirty-page windows smooth out shader-cache / save-file write bursts.
# While active, the *_bytes knobs replace the *_ratio ones; all four originals
# are saved and restored. Skipped when dirty_bytes is already <= the target.
SET_VM_DIRTY=1
VM_DIRTY_BACKGROUND_BYTES=67108864
VM_DIRTY_BYTES=268435456
# NUMA auto-balancing = background page scanning/migration (page-fault noise).
SET_NUMA_BALANCING=1

# --- IRQ affinity (opt-in) ----------------------------------------------------
# Moves the IRQs of the selected PCI device classes (gpu nvme net usb audio)
# onto the "system" CPUs: IRQ_AFFINITY_CPUS if set, else the system CCD group.
SET_IRQ_AFFINITY=0
IRQ_AFFINITY_CLASSES="gpu nvme net usb"
IRQ_AFFINITY_CPUS=""

# --- GPU ----------------------------------------------------------------------
# NVIDIA persistence mode (only touches GPUs where it is currently disabled).
SET_NVIDIA_PERSISTENCE=0
# amdgpu power_dpm_force_performance_level (raises power draw; opt-in).
SET_AMDGPU_PERF_LEVEL=0
AMDGPU_PERF_LEVEL="high"

# --- Network / storage --------------------------------------------------------
SET_WIFI_POWERSAVE=1
SET_NVME_SCHED=1
NVME_IO_SCHEDULER="none"

# --- Dry run / profile (runtime state, not config keys) -----------------------
DRY_RUN=0

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
# "timed"   = only scan for CCD_MONITOR_SECONDS, then stop (v4.1 behaviour)
# "session" = after the initial timed window, keep a detached low-priority
#             watcher alive until POST. Late-spawning helpers (overlays,
#             anti-cheat, Wine services) are what land unconstrained a few
#             minutes in, so this is the default.
CCD_MONITOR_MODE="session"
# Sweep interval of the detached session watcher, in seconds.
CCD_MONITOR_INTERVAL=5
# cpuset.cpus.partition type: "member", "root" or "isolated".
# theGood should be a partition root ("root"/"isolated") so that CCD0 is
# exclusively owned and nothing outside it can be scheduled there.
# theUgly should normally stay "member": if BOTH groups claim their CPUs
# exclusively, the root cgroup's effective cpuset can end up empty and the
# kernel silently invalidates the partition, disabling isolation entirely.
CCD_GOOD_PARTITION_TYPE="root"
CCD_UGLY_PARTITION_TYPE="member"

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
    if [[ "${owner}" != "${OWNER_UID}" ]]; then
        err "SECURITY: ${STATE_DIR} is not owned by root (uid=${owner}) — aborting."
        exit 1
    fi
    if [[ "${mode}" != "700" ]]; then
        chmod 0700 "${STATE_DIR}"
    fi
}

# Read the config file SAFELY: no source (would allow code execution in the
# setuid chain) — only whitelisted KEY=VALUE lines are accepted.
load_config_file() {
    local cf="$1"
    [[ -f "${cf}" ]] || return 0
    if [[ -L "${cf}" ]]; then
        warn "Config is a symlink, ignored: ${cf}"
        return 3
    fi
    local owner mode
    owner=$(stat -c '%u' "${cf}")
    mode=$(( 8#$(stat -c '%a' "${cf}") ))
    if [[ "${owner}" != "${OWNER_UID}" ]]; then
        warn "Config is not owned by root, ignored: ${cf}"
        return 3
    fi
    # reject if group (020) or other (002) write bit is set
    if (( mode & 8#022 )); then
        warn "Config is group/other writable, ignored: ${cf}"
        return 3
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
        elif [[ "${line}" =~ ^(IRQ_AFFINITY_CPUS|IRQ_AFFINITY_CLASSES)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            val="${val#\"}"; val="${val%\"}"
        elif [[ "${line}" =~ ^([A-Z0-9_]+)=([[:alnum:]_.-]+)$ ]]; then
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
            SET_EPP_BOOST)         SET_EPP_BOOST="${val}" ;;
            SET_X3D_VCACHE_MODE)   SET_X3D_VCACHE_MODE="${val}" ;;
            X3D_VCACHE_MODE)
                case "${val,,}" in
                    frequency|cache) X3D_VCACHE_MODE="${val,,}" ;;
                    *) warn "Invalid X3D_VCACHE_MODE '${val}' (frequency|cache), using default ${X3D_VCACHE_MODE}" ;;
                esac ;;
            SET_VM_DIRTY)          SET_VM_DIRTY="${val}" ;;
            SET_NUMA_BALANCING)    SET_NUMA_BALANCING="${val}" ;;
            SET_IRQ_AFFINITY)      SET_IRQ_AFFINITY="${val}" ;;
            SET_NVIDIA_PERSISTENCE) SET_NVIDIA_PERSISTENCE="${val}" ;;
            SET_AMDGPU_PERF_LEVEL) SET_AMDGPU_PERF_LEVEL="${val}" ;;
            SET_WIFI_POWERSAVE)    SET_WIFI_POWERSAVE="${val}" ;;
            SET_NVME_SCHED)        SET_NVME_SCHED="${val}" ;;
            VM_MAX_MAP_COUNT)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} <= 2147483647 )); then
                    VM_MAX_MAP_COUNT="$(( 10#${val} ))"
                else
                    warn "Invalid VM_MAX_MAP_COUNT '${val}' (0-2147483647), using default ${VM_MAX_MAP_COUNT}"
                fi ;;
            VM_DIRTY_BACKGROUND_BYTES)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} >= 1048576 && 10#${val} <= 2147483647 )); then
                    VM_DIRTY_BACKGROUND_BYTES="$(( 10#${val} ))"
                else
                    warn "Invalid VM_DIRTY_BACKGROUND_BYTES '${val}' (1048576-2147483647), using default ${VM_DIRTY_BACKGROUND_BYTES}"
                fi ;;
            VM_DIRTY_BYTES)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} >= 1048576 && 10#${val} <= 2147483647 )); then
                    VM_DIRTY_BYTES="$(( 10#${val} ))"
                else
                    warn "Invalid VM_DIRTY_BYTES '${val}' (1048576-2147483647), using default ${VM_DIRTY_BYTES}"
                fi ;;
            AMDGPU_PERF_LEVEL)
                case "${val}" in
                    auto|low|high|manual|profile_standard|profile_min_sclk|profile_min_mclk|profile_peak)
                        AMDGPU_PERF_LEVEL="${val}" ;;
                    *) warn "Invalid AMDGPU_PERF_LEVEL '${val}', using default ${AMDGPU_PERF_LEVEL}" ;;
                esac ;;
            NVME_IO_SCHEDULER)
                case "${val}" in
                    none|mq-deadline|kyber|bfq) NVME_IO_SCHEDULER="${val}" ;;
                    *) warn "Invalid NVME_IO_SCHEDULER '${val}' (none|mq-deadline|kyber|bfq), using default ${NVME_IO_SCHEDULER}" ;;
                esac ;;
            IRQ_AFFINITY_CPUS)
                if [[ -z "${val}" || "${val}" =~ ^[0-9]+([,-][0-9]+)*$ ]]; then
                    IRQ_AFFINITY_CPUS="${val}"
                else
                    warn "Invalid IRQ_AFFINITY_CPUS '${val}' (cpulist like 8-15,24-31), ignored"
                fi ;;
            IRQ_AFFINITY_CLASSES)
                if [[ "${val}" =~ ^(gpu|nvme|net|usb|audio)( (gpu|nvme|net|usb|audio))*$ ]]; then
                    IRQ_AFFINITY_CLASSES="${val}"
                else
                    warn "Invalid IRQ_AFFINITY_CLASSES '${val}' (any of: gpu nvme net usb audio), using default '${IRQ_AFFINITY_CLASSES}'"
                fi ;;
            SET_INTEL_TURBO)       SET_INTEL_TURBO="${val}" ;;
            SET_INTEL_HWP_BOOST)   SET_INTEL_HWP_BOOST="${val}" ;;
            SET_INTEL_EPB)         SET_INTEL_EPB="${val}" ;;
            SET_INTEL_ITMT)        SET_INTEL_ITMT="${val}" ;;
            HYBRID_CORE_ISOLATION) HYBRID_CORE_ISOLATION="${val}" ;;
            INTEL_MAX_PERF_PCT)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} <= 100 )); then
                    INTEL_MAX_PERF_PCT="$(( 10#${val} ))"
                else
                    warn "Invalid INTEL_MAX_PERF_PCT '${val}' (0-100), using default ${INTEL_MAX_PERF_PCT}"
                fi ;;
            INTEL_MIN_PERF_PCT)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} <= 100 )); then
                    INTEL_MIN_PERF_PCT="$(( 10#${val} ))"
                else
                    warn "Invalid INTEL_MIN_PERF_PCT '${val}' (0-100), using default ${INTEL_MIN_PERF_PCT}"
                fi ;;
            HYBRID_MIN_P_THREADS)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( 10#${val} >= 1 && 10#${val} <= 512 )); then
                    HYBRID_MIN_P_THREADS="$(( 10#${val} ))"
                else
                    warn "Invalid HYBRID_MIN_P_THREADS '${val}' (1-512), using default ${HYBRID_MIN_P_THREADS}"
                fi ;;
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
            VM_COMPACTION_PROACTIVENESS)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 0 && val <= 100 )); then
                    VM_COMPACTION_PROACTIVENESS="${val}"
                else
                    warn "Invalid VM_COMPACTION_PROACTIVENESS '${val}' (0-100), using default ${VM_COMPACTION_PROACTIVENESS}"
                fi ;;
            VM_WATERMARK_BOOST_FACTOR)
                if [[ "${val}" =~ ^[0-9]+$ ]]; then
                    VM_WATERMARK_BOOST_FACTOR="${val}"
                else
                    warn "Invalid VM_WATERMARK_BOOST_FACTOR '${val}', using default ${VM_WATERMARK_BOOST_FACTOR}"
                fi ;;
            VM_STAT_INTERVAL)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= 120 )); then
                    VM_STAT_INTERVAL="${val}"
                else
                    warn "Invalid VM_STAT_INTERVAL '${val}' (1-120), using default ${VM_STAT_INTERVAL}"
                fi ;;
            LRU_GEN_ENABLED)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 0 && val <= 7 )); then
                    LRU_GEN_ENABLED="${val}"
                else
                    warn "Invalid LRU_GEN_ENABLED '${val}' (0-7), using default ${LRU_GEN_ENABLED}"
                fi ;;
            SCHED_MIN_BASE_SLICE_NS)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 100000 )); then
                    SCHED_MIN_BASE_SLICE_NS="${val}"
                else
                    warn "Invalid SCHED_MIN_BASE_SLICE_NS '${val}' (>=100000), using default ${SCHED_MIN_BASE_SLICE_NS}"
                fi ;;
            SCHED_MIGRATION_COST_NS)
                if [[ "${val}" =~ ^[0-9]+$ ]]; then
                    SCHED_MIGRATION_COST_NS="${val}"
                else
                    warn "Invalid SCHED_MIGRATION_COST_NS '${val}', using default ${SCHED_MIGRATION_COST_NS}"
                fi ;;
            SCHED_NR_MIGRATE)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= 128 )); then
                    SCHED_NR_MIGRATE="${val}"
                else
                    warn "Invalid SCHED_NR_MIGRATE '${val}' (1-128), using default ${SCHED_NR_MIGRATE}"
                fi ;;
            CCD_MONITOR_MODE)
                case "${val,,}" in
                    timed|session) CCD_MONITOR_MODE="${val,,}" ;;
                    *) warn "Invalid CCD_MONITOR_MODE '${val}' (timed|session), using default ${CCD_MONITOR_MODE}" ;;
                esac ;;
            CCD_MONITOR_INTERVAL)
                if [[ "${val}" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= 60 )); then
                    CCD_MONITOR_INTERVAL="${val}"
                else
                    warn "Invalid CCD_MONITOR_INTERVAL '${val}' (1-60), using default ${CCD_MONITOR_INTERVAL}"
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
    done < "${cf}"
    log "Config loaded: ${cf}"
}

load_config() { load_config_file "${CONFIG_FILE}" || true; }

# --- Per-game profiles -----------------------------------------------------------
# PRE <profile> loads PROFILE_DIR/<profile>.conf on top of the global config.
# Same whitelist parser, same ownership/permission rules. The profile name is
# validated here AND in the wrapper (no slashes, no leading dot -> no traversal).
valid_profile_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; }

load_profile() {
    local name="$1" pf downer dmode
    if ! valid_profile_name "${name}"; then
        err "Invalid profile name: '${name}'"
        return 1
    fi
    if [[ -L "${PROFILE_DIR}" || ! -d "${PROFILE_DIR}" ]]; then
        err "Profile directory missing or a symlink: ${PROFILE_DIR}"
        return 1
    fi
    downer="$(stat -c '%u' "${PROFILE_DIR}")"
    dmode=$(( 8#$(stat -c '%a' "${PROFILE_DIR}") ))
    if [[ "${downer}" != "${OWNER_UID}" ]] || (( dmode & 8#022 )); then
        err "Profile directory must be root-owned and not group/other writable: ${PROFILE_DIR}"
        return 1
    fi
    pf="${PROFILE_DIR}/${name}.conf"
    if [[ ! -f "${pf}" ]]; then
        err "Profile not found: ${pf}"
        return 1
    fi
    if ! load_config_file "${pf}"; then
        err "Profile rejected (ownership/permissions): ${pf}"
        return 1
    fi
    log "Profile loaded: ${name}"
}

# Check debugfs mount, mount it if needed
ensure_debugfs() {
    if (( DRY_RUN )) && ! mountpoint -q /sys/kernel/debug 2>/dev/null; then
        return 1   # dry run never mounts anything
    fi
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

# --- Run statistics / verification / dry run ---------------------------------------
STAT_APPLIED=0
STAT_SKIPPED=0
STAT_FAILED=0
STAT_MISMATCH=0

# NOTE: plain assignments, never ((n++)) — that returns 1 when n was 0 and
# would kill the script under set -e.
_stat_applied() { STAT_APPLIED=$((STAT_APPLIED + 1)); }
_stat_skipped() { STAT_SKIPPED=$((STAT_SKIPPED + 1)); }
_stat_failed()  { STAT_FAILED=$((STAT_FAILED + 1)); }

# Dry-run output always goes to the terminal, whatever LOG_LEVEL says.
dry() { printf '[dry-run] %s\n' "$*"; }

# Numeric value of a decimal or 0x-hex token (prints nothing otherwise)
_to_num() {
    local v="$1"
    if [[ "${v}" =~ ^0[xX][0-9a-fA-F]+$ ]]; then
        echo "$(( v ))"
    elif [[ "${v}" =~ ^[0-9]+$ ]]; then
        echo "$(( 10#${v} ))"
    fi
    return 0
}

# Read the value back after writing it and compare with what we wrote.
# "a [b] c" choice files compare on the bracketed item; numbers compare
# numerically (lru_gen reads back 0x0007 after writing 7).
_verify_applied() {
    local path="$1" want="$2" desc="$3" raw got wn gn
    raw="$(cat "${path}" 2>/dev/null || true)"
    if [[ "${raw}" =~ \[([^]]+)\] ]]; then got="${BASH_REMATCH[1]}"; else got="${raw}"; fi
    got="${got//[[:space:]]/}"
    wn="$(_to_num "${want}")"
    gn="$(_to_num "${got}")"
    if [[ "${got}" == "${want}" ]] || [[ -n "${wn}" && "${wn}" == "${gn}" ]]; then
        log "Set         [${desc}]: ${want}"
        _stat_applied
    else
        warn "Set         [${desc}]: wrote '${want}' but it reads back '${got}'"
        STAT_MISMATCH=$((STAT_MISMATCH + 1))
    fi
}

_print_summary() {
    if (( DRY_RUN )); then
        dry "Summary: ${STAT_SKIPPED} knob(s) not present on this system; everything else would be applied."
        return 0
    fi
    local msg="${STAT_APPLIED} applied, ${STAT_MISMATCH} read back differently, ${STAT_FAILED} failed, ${STAT_SKIPPED} skipped (interface missing)"
    if (( STAT_MISMATCH + STAT_FAILED > 0 )); then
        warn "Summary: ${msg}"
    else
        log "Summary: ${msg}"
    fi
    _state_write "${STATE_DIR}/.summary" "${msg} [$(date '+%F %T')]" || true
}

# --- Parameter helpers -----------------------------------------------------
# Read current value → save it → write new value
# tune_param <path> <new_value> <description>
tune_param() {
    local path="$1" new_val="$2" desc="$3"
    local save_file; save_file="$(_save_name "${path}")"

    if [[ ! -e "${path}" ]]; then
        warn "Does not exist, skipped: ${desc}"
        _stat_skipped
        return 0
    fi
    if (( DRY_RUN )); then
        dry "would set [${desc}]: $(cat "${path}" 2>/dev/null || echo '?') -> ${new_val}"
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
        _stat_failed
        return 0
    fi
    _verify_applied "${path}" "${new_val}" "${desc}"
}

# For "a [b] c"-style choice files (THP, ASPM, io scheduler): extracts the
# current selection from the brackets and saves it.
# tune_choice_param <path> <new_value> <description>
tune_choice_param() {
    local path="$1" new_val="$2" desc="$3"
    local save_file; save_file="$(_save_name "${path}")"

    if [[ ! -e "${path}" ]]; then
        warn "Does not exist, skipped: ${desc}"
        _stat_skipped
        return 0
    fi
    if (( DRY_RUN )); then
        dry "would set [${desc}]: $(cat "${path}" 2>/dev/null || echo '?') -> ${new_val}"
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
        _stat_failed
        return 0
    fi
    _verify_applied "${path}" "${new_val}" "${desc}"
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
    if (( DRY_RUN )); then dry "would set PCI latency timers (bridge=80, host=00, other=20)"; return 0; fi
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

# --- CPU vendor -----------------------------------------------------------------
# Must ALWAYS return 0 (set -e): grep's "no match" status is swallowed.
detect_cpu_vendor() {
    local line=""
    line="$(grep -m1 '^vendor_id' /proc/cpuinfo 2>/dev/null)" || true
    case "${line}" in
        *GenuineIntel*) CPU_VENDOR="intel" ;;
        *AuthenticAMD*) CPU_VENDOR="amd" ;;
        *)              CPU_VENDOR="unknown" ;;
    esac
    log_debug "CPU vendor: ${CPU_VENDOR}"
    return 0
}

# --- Intel: intel_pstate / EPB / ITMT --------------------------------------------
INTEL_PSTATE_DIR="/sys/devices/system/cpu/intel_pstate"
ITMT_PATH="/proc/sys/kernel/sched_itmt_enabled"

tune_intel() {
    local d="${INTEL_PSTATE_DIR}"
    if [[ -d "${d}" ]]; then
        log_debug "intel_pstate status: $(cat "${d}/status" 2>/dev/null || echo unknown)"
        if [[ "${SET_INTEL_TURBO}" == "1" ]]; then
            tune_param "${d}/no_turbo" "0" "intel_pstate.no_turbo"
        fi
        if [[ "${SET_INTEL_HWP_BOOST}" == "1" && -e "${d}/hwp_dynamic_boost" ]]; then
            tune_param "${d}/hwp_dynamic_boost" "1" "intel_pstate.hwp_dynamic_boost"
        fi
        # max before min: min_perf_pct must never exceed max_perf_pct
        if (( INTEL_MAX_PERF_PCT > 0 )); then
            tune_param "${d}/max_perf_pct" "${INTEL_MAX_PERF_PCT}" "intel_pstate.max_perf_pct"
        fi
        if (( INTEL_MIN_PERF_PCT > 0 )); then
            tune_param "${d}/min_perf_pct" "${INTEL_MIN_PERF_PCT}" "intel_pstate.min_perf_pct"
        fi
    else
        log_debug "intel_pstate not present (acpi-cpufreq or disabled), skipped"
    fi

    if [[ "${SET_INTEL_EPB}" == "1" ]]; then
        local epb cpu_name
        for epb in /sys/devices/system/cpu/cpu*/power/energy_perf_bias; do
            [[ -f "${epb}" ]] || continue
            [[ "${epb}" =~ /(cpu[0-9]+)/power/ ]] && cpu_name="${BASH_REMATCH[1]}" || cpu_name="cpu?"
            tune_param "${epb}" "0" "epb.${cpu_name}"
        done
    fi

    if [[ "${SET_INTEL_ITMT}" == "1" && -e "${ITMT_PATH}" ]]; then
        tune_param "${ITMT_PATH}" "1" "kernel.sched_itmt_enabled"
    fi
}

restore_intel() {
    local d="${INTEL_PSTATE_DIR}"
    # min before max (reverse of tune order); restore_param is a quiet no-op
    # for anything that was never saved.
    restore_param "${d}/min_perf_pct"      "intel_pstate.min_perf_pct"
    restore_param "${d}/max_perf_pct"      "intel_pstate.max_perf_pct"
    restore_param "${d}/hwp_dynamic_boost" "intel_pstate.hwp_dynamic_boost"
    restore_param "${d}/no_turbo"          "intel_pstate.no_turbo"

    local epb cpu_name
    for epb in /sys/devices/system/cpu/cpu*/power/energy_perf_bias; do
        [[ -f "${epb}" ]] || continue
        [[ "${epb}" =~ /(cpu[0-9]+)/power/ ]] && cpu_name="${BASH_REMATCH[1]}" || cpu_name="cpu?"
        restore_param "${epb}" "epb.${cpu_name}"
    done

    restore_param "${ITMT_PATH}" "kernel.sched_itmt_enabled"
}

# --- amd-pstate EPP boost (per-core, not yet upstream) ------------------------
# /sys/module/amd_pstate/parameters/epp_boost only exists on kernels built
# with the epp_boost patch series applied. tune_param already no-ops cleanly
# via its [[ ! -e "${path}" ]] check, but we check here too so the log
# reflects "feature not present on this kernel" rather than a generic skip.
EPP_BOOST_PATH="/sys/module/amd_pstate/parameters/epp_boost"

tune_epp_boost() {
    [[ "${SET_EPP_BOOST}" == "1" ]] || return 0
    [[ "${CPU_VENDOR}" != "intel" ]] || return 0   # AMD-only knob
    if [[ ! -e "${EPP_BOOST_PATH}" ]]; then
        log_debug "epp_boost not present on this kernel, skipped"
        return 0
    fi
    tune_param "${EPP_BOOST_PATH}" "1" "amd_pstate.epp_boost"
}

restore_epp_boost() {
    [[ -e "${EPP_BOOST_PATH}" ]] || return 0
    restore_param "${EPP_BOOST_PATH}" "amd_pstate.epp_boost"
}

# --- AMD 3D V-Cache core preference (amd_x3d_vcache driver) --------------------
# /sys/bus/platform/drivers/amd_x3d_vcache/<instance>/amd_x3d_mode only exists
# when the driver is bound (AMD 3D V-Cache CPU + supporting kernel). <instance>
# is an ACPI device instance id (e.g. AMDI0101:00) and is NOT guaranteed to be
# ":00" on every board/BIOS, so it is discovered rather than hardcoded.
# Accepts "frequency" (prefer the higher-clocking CCD) or "cache" (prefer
# cores on the CCD with the larger L3), matching the driver's own ABI.
find_x3d_vcache_path() {
    # `find -P` (the default, and what earlier versions used) refuses to
    # descend into symlinked directories, and `/sys/bus/.../drivers/<driver>/
    # <device>` entries are ALWAYS symlinks to the real device under
    # /sys/devices/... — so `find -P` can never see anything past that
    # symlink. Using `find -L` (follow symlinks) fixes that in principle,
    # but has been reported to still miss the file in the field on some
    # setups (chained symlinks, `find` build without full -L support in a
    # minimal environment, etc.) — a plain bash glob doesn't have any of
    # `find`'s traversal restrictions and resolves the symlink like any
    # normal path lookup, so use that instead: it's what the DRSTool.py GUI
    # detector (Path.glob) does too, and that one has always found it fine.
    # IMPORTANT: this function must ALWAYS return 0, match or no match.
    # It is always called as `path="$(find_x3d_vcache_path)"` — a bare
    # assignment whose right-hand side is a command substitution — and
    # under `set -euo pipefail` bash propagates a non-zero exit status
    # from THAT substitution to the assignment itself, which then kills
    # the entire script right there with NO error message at all. This
    # was silently aborting the whole PRE/POST run (nothing after this
    # point — ASPM, audio, PCI latency, CCD isolation — ever ran) on any
    # system where the match legitimately fails (non-X3D CPU, or a kernel
    # without the driver). `find ... -print -quit` has the same landmine:
    # it happens to exit 0 when the search root exists but nothing
    # matches, but exits 1 (killing the script the same way) if the
    # search root itself doesn't exist yet — e.g. a race against the
    # driver being probed at boot.
    local d
    for d in /sys/bus/platform/drivers/amd_x3d_vcache/*/; do
        if [[ -f "${d}amd_x3d_mode" ]]; then
            printf '%s' "${d}amd_x3d_mode"
            return 0
        fi
    done
    return 0
}

tune_x3d_vcache() {
    [[ "${SET_X3D_VCACHE_MODE}" == "1" ]] || return 0
    [[ "${CPU_VENDOR}" != "intel" ]] || return 0   # AMD-only driver
    local path
    path="$(find_x3d_vcache_path)"
    if [[ -z "${path}" ]]; then
        if [[ -d /sys/bus/platform/drivers/amd_x3d_vcache ]]; then
            warn "amd_x3d_vcache: driver present but no */amd_x3d_mode yet (module not probed?), skipped"
        else
            log_debug "amd_x3d_vcache driver not present (non-X3D CPU or unsupported kernel), skipped"
        fi
        return 0
    fi
    log_debug "amd_x3d_vcache path resolved: ${path}"
    if (( DRY_RUN )); then dry "would set [amd_x3d_vcache.amd_x3d_mode] -> ${X3D_VCACHE_MODE}"; return 0; fi

    # amd_x3d_mode writes trigger a synchronous ACPI _DSM call inside the
    # kernel driver (unlike every other sysfs write in this script, which
    # is a plain, instant procfs/sysfs store). On some BIOS/AGESA versions
    # this _DSM call can stall indefinitely, hanging the whole PRE run
    # (and, on POST, the whole restore) with no error and no timeout of
    # its own. Guard both the read and the write with `timeout` so a
    # firmware stall degrades to a skipped/warned tunable instead of a
    # frozen game-tune run.
    local save_file; save_file="$(_save_name "${path}")"
    if [[ ! -f "${save_file}" ]]; then
        local current_val
        if ! current_val="$(timeout 3 cat "${path}" 2>/dev/null)"; then
            warn "Read timed out or failed, skipped: amd_x3d_vcache.amd_x3d_mode"
            return 0
        fi
        if [[ -z "${current_val}" ]]; then
            warn "Read empty value, not saving (will retry next run): amd_x3d_vcache.amd_x3d_mode"
            return 0
        fi
        _state_write "${save_file}" "${current_val}" || return 0
        log_debug "Saved       [amd_x3d_vcache.amd_x3d_mode]: '${current_val}'"
    else
        log_debug "Already saved [amd_x3d_vcache.amd_x3d_mode], not overwriting"
    fi

    if ! timeout 3 bash -c "printf '%s' \"\$1\" > \"\$2\"" _ "${X3D_VCACHE_MODE}" "${path}" 2>/dev/null; then
        warn "Write timed out (possible ACPI _DSM stall) or failed: amd_x3d_vcache.amd_x3d_mode = ${X3D_VCACHE_MODE}"
        return 0
    fi
    log "Set         [amd_x3d_vcache.amd_x3d_mode]: ${X3D_VCACHE_MODE}"
}

restore_x3d_vcache() {
    local path
    path="$(find_x3d_vcache_path)"
    [[ -n "${path}" ]] || return 0

    local save_file; save_file="$(_save_name "${path}")"
    [[ -f "${save_file}" ]] || return 0
    if [[ -L "${save_file}" ]]; then
        err "SECURITY: state file is a symlink, not read: ${save_file}"
        rm -f "${save_file}"
        return 0
    fi

    local saved_val
    saved_val="$(cat "${save_file}")"

    # Same stall risk as tune_x3d_vcache() above — guard the restore write
    # too, so a firmware hang on POST can't block the rest of the restore
    # sequence (governor, ASPM, CCD teardown, ...) forever.
    if ! timeout 3 bash -c "printf '%s' \"\$1\" > \"\$2\"" _ "${saved_val}" "${path}" 2>/dev/null; then
        warn "Restore timed out (possible ACPI _DSM stall) or failed: amd_x3d_vcache.amd_x3d_mode = '${saved_val}'"
    else
        log "Restored    [amd_x3d_vcache.amd_x3d_mode]: '${saved_val}'"
    fi
    rm -f "${save_file}"
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

# =============================================================================
# Proton / Wine sysctls, IRQ affinity, GPU, network, storage
# =============================================================================

# Raise-only variant of tune_param (vm.max_map_count must never be LOWERED).
tune_param_atleast() {
    local path="$1" min="$2" desc="$3" cur=""
    if [[ -r "${path}" ]]; then
        cur="$(cat "${path}" 2>/dev/null || true)"
        if [[ "${cur}" =~ ^[0-9]+$ ]] && (( 10#${cur} >= min )); then
            log_debug "${desc} already ${cur} >= ${min}, left as is"
            return 0
        fi
    fi
    tune_param "${path}" "${min}" "${desc}"
}

# dirty_ratio/dirty_bytes (and the background pair) are mutually exclusive:
# writing one zeroes the other. So all four originals are saved, and restore
# writes back whichever of each pair was actually in use.
tune_vm_dirty() {
    [[ "${SET_VM_DIRTY}" == "1" ]] || return 0
    local v=/proc/sys/vm p sf
    for p in dirty_ratio dirty_bytes dirty_background_ratio dirty_background_bytes; do
        if [[ ! -w "${v}/${p}" ]]; then
            warn "vm.${p} not writable — dirty limits skipped"
            _stat_skipped
            return 0
        fi
    done
    if (( VM_DIRTY_BACKGROUND_BYTES >= VM_DIRTY_BYTES )); then
        warn "VM_DIRTY_BACKGROUND_BYTES must be < VM_DIRTY_BYTES — dirty limits skipped"
        return 0
    fi
    local cur_bytes
    cur_bytes="$(cat "${v}/dirty_bytes" 2>/dev/null || echo 0)"
    if [[ "${cur_bytes}" =~ ^[0-9]+$ ]] && (( cur_bytes > 0 && cur_bytes <= VM_DIRTY_BYTES )); then
        log_debug "vm.dirty_bytes already ${cur_bytes} <= ${VM_DIRTY_BYTES}, left as is"
        return 0
    fi
    if (( DRY_RUN )); then
        dry "would set [vm.dirty_bytes]: ${VM_DIRTY_BYTES}, [vm.dirty_background_bytes]: ${VM_DIRTY_BACKGROUND_BYTES}"
        return 0
    fi
    for p in dirty_ratio dirty_bytes dirty_background_ratio dirty_background_bytes; do
        sf="$(_save_name "${v}/${p}")"
        [[ -f "${sf}" ]] || _state_write "${sf}" "$(cat "${v}/${p}")" || return 0
    done
    if printf '%s' "${VM_DIRTY_BYTES}" > "${v}/dirty_bytes" 2>/dev/null &&
       printf '%s' "${VM_DIRTY_BACKGROUND_BYTES}" > "${v}/dirty_background_bytes" 2>/dev/null; then
        log "Set         [vm.dirty_bytes / dirty_background_bytes]: ${VM_DIRTY_BYTES} / ${VM_DIRTY_BACKGROUND_BYTES}"
        _stat_applied
    else
        warn "Write failed: vm.dirty_* limits"
        _stat_failed
    fi
}

restore_vm_dirty() {
    local v=/proc/sys/vm pair ratio bytes sr sb vr vb
    for pair in "dirty_ratio dirty_bytes" "dirty_background_ratio dirty_background_bytes"; do
        read -r ratio bytes <<< "${pair}"
        sr="$(_save_name "${v}/${ratio}")"
        sb="$(_save_name "${v}/${bytes}")"
        [[ -f "${sr}" && -f "${sb}" ]] || { rm -f "${sr}" "${sb}"; continue; }
        vr="$(cat "${sr}")"
        vb="$(cat "${sb}")"
        if [[ "${vb}" =~ ^[0-9]+$ ]] && (( vb > 0 )); then
            printf '%s' "${vb}" > "${v}/${bytes}" 2>/dev/null || warn "Restore failed: vm.${bytes}"
            log "Restored    [vm.${bytes}]: '${vb}'"
        else
            printf '%s' "${vr}" > "${v}/${ratio}" 2>/dev/null || warn "Restore failed: vm.${ratio}"
            log "Restored    [vm.${ratio}]: '${vr}'"
        fi
        rm -f "${sr}" "${sb}"
    done
}

# --- IRQ affinity ------------------------------------------------------------------
_irq_class_wanted() {
    local c=""
    case "$1" in
        0x03*)   c="gpu" ;;
        0x0108*) c="nvme" ;;
        0x02*)   c="net" ;;
        0x0c03*) c="usb" ;;
        0x04*)   c="audio" ;;
        *) return 1 ;;
    esac
    [[ " ${IRQ_AFFINITY_CLASSES} " == *" ${c} "* ]]
}

# Save the original affinity, write the new one. Managed IRQs (NVMe queues...)
# refuse the write: the save file is dropped again and 1 is returned.
_move_one_irq() {
    local irq="$1" target="$2"
    local path="/proc/irq/${irq}/smp_affinity_list" sf cur had=0
    [[ -w "${path}" ]] || return 1
    sf="$(_save_name "${path}")"
    if [[ -f "${sf}" ]]; then
        had=1
    else
        cur="$(cat "${path}" 2>/dev/null)" || return 1
        [[ -n "${cur}" ]] || return 1
        _state_write "${sf}" "${cur}" || return 1
    fi
    if ! printf '%s' "${target}" > "${path}" 2>/dev/null; then
        if (( had == 0 )); then rm -f "${sf}"; fi
        return 1
    fi
    return 0
}

tune_irq_affinity() {
    [[ "${SET_IRQ_AFFINITY}" == "1" ]] || return 0
    local target="${IRQ_AFFINITY_CPUS}"
    if [[ -z "${target}" ]]; then
        if detect_ccx_groups; then target="${CCX_GROUPS[1]}"; fi
    fi
    if [[ -z "${target}" ]]; then
        warn "IRQ affinity: no target CPUs (no second CCX/CCD group and IRQ_AFFINITY_CPUS unset) — skipped"
        return 0
    fi
    if pgrep -x irqbalance &>/dev/null; then
        warn "IRQ affinity: irqbalance is running and may undo these changes (stop it, or ban the IRQs)."
    fi
    local dev class f irq moved=0 skipped=0
    local -a irqs
    for dev in /sys/bus/pci/devices/*; do
        [[ -d "${dev}" ]] || continue
        class="$(cat "${dev}/class" 2>/dev/null)" || continue
        _irq_class_wanted "${class}" || continue
        irqs=()
        if [[ -d "${dev}/msi_irqs" ]]; then
            for f in "${dev}"/msi_irqs/*; do
                if [[ -e "${f}" ]]; then irqs+=("${f##*/}"); fi
            done
        elif [[ -r "${dev}/irq" ]]; then
            irq=""
            read -r irq < "${dev}/irq" || true
            if [[ "${irq}" =~ ^[1-9][0-9]*$ ]]; then irqs+=("${irq}"); fi
        fi
        for irq in ${irqs[@]+"${irqs[@]}"}; do
            if (( DRY_RUN )); then
                dry "would move IRQ ${irq} (${dev##*/}, ${class}) -> CPUs ${target}"
                continue
            fi
            if _move_one_irq "${irq}" "${target}"; then
                moved=$((moved + 1))
            else
                skipped=$((skipped + 1))
            fi
        done
    done
    if (( DRY_RUN == 0 )); then
        log "IRQ affinity: ${moved} IRQ(s) moved to CPUs ${target}, ${skipped} not movable (managed/unsupported)"
        STAT_APPLIED=$((STAT_APPLIED + moved))
        STAT_SKIPPED=$((STAT_SKIPPED + skipped))
    fi
}

restore_irq_affinity() {
    local f irq n=0
    for f in "${STATE_DIR}"/_proc_irq_*_smp_affinity_list; do
        [[ -f "${f}" ]] || continue
        irq="${f##*/_proc_irq_}"
        irq="${irq%_smp_affinity_list}"
        if [[ ! "${irq}" =~ ^[0-9]+$ ]]; then rm -f "${f}"; continue; fi
        restore_param "/proc/irq/${irq}/smp_affinity_list" "irq.${irq}"
        n=$((n + 1))
    done
    if (( n > 0 )); then log "IRQ affinity restored for ${n} IRQ(s)"; fi
}

# --- GPU --------------------------------------------------------------------------
NVIDIA_PM_FILE_NAME="nvidia_persistence"

tune_nvidia_persistence() {
    [[ "${SET_NVIDIA_PERSISTENCE}" == "1" ]] || return 0
    if ! command -v nvidia-smi &>/dev/null; then
        log_debug "nvidia-smi not found, persistence mode skipped"
        return 0
    fi
    local out idx mode save="${STATE_DIR}/${NVIDIA_PM_FILE_NAME}"
    out="$(timeout 5 nvidia-smi --query-gpu=index,persistence_mode --format=csv,noheader 2>/dev/null)" || {
        warn "nvidia-smi query failed/timed out — persistence mode skipped"
        return 0
    }
    while IFS=, read -r idx mode; do
        idx="${idx//[[:space:]]/}"
        mode="${mode//[[:space:]]/}"
        [[ "${idx}" =~ ^[0-9]+$ && "${mode}" == "Disabled" ]] || continue
        if (( DRY_RUN )); then dry "would enable persistence mode on GPU ${idx}"; continue; fi
        if [[ -L "${save}" ]]; then err "SECURITY: ${save} is a symlink"; return 0; fi
        if timeout 5 nvidia-smi -i "${idx}" -pm 1 &>/dev/null; then
            echo "${idx}" >> "${save}"
            log "Set         [nvidia.persistence.gpu${idx}]: Enabled"
            _stat_applied
        else
            warn "Could not enable persistence mode on GPU ${idx}"
            _stat_failed
        fi
    done <<< "${out}"
}

restore_nvidia_persistence() {
    local save="${STATE_DIR}/${NVIDIA_PM_FILE_NAME}" idx
    [[ -f "${save}" && ! -L "${save}" ]] || { rm -f "${save}"; return 0; }
    if command -v nvidia-smi &>/dev/null; then
        while read -r idx; do
            [[ "${idx}" =~ ^[0-9]+$ ]] || continue
            if timeout 5 nvidia-smi -i "${idx}" -pm 0 &>/dev/null; then
                log "Restored    [nvidia.persistence.gpu${idx}]: Disabled"
            else
                warn "Could not restore persistence mode on GPU ${idx}"
            fi
        done < "${save}"
    fi
    rm -f "${save}"
}

tune_amdgpu_perf() {
    [[ "${SET_AMDGPU_PERF_LEVEL}" == "1" ]] || return 0
    local f card
    for f in /sys/class/drm/card*/device/power_dpm_force_performance_level; do
        [[ -f "${f}" ]] || continue
        card="${f#/sys/class/drm/}"; card="${card%%/*}"
        tune_param "${f}" "${AMDGPU_PERF_LEVEL}" "amdgpu.perf_level.${card}"
    done
}

restore_amdgpu_perf() {
    local f card
    for f in /sys/class/drm/card*/device/power_dpm_force_performance_level; do
        [[ -f "${f}" ]] || continue
        card="${f#/sys/class/drm/}"; card="${card%%/*}"
        restore_param "${f}" "amdgpu.perf_level.${card}"
    done
}

tune_gpu()    { tune_nvidia_persistence; tune_amdgpu_perf; }
restore_gpu() { restore_nvidia_persistence; restore_amdgpu_perf; }

# --- Wi-Fi power save / NVMe I/O scheduler --------------------------------------------
WIFI_PS_FILE_NAME="wifi_powersave"

tune_wifi_powersave() {
    [[ "${SET_WIFI_POWERSAVE}" == "1" ]] || return 0
    if ! command -v iw &>/dev/null; then
        log_debug "iw not found, Wi-Fi power save skipped"
        return 0
    fi
    local w ifc cur save="${STATE_DIR}/${WIFI_PS_FILE_NAME}"
    for w in /sys/class/net/*/wireless; do
        [[ -d "${w}" ]] || continue
        ifc="${w%/wireless}"; ifc="${ifc##*/}"
        [[ "${ifc}" =~ ^[A-Za-z0-9_.:-]+$ ]] || continue
        cur="$(iw dev "${ifc}" get power_save 2>/dev/null)" || continue
        [[ "${cur}" == *"Power save: on"* ]] || continue
        if (( DRY_RUN )); then dry "would turn Wi-Fi power save off on ${ifc}"; continue; fi
        if [[ -L "${save}" ]]; then err "SECURITY: ${save} is a symlink"; return 0; fi
        if iw dev "${ifc}" set power_save off &>/dev/null; then
            echo "${ifc}" >> "${save}"
            log "Set         [wifi.power_save.${ifc}]: off"
            _stat_applied
        else
            warn "Could not turn Wi-Fi power save off on ${ifc}"
            _stat_failed
        fi
    done
}

restore_wifi_powersave() {
    local save="${STATE_DIR}/${WIFI_PS_FILE_NAME}" ifc
    [[ -f "${save}" && ! -L "${save}" ]] || { rm -f "${save}"; return 0; }
    if command -v iw &>/dev/null; then
        while read -r ifc; do
            [[ "${ifc}" =~ ^[A-Za-z0-9_.:-]+$ ]] || continue
            if iw dev "${ifc}" set power_save on &>/dev/null; then
                log "Restored    [wifi.power_save.${ifc}]: on"
            else
                warn "Could not restore Wi-Fi power save on ${ifc}"
            fi
        done < "${save}"
    fi
    rm -f "${save}"
}

tune_nvme_sched() {
    [[ "${SET_NVME_SCHED}" == "1" ]] || return 0
    local f dev raw
    for f in /sys/block/nvme*n*/queue/scheduler; do
        [[ -f "${f}" ]] || continue
        dev="${f#/sys/block/}"; dev="${dev%%/*}"
        raw="$(cat "${f}" 2>/dev/null || true)"
        raw="${raw//[\[\]]/}"
        if [[ " ${raw} " != *" ${NVME_IO_SCHEDULER} "* ]]; then
            log_debug "iosched.${dev}: '${NVME_IO_SCHEDULER}' not offered by this kernel/device, skipped"
            continue
        fi
        tune_choice_param "${f}" "${NVME_IO_SCHEDULER}" "iosched.${dev}"
    done
}

restore_nvme_sched() {
    local f dev
    for f in /sys/block/nvme*n*/queue/scheduler; do
        [[ -f "${f}" ]] || continue
        dev="${f#/sys/block/}"; dev="${dev%%/*}"
        restore_param "${f}" "iosched.${dev}"
    done
}

# --- CCD/CCX core isolation ----------------------------------------------------
readonly CGROUP_V2_ROOT="/sys/fs/cgroup"
readonly CCD_GOOD_GROUP="theGood"
readonly CCD_UGLY_GROUP="theUgly"
CCD_AVAILABLE=0   # set by detect_ccx_groups()

# N-CCX-aware topology detection. Returns 1 if only a single CCX/CCD is found
# (not an error) — the caller uses this as a "skip silently" signal.
CCX_LABEL_GOOD="CCX0 (CCD0 - performance)"
CCX_LABEL_SYS="CCX1 (CCD1 - system)"
CCX_GROUPS=()

# Number of CPUs in a cpulist string ("0-7,16-23" -> 16)
_cpulist_count() {
    local list="$1" part a b n=0
    local IFS=','
    for part in ${list}; do
        if [[ "${part}" == *-* ]]; then
            a="${part%-*}"; b="${part#*-}"
            n=$(( n + 10#${b} - 10#${a} + 1 ))
        elif [[ -n "${part}" ]]; then
            n=$(( n + 1 ))
        fi
    done
    echo "${n}"
}

# Intel hybrid (Alder Lake and newer): P-cores = performance group, E-cores
# (+ LP-E cores on Meteor Lake and newer) = system group. Returns 1 when the
# CPU is not hybrid or the P-cores expose too few threads.
detect_hybrid_groups() {
    local pf="/sys/devices/cpu_core/cpus" ef="/sys/devices/cpu_atom/cpus" lf="/sys/devices/cpu_lowpower/cpus"
    local p e l pn
    [[ -r "${pf}" && -r "${ef}" ]] || return 1
    p="$(cat "${pf}" 2>/dev/null)" || return 1
    e="$(cat "${ef}" 2>/dev/null)" || return 1
    [[ -n "${p}" && -n "${e}" ]] || return 1
    if [[ -r "${lf}" ]]; then
        l="$(cat "${lf}" 2>/dev/null || true)"
        if [[ -n "${l}" ]]; then e="${e},${l}"; fi
    fi
    pn="$(_cpulist_count "${p}")"
    if (( pn < HYBRID_MIN_P_THREADS )); then
        warn "Intel hybrid: P-cores expose only ${pn} threads (< HYBRID_MIN_P_THREADS=${HYBRID_MIN_P_THREADS}) — P/E isolation skipped."
        return 1
    fi
    CCX_GROUPS=("${p}" "${e}")
    CCX_LABEL_GOOD="P-cores (performance)"
    CCX_LABEL_SYS="E-cores (system)"
    return 0
}

detect_ccx_groups() {
    if [[ "${CPU_VENDOR}" == "intel" && "${HYBRID_CORE_ISOLATION}" == "1" ]]; then
        if detect_hybrid_groups; then return 0; fi
    fi
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
    if [[ "${partition_type}" != "member" ]]; then
        echo "${partition_type}" > "${dir}/cpuset.cpus.partition" 2>/dev/null || \
            warn "Failed to write cpuset.cpus.partition: ${name}"
    fi

    local effective="" pstate=""
    [[ -r "${dir}/cpuset.cpus.effective" ]] && effective="$(cat "${dir}/cpuset.cpus.effective" 2>/dev/null)"
    [[ -r "${dir}/cpuset.cpus.partition" ]] && pstate="$(cat "${dir}/cpuset.cpus.partition" 2>/dev/null)"
    log "${name} effective cpus: ${effective:-<none>} (partition: ${pstate:-n/a})"

    # A partition the kernel refused shows up as "root invalid"/"isolated
    # invalid". The cgroup still exists and still looks configured, but the
    # CPUs are NOT exclusively owned — i.e. isolation silently does nothing.
    # Report it loudly instead of pretending everything worked.
    if [[ "${pstate}" == *invalid* ]]; then
        warn "${name}: cpuset partition is INVALID ('${pstate}') — CPUs are not exclusively owned,"
        warn "  so processes outside ${name} can still be scheduled on ${cpus}."
        warn "  Most common cause: another cgroup already claims these CPUs exclusively, or the"
        warn "  root cgroup would be left with no CPUs. Try CCD_UGLY_PARTITION_TYPE=member."
        return 2
    fi
    # Empty effective set means every task in this group would be unrunnable.
    if [[ -z "${effective}" ]]; then
        warn "${name}: effective cpuset is empty — not usable."
        return 2
    fi
    return 0
}

# Read a PID's cgroup-v2 path (e.g. "/theGood"). Empty on failure.
_pid_cgroup_path() {
    local pid="$1" line
    while IFS= read -r line; do
        if [[ "${line}" == 0::* ]]; then
            printf '%s' "${line#0::}"
            return 0
        fi
    done < "/proc/${pid}/cgroup" 2>/dev/null
    return 1
}

# Build a PID -> PPID map for the whole system in ONE pass over /proc.
# The previous implementation forked `pgrep -P <pid>` once per PID, i.e.
# hundreds of forks per monitor pass, and the cost grew with the size of the
# game's process tree — the sweep itself became a jitter source over time.
declare -gA PPID_MAP=()
declare -gA CHILDREN_MAP=()
build_process_map() {
    PPID_MAP=()
    CHILDREN_MAP=()
    local stat_file line pid after ppid
    for stat_file in /proc/[0-9]*/stat; do
        read -r line < "${stat_file}" 2>/dev/null || continue
        [[ -n "${line}" ]] || continue
        # Format: "<pid> (<comm>) <state> <ppid> ..." — comm may contain
        # spaces and parentheses, so cut at the LAST ") " rather than
        # splitting on whitespace. Pure parameter expansion: no forks.
        pid="${line%% *}"
        after="${line##*) }"      # "<state> <ppid> ..."
        after="${after#* }"       # "<ppid> ..."
        ppid="${after%% *}"
        [[ "${ppid}" =~ ^[0-9]+$ ]] || continue
        PPID_MAP["${pid}"]="${ppid}"
        CHILDREN_MAP["${ppid}"]+="${pid} "
    done
}

get_process_tree() {
    local root_pid="$1"
    local -a queue=("${root_pid}") result=()
    local -A seen=()
    local pid child

    (( ${#PPID_MAP[@]} == 0 )) && build_process_map

    while (( ${#queue[@]} > 0 )); do
        pid="${queue[0]}"
        queue=("${queue[@]:1}")
        [[ -n "${seen[$pid]:-}" ]] && continue
        seen[$pid]=1
        result+=("${pid}")
        for child in ${CHILDREN_MAP[$pid]:-}; do
            [[ -n "${child}" ]] && queue+=("${child}")
        done
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

# Move PIDs into a cgroup, SKIPPING any process that is already there.
#
# This skip is the single most important fix in v4.2. Writing a PID to
# cgroup.procs is never free even when it is a no-op migration: the kernel
# takes cgroup_threadgroup_rwsem for WRITE first (a percpu_rwsem, so
# rcu_sync_enter -> synchronize_rcu), which blocks every fork/exec/clone on
# the machine for as long as it is held, and only afterwards notices that
# source and destination csets are identical. The old code re-wrote every
# tracked PID on every 2s pass, so the number of these global stalls grew
# with the game's process tree — smooth at the start of a session, steadily
# worse a few minutes in, and completely invisible to a present-to-present
# frametime graph because the stall lands on whatever thread happens to be
# forking or waiting on the lock.
#
# $1 = absolute path to the destination cgroup directory
move_pid_list_to_group() {
    local group_dir="$1"; shift
    local group_procs="${group_dir}/cgroup.procs"
    local rel="${group_dir#${CGROUP_V2_ROOT}}"
    [[ -z "${rel}" ]] && rel="/"
    local success=0 fail=0 skipped=0 pid cur
    for pid in "$@"; do
        [[ -z "${pid}" ]] && continue
        [[ -d "/proc/${pid}" ]] || continue
        if is_pid_protected_by_name "${pid}"; then
            continue
        fi
        cur="$(_pid_cgroup_path "${pid}" || true)"
        if [[ "${cur}" == "${rel}" ]]; then
            skipped=$((skipped + 1))
            continue
        fi
        if echo "${pid}" > "${group_procs}" 2>/dev/null; then
            success=$((success + 1))
        else
            fail=$((fail + 1))
        fi
    done
    echo "${success} ${fail} ${skipped}"
}

move_launcher_tree_once() {
    local good_dir="$1"
    local -A all_pids=()
    local pid p
    local -a patterns=()
    read -r -a patterns <<< "${CCD_LAUNCHER} ${CCD_EXTRA_GOOD_PROCS}"

    # One /proc pass for the whole sweep, reused by the tree walk below.
    build_process_map

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
        for child in ${CHILDREN_MAP[$pid]:-}; do
            if [[ -n "${child}" && -z "${all_pids[$child]:-}" ]]; then
                all_pids[$child]=1
                queue+=("${child}")   # newly discovered child enters the scan loop
            fi
        done
    done

    # Step 4: Persist the live process list for the next monitoring iteration
    printf '%s\n' "${!all_pids[@]}" > "${tracked_file}"

    # Step 5: Move only the processes that are not already in the group
    local s f sk
    read -r s f sk <<< "$(move_pid_list_to_group "${good_dir}" "${!all_pids[@]}")"
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
    # Only track origins discovered in THIS sweep: with the session watcher
    # running every few seconds, keeping the map across sweeps would make
    # append_pid_origins() re-read a growing file forever.
    CCD_PID_ORIGIN=()
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
    (( ${#CCD_PID_ORIGIN[@]} > 0 )) && append_pid_origins
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
    if (( DRY_RUN )); then
        dry "would isolate: ${CCX_LABEL_GOOD} = ${CCX_GROUPS[0]}; ${CCX_LABEL_SYS} = ${CCX_GROUPS[1]}"
        return 0
    fi

    if ! mount | grep -q "on ${CGROUP_V2_ROOT} type cgroup2"; then
        warn "cgroup v2 is not mounted on ${CGROUP_V2_ROOT} — CCD isolation skipped."
        return 0
    fi

    log "--- CCD/CCX core isolation ---"
    refresh_protected_session_pids

    # Clean up leftover cgroups from a previous failed restore
    cleanup_stale_ccd_cgroups

    local ccx0="${CCX_GROUPS[0]}" ccx1="${CCX_GROUPS[1]}"
    log "  ${CCX_LABEL_GOOD}: ${ccx0}"
    log "  ${CCX_LABEL_SYS}: ${ccx1}"

    if ! grep -qw "cpuset" "${CGROUP_V2_ROOT}/cgroup.controllers"; then
        warn "Kernel does not support the cpuset controller — CCD isolation skipped."
        return 0
    fi
    if ! grep -qw "cpuset" "${CGROUP_V2_ROOT}/cgroup.subtree_control"; then
        echo "+cpuset" > "${CGROUP_V2_ROOT}/cgroup.subtree_control" 2>/dev/null || \
            warn "Failed to enable the cpuset subtree_control."
    fi

    # Create theUgly first; bail out only on a hard failure (rc 1 = mkdir/
    # write failed). rc 2 means "created but the partition is invalid", which
    # has already been warned about and is survivable.
    local rc=0
    setup_cpuset_group "${CCD_UGLY_GROUP}" "${ccx1}" "${CCD_UGLY_PARTITION_TYPE}" || rc=$?
    (( rc == 1 )) && return 0
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
    rc=0
    setup_cpuset_group "${CCD_GOOD_GROUP}" "${ccx0}" "${CCD_GOOD_PARTITION_TYPE}" || rc=$?
    if (( rc == 1 )); then
        # If theGood creation fails, remove theUgly and abort
        warn "Failed to create theGood cgroup — cleaning up theUgly"
        cleanup_stale_ccd_cgroups
        return 0
    fi
    local good_dir="${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}"

    local success fail total
    read -r success fail total <<< "$(move_launcher_tree_once "${good_dir}")"
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
            read -r s f t <<< "$(move_launcher_tree_once "${good_dir}")"
            (( s > 0 )) && log "  [watch] ${t} processes tracked (${s} newly moved)."
            # Also re-sweep the root cgroup: wine/proton workers that are
            # reparented or setsid'd away from the launcher's parent chain
            # never show up in move_launcher_tree_once's targets, and would
            # otherwise sit unconstrained in root.
            read -r rs rf <<< "$(move_root_level_procs "${ugly_dir}/cgroup.procs")"
            (( rs > 0 )) && log "  [watch] ${rs} new stray root-level process(es) moved to ${CCD_UGLY_GROUP}."
        done
        log "  Initial monitoring window finished."
    fi

    if [[ "${CCD_MONITOR_MODE}" == "session" ]]; then
        start_session_monitor "${good_dir}" "${ugly_dir}"
    fi
}

# --- Session-long CCD watcher --------------------------------------------------
# The v4.1 design only swept for CCD_MONITOR_SECONDS (30s by default) and then
# stopped. But the processes that matter most spawn LATER: launcher/store
# overlays, anti-cheat services, Wine services, background shader-cache
# workers, second-stage game executables. Anything that appears after the
# window and is reparented away from the launcher tree ends up in the root
# cgroup, unconstrained — free to be scheduled right next to the game on CCD0.
# That is a textbook "fine for the first few minutes, then it degrades"
# pattern, so the watcher now optionally runs for the whole session.
#
# The watcher itself must not become a jitter source, so it: runs at nice 19,
# lives inside theUgly (never on the game's CCD), sweeps at a low frequency,
# and — critically — closes the inherited flock file descriptor so it does not
# hold the PRE/POST lock for the entire session.
readonly MONITOR_PID_FILE_NAME=".monitor_pid"

start_session_monitor() {
    local good_dir="$1" ugly_dir="$2"
    local pid_file="${STATE_DIR}/${MONITOR_PID_FILE_NAME}"

    stop_session_monitor   # never leave two watchers running

    (
        # Release the PRE/POST lock inherited from the parent, otherwise POST
        # would block on it until the game exits and the watcher is killed.
        exec 9>&- 2>/dev/null || true
        exec 0</dev/null
        trap 'exit 0' TERM INT

        renice -n 19 -p "$$" >/dev/null 2>&1 || true
        # Put the watcher on the system CCD so its own work never lands on
        # the cores the game is using.
        echo "$$" > "${ugly_dir}/cgroup.procs" 2>/dev/null || true

        local s f t rs rf
        while :; do
            sleep "${CCD_MONITOR_INTERVAL}"
            # Stop as soon as game mode is over or the groups are gone.
            [[ -f "${REFCOUNT_FILE}" ]] || break
            [[ -d "${good_dir}" && -d "${ugly_dir}" ]] || break

            refresh_protected_session_pids
            read -r s f t <<< "$(move_launcher_tree_once "${good_dir}")"
            (( s > 0 )) && log_debug "[monitor] ${s} process(es) moved to ${CCD_GOOD_GROUP} (${t} tracked)."
            read -r rs rf <<< "$(move_root_level_procs "${ugly_dir}/cgroup.procs")"
            (( rs > 0 )) && log_debug "[monitor] ${rs} stray root-level process(es) moved to ${CCD_UGLY_GROUP}."
        done
    ) &
    local mon_pid=$!
    disown "${mon_pid}" 2>/dev/null || true
    _state_write "${pid_file}" "${mon_pid}" || true
    log "  Session watcher started (pid ${mon_pid}, every ${CCD_MONITOR_INTERVAL}s, nice 19, inside ${CCD_UGLY_GROUP})."
}

stop_session_monitor() {
    local pid_file="${STATE_DIR}/${MONITOR_PID_FILE_NAME}"
    [[ -f "${pid_file}" ]] || return 0
    local mon_pid
    mon_pid="$(cat "${pid_file}" 2>/dev/null)"
    rm -f "${pid_file}"
    [[ "${mon_pid}" =~ ^[0-9]+$ ]] || return 0
    [[ -d "/proc/${mon_pid}" ]] || return 0
    # Only kill it if it really is one of ours.
    local comm
    comm="$(cat "/proc/${mon_pid}/comm" 2>/dev/null || true)"
    [[ "${comm}" == "bash" || "${comm}" == "lutris-game-tun"* ]] || return 0
    kill -TERM "${mon_pid}" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ -d "/proc/${mon_pid}" ]] || break
        sleep 0.1
    done
    [[ -d "/proc/${mon_pid}" ]] && kill -KILL "${mon_pid}" 2>/dev/null || true
    log "  Session watcher stopped (pid ${mon_pid})."
    return 0
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

    # Stop the session watcher first — otherwise it would keep moving
    # processes back into theGood/theUgly while we are trying to empty them.
    stop_session_monitor

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

# --- Game entries / reference counter --------------------------------------------
# One small file per running game under STATE_DIR/.games/. PRE adds an entry,
# POST removes one; the restore happens when the last entry is gone. Each entry
# records an "anchor": the long-lived launcher process (the TOPMOST ancestor of
# the PRE caller whose name is CCD_LAUNCHER or starts with "lutris") plus its
# start time. If that launcher dies without POST ever running (crash, kill -9),
# the entry is recognised as stale and dropped on the next PRE — otherwise the
# counter would never reach zero again and nothing would ever be restored.
# Entries WITHOUT an anchor (launcher not found in the ancestry) are never
# auto-removed; use the RESTORE command for those.
# .refcount mirrors the entry count (the CCD session watcher polls it).
readonly REFCOUNT_FILE="${STATE_DIR}/.refcount"
readonly GAMES_DIR="${STATE_DIR}/.games"
readonly STALE_MIN_AGE=15   # seconds; never drop entries younger than this

# Process start time (field 22 of /proc/PID/stat), robust against ')' in comm
_pid_start() {
    local line
    local -a f
    line="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    line="${line##*) }"
    read -ra f <<< "${line}"
    printf '%s' "${f[19]:-}"
}

# Prints "<pid> <starttime>" of the launcher ancestor, or "0 0"
_find_anchor() {
    local pid="$1" depth=0 comm line best=""
    local -a f
    while (( pid > 1 && depth < 32 )); do
        comm="$(cat "/proc/${pid}/comm" 2>/dev/null)" || break
        if [[ "${comm}" == "${CCD_LAUNCHER}" || "${comm}" == lutris* ]]; then best="${pid}"; fi
        line="$(cat "/proc/${pid}/stat" 2>/dev/null)" || break
        line="${line##*) }"
        read -ra f <<< "${line}"
        pid="${f[1]:-0}"
        depth=$((depth + 1))
    done
    if [[ -n "${best}" ]]; then
        echo "${best} $(_pid_start "${best}" || echo 0)"
    else
        echo "0 0"
    fi
}

_games_init() {
    ensure_state_dir
    if [[ -L "${GAMES_DIR}" ]]; then
        err "SECURITY: ${GAMES_DIR} is a symlink — removed"
        rm -f "${GAMES_DIR}"
    fi
    if [[ ! -d "${GAMES_DIR}" ]]; then
        mkdir -m 0700 "${GAMES_DIR}"
        # one-time migration from the pre-4.6 plain counter
        if [[ -f "${REFCOUNT_FILE}" && ! -L "${REFCOUNT_FILE}" ]]; then
            local n i
            n="$(cat "${REFCOUNT_FILE}" 2>/dev/null || true)"
            if [[ "${n}" =~ ^[0-9]{1,4}$ ]]; then
                for ((i = 0; i < 10#${n}; i++)); do
                    printf '0 0 legacy\n' > "${GAMES_DIR}/0-legacy-${i}"
                done
            fi
        fi
    fi
}

_games_count() {
    local f n=0
    for f in "${GAMES_DIR}"/*; do
        if [[ -f "${f}" ]]; then n=$((n + 1)); fi
    done
    echo "${n}"
}

_refcount_sync() {
    local n
    n="$(_games_count)"
    if [[ -L "${REFCOUNT_FILE}" ]]; then rm -f "${REFCOUNT_FILE}"; fi
    if (( n == 0 )); then rm -f "${REFCOUNT_FILE}"; else printf '%s' "${n}" > "${REFCOUNT_FILE}"; fi
}

# Number of running games (0 if STATE_DIR or the entries don't exist)
_refcount_read() {
    local n
    if [[ -d "${GAMES_DIR}" && ! -L "${GAMES_DIR}" ]]; then
        _games_count
        return 0
    fi
    # pre-4.6 state (counter file only)
    if [[ -f "${REFCOUNT_FILE}" && ! -L "${REFCOUNT_FILE}" ]]; then
        n="$(cat "${REFCOUNT_FILE}" 2>/dev/null || true)"
        if [[ "${n}" =~ ^[0-9]+$ ]]; then echo "${n}"; return 0; fi
    fi
    echo 0
}

# Drop entries whose launcher is gone. Runs in the main shell (it logs).
sweep_stale_games() {
    [[ -d "${GAMES_DIR}" && ! -L "${GAMES_DIR}" ]] || return 0
    local f anchor start cur now mtime removed=0
    now="$(date +%s)"
    for f in "${GAMES_DIR}"/*; do
        [[ -f "${f}" ]] || continue
        anchor=""; start=""
        read -r anchor start _ < "${f}" || true
        [[ "${anchor}" =~ ^[0-9]+$ && "${start}" =~ ^[0-9]+$ ]] || continue
        (( anchor > 0 )) || continue
        mtime="$(stat -c '%Y' "${f}" 2>/dev/null || echo "${now}")"
        (( now - mtime >= STALE_MIN_AGE )) || continue
        cur="$(_pid_start "${anchor}" 2>/dev/null || true)"
        if [[ "${cur}" != "${start}" ]]; then
            rm -f "${f}"
            removed=$((removed + 1))
        fi
    done
    if (( removed > 0 )); then
        warn "Dropped ${removed} stale game entry(ies): the launcher exited without running POST (crash/kill)."
        _refcount_sync
    fi
}

# Called by PRE (in a command substitution — must stay silent): add an entry,
# print the new number of running games.
refcount_inc() {
    local profile="${1:-}" anchor start id
    _games_init
    read -r anchor start <<< "$(_find_anchor "${PPID}")"
    id="$(date +%s%N)-$$"
    printf '%s %s %s\n' "${anchor}" "${start}" "${profile:-none}" > "${GAMES_DIR}/${id}"
    _refcount_sync
    _games_count
}

# Called by POST (also silent): remove ONE entry — preferably the one from the
# same launcher, else one whose launcher is gone, else the oldest — and print
# the number of games still running.
refcount_dec() {
    local f anchor start my_anchor my_start target="" first="" cur
    if [[ -d "${GAMES_DIR}" || -f "${REFCOUNT_FILE}" ]]; then _games_init; fi
    read -r my_anchor my_start <<< "$(_find_anchor "${PPID}")"
    for f in "${GAMES_DIR}"/*; do
        [[ -f "${f}" ]] || continue
        [[ -n "${first}" ]] || first="${f}"
        anchor=""; start=""
        read -r anchor start _ < "${f}" || true
        if (( my_anchor > 0 )) && [[ "${anchor}" == "${my_anchor}" && "${start}" == "${my_start}" ]]; then
            target="${f}"
            break
        fi
    done
    if [[ -z "${target}" ]]; then
        for f in "${GAMES_DIR}"/*; do
            [[ -f "${f}" ]] || continue
            anchor=""; start=""
            read -r anchor start _ < "${f}" || true
            [[ "${anchor}" =~ ^[0-9]+$ ]] && (( anchor > 0 )) || continue
            cur="$(_pid_start "${anchor}" 2>/dev/null || true)"
            if [[ "${cur}" != "${start}" ]]; then target="${f}"; break; fi
        done
    fi
    [[ -n "${target}" ]] || target="${first}"
    if [[ -n "${target}" ]]; then rm -f "${target}"; fi
    if [[ -d "${GAMES_DIR}" ]]; then _refcount_sync; fi
    _refcount_read
}

# =============================================================================
# PRE — apply settings before the game starts
# =============================================================================
apply_game_settings() {
    local profile="${1:-}"
    log "========== ENTERING GAME MODE =========="
    ensure_state_dir
    if [[ -n "${profile}" ]] && ! valid_profile_name "${profile}"; then
        warn "Invalid profile name '${profile}' — ignored, using the global config."
        profile=""
    fi
    sweep_stale_games
    local count
    count="$(refcount_inc "${profile}")"
    if (( count > 1 )); then
        local active_profile=""
        if [[ -f "${STATE_DIR}/.profile" ]]; then
            active_profile="$(cat "${STATE_DIR}/.profile" 2>/dev/null || true)"
        fi
        if [[ "${profile}" != "${active_profile}" ]]; then
            warn "Game mode already active with profile '${active_profile:-<global>}'; requested '${profile:-<global>}' is ignored until all games exit."
        fi
        log "Reference count: ${count} (parameters already set, not rewriting)"
        log "========== GAME MODE ALREADY ACTIVE (${count} games running) =========="
        return 0
    fi
    log "Reference count: ${count} (first game — applying parameters)"

    if [[ -n "${profile}" ]]; then
        if load_profile "${profile}"; then
            _state_write "${STATE_DIR}/.profile" "${profile}" || true
        else
            warn "Profile '${profile}' could not be loaded — continuing with the global config."
        fi
    fi

    # First game — stop any watcher left behind by a crashed session and
    # reset stale CCD tracking files
    stop_session_monitor
    rm -f "${STATE_DIR}/.tracked_game_pids" "${STATE_DIR}/.ccd_pid_origin" "${STATE_DIR}/.ccd_cpuset_saved" "${STATE_DIR}/.summary"

    _apply_all
}

# DRYRUN [profile]: show what PRE would do. Writes, saves and moves nothing.
dry_run() {
    local profile="${1:-}"
    DRY_RUN=1
    dry "DRY RUN — nothing will be written, saved or moved."
    dry "CPU vendor: ${CPU_VENDOR}"
    if [[ -n "${profile}" ]]; then
        if load_profile "${profile}"; then
            dry "profile: ${profile}"
        else
            dry "profile '${profile}' could not be loaded — showing the global config"
        fi
    fi
    _apply_all
}

# The actual parameter application (shared by PRE and DRYRUN)
_apply_all() {

    log "--- VM Parameters ---"
    tune_param "/proc/sys/vm/compaction_proactiveness"        "${VM_COMPACTION_PROACTIVENESS}" "vm.compaction_proactiveness"
    tune_param "/proc/sys/vm/watermark_boost_factor"          "${VM_WATERMARK_BOOST_FACTOR}"   "vm.watermark_boost_factor"
    tune_param "/proc/sys/vm/min_free_kbytes"                 "262144"            "vm.min_free_kbytes"
    tune_param "/proc/sys/vm/watermark_scale_factor"          "50"                "vm.watermark_scale_factor"
    tune_param "/proc/sys/vm/swappiness"                      "${VM_SWAPPINESS}"  "vm.swappiness"
    tune_param "/proc/sys/vm/zone_reclaim_mode"               "0"                 "vm.zone_reclaim_mode"
    tune_param "/proc/sys/vm/page_lock_unfairness"            "1"                 "vm.page_lock_unfairness"
    # extend the vmstat update interval -> fewer periodic per-cpu timer wakeups
    tune_param "/proc/sys/vm/stat_interval"                   "${VM_STAT_INTERVAL}" "vm.stat_interval"
    # disable swap readahead -> single-page swap-in, lower latency (ideal with zram)
    tune_param "/proc/sys/vm/page-cluster"                    "0"                 "vm.page-cluster"
    if (( VM_MAX_MAP_COUNT > 0 )); then
        tune_param_atleast "/proc/sys/vm/max_map_count"       "${VM_MAX_MAP_COUNT}" "vm.max_map_count"
    fi
    tune_vm_dirty

    log "--- LRU Gen ---"
    tune_param "/sys/kernel/mm/lru_gen/enabled"               "${LRU_GEN_ENABLED}" "lru_gen.enabled"

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
    if [[ "${SET_NUMA_BALANCING}" == "1" ]]; then
        tune_param "/proc/sys/kernel/numa_balancing"          "0"       "kernel.numa_balancing"
    fi

    log "--- Scheduler (procfs) ---"
    tune_param "/proc/sys/kernel/sched_autogroup_enabled"     "1"       "sched.autogroup_enabled"
    tune_param "/proc/sys/kernel/sched_cfs_bandwidth_slice_us" "3000"   "sched.cfs_bandwidth_slice_us"

    log "--- Scheduler (debugfs) ---"
    if ensure_debugfs; then
        tune_param "/sys/kernel/debug/sched/min_base_slice_ns" "${SCHED_MIN_BASE_SLICE_NS}" "sched_debug.min_base_slice_ns"
        tune_param "/sys/kernel/debug/sched/migration_cost_ns" "${SCHED_MIGRATION_COST_NS}" "sched_debug.migration_cost_ns"
        tune_param "/sys/kernel/debug/sched/nr_migrate"        "${SCHED_NR_MIGRATE}"        "sched_debug.nr_migrate"
    else
        warn "debugfs not accessible — sched debug parameters skipped"
    fi

    log "--- CPU Governor / EPP ---"
    tune_cpu_governor
    tune_epp_boost

    if [[ "${CPU_VENDOR}" == "intel" ]]; then
        log "--- Intel (intel_pstate / EPB / ITMT) ---"
        tune_intel
    else
        log "--- AMD 3D V-Cache ---"
        tune_x3d_vcache
    fi

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

    log "--- GPU ---"
    tune_gpu

    log "--- Network / Storage ---"
    tune_wifi_powersave
    tune_nvme_sched

    apply_ccd_isolation

    log "--- IRQ affinity ---"
    tune_irq_affinity

    _print_summary
    if (( DRY_RUN )); then
        dry "DRY RUN COMPLETE"
    else
        log "========== GAME MODE ACTIVE =========="
    fi
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
    _restore_all
}

# RESTORE: force a full restore regardless of the game count (stuck state).
force_restore() {
    log "========== FORCED RESTORE =========="
    if [[ ! -d "${STATE_DIR}" ]]; then
        log "Nothing to restore (no state directory)."
        return 0
    fi
    rm -f "${GAMES_DIR}"/* "${REFCOUNT_FILE}" 2>/dev/null || true
    _restore_all
}

_restore_all() {
    log "--- RESTORE ---"
    # Re-load the profile PRE used: restore must see the same config gating.
    local saved_profile=""
    if [[ -f "${STATE_DIR}/.profile" && ! -L "${STATE_DIR}/.profile" ]]; then
        saved_profile="$(cat "${STATE_DIR}/.profile" 2>/dev/null || true)"
        if [[ -n "${saved_profile}" ]] && ! load_profile "${saved_profile}"; then
            warn "Could not re-load profile '${saved_profile}' for the restore — using the global config."
        fi
    fi

    # Unconditional: restore_ccd_isolation() returns early when no cgroups
    # exist, and a watcher must never outlive game mode.
    stop_session_monitor

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
    restore_param "/proc/sys/vm/max_map_count"                   "vm.max_map_count"
    restore_vm_dirty

    log "--- LRU Gen ---"
    restore_param "/sys/kernel/mm/lru_gen/enabled"               "lru_gen.enabled"

    log "--- Transparent HugePage ---"
    restore_param "/sys/kernel/mm/transparent_hugepage/enabled"       "thp.enabled"
    restore_param "/sys/kernel/mm/transparent_hugepage/shmem_enabled" "thp.shmem_enabled"
    restore_param "/sys/kernel/mm/transparent_hugepage/defrag"        "thp.defrag"

    log "--- Kernel / Latency ---"
    restore_param "/proc/sys/kernel/split_lock_mitigate"         "kernel.split_lock_mitigate"
    restore_param "/proc/sys/kernel/watchdog"                    "kernel.watchdog"
    restore_param "/proc/sys/kernel/numa_balancing"              "kernel.numa_balancing"

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
    restore_epp_boost

    if [[ "${CPU_VENDOR}" == "intel" ]]; then
        log "--- Intel (intel_pstate / EPB / ITMT) ---"
        restore_intel
    else
        log "--- AMD 3D V-Cache ---"
        restore_x3d_vcache
    fi

    restore_cstates

    log "--- PCIe ASPM ---"
    restore_param "/sys/module/pcie_aspm/parameters/policy"     "pcie_aspm.policy"

    log "--- Audio (HDA power save) ---"
    restore_param "/sys/module/snd_hda_intel/parameters/power_save"            "snd_hda.power_save"
    restore_param "/sys/module/snd_hda_intel/parameters/power_save_controller" "snd_hda.power_save_controller"

    log "--- PCI Latency Timer ---"
    restore_pci_latency

    log "--- GPU / Network / Storage / IRQ ---"
    restore_gpu
    restore_wifi_powersave
    restore_nvme_sched
    restore_irq_affinity

    # Clean up the state directory
    rmdir "${GAMES_DIR}" 2>/dev/null || true
    rm -f "${STATE_DIR}/.profile" "${STATE_DIR}/.summary"
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
    if [[ -d "${STATE_DIR}" ]] && [[ -n "$(ls -A "${STATE_DIR}" 2>/dev/null)" ]]; then
        local game_count
        game_count="$(_refcount_read)"
        local param_count
        param_count="$(find -P "${STATE_DIR}" -maxdepth 1 -type f -not -name '.*' | wc -l)"
        echo "Game mode: ACTIVE (${game_count} game(s) running, ${param_count} parameter(s) saved)"
        if [[ -f "${STATE_DIR}/.profile" ]]; then echo "Profile: $(cat "${STATE_DIR}/.profile" 2>/dev/null)"; fi
        local gf ga gs gp gstate
        for gf in "${GAMES_DIR}"/*; do
            [[ -f "${gf}" ]] || continue
            ga=""; gs=""; gp=""
            read -r ga gs gp < "${gf}" || true
            if [[ "${ga}" =~ ^[0-9]+$ ]] && (( ga > 0 )); then
                if [[ "$(_pid_start "${ga}" 2>/dev/null || true)" == "${gs}" ]]; then
                    gstate="launcher pid ${ga} alive"
                else
                    gstate="launcher pid ${ga} GONE (stale; dropped on next PRE)"
                fi
            else
                gstate="no launcher anchor"
            fi
            echo "  game ${gf##*/}: profile=${gp:-none}, ${gstate}"
        done
        if [[ -f "${STATE_DIR}/.summary" ]]; then echo "Last PRE: $(cat "${STATE_DIR}/.summary" 2>/dev/null)"; fi
        echo
        printf '%-55s %s\n' "PARAMETER (save file)" "ORIGINAL VALUE"
        printf '%-55s %s\n' "----------------------" "--------------"
        local f
        for f in "${STATE_DIR}"/*; do
            [[ -f "${f}" ]] || continue
            local fname
            fname="$(basename "${f}")"
            [[ "${fname}" == .* ]] && continue
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
        echo "  ${CCD_GOOD_GROUP} partition:      $(cat "${CGROUP_V2_ROOT}/${CCD_GOOD_GROUP}/cpuset.cpus.partition" 2>/dev/null)"
        echo "  ${CCD_UGLY_GROUP} partition:      $(cat "${CGROUP_V2_ROOT}/${CCD_UGLY_GROUP}/cpuset.cpus.partition" 2>/dev/null)"
        local mon_pid=""
        [[ -f "${STATE_DIR}/${MONITOR_PID_FILE_NAME}" ]] && mon_pid="$(cat "${STATE_DIR}/${MONITOR_PID_FILE_NAME}" 2>/dev/null)"
        if [[ -n "${mon_pid}" && -d "/proc/${mon_pid}" ]]; then
            echo "  session watcher:        RUNNING (pid ${mon_pid})"
        else
            echo "  session watcher:        not running"
        fi
    else
        echo "CCD isolation: OFF (may be single CCX/CCD, disabled, or game mode is not active)"
    fi
    echo
    echo "CPU vendor: ${CPU_VENDOR}"
    echo "Current values:"
    local p
    for p in /proc/sys/vm/swappiness \
             /proc/sys/kernel/split_lock_mitigate \
             /proc/sys/kernel/watchdog \
             /sys/kernel/mm/transparent_hugepage/enabled \
             /sys/module/pcie_aspm/parameters/policy \
             /sys/devices/system/cpu/cpufreq/policy0/scaling_governor \
             /sys/module/amd_pstate/parameters/epp_boost \
             /sys/devices/system/cpu/intel_pstate/status \
             /sys/devices/system/cpu/intel_pstate/no_turbo \
             /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost \
             /sys/devices/system/cpu/intel_pstate/min_perf_pct \
             /sys/devices/system/cpu/intel_pstate/max_perf_pct \
             /sys/devices/system/cpu/cpu0/power/energy_perf_bias \
             /proc/sys/kernel/sched_itmt_enabled; do
        [[ -e "${p}" ]] && printf '  %-60s %s\n' "${p}" "$(cat "${p}" 2>/dev/null)"
    done
    local x3d_path
    x3d_path="$(find_x3d_vcache_path)"
    [[ -n "${x3d_path}" ]] && printf '  %-60s %s\n' "${x3d_path}" "$(cat "${x3d_path}" 2>/dev/null)"
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

# Sourced by tests/run-tests.sh (non-root + GT_TEST_DIR only): stop here.
if [[ "${GT_TEST_MODE}" == "1" && "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 0
fi

require_root
rotate_log
load_config
detect_cpu_vendor

ACTION="${1:-}"
case "${ACTION}" in
    PRE|pre|POST|post|RESTORE|restore)
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
            PRE|pre)         apply_game_settings "${2:-}" ;;
            POST|post)       restore_game_settings ;;
            RESTORE|restore) force_restore ;;
        esac
        ;;
    DRYRUN|dryrun)
        dry_run "${2:-}"
        ;;
    STATUS|status)
        show_status
        ;;
    *)
        err "Invalid argument: '${ACTION}'. Expected: PRE [profile], POST, RESTORE, DRYRUN [profile], or STATUS"
        exit 1
        ;;
esac
