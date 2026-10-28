#!/usr/bin/env bash
# =============================================================================
# tests/run-tests.sh — dependency-free unit tests for lutris-game-tune.sh
#
# The script is SOURCED with GT_TEST_DIR set, which redirects STATE_DIR, config,
# profile dir and log into a temp dir and relaxes the "owned by root" checks to
# "owned by me". That hook only works for NON-root users, so when started as
# root this file re-executes itself as "nobody".
#
# Usage:  bash tests/run-tests.sh
# =============================================================================
# shellcheck disable=SC2034
set -euo pipefail

if (( EUID == 0 )); then
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=65534 --regid=65534 --clear-groups bash "$0" "$@"
    fi
    echo "SKIP: running as root and setpriv not available" >&2
    exit 0
fi

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
export GT_TEST_DIR="${TMP}"
mkdir -p "${TMP}/profiles"
chmod 755 "${TMP}/profiles"

# shellcheck source=../lutris-game-tune.sh
source "${HERE}/../lutris-game-tune.sh"
LOG_LEVEL=ERROR   # keep test output quiet

PASS=0; FAIL=0
t() {   # t "description" command...   (command must succeed)
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: ${desc}"; fi
}
eq() {  # eq "description" expected actual
    if [[ "$2" == "$3" ]]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $1 (expected '$2', got '$3')"; fi
}
no() {  # no "description" command...   (command must FAIL)
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then FAIL=$((FAIL + 1)); echo "FAIL: ${desc} (unexpectedly succeeded)"; else PASS=$((PASS + 1)); fi
}
newconf() { printf '%s\n' "$@" > "${TMP}/c.conf"; chmod 644 "${TMP}/c.conf"; }

# --- cpulist --------------------------------------------------------------------
eq "cpulist 0-15"       16 "$(_cpulist_count "0-15")"
eq "cpulist ranges"     16 "$(_cpulist_count "0-7,16-23")"
eq "cpulist single"      1 "$(_cpulist_count "3")"
eq "cpulist mixed"      13 "$(_cpulist_count "0-5,12-17,20")"

# --- numeric/verify helpers --------------------------------------------------------
eq "to_num dec"   7 "$(_to_num 007)"
eq "to_num hex"   7 "$(_to_num 0x0007)"
eq "to_num text"  "" "$(_to_num performance)"
printf '0x0007\n' > "${TMP}/f1"
before=${STAT_APPLIED}; _verify_applied "${TMP}/f1" 7 hexcase >/dev/null 2>&1
eq "verify: hex readback counts as applied" $((before + 1)) "${STAT_APPLIED}"
printf 'always madvise [never]\n' > "${TMP}/f2"
before=${STAT_APPLIED}; _verify_applied "${TMP}/f2" never choice >/dev/null 2>&1
eq "verify: bracketed choice matches" $((before + 1)) "${STAT_APPLIED}"
printf '5\n' > "${TMP}/f3"
before=${STAT_MISMATCH}; _verify_applied "${TMP}/f3" 9 mism >/dev/null 2>&1
eq "verify: mismatch is counted" $((before + 1)) "${STAT_MISMATCH}"

# --- tune_param / restore_param round trip --------------------------------------------
mkdir -p "${STATE_DIR}"; chmod 700 "${STATE_DIR}"
printf '60\n' > "${TMP}/swap"
tune_param "${TMP}/swap" 10 swappiness >/dev/null 2>&1
eq "tune_param writes"          10 "$(cat "${TMP}/swap")"
tune_param "${TMP}/swap" 20 swappiness >/dev/null 2>&1
restore_param "${TMP}/swap" swappiness >/dev/null 2>&1
eq "restore keeps FIRST original" 60 "$(cat "${TMP}/swap")"
printf 'always [madvise] never\n' > "${TMP}/thp"
tune_choice_param "${TMP}/thp" never thp >/dev/null 2>&1
restore_param "${TMP}/thp" thp >/dev/null 2>&1
eq "choice param restores bracketed item" madvise "$(cat "${TMP}/thp")"
printf '100\n' > "${TMP}/mm"
tune_param_atleast "${TMP}/mm" 50 mm >/dev/null 2>&1
eq "atleast never lowers" 100 "$(cat "${TMP}/mm")"
tune_param_atleast "${TMP}/mm" 500 mm >/dev/null 2>&1
eq "atleast raises" 500 "$(cat "${TMP}/mm")"
restore_param "${TMP}/mm" mm >/dev/null 2>&1
eq "atleast restores" 100 "$(cat "${TMP}/mm")"
DRY_RUN=1; printf '1\n' > "${TMP}/dry"
tune_param "${TMP}/dry" 2 dry >/dev/null 2>&1
DRY_RUN=0
eq "dry-run writes nothing" 1 "$(cat "${TMP}/dry")"
no "dry-run saves nothing" test -e "$(_save_name "${TMP}/dry")"

# --- config parser ---------------------------------------------------------------------
newconf "VM_SWAPPINESS=33" "SET_IRQ_AFFINITY=1" 'IRQ_AFFINITY_CLASSES="gpu net"' "IRQ_AFFINITY_CPUS=8-15,24-31" "NVME_IO_SCHEDULER=bfq" "INTEL_MAX_PERF_PCT=90"
load_config_file "${TMP}/c.conf" >/dev/null 2>&1
eq "conf: int key"        33 "${VM_SWAPPINESS}"
eq "conf: bool key"        1 "${SET_IRQ_AFFINITY}"
eq "conf: quoted class list" "gpu net" "${IRQ_AFFINITY_CLASSES}"
eq "conf: cpulist with comma" "8-15,24-31" "${IRQ_AFFINITY_CPUS}"
eq "conf: scheduler"     bfq "${NVME_IO_SCHEDULER}"
eq "conf: intel pct"      90 "${INTEL_MAX_PERF_PCT}"
newconf "VM_SWAPPINESS=500" "NVME_IO_SCHEDULER=cfq" "IRQ_AFFINITY_CPUS=8;rm" "IRQ_AFFINITY_CLASSES=gpu;x" "VM_DIRTY_BYTES=5" "VM_MAX_MAP_COUNT=99999999999" "AMDGPU_PERF_LEVEL=evil"
load_config_file "${TMP}/c.conf" >/dev/null 2>&1
eq "conf: out-of-range rejected"  33 "${VM_SWAPPINESS}"
eq "conf: bad scheduler rejected" bfq "${NVME_IO_SCHEDULER}"
eq "conf: bad cpulist rejected"   "8-15,24-31" "${IRQ_AFFINITY_CPUS}"
eq "conf: bad class list rejected" "gpu net" "${IRQ_AFFINITY_CLASSES}"
eq "conf: dirty bytes lower bound" 268435456 "${VM_DIRTY_BYTES}"
eq "conf: max_map_count upper bound" 2147483642 "${VM_MAX_MAP_COUNT}"
eq "conf: amdgpu level whitelist" high "${AMDGPU_PERF_LEVEL}"
newconf "VM_SWAPPINESS=44 # trailing comment"
load_config_file "${TMP}/c.conf" >/dev/null 2>&1
eq "conf: trailing comment line is rejected as a whole" 33 "${VM_SWAPPINESS}"
chmod 666 "${TMP}/c.conf"; newconf "VM_SWAPPINESS=11"; chmod 666 "${TMP}/c.conf"
rc=0; load_config_file "${TMP}/c.conf" >/dev/null 2>&1 || rc=$?
eq "conf: group/other-writable file refused (rc 3)" 3 "${rc}"
eq "conf: ...and not applied" 33 "${VM_SWAPPINESS}"
ln -sf "${TMP}/c.conf" "${TMP}/link.conf"
rc=0; load_config_file "${TMP}/link.conf" >/dev/null 2>&1 || rc=$?
eq "conf: symlink refused (rc 3)" 3 "${rc}"

# --- profiles ----------------------------------------------------------------------------
t  "profile name ok"            valid_profile_name "witcher3"
t  "profile name with dots"     valid_profile_name "a.b-c_d"
no "profile: path traversal"    valid_profile_name "../etc/passwd"
no "profile: slash"             valid_profile_name "a/b"
no "profile: leading dot"       valid_profile_name ".hidden"
no "profile: empty"             valid_profile_name ""
no "profile: too long"          valid_profile_name "$(printf 'a%.0s' {1..65})"
printf 'VM_SWAPPINESS=7\n' > "${TMP}/profiles/g1.conf"; chmod 644 "${TMP}/profiles/g1.conf"
t  "profile loads"              load_profile g1
eq "profile overrides"          7 "${VM_SWAPPINESS}"
no "missing profile fails"      load_profile nope
chmod 666 "${TMP}/profiles/g1.conf"
no "writable profile refused"   load_profile g1
chmod 644 "${TMP}/profiles/g1.conf"; chmod 777 "${TMP}/profiles"
no "writable profile dir refused" load_profile g1
chmod 755 "${TMP}/profiles"

# --- game entries / stale detection ------------------------------------------------------
CCD_LAUNCHER=bash   # make _find_anchor() find this very test shell's ancestry
start_me="$(_pid_start "$$")"
t  "pid start is numeric" test -n "${start_me}"
eq "inc: first game"  1 "$(refcount_inc one)"
eq "inc: second game" 2 "$(refcount_inc two)"
eq "mirror file"      2 "$(cat "${REFCOUNT_FILE}")"
eq "dec: one left"    1 "$(refcount_dec)"
eq "dec: none left"   0 "$(refcount_dec)"
no "mirror removed at 0" test -e "${REFCOUNT_FILE}"
eq "dec below zero is safe" 0 "$(refcount_dec)"

# stale: anchor process that is dead / reused
sleep 0 & dead=$!; wait "${dead}" 2>/dev/null || true
mkdir -p "${GAMES_DIR}"
printf '%s %s dead\n' "${dead}" 12345 > "${GAMES_DIR}/1-dead"
printf '%s %s alive\n' "$$" "${start_me}" > "${GAMES_DIR}/2-alive"
printf '0 0 anchorless\n' > "${GAMES_DIR}/3-anchorless"
touch -d '2 minutes ago' "${GAMES_DIR}"/*
sweep_stale_games >/dev/null 2>&1
no "stale entry (dead launcher) dropped"  test -e "${GAMES_DIR}/1-dead"
t  "live entry kept"                      test -e "${GAMES_DIR}/2-alive"
t  "anchorless entry kept"                test -e "${GAMES_DIR}/3-anchorless"
eq "count after sweep" 2 "$(_refcount_read)"
# young entries are never swept
printf '%s %s young\n' "${dead}" 12345 > "${GAMES_DIR}/4-young"
sweep_stale_games >/dev/null 2>&1
t  "entry younger than STALE_MIN_AGE kept" test -e "${GAMES_DIR}/4-young"
# POST prefers an entry of a dead launcher over a live one
rm -f "${GAMES_DIR}/4-young"; touch -d '2 minutes ago' "${GAMES_DIR}"/*
printf '%s %s dead2\n' "${dead}" 12345 > "${GAMES_DIR}/0-dead2"; touch -d '2 minutes ago' "${GAMES_DIR}/0-dead2"
refcount_dec >/dev/null 2>&1 || true
no "dec prefers an entry whose launcher is gone" test -e "${GAMES_DIR}/0-dead2"
t  "dec keeps the live entry"                    test -e "${GAMES_DIR}/2-alive"
# same-launcher match: an entry anchored to POST's own launcher goes first
rm -rf "${GAMES_DIR}"; mkdir -p "${GAMES_DIR}"
read -r ma ms <<< "$(_find_anchor "${PPID}")"
if (( ma > 0 )); then
    printf '0 0 other\n'      > "${GAMES_DIR}/1-other"
    printf '%s %s mine\n' "${ma}" "${ms}" > "${GAMES_DIR}/2-mine"
    refcount_dec >/dev/null 2>&1 || true
    no "dec removes the same-launcher entry" test -e "${GAMES_DIR}/2-mine"
    t  "dec leaves the other launcher's entry" test -e "${GAMES_DIR}/1-other"
fi
rm -rf "${GAMES_DIR}"; rm -f "${REFCOUNT_FILE}"
# legacy counter file migrates
printf '2' > "${REFCOUNT_FILE}"
eq "legacy counter migrated + 1" 3 "$(refcount_inc)"
rm -rf "${GAMES_DIR}"; rm -f "${REFCOUNT_FILE}"

# --- IRQ class mapping ----------------------------------------------------------------------
IRQ_AFFINITY_CLASSES="gpu nvme net usb"
t  "irq: gpu class"   _irq_class_wanted 0x030000
t  "irq: nvme class"  _irq_class_wanted 0x010802
t  "irq: net class"   _irq_class_wanted 0x020000
t  "irq: usb class"   _irq_class_wanted 0x0c0330
no "irq: audio not selected"  _irq_class_wanted 0x040300
no "irq: storage sata ignored" _irq_class_wanted 0x010601
IRQ_AFFINITY_CLASSES="audio"
t  "irq: audio when selected" _irq_class_wanted 0x040300

# --- vendor ----------------------------------------------------------------------------------
detect_cpu_vendor >/dev/null 2>&1
t  "vendor detected" test "${CPU_VENDOR}" = intel -o "${CPU_VENDOR}" = amd -o "${CPU_VENDOR}" = unknown

echo
echo "passed: ${PASS}   failed: ${FAIL}"
(( FAIL == 0 ))
