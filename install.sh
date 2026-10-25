#!/usr/bin/env bash
# =============================================================================
# install.sh — interactive installer for DRSTool (any distribution)
#
#   ./install.sh                 ask everything (run as your normal user;
#                                sudo is used only for the parts that need root)
#   ./install.sh --yes           accept the defaults without asking
#   ./install.sh --help          all options
#
# What can be installed:
#   DRSTool GUI          ~/.local (just you, no root)  or  /usr/local (all users)
#   PySide6 / PyYAML     from your distribution's packages, or a private venv (pip)
#   vk_flip_meter        the Vulkan frame-pacing layer (built with CMake)
#   lutris-game-tune     the system tuner (always system-wide; setuid or polkit)
#
# Gentoo and Arch have real packages (drstool-9999.ebuild, packaging/arch/PKGBUILD);
# this script is for everything else, and works there too.
# Remove everything again with ./uninstall.sh
# =============================================================================

set -euo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
MANIFEST_NAME="install-manifest.txt"
MIN_PY="3.12"

# ── language / colours ───────────────────────────────────────────────────────
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

# ── options ──────────────────────────────────────────────────────────────────
ASSUME_YES=0
OPT_SCOPE=""          # user | system
OPT_DEPS=""           # distro | venv | skip
OPT_FLM=""            # yes | no
OPT_LGT=""            # yes | no
OPT_LGT_MODE=""       # polkit | setuid

usage() {
    cat <<EOF
Usage: ./install.sh [options]

  --yes, -y              do not ask; use the defaults (or the options below)
  --user                 install for the current user only (~/.local, no root)
  --system               install for all users (/usr/local, uses sudo)
  --deps=distro|venv|skip
                         how to get PySide6 / PyYAML if they are missing
  --with-flm / --no-flm  build and install the vk_flip_meter Vulkan layer
  --with-lgtune / --no-lgtune
                         install lutris-game-tune (system-wide, needs sudo)
  --lgtune-mode=polkit|setuid
                         polkit: no setuid binary (pkexec + file capability)
  -h, --help             this help

Run it again to update an existing installation. Remove with ./uninstall.sh
EOF
}

for a in "$@"; do
    case "$a" in
        -y|--yes) ASSUME_YES=1 ;;
        --user) OPT_SCOPE=user ;;
        --system) OPT_SCOPE=system ;;
        --deps=distro|--deps=venv|--deps=skip) OPT_DEPS="${a#--deps=}" ;;
        --with-flm) OPT_FLM=yes ;;
        --no-flm) OPT_FLM=no ;;
        --with-lgtune) OPT_LGT=yes ;;
        --no-lgtune) OPT_LGT=no ;;
        --lgtune-mode=polkit|--lgtune-mode=setuid) OPT_LGT_MODE="${a#--lgtune-mode=}" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $a" ;;
    esac
done

# ── input (works when piped too: questions are read from the terminal) ──────
if [[ -t 0 ]]; then
    IN=/dev/stdin
elif (exec </dev/tty) 2>/dev/null; then
    IN=/dev/tty
else
    IN=/dev/stdin
fi

# ask_yn "question" y|n  -> 0 for yes
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

# ask_choice "question" default_index option1 option2 ...  -> sets CHOICE (1-based)
ask_choice() {
    local q="$1" def="$2" i ans
    shift 2
    if (( ASSUME_YES )); then CHOICE="$def"; return; fi
    say "$q"
    i=1
    for o in "$@"; do
        if (( i == def )); then printf '  %s%d)%s %s %s%s%s\n' "$B" "$i" "$N" "$o" "$D" "$(L "(default)" "(varsayılan)")" "$N"
        else printf '  %s%d)%s %s\n' "$B" "$i" "$N" "$o"; fi
        i=$((i + 1))
    done
    while true; do
        printf '%s [%d]: ' "$(L "Choice" "Seçim")" "$def"
        IFS= read -r ans <"$IN" || ans=""
        [[ -z "$ans" ]] && { CHOICE="$def"; return; }
        if [[ "$ans" =~ ^[0-9]+$ ]] && (( ans >= 1 && ans <= $# )); then CHOICE="$ans"; return; fi
    done
}

# ── root helper ──────────────────────────────────────────────────────────────
if [[ "$EUID" -eq 0 ]]; then
    SUDO=()
elif command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
elif command -v doas >/dev/null 2>&1; then
    SUDO=(doas)
else
    SUDO=(__none__)
fi
as_root() {
    if [[ "${SUDO[0]:-}" == "__none__" ]]; then
        die "$(L "root is needed for this step, but neither sudo nor doas is installed" \
                 "bu adım root gerektiriyor ama sudo da doas da kurulu değil")"
    fi
    "${SUDO[@]}" "$@"
}

# ── system detection ─────────────────────────────────────────────────────────
OS_NAME="Linux"; OS_ID=""; OS_LIKE=""
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    {
        OS_NAME="$(. /etc/os-release; printf '%s' "${PRETTY_NAME:-${NAME:-Linux}}")"
        OS_ID="$(. /etc/os-release; printf '%s' "${ID:-}")"
        OS_LIKE="$(. /etc/os-release; printf '%s' "${ID_LIKE:-}")"
    }
fi
PM=""
for p in apt-get dnf pacman zypper emerge; do
    if command -v "$p" >/dev/null 2>&1; then PM="$p"; break; fi
done

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "$(L "python3 was not found — install Python ${MIN_PY} or newer first" \
                          "python3 bulunamadı — önce Python ${MIN_PY} veya daha yenisini kurun")"
PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PY/./, }) else 1)"; then
    die "$(L "Python ${PY_VER} is too old; DRSTool needs ${MIN_PY} or newer" \
             "Python ${PY_VER} çok eski; DRSTool ${MIN_PY} veya daha yenisini gerektiriyor")"
fi
has_mod() { "$1" -c "import $2" >/dev/null 2>&1; }

# Distribution packages for each need: "<runtime>|<flm build>|<lgtune build>|<polkit extras>"
pkgs_for() {
    case "$PM:$1" in
        apt-get:runtime) echo "python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-yaml" ;;
        apt-get:flm)     echo "cmake g++ libvulkan-dev" ;;
        apt-get:lgt)     echo "gcc util-linux" ;;
        apt-get:polkit)  echo "pkexec libcap2-bin" ;;
        dnf:runtime)     echo "python3-pyside6 python3-pyyaml" ;;
        dnf:flm)         echo "cmake gcc-c++ vulkan-headers vulkan-loader-devel" ;;
        dnf:lgt)         echo "gcc util-linux" ;;
        dnf:polkit)      echo "polkit libcap" ;;
        pacman:runtime)  echo "pyside6 python-yaml" ;;
        pacman:flm)      echo "cmake gcc vulkan-headers vulkan-icd-loader" ;;
        pacman:lgt)      echo "gcc util-linux" ;;
        pacman:polkit)   echo "polkit libcap" ;;
        zypper:runtime)  echo "python3-pyside6 python3-PyYAML" ;;
        zypper:flm)      echo "cmake gcc-c++ vulkan-devel vulkan-headers" ;;
        zypper:lgt)      echo "gcc util-linux" ;;
        zypper:polkit)   echo "polkit libcap-progs" ;;
        *) echo "" ;;
    esac
}

# Are these packages installable here? (apt: an older Debian/Ubuntu may not ship PySide6)
pkgs_available() {
    local p
    case "$PM" in
        apt-get)
            for p in "$@"; do
                apt-cache policy "$p" 2>/dev/null | grep -q 'Candidate: [^(]' || return 1
            done ;;
        dnf)    dnf -q list "$@" >/dev/null 2>&1 || return 1 ;;
        pacman) pacman -Si "$@" >/dev/null 2>&1 || return 1 ;;
        zypper) for p in "$@"; do zypper -q info "$p" 2>/dev/null | grep -qi '^name' || return 1; done ;;
        *) return 1 ;;
    esac
}

pm_install() {
    case "$PM" in
        apt-get) as_root apt-get install -y "$@" ;;
        dnf)     as_root dnf install -y "$@" ;;
        pacman)  as_root pacman -S --needed --noconfirm "$@" ;;
        zypper)  as_root zypper --non-interactive install "$@" ;;
        *) return 1 ;;
    esac
}

# ── banner ───────────────────────────────────────────────────────────────────
say "${B}DRSTool$(L " installer" " kurulumu")${N}  ${D}($SRC)${N}"
say "$(L "System" "Sistem"):  $OS_NAME${PM:+  ·  $PM}  ·  Python $PY_VER"
[[ -f "$SRC/DRSTool.py" && -f "$SRC/drstool_launch.py" && -d "$SRC/drstool_core" ]] \
    || die "$(L "run this script from the DRSTool folder (DRSTool.py / drstool_core missing)" \
                "bu betiği DRSTool klasöründen çalıştırın (DRSTool.py / drstool_core yok)")"
if [[ "$OS_ID" == gentoo || " $OS_LIKE " == *" gentoo "* ]]; then
    warn "$(L "Gentoo: the ebuild (drstool-9999.ebuild) is the cleaner way; continuing anyway." \
              "Gentoo: ebuild (drstool-9999.ebuild) daha temiz bir yol; yine de devam ediliyor.")"
elif [[ "$OS_ID" == arch || " $OS_LIKE " == *" arch "* ]]; then
    warn "$(L "Arch: packaging/arch/PKGBUILD (makepkg -si) is the cleaner way; continuing anyway." \
              "Arch: packaging/arch/PKGBUILD (makepkg -si) daha temiz bir yol; yine de devam ediliyor.")"
fi

# ── 1. scope ─────────────────────────────────────────────────────────────────
step "1/5  $(L "Where to install" "Nereye kurulsun")"
if [[ -z "$OPT_SCOPE" ]]; then
    if [[ "$EUID" -eq 0 ]]; then
        OPT_SCOPE=system
        say "$(L "Running as root → system-wide (/usr/local)." "root olarak çalışıyor → tüm sistem (/usr/local).")"
    else
        ask_choice "$(L "Install DRSTool for:" "DRSTool kimin için kurulsun:")" 1 \
            "$(L "only me — ~/.local, no root needed" "yalnızca ben — ~/.local, root gerekmez")" \
            "$(L "all users — /usr/local, uses sudo" "tüm kullanıcılar — /usr/local, sudo kullanır")"
        [[ "$CHOICE" == 1 ]] && OPT_SCOPE=user || OPT_SCOPE=system
    fi
fi
if [[ "$OPT_SCOPE" == user && "$EUID" -eq 0 ]]; then
    die "$(L "--user as root would install into /root — run as your normal user" \
             "--user root olarak /root içine kurar — normal kullanıcınızla çalıştırın")"
fi
if [[ "$OPT_SCOPE" == user ]]; then
    PREFIX="$HOME/.local"; RUN=()
else
    PREFIX="/usr/local"; RUN=(as_root)
fi
LIB="$PREFIX/lib/drstool"
BIN="$PREFIX/bin/drstool"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor/512x512/apps"
UPDATE=0
if [[ -f "$LIB/$MANIFEST_NAME" ]]; then
    UPDATE=1
    say "$(L "An existing installation was found in $LIB — it will be updated." \
             "$LIB içinde mevcut bir kurulum bulundu — güncellenecek.")"
fi
ok "$(L "target" "hedef"): $PREFIX"

# ── 2. Python dependencies ───────────────────────────────────────────────────
step "2/5  $(L "Python libraries (PySide6, PyYAML)" "Python kütüphaneleri (PySide6, PyYAML)")"
VENV="$LIB/venv"
USE_VENV=0
[[ -x "$VENV/bin/python3" ]] && USE_VENV=1
need_rt=0
if (( USE_VENV )); then
    has_mod "$VENV/bin/python3" PySide6.QtWidgets && has_mod "$VENV/bin/python3" yaml || need_rt=1
    say "$(L "Using the existing private environment" "Mevcut özel ortam kullanılıyor"): $VENV"
else
    has_mod "$PY" PySide6.QtWidgets || need_rt=1
    has_mod "$PY" yaml || need_rt=1
fi
DEPS_ACTION=none
if (( need_rt )); then
    rt_pkgs="$(pkgs_for runtime)"
    distro_ok=0
    # shellcheck disable=SC2086
    [[ -n "$rt_pkgs" ]] && pkgs_available $rt_pkgs && distro_ok=1
    say "$(L "PySide6 and/or PyYAML are missing." "PySide6 ve/veya PyYAML eksik.")"
    if [[ -z "$OPT_DEPS" ]]; then
        opts=()
        if (( distro_ok )); then
            opts+=("$(L "distribution packages" "dağıtım paketleri"): $rt_pkgs ${D}(sudo)${N}")
        fi
        opts+=("$(L "private environment with pip in $VENV (~300 MB, no root)" \
                    "$VENV içinde pip ile özel ortam (~300 MB, root gerekmez)")")
        opts+=("$(L "skip — I will install them myself" "atla — kendim kuracağım")")
        ask_choice "$(L "How should they be installed?" "Nasıl kurulsun?")" 1 "${opts[@]}"
        if (( distro_ok )); then
            case "$CHOICE" in 1) OPT_DEPS=distro ;; 2) OPT_DEPS=venv ;; *) OPT_DEPS=skip ;; esac
        else
            case "$CHOICE" in 1) OPT_DEPS=venv ;; *) OPT_DEPS=skip ;; esac
        fi
    fi
    if [[ "$OPT_DEPS" == distro && "$distro_ok" != 1 ]]; then
        warn "$(L "no distribution packages known/available here — using a private environment" \
                  "burada uygun dağıtım paketi yok — özel ortam kullanılacak")"
        OPT_DEPS=venv
    fi
    DEPS_ACTION="$OPT_DEPS"
else
    ok "$(L "PySide6 and PyYAML are already installed" "PySide6 ve PyYAML zaten kurulu")"
fi

# ── 3. vk_flip_meter ─────────────────────────────────────────────────────────
step "3/5  vk_flip_meter $(L "(Vulkan frame-pacing layer, optional)" "(Vulkan kare düzenleme katmanı, isteğe bağlı)")"
say "${D}$(L "Frame pacing on VRR screens with frame generation, and a precise FPS limiter." \
             "VRR ekranlarda frame generation ile kare düzenleme ve hassas FPS sınırlayıcı.")${N}"
FLM_INSTALLED=0
[[ -f "$PREFIX/share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json" ]] && FLM_INSTALLED=1
if [[ -z "$OPT_FLM" ]]; then
    if ask_yn "$(L "Build and install it?" "Derlenip kurulsun mu?")" "$( (( FLM_INSTALLED )) && echo y || echo n)"; then
        OPT_FLM=yes
    else
        OPT_FLM=no
    fi
fi
FLM_DEPS=""
if [[ "$OPT_FLM" == yes ]]; then
    have_tools=1
    command -v cmake >/dev/null 2>&1 || have_tools=0
    command -v c++ >/dev/null 2>&1 || command -v g++ >/dev/null 2>&1 || have_tools=0
    if (( have_tools )) && [[ -f /usr/include/vulkan/vulkan.h ]]; then
        :
    else
        FLM_DEPS="$(pkgs_for flm)"
        if [[ -n "$FLM_DEPS" ]] && ask_yn "$(L "Build tools are missing. Install them ($FLM_DEPS)?" \
                                              "Derleme araçları eksik. Kurulsun mu ($FLM_DEPS)?")" y; then
            :
        else
            FLM_DEPS=""
            warn "$(L "without cmake, a C++ compiler and the Vulkan headers the build will fail" \
                      "cmake, bir C++ derleyicisi ve Vulkan başlıkları olmadan derleme başarısız olur")"
        fi
    fi
fi

# ── 4. lutris-game-tune ──────────────────────────────────────────────────────
step "4/5  lutris-game-tune $(L "(system tuner, optional, always system-wide)" "(sistem ayarlayıcı, isteğe bağlı, her zaman tüm sistem)")"
say "${D}$(L "CPU governor, power saving, CCD/P-core isolation and game priority while a game runs." \
             "Oyun çalışırken CPU governor, güç tasarrufu, CCD/P-çekirdek izolasyonu ve oyun önceliği.")${N}"
LGT_WRAPPER=/usr/local/bin/lutris-game-tune-wrapper
LGT_INSTALLED=0
[[ -x "$LGT_WRAPPER" ]] && LGT_INSTALLED=1
if [[ -z "$OPT_LGT" ]]; then
    if ask_yn "$(L "Install it?" "Kurulsun mu?")" "$( (( LGT_INSTALLED )) && echo y || echo n)"; then
        OPT_LGT=yes
    else
        OPT_LGT=no
    fi
fi
LGT_DEPS=""
if [[ "$OPT_LGT" == yes ]]; then
    if [[ -z "$OPT_LGT_MODE" ]]; then
        cur_mode=""
        if (( LGT_INSTALLED )); then
            if [[ -u "$LGT_WRAPPER" ]]; then cur_mode=setuid; else cur_mode=polkit; fi
        fi
        def=1
        [[ "$cur_mode" == setuid ]] && def=2
        ask_choice "$(L "How should it get root rights?" "Root yetkisini nasıl alsın?")" "$def" \
            "$(L "polkit — no setuid binary; pkexec for PRE/POST, only CAP_SYS_NICE for RUN (recommended)" \
                 "polkit — setuid dosya yok; PRE/POST için pkexec, RUN için yalnızca CAP_SYS_NICE (önerilen)")" \
            "$(L "setuid — the classic way, works without polkit" "setuid — klasik yol, polkit olmadan da çalışır")"
        [[ "$CHOICE" == 1 ]] && OPT_LGT_MODE=polkit || OPT_LGT_MODE=setuid
    fi
    need=()
    command -v gcc >/dev/null 2>&1 || need+=(gcc)
    command -v flock >/dev/null 2>&1 || need+=(flock)
    if [[ "$OPT_LGT_MODE" == polkit ]]; then
        command -v pkexec >/dev/null 2>&1 || need+=(pkexec)
        command -v setcap >/dev/null 2>&1 || need+=(setcap)
    fi
    if (( ${#need[@]} )); then
        LGT_DEPS="$(pkgs_for lgt)"
        [[ "$OPT_LGT_MODE" == polkit ]] && LGT_DEPS="$LGT_DEPS $(pkgs_for polkit)"
        LGT_DEPS="${LGT_DEPS# }"
        if [[ -n "${LGT_DEPS// /}" ]] && ask_yn "$(L "Missing: ${need[*]}. Install ($LGT_DEPS)?" \
                                                   "Eksik: ${need[*]}. Kurulsun mu ($LGT_DEPS)?")" y; then
            :
        else
            LGT_DEPS=""
            warn "$(L "missing: ${need[*]} — the lutris-game-tune step may fail" \
                      "eksik: ${need[*]} — lutris-game-tune adımı başarısız olabilir")"
        fi
    fi
fi

# ── 5. summary ───────────────────────────────────────────────────────────────
step "5/5  $(L "Summary" "Özet")"
say "  DRSTool          → $BIN  ${D}($LIB)${N}$( (( UPDATE )) && L "  [update]" "  [güncelleme]")"
case "$DEPS_ACTION" in
    distro) say "  PySide6/PyYAML   → $(L "distribution packages" "dağıtım paketleri"): $(pkgs_for runtime)" ;;
    venv)   say "  PySide6/PyYAML   → pip → $VENV" ;;
    skip)   say "  PySide6/PyYAML   → ${Y}$(L "skipped — DRSTool will not start until they are installed" "atlandı — kurulana kadar DRSTool açılmaz")${N}" ;;
    *)      say "  PySide6/PyYAML   → $(L "already installed" "zaten kurulu")" ;;
esac
lbl="$(L "Desktop entry" "Menü girdisi")"
printf '  %s%*s → %s\n' "$lbl" $((16 - ${#lbl})) "" "$APPS/drstool.desktop"
[[ "$OPT_FLM" == yes ]] && say "  vk_flip_meter    → $PREFIX/lib/libvk_flip_meter.so${FLM_DEPS:+  ($(L "build deps" "derleme bağımlılıkları"): $FLM_DEPS)}"
[[ "$OPT_LGT" == yes ]] && say "  lutris-game-tune → /usr/local ($OPT_LGT_MODE)${LGT_DEPS:+  ($(L "deps" "bağımlılıklar"): $LGT_DEPS)}"
if [[ "$OPT_SCOPE" == system || "$DEPS_ACTION" == distro || -n "$FLM_DEPS$LGT_DEPS" || "$OPT_LGT" == yes ]]; then
    say "  ${D}$(L "Some steps use sudo — you may be asked for your password." "Bazı adımlar sudo kullanır — şifreniz sorulabilir.")${N}"
fi
ask_yn "$(L "Proceed?" "Devam edilsin mi?")" y || { say "$(L "Nothing was changed." "Hiçbir şey değiştirilmedi.")"; exit 0; }

# ── do it ────────────────────────────────────────────────────────────────────
MANIFEST_TMP="$(mktemp)"
BUILD_DIR=""
cleanup() { rm -f "$MANIFEST_TMP"; if [[ -n "$BUILD_DIR" ]]; then rm -rf "$BUILD_DIR"; fi; }
trap cleanup EXIT
track() { printf '%s\n' "$@" >>"$MANIFEST_TMP"; }
# Written as soon as the program folder exists (and again at the end), so even an
# interrupted run leaves something uninstall.sh can work from.
write_manifest() {
    local t
    t="$(mktemp)"
    {
        printf '# DRSTool install manifest — read by uninstall.sh\n'
        printf 'scope=%s\nprefix=%s\nsource=%s\ndate=%s\n' "$OPT_SCOPE" "$PREFIX" "$SRC" "$(date -Iseconds)"
        cat "$MANIFEST_TMP"
    } >"$t"
    "${RUN[@]}" install -m 644 "$t" "$LIB/$MANIFEST_NAME"
    rm -f "$t"
}

# distribution packages first (one transaction)
all_pkgs=""
[[ "$DEPS_ACTION" == distro ]] && all_pkgs="$(pkgs_for runtime)"
all_pkgs="$all_pkgs $FLM_DEPS $LGT_DEPS"
if [[ -n "${all_pkgs// /}" ]]; then
    step "$(L "Installing packages" "Paketler kuruluyor"): $all_pkgs"
    [[ "$PM" == apt-get ]] && as_root apt-get update -qq || true
    # shellcheck disable=SC2086
    pm_install $all_pkgs || die "$(L "package installation failed" "paket kurulumu başarısız")"
fi

step "$(L "Installing DRSTool" "DRSTool kuruluyor")"
"${RUN[@]}" mkdir -p "$LIB" "$PREFIX/bin" "$APPS" "$ICONS"
track "$LIB"
(( UPDATE )) || write_manifest
# Python libraries first: if pip fails, an existing installation is still intact.
if [[ "$DEPS_ACTION" == venv ]]; then
    if [[ ! -x "$VENV/bin/python3" ]]; then
        "${RUN[@]}" "$PY" -m venv --system-site-packages "$VENV" \
            || die "$(L "could not create the venv — install python3-venv (Debian/Ubuntu) and retry" \
                        "venv oluşturulamadı — python3-venv paketini kurun (Debian/Ubuntu) ve tekrar deneyin")"
    fi
    "${RUN[@]}" "$VENV/bin/python3" -m pip install --upgrade --disable-pip-version-check -q \
        "PySide6>=6.5" "PyYAML>=6" setproctitle \
        || die "$(L "pip install failed" "pip kurulumu başarısız")"
    USE_VENV=1
fi
# Update: replace the program files, keep the private Python environment.
for old in "$LIB"/*; do
    [[ -e "$old" ]] || continue
    [[ "$old" == "$VENV" ]] && continue
    "${RUN[@]}" rm -rf "$old"
done
"${RUN[@]}" cp -r "$SRC/DRSTool.py" "$SRC/drstool_launch.py" "$SRC/drstool_core" "$SRC/assets" \
    "$SRC/vk-flip-meter-main" "$SRC/lutris-game-tune-main" "$LIB/"
[[ -f "$SRC/LICENSE" ]] && "${RUN[@]}" cp "$SRC/LICENSE" "$LIB/"
"${RUN[@]}" find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
track "$BIN" "$APPS/drstool.desktop" "$ICONS/drstool.png"
write_manifest

if (( USE_VENV )); then
    RUNPY="$VENV/bin/python3"
else
    RUNPY="$PY"
fi

# Bytecode must be written now: $LIB is root-owned, so a normal user's first run could not cache it.
"${RUN[@]}" "$RUNPY" -m compileall -q "$LIB/DRSTool.py" "$LIB/drstool_core" >/dev/null 2>&1 || true

tmpf="$(mktemp)"
cat >"$tmpf" <<EOF
#!/bin/sh
# DRSTool launcher — written by install.sh
exec "$RUNPY" "$LIB/drstool_launch.py" "\$@"
EOF
"${RUN[@]}" install -m 755 "$tmpf" "$BIN"
sed -e "s|^Exec=drstool|Exec=$BIN|" "$SRC/data/drstool.desktop" >"$tmpf"
"${RUN[@]}" install -m 644 "$tmpf" "$APPS/drstool.desktop"
rm -f "$tmpf"
"${RUN[@]}" install -m 644 "$SRC/assets/drstool.png" "$ICONS/drstool.png"
command -v update-desktop-database >/dev/null 2>&1 && "${RUN[@]}" update-desktop-database -q "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    "${RUN[@]}" gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
ok "DRSTool → $BIN"

if [[ "$OPT_FLM" == yes ]]; then
    step "$(L "Building vk_flip_meter" "vk_flip_meter derleniyor")"
    BUILD_DIR="$(mktemp -d)"
    blog="$BUILD_DIR/build.log"
    if ! cmake -S "$SRC/vk-flip-meter-main" -B "$BUILD_DIR/b" -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR=lib -DFLM_NATIVE_BUILD=OFF \
            -DFLM_LIB_PATH="$PREFIX/lib/libvk_flip_meter.so" >"$blog" 2>&1; then
        tail -n 15 "$blog" >&2
        die "$(L "cmake configure failed (Vulkan headers / loader installed?)" \
                 "cmake yapılandırması başarısız (Vulkan başlıkları / loader kurulu mu?)")"
    fi
    if ! cmake --build "$BUILD_DIR/b" -j "$(nproc 2>/dev/null || echo 4)" >>"$blog" 2>&1; then
        tail -n 25 "$blog" >&2
        die "$(L "vk_flip_meter build failed" "vk_flip_meter derlemesi başarısız")"
    fi
    "${RUN[@]}" cmake --install "$BUILD_DIR/b" >>"$blog" 2>&1 \
        || { tail -n 10 "$blog" >&2; die "$(L "vk_flip_meter install failed" "vk_flip_meter kurulumu başarısız")"; }
    track "$PREFIX/lib/libvk_flip_meter.so" "$PREFIX/share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json"
    ok "vk_flip_meter → $PREFIX/lib/libvk_flip_meter.so  ${D}($(L "enable per game with" "oyun başına açmak için") ENABLE_LAYER_cpu_flip_meter=1)${N}"
fi

if [[ "$OPT_LGT" == yes ]]; then
    step "$(L "Installing lutris-game-tune" "lutris-game-tune kuruluyor") ($OPT_LGT_MODE)"
    lgt_args=()
    [[ "$OPT_LGT_MODE" == polkit ]] && lgt_args=(--polkit)
    as_root bash "$SRC/lutris-game-tune-main/install.sh" "${lgt_args[@]}" \
        || die "$(L "lutris-game-tune installation failed" "lutris-game-tune kurulumu başarısız")"
    printf 'lutris-game-tune=%s\n' "$OPT_LGT_MODE" >>"$MANIFEST_TMP"
fi

write_manifest

step "$(L "Done" "Bitti")"
ok "$(L "Start DRSTool from the application menu, or run" "DRSTool'u uygulama menüsünden başlatın ya da şunu çalıştırın"): ${B}$BIN${N}"
if [[ "$OPT_SCOPE" == user && ":$PATH:" != *":$PREFIX/bin:"* ]]; then
    warn "$(L "$PREFIX/bin is not in your PATH; the menu entry works, or add it to PATH to type 'drstool'" \
              "$PREFIX/bin PATH içinde değil; menü girdisi çalışır, 'drstool' yazmak için PATH'e ekleyin")"
fi
if [[ "$DEPS_ACTION" == skip ]]; then
    warn "$(L "install PySide6 and PyYAML before starting DRSTool" "DRSTool'u başlatmadan önce PySide6 ve PyYAML kurun")"
fi
say "$(L "Remove it again with" "Kaldırmak için"): ./uninstall.sh"
