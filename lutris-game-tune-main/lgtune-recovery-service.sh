#!/usr/bin/env bash
# Run as root via OpenRC, systemd service, or supervised process.
# Continuous read-only polling until a safe, fully anchored stale session
# warrants the script's locked RECOVER transaction. Never force RESTORE.
set -euo pipefail
TUNE="${LGTUNE_COMMAND:-/usr/local/lib/lutris-game-tune/lutris-game-tune.sh}"
[[ $EUID == 0 ]] || { echo 'Recovery monitor requires root' >&2; exit 1; }
[[ -x "${TUNE}" ]] || { echo "Cannot execute: ${TUNE}" >&2; exit 1; }
while :; do
  if [[ -d /run/lutris-game-tune && ! -L /run/lutris-game-tune ]]; then
    "${TUNE}" RECOVER || true
  fi
  sleep 30
done
