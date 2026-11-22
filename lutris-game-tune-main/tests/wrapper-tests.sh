#!/usr/bin/env bash
# tests/wrapper-tests.sh — script-integrity checks of lutris-game-tune-wrapper.
# Needs root (the wrapper escalates before checking the script); skips otherwise.
#   sudo bash tests/wrapper-tests.sh
set -euo pipefail
if (( EUID != 0 )); then echo "SKIP: wrapper tests need root" >&2; exit 0; fi

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
chmod 755 "${TMP}"
S="${TMP}/tune.sh"
gcc -O2 -Wall -Wextra -Werror -DSCRIPT_PATH="\"${S}\"" -o "${TMP}/w" "${HERE}/../lutris-game-tune-wrapper.c"

PASS=0; FAIL=0
ok()  { if "${@:2}" >/dev/null 2>&1; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); echo "FAIL: $1"; fi; }
bad() { if "${@:2}" >/dev/null 2>&1; then FAIL=$((FAIL + 1)); echo "FAIL: $1 (unexpectedly ran)"; else PASS=$((PASS + 1)); fi; }

printf '#!/bin/bash\necho "ran:$*" > %q\n' "${TMP}/out" > "${TMP}/real.sh"
chmod 755 "${TMP}/real.sh"

cp "${TMP}/real.sh" "${S}"
ok  "root-owned script runs with its args" "${TMP}/w" PRE myprofile
ok  "args reach the script"                grep -qx 'ran:PRE myprofile' "${TMP}/out"
rm -f "${TMP}/out"

chmod 775 "${S}"
bad "group-writable script is refused"    "${TMP}/w" POST
chmod 755 "${S}"

rm -f "${S}"; ln -s "${TMP}/real.sh" "${S}"
bad "symlinked script is refused"         "${TMP}/w" POST
[[ ! -e "${TMP}/out" ]] && PASS=$((PASS + 1)) || { FAIL=$((FAIL + 1)); echo "FAIL: symlink target was executed"; }

echo "passed: ${PASS}   failed: ${FAIL}"
(( FAIL == 0 ))
