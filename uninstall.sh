#!/usr/bin/env bash
# =============================================================================
# uninstall.sh — removes what ./install.sh installed (interactive)
#
#   ./uninstall.sh            ask before each part
#   ./uninstall.sh --yes      remove the program parts without asking
#                             (your settings / backups are kept)
#   ./uninstall.sh --purge    also remove your settings, caches, launchers and
#                             DLL backups (asks unless --yes is given too)
#
# Only files listed in the install manifest (<prefix>/lib/drstool/install-manifest.txt)
# are removed, and only if they are inside that installation's prefix.
# Packages installed from your distribution (PySide6, cmake, …) are left alone.
# =============================================================================

set -euo pipefail

MANIFEST_NAME="install-manifest.txt"

case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
    tr*) TR=1 ;;
    *)   TR=0 ;;
esac
L() { if (( TR )); then printf '%s' "$2"; else printf '%s' "$1"; fi; }
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; D=$'\e[2m'; N=$'\e[0m'
else
    B=""; G=""; Y=""; R=""; C=""; D=""; N=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s⚠%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%s✖ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$B$C" "$*" "$N"; }

ASSUME_YES=0
PURGE=0
for a in "$@"; do
    case "$a" in
        -y|--yes) ASSUME_YES=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $a (see --help)" ;;
    esac
done

if [[ -t 0 ]]; then IN=/dev/stdin
elif (exec </dev/tty) 2>/dev/null; then IN=/dev/tty
else IN=/dev/stdin; fi

ask_yn() {
    local q="$1" def="$2" hint ans
    if (( ASSUME_YES )); then [[ "$def" == y ]]; return; fi
    if [[ "$def" == y ]]; then hint="[$(L Y/n E/h)]"; else hint="[$(L y/N e/H)]"; fi
    while true; do
        printf '%s %s ' "$q" "$hint"
        IFS= read -r ans <"$IN" || ans=""
        ans="${ans,,}"
        case "$ans" in
            "") [[ "$def" == y ]]; return ;;
            y|yes|e|evet) return 0 ;;
            n|no|h|hayir|hayır) return 1 ;;
        esac
    done
}

if [[ "$EUID" -eq 0 ]]; then SUDO=()
elif command -v sudo >/dev/null 2>&1; then SUDO=(sudo)
elif command -v doas >/dev/null 2>&1; then SUDO=(doas)
else SUDO=(__none__); fi
as_root() {
    [[ "${SUDO[0]:-}" == "__none__" ]] && die "$(L "root is needed, but neither sudo nor doas is installed" \
                                                   "root gerekiyor ama sudo da doas da kurulu değil")"
    "${SUDO[@]}" "$@"
}

say "${B}DRSTool$(L " uninstaller" " kaldırma")${N}"
removed_any=0

# ── 1. DRSTool installations made by install.sh ──────────────────────────────
remove_install() {
    local mf="$1" prefix scope lib line runner=()
    prefix="$(sed -n 's/^prefix=//p' "$mf" | head -n1)"
    scope="$(sed -n 's/^scope=//p' "$mf" | head -n1)"
    lib="$(dirname "$mf")"
    [[ -n "$prefix" && "$lib" == "$prefix/lib/drstool" ]] \
        || { warn "$(L "manifest $mf looks wrong — skipped" "$mf manifest dosyası hatalı görünüyor — atlandı")"; return; }
    [[ "$scope" == system && "$EUID" -ne 0 ]] && runner=(as_root)
    local paths=()
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* || "$line" == *=* ]] && continue
        # never touch anything outside this installation's prefix
        if [[ "$line" != "$prefix/"* || "$line" == *"/../"* || "$line" == *"/.." ]]; then
            warn "$(L "outside $prefix, not removed" "$prefix dışında, kaldırılmadı"): $line"
            continue
        fi
        paths+=("$line")
    done <"$mf"
    step "DRSTool  ${D}($prefix, $scope)${N}"
    for p in "${paths[@]}"; do
        if [[ -e "$p" || -L "$p" ]]; then say "  $p"; fi
    done
    if ! ask_yn "$(L "Remove these?" "Bunlar kaldırılsın mı?")" y; then
        say "$(L "kept" "korundu")"
        return
    fi
    for p in "${paths[@]}"; do
        if [[ -e "$p" || -L "$p" ]]; then "${runner[@]}" rm -rf -- "$p"; fi
    done
    # the program folder holds the manifest itself (and a private venv)
    if [[ -d "$lib" ]]; then "${runner[@]}" rm -rf -- "$lib"; fi
    local apps="$prefix/share/applications"
    command -v update-desktop-database >/dev/null 2>&1 && "${runner[@]}" update-desktop-database -q "$apps" 2>/dev/null || true
    ok "$(L "removed" "kaldırıldı"): DRSTool ($prefix)"
    removed_any=1
}

found=0
for mf in "$HOME/.local/lib/drstool/$MANIFEST_NAME" "/usr/local/lib/drstool/$MANIFEST_NAME"; do
    if [[ -f "$mf" ]]; then
        found=1
        remove_install "$mf"
    fi
done
if (( ! found )); then
    say "$(L "No DRSTool installation made by install.sh was found (~/.local or /usr/local)." \
             "install.sh ile yapılmış bir DRSTool kurulumu bulunamadı (~/.local ya da /usr/local).")"
    if command -v drstool >/dev/null 2>&1; then
        say "${D}$(L "'drstool' is installed some other way (package manager / pip): $(command -v drstool)" \
                     "'drstool' başka bir yolla kurulmuş (paket yöneticisi / pip): $(command -v drstool)")${N}"
    fi
fi

# ── 2. lutris-game-tune ──────────────────────────────────────────────────────
LGT_UNINSTALL=/usr/local/lib/lutris-game-tune/uninstall.sh
if [[ -x /usr/local/bin/lutris-game-tune-wrapper || -f "$LGT_UNINSTALL" ]]; then
    step "lutris-game-tune  ${D}(/usr/local)${N}"
    if ask_yn "$(L "Remove lutris-game-tune too? (an active game mode is restored first)" \
                   "lutris-game-tune da kaldırılsın mı? (etkin oyun modu önce geri alınır)")" n; then
        lgt_args=()
        if ask_yn "$(L "Also delete its configuration (/etc/lutris-game-tune.conf, profiles, log)?" \
                       "Yapılandırması da silinsin mi (/etc/lutris-game-tune.conf, profiller, log)?")" n; then
            lgt_args=(--purge)
        fi
        if [[ -f "$LGT_UNINSTALL" ]]; then
            as_root bash "$LGT_UNINSTALL" "${lgt_args[@]}"
        else
            here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
            [[ -f "$here/lutris-game-tune-main/uninstall.sh" ]] \
                || die "$(L "lutris-game-tune's uninstall.sh was not found" "lutris-game-tune'un uninstall.sh dosyası bulunamadı")"
            as_root bash "$here/lutris-game-tune-main/uninstall.sh" "${lgt_args[@]}"
        fi
        removed_any=1
    fi
fi

# ── 3. your data ─────────────────────────────────────────────────────────────
cfg="${XDG_CONFIG_HOME:-$HOME/.config}/drstool"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/drstool"
data="${XDG_DATA_HOME:-$HOME/.local/share}/drstool"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
user_items=()
for p in "$cfg" "$cache" "$data"; do
    [[ -e "$p" ]] && user_items+=("$p")
done
while IFS= read -r -d '' f; do user_items+=("$f"); done \
    < <(find "$apps" -maxdepth 1 -name 'drstool-*.desktop' -print0 2>/dev/null)
if (( ${#user_items[@]} )); then
    step "$(L "Your data" "Verileriniz")"
    [[ -e "$cfg" ]] && say "  $cfg  ${D}$(L "profiles, history" "profiller, geçmiş")${N}"
    [[ -e "$cache" ]] && say "  $cache  ${D}$(L "downloads, hints" "indirmeler, ipuçları")${N}"
    [[ -e "$data" ]] && say "  $data  ${D}$(L "launchers, DLL backups" "başlatıcılar, DLL yedekleri")${N}"
    if compgen -G "$apps/drstool-*.desktop" >/dev/null; then
        say "  $apps/drstool-*.desktop  ${D}$(L "launchers from the Shortcut tab" "Shortcut sekmesinin başlatıcıları")${N}"
    fi
    if [[ -d "$data/dll-backups" ]] && [[ -n "$(ls -A "$data/dll-backups" 2>/dev/null)" ]]; then
        warn "$(L "dll-backups holds the original DLLs of games updated with the DLL Manager — without it they cannot be restored." \
                  "dll-backups, DLL Manager ile güncellenen oyunların orijinal DLL'lerini tutar — silinirse geri yüklenemezler.")"
    fi
    if (( PURGE )) && ask_yn "$(L "Delete all of this?" "Bunların hepsi silinsin mi?")" y; then
        rm -rf -- "${user_items[@]}"
        ok "$(L "your DRSTool data was removed" "DRSTool verileriniz silindi")"
        removed_any=1
    elif (( ! PURGE )) && (( ! ASSUME_YES )) && ask_yn "$(L "Delete all of this?" "Bunların hepsi silinsin mi?")" n; then
        rm -rf -- "${user_items[@]}"
        ok "$(L "your DRSTool data was removed" "DRSTool verileriniz silindi")"
        removed_any=1
    else
        say "$(L "kept" "korundu")"
    fi
fi

say ""
if (( removed_any )); then
    ok "$(L "Done." "Bitti.")"
else
    say "$(L "Nothing was removed." "Hiçbir şey kaldırılmadı.")"
fi
say "${D}$(L "Steam shortcuts added with DRSTool stay in Steam; remove them there or in DRSTool's Add to Steam tab." \
             "DRSTool ile Steam'e eklenen oyunlar Steam'de kalır; onları Steam'den ya da DRSTool'un Add to Steam sekmesinden kaldırın.")${N}"
