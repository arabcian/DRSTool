#!/usr/bin/env bash
# Install this EXACT source snapshot for a Gentoo/OpenRC user's account.
# Does not run emerge, sudo, install a setuid program, or enable Vulkan layers.
set -Eeuo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
MODE="${1:---check}"
[[ $# -le 1 && "$MODE" =~ ^--(check|install|rollback)$ ]] || {
    echo 'Usage: bash scripts/gentoo-local-install.sh [--check|--install|--rollback]' >&2
    exit 64
}
[[ $EUID -ne 0 ]] || { echo 'Run as your normal user, never as root.' >&2; exit 1; }
[[ -n ${HOME:-} && -d $HOME && ! -L $HOME ]] || { echo 'Unsafe HOME directory.' >&2; exit 1; }

PREFIX="$HOME/.local"
LIB="$PREFIX/lib"
TARGET="$LIB/drstool"
BIN="$PREFIX/bin/drstool"
DESKTOP="$PREFIX/share/applications/drstool.desktop"
ICON="$PREFIX/share/icons/hicolor/512x512/apps/drstool.png"
BACKUPS="$LIB/.drstool-backups"
PY="${DRSTOOL_PYTHON:-$(command -v python3 || :)}"
[[ -n "$PY" && -x "$PY" ]] || { echo 'Python 3 not found.' >&2; exit 1; }

check_python() {
    "$PY" - <<'PY'
import importlib.util, sys
if sys.version_info < (3, 12):
    raise SystemExit('Python >= 3.12 required')
missing = []
for mod in ('PySide6.QtWidgets', 'yaml'):
    try:
        found = importlib.util.find_spec(mod)
    except (ModuleNotFoundError, ValueError):
        found = None
    if found is None:
        missing.append(mod)
for mod in ('ruamel.yaml',):
    try:
        found = importlib.util.find_spec(mod)
    except (ModuleNotFoundError, ValueError):
        found = None
    print(f'{mod}: {"available" if found else "optional: missing"}')
print('Python:', sys.executable, sys.version.split()[0])
if missing:
    raise SystemExit('Required Python modules missing: ' + ', '.join(missing))
PY
}

check_files() {
    for name in DRSTool.py drstool_launch.py drstool_gui/app.py drstool_core/models.py \
        assets/drstool.png data/drstool.desktop; do
        [[ -f "$SRC/$name" ]] || { echo "Source incomplete: $name" >&2; return 1; }
    done
    DRSTOOL_SOURCE="$SRC" "$PY" - <<'PYCODE'
import ast, os
from pathlib import Path
source = Path(os.environ['DRSTOOL_SOURCE'])
for path in (source / 'DRSTool.py', *source.joinpath('drstool_core').rglob('*.py'),
             *source.joinpath('drstool_gui').rglob('*.py')):
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
PYCODE
}

preflight() {
    echo "DRSTool source: $SRC"
    echo "Destination: $TARGET"
    echo 'Only GUI will be installed; FLM and privileged tuner remain opt-in.'
    check_files || return 1
    echo '--- Python dependencies ---'
    check_python || {
        echo 'Gentoo: emerge -av "dev-python/pyside[gui,widgets]" dev-python/pyyaml dev-python/ruamel-yaml' >&2
        echo 'Check your active Python target matches the PySide6 ebuild.' >&2
        return 2
    }
    if [[ -e "$TARGET" || -L "$TARGET" ]]; then
        [[ -d "$TARGET" && ! -L "$TARGET" && -f "$TARGET/install-manifest.txt" ]] || {
            echo 'Existing installation is unmanaged; refusing to overwrite it.' >&2
            return 1
        }
        grep -Fxq 'scope=user' "$TARGET/install-manifest.txt" || {
            echo 'Existing installation is not marked as a user install.' >&2
            return 1
        }
        grep -Fxq "prefix=$PREFIX" "$TARGET/install-manifest.txt" || {
            echo 'Existing installation prefix does not match HOME.' >&2
            return 1
        }
    else
        [[ ! -e "$BIN" && ! -L "$BIN" ]] || {
            echo 'Another launcher already exists at ~/.local/bin/drstool. Refusing overwrite.' >&2
            return 1
        }
    fi
    for f in "$BIN" "$DESKTOP" "$ICON"; do
        [[ ! -L "$f" ]] || { echo "Symlink in install destination: $f" >&2; return 1; }
    done
    echo 'Preflight OK. No system settings have changed.'
}

if [[ "$MODE" == --check ]]; then
    preflight
    exit $?
fi

[[ "$MODE" == --rollback ]] || preflight
mkdir -p -- "$LIB" "$BACKUPS"
[[ ! -L "$BACKUPS" ]] || { echo 'Backup directory is a symlink.' >&2; exit 1; }

# Snapshot desktop integration, so a rollback restores the old launcher and icon.
snapshot_files() {
    local snapshot="$1"
    mkdir -p -- "$snapshot"
    [[ ! -e "$TARGET" ]] || mv -- "$TARGET" "$snapshot/app"
    [[ ! -e "$BIN" ]] || cp -a -- "$BIN" "$snapshot/launcher"
    [[ ! -e "$DESKTOP" ]] || cp -a -- "$DESKTOP" "$snapshot/desktop"
    [[ ! -e "$ICON" ]] || cp -a -- "$ICON" "$snapshot/icon"
}
restore_files() {
    local snapshot="$1"
    [[ ! -e "$TARGET" ]] || rm -rf -- "$TARGET"
    if [[ -d "$snapshot/app" ]]; then mv -- "$snapshot/app" "$TARGET"; fi
    local source dest
    for kind in launcher desktop icon; do
        source="$snapshot/$kind"
        case "$kind" in launcher) dest="$BIN";;desktop) dest="$DESKTOP";;icon) dest="$ICON";;esac
        if [[ -f "$source" ]]; then cp -a -- "$source" "$dest"; else rm -f -- "$dest"; fi
    done
}

if [[ "$MODE" == --rollback ]]; then
    shopt -s nullglob
    candidates=("$BACKUPS"/*)
    (( ${#candidates[@]} )) || { echo 'No saved release to roll back to.' >&2; exit 1; }
    snapshot="${candidates[-1]}"
    [[ -d "$snapshot" && ! -L "$snapshot" && -d "$snapshot/app" ]] || {
        echo 'Latest backup is not a complete managed install.' >&2; exit 1;
    }
    [[ -d "$TARGET" && -f "$TARGET/install-manifest.txt" && ! -L "$TARGET" ]] || {
        echo 'Active installation is unmanaged; rollback aborted.' >&2; exit 1;
    }
    current="$BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-replaced-$$"
    mkdir -p "$current"
    snapshot_files "$current"
    restore_files "$snapshot"
    rmdir "$snapshot" 2>/dev/null || true
    echo "Restored previous release. Replaced release backed up in: $current"
    exit 0
fi

stage="$(mktemp -d "$LIB/.drstool-stage.XXXXXXXX")"
snapshot="$BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-$$"
rolled=0
finish() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then
        if (( rolled )); then
            restore_files "$snapshot"
            echo 'Installation failed; previous managed release was restored.' >&2
        fi
    fi
    [[ ! -e "$stage" ]] || rm -rf -- "$stage"
    exit "$status"
}
trap finish EXIT

for f in DRSTool.py drstool_launch.py drstool_core drstool_gui assets \
    vk-flip-meter-main lutris-game-tune-main src LICENSE; do
    cp -a -- "$SRC/$f" "$stage/"
done
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf -- {} +
"$PY" -m compileall -q "$stage/DRSTool.py" "$stage/drstool_core" "$stage/drstool_gui"
{
    printf '# DRSTool local Gentoo install; uninstall.sh-compatible\n'
    printf 'scope=user\nprefix=%s\nsource=%s\ndate=%s\n' "$PREFIX" "$SRC" "$(date -u +%FT%TZ)"
    printf '%s\n' "$BIN" "$DESKTOP" "$ICON"
} > "$stage/install-manifest.txt"
mkdir -p -- "$(dirname -- "$BIN")" "$(dirname -- "$DESKTOP")" "$(dirname -- "$ICON")"
mkdir -p "$snapshot"
snapshot_files "$snapshot"
rolled=1
mv -- "$stage" "$TARGET"
launcher_tmp="$(mktemp "$PREFIX/bin/.drstool-launcher.XXXXXX")"
printf '#!/bin/sh\nexec "%s" "%s/drstool_launch.py" "$@"\n' "$PY" "$TARGET" > "$launcher_tmp"
chmod 755 "$launcher_tmp"
mv -f -- "$launcher_tmp" "$BIN"
desktop_tmp="$(mktemp "$PREFIX/share/applications/.drstool.XXXXXX")"
sed "s|^Exec=drstool$|Exec=$BIN|" "$SRC/data/drstool.desktop" > "$desktop_tmp"
chmod 644 "$desktop_tmp"
mv -f -- "$desktop_tmp" "$DESKTOP"
icon_tmp="$(mktemp "$(dirname -- "$ICON")/.drstool-icon.XXXXXX")"
cp -- "$SRC/assets/drstool.png" "$icon_tmp"
chmod 644 "$icon_tmp"
mv -f -- "$icon_tmp" "$ICON"
# Verify the actual source that the launcher will import; no GUI is displayed.
"$PY" -c "import sys;sys.path.insert(0, '$TARGET');import drstool_gui.app;import drstool_core.benchmark"
rolled=0
trap - EXIT
[[ -d "$snapshot/app" ]] || rmdir "$snapshot" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$(dirname "$DESKTOP")" || true
echo "Installed: $BIN"
echo "User settings in ~/.config/drstool were not touched."
echo "Rollback: bash scripts/gentoo-local-install.sh --rollback"
