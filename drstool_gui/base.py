"""Process-level helpers: app constants and paths, optional imports (yaml, drstool_core,
setproctitle), file-write helpers and shared state flags.
"""

import glob
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List


try:
    import yaml
    _HAVE_YAML = True
    # libyaml-backed loader is ~8x faster than the pure-Python SafeLoader (same safe semantics)
    _YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
except ImportError:
    _HAVE_YAML = False
    yaml = _YAML_LOADER = None     # so other modules can import the names; uses are guarded by _HAVE_YAML


# Qt-free helpers (share codes, Steam shortcuts, game/DLL scanning). They live in the
# drstool_core package next to DRSTool.py (git checkout) or in site-packages (package
# installs). The catalog (drstool_core.catalog) is required; if one of the helper modules
# below cannot be imported, the pages that need it say what is missing.
try:
    from drstool_core import share_code, steam_shortcuts, game_scan, dll_ops, pe_version
    _CORE_ERR = ""
except ImportError as _ex:          # pragma: no cover - depends on the install
    share_code = steam_shortcuts = game_scan = dll_ops = pe_version = None
    _CORE_ERR = (f"The drstool_core package was not found ({_ex}). Install DRSTool with its "
                 "package (or run DRSTool.py from the repository folder) to use this feature.")


# ============================================================================
# App constants and paths
# ============================================================================

APP_TITLE = "DXVK NVAPI DRS Settings Configurator"

# Uygulama ikonu: script'in yanındaki assets/drstool.png dosyasından yüklenir.
# Wayland altında sistem barı/taskbar ikonu genellikle .desktop dosyasındaki
# Icon= alanına göre eşleşir; paket kurulumunda .desktop girdisini ebuild
# sağlar (bkz. main()).
APP_ID = "drstool"
_SCRIPT_DIR = Path(__file__).resolve().parent.parent      # project root (parent of the drstool_gui package)
ICON_PATH = _SCRIPT_DIR / "assets" / "drstool.png"


def _as_dict(v) -> Dict:
    """Hand-edited profiles.json may hold null / a list where a mapping is expected."""
    return dict(v) if isinstance(v, dict) else {}


def find_bundled_dir(name: str, marker: str) -> Path:
    """Locate a bundled helper source tree (vk-flip-meter-main, lutris-game-tune-main).

    Checked in order: $DRSTOOL_DATA_DIR, next to this script (git checkout),
    <sys.prefix>/share/drstool (pip / pipx install), /usr/share/drstool and
    /usr/local/share/drstool (ebuild / package install, where the script itself
    lives under /usr/lib/python-exec/ and has no sibling directories)."""
    roots: List[Path] = []
    env = os.environ.get("DRSTOOL_DATA_DIR")
    if env:
        roots.append(Path(env))
    roots += [_SCRIPT_DIR, Path(sys.prefix) / "share" / "drstool",
              Path("/usr/share/drstool"), Path("/usr/local/share/drstool")]
    for root in roots:
        if (root / name / marker).is_file():
            return root / name
    return _SCRIPT_DIR / name


def user_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "drstool"


def default_flm_conf_path() -> Path:
    """Per-user, non-world-writable default for the FLM live-tuning file."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt) and os.access(rt, os.W_OK):
        return Path(rt) / "flm.conf"
    return Path(tempfile.gettempdir()) / f"flm-{os.getuid()}.conf"


def detect_libdir() -> str:
    """Distro-appropriate library directory (relative to the install prefix)."""
    lib64 = Path("/usr/lib64")
    if lib64.is_dir() and not lib64.is_symlink():
        return "lib64"                                   # Gentoo, Fedora, openSUSE
    multiarch = f"lib/{platform.machine()}-linux-gnu"
    if Path("/usr", multiarch).is_dir():
        return multiarch                                 # Debian / Ubuntu
    return "lib"                                         # Arch (lib64 -> lib symlink), others


def pkexec_auth_failed(exit_code: int, output: str) -> bool:
    """126/127 are ambiguous: pkexec uses them for a dismissed/denied auth
    dialog, but a missing program inside `bash -c` also exits 127. Only
    treat them as "authorization failed" when pkexec said so."""
    if exit_code not in (126, 127):
        return False
    low = output.lower()
    return any(m in low for m in ("dismissed", "not authorized", "authentication"))

try:
    import setproctitle
    _HAVE_SETPROCTITLE = True
except ImportError:
    _HAVE_SETPROCTITLE = False


def apply_wayland_app_id(app_id: str) -> None:
    """python-exec2c gibi bir wrapper üzerinden (emerge/ebuild kurulumu) veya
    doğrudan 'python3 DRSTool.py' ile çalıştırıldığında, gerçek process adı
    'python3'/'python3.14' olarak kalır. KDE Plasma Wayland (KWin) altında
    Qt'nin QApplication.setDesktopFileName() çağrısı bu durumda native
    Wayland app_id protokol mesajını etkilemiyor; KWin app_id'yi gerçek
    process adından türetiyor ve .desktop/Icon eşleşmesini kaybedip jenerik
    Wayland ikonuna düşüyor.
    setproctitle mevcutsa (dev-python/setproctitle veya `pip install
    setproctitle`), argv[0] belleğini gerçekten patch'leyerek process adını
    app_id ile aynı yapar — hem ebuild kurulumunda hem de repo'yu klonlayıp
    doğrudan çalıştıranlarda (kurulum betiği/elle) aynı şekilde çalışır.
    setproctitle yoksa sessizce atlanır; sadece ikon eşleşmesi etkilenir,
    program normal çalışmaya devam eder."""
    if not _HAVE_SETPROCTITLE:
        return
    try:
        setproctitle.setproctitle(app_id)
    except Exception:
        pass


def _run(cmd: List[str], timeout: float = 15.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
        return r.stdout or ""
    except Exception:
        return ""


def _backup_file(path: Path) -> Path:
    bak = path.with_name(f"{path.name}.drstool-{datetime.now():%Y%m%d-%H%M%S-%f}.bak")
    shutil.copy2(path, bak)
    _PENDING_BAKS.append((path, bak, time.monotonic()))
    del _PENDING_BAKS[:-40]
    # keep the 20 newest backups of this file
    olds = sorted(path.parent.glob(f"{glob.escape(path.name)}.drstool-*.bak"))
    for o in olds[:-20]:
        try:
            o.unlink()
        except OSError:
            pass
    return bak


def _atomic_write(path: Path, text: str):
    """Write via an unpredictable temp file + fsync + rename. Follows a symlinked
    target (os.replace on the link would turn it into a plain file) and keeps the
    original file's permission bits — write_text() used to recreate e.g. a 0600
    config with the umask default (usually 0644)."""
    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".drstool-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644 & ~_umask())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _umask() -> int:
    m = os.umask(0)
    os.umask(m)
    return m


def _proc_running(*names: str) -> bool:
    if shutil.which("pgrep") is None:
        # no pgrep: scan /proc instead of silently reporting "not running"
        want = set(names)
        try:
            for d in os.scandir("/proc"):
                if d.name.isdigit():
                    try:
                        with open(f"/proc/{d.name}/comm") as f:
                            if f.read().strip() in want:
                                return True
                    except OSError:
                        pass
        except OSError:
            pass
        return False
    for n in names:
        if _run(["pgrep", "-x", n], 3).strip():
            return True
    return False


def _fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


# ----------------------------------------------------------------------------
# Add to Steam: non-Steam game shortcuts (userdata/<id>/config/shortcuts.vdf)
# ----------------------------------------------------------------------------

def _atomic_write_bytes(path: Path, data: bytes):
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".drstool-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644 & ~_umask())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------
# Undo for file writes: _backup_file() records what it saved; after a write the
# UI calls offer_undo(), which turns the backups into a one-click "Undo".
# ----------------------------------------------------------------------------

_PENDING_BAKS: List[tuple] = []        # (target, backup, time.monotonic())


# ============================================================================
# Command-line mode flag. cli_run() sets it; because it is rebound at run time other
# modules must read it as `base._CLI_MODE`, never `from .base import _CLI_MODE`.
# ============================================================================

_CLI_MODE = False
