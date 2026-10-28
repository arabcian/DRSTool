"""SettingsManager: the editor state (DRS values, arch, env vars, gamescope) and profile
persistence.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from drstool_core.models import GPUArch

from .base import _as_dict, _umask
from .catalog_data import ALL_SETTINGS, GPU_ARCHS
from .gamescope import normalize_gs_flags
from .lookup import canonical_drs_key, canonical_drs_value, drs_lookup
from .profile_io import unique_profile_name


# ============================================================================
# Settings Manager
# ============================================================================

class SettingsManager(QObject):
    settings_changed = Signal()
    arch_changed = Signal()
    profile_loaded = Signal(str)
    # Fired only when the profile *list itself* (names/current) changes —
    # save/load/delete. Previously ProfileManagerWidget rebuilt its whole
    # list on every settings_changed, i.e. on every single DRS setting edit,
    # even though the set of saved profiles hadn't changed at all.
    profiles_changed = Signal()
    profile_save_error = Signal(str)
    # Fired after profiles.json was (re)written or (re)read by us; the external-change watcher uses it.
    disk_written = Signal()
    # Emitted after a profile was written successfully (drives the
    # "unsaved changes" indicator back to clean).
    profile_saved = Signal(str)

    def __init__(self):
        super().__init__()
        self._settings: Dict[str, str] = {}
        self._arch: Optional[GPUArch] = None
        self._profiles: Dict[str, Dict] = {}
        self._current_profile: Optional[str] = None
        self._loaded_env_vars: Dict[str, str] = {}
        self._loaded_gamescope_enabled = False
        self._loaded_gamescope_flags: Dict[str, str] = {}
        self.load_warnings: List[str] = []
        self._all_settings = ALL_SETTINGS
        # profile_save_error fires from the constructor before MainWindow has
        # connected it, so the message would be lost; MainWindow re-emits this.
        self.startup_notice: str = ""
        self._load_profiles()

    def get_setting(self, setting_id: str) -> Optional[str]:
        return self._settings.get(setting_id)

    def get_settings_list(self) -> Dict[str, str]:
        return self._settings.copy()

    def set_setting(self, setting_id: str, value: str):
        self._settings[setting_id] = value
        self.settings_changed.emit()

    def remove_setting(self, setting_id: str):
        if setting_id in self._settings:
            del self._settings[setting_id]
            self.settings_changed.emit()

    def clear_all(self):
        self._settings.clear()
        self.settings_changed.emit()

    def reset_everything(self):
        """Clear DRS settings, GPU arch, and the active-profile marker in one
        go. Used by the 'Reset All' button. Replaces the previous pattern of
        calling clear_all() + set_arch(None) separately (which fired
        settings_changed twice) and reaching into the private
        self._current_profile field directly from OutputBarWidget."""
        self._settings.clear()
        self._arch = None
        self._current_profile = None
        self.arch_changed.emit()
        self.settings_changed.emit()
        self.profiles_changed.emit()  # "current profile" highlight must clear too

    def get_arch(self) -> Optional[GPUArch]:
        return self._arch

    def replace_state(self, settings: Dict[str, str], arch_code: Optional[str]):
        """Swap DRS settings + arch in one go (undo/redo, imports): one signal each."""
        self._settings = dict(settings)
        self._arch = next((a for a in GPU_ARCHS if a.code == arch_code), None) if arch_code else None
        self.arch_changed.emit()
        self.settings_changed.emit()

    def set_arch(self, arch: Optional[GPUArch]):
        self._arch = arch
        self.arch_changed.emit()
        self.settings_changed.emit()

    def get_settings_string(self) -> str:
        pairs = [f"{k}={v}" for k, v in self._settings.items() if v is not None]
        return ",".join(pairs)

    def get_full_env_string(self) -> str:
        parts = []
        if self._arch:
            parts.append(f"DXVK_NVAPI_GPU_ARCH={self._arch.code}")
        settings_str = self.get_settings_string()
        if settings_str:
            parts.append(f"DXVK_NVAPI_DRS_SETTINGS={settings_str}")
        return " ".join(parts)

    def get_profiles(self) -> Dict[str, Dict]:
        return self._profiles

    def get_current_profile(self) -> Optional[str]:
        return self._current_profile

    def save_profile(self, name: str, env_vars: Optional[Dict] = None,
                     gamescope_enabled: bool = False,
                     gamescope_flags: Optional[Dict[str, str]] = None):
        self._profiles[name] = {
            "settings": self._settings.copy(),
            "arch": self._arch.code if self._arch else None,
            "env_vars": env_vars.copy() if env_vars else {},
            "gamescope_enabled": gamescope_enabled,
            "gamescope_flags": gamescope_flags.copy() if gamescope_flags else {},
        }
        self._current_profile = name
        ok = self._save_profiles()
        self.profiles_changed.emit()
        if ok:
            self.profile_saved.emit(name)

    def load_profile(self, name: str):
        if name not in self._profiles:
            return
        profile = self._profiles[name]
        if not isinstance(profile, dict):
            # Hand-edited/corrupt entry — refuse to load rather than crash
            # on profile.get(...) below.
            self.profile_save_error.emit(
                f'Profile "{name}" is malformed in profiles.json and cannot be loaded.'
            )
            return
        # One spelling per ID (catalog form) so the sidebar dot and the enum
        # button highlight match even for hand-written / .nip-derived profiles.
        # Values too: a .nip/hand-written "0x00000001" must match the catalog's
        # "0x1" or the enum/preset button never lights up and a click on it
        # sets instead of toggling off. JSON null entries are dropped (they
        # used to become the literal string "None" in the launch line).
        self._settings = {canonical_drs_key(k): canonical_drs_value(drs_lookup(k), v)
                          for k, v in _as_dict(profile.get("settings")).items()
                          if v is not None and str(v).strip()}
        self.load_warnings: List[str] = []
        arch_code = profile.get("arch")
        if arch_code:
            self._arch = next((a for a in GPU_ARCHS if a.code == str(arch_code).strip().upper()), None)
            if self._arch is None:
                self.load_warnings.append(f"unknown GPU arch “{arch_code}” ignored")
        else:
            self._arch = None
        self._current_profile = name
        self._loaded_env_vars = _as_dict(profile.get("env_vars"))
        self._loaded_gamescope_enabled = bool(profile.get("gamescope_enabled", False))
        self._loaded_gamescope_flags, bad = normalize_gs_flags(profile.get("gamescope_flags"))
        if bad:
            self.load_warnings.append("unknown gamescope flag(s) ignored: " + ", ".join(bad))
        self.profile_loaded.emit(name)
        self.settings_changed.emit()
        self.arch_changed.emit()

    def get_loaded_env_vars(self) -> Dict:
        """Returns env vars from the last loaded profile."""
        return self._loaded_env_vars

    def get_loaded_gamescope_enabled(self) -> bool:
        """Returns gamescope enabled state from the last loaded profile."""
        return self._loaded_gamescope_enabled

    def get_loaded_gamescope_flags(self) -> Dict[str, str]:
        """Returns gamescope flag values from the last loaded profile."""
        return self._loaded_gamescope_flags

    def rename_profile(self, old: str, new: str) -> bool:
        new = new.strip()
        if not new or old not in self._profiles or (new in self._profiles and new != old):
            return False
        if new == old:
            return True
        # keep the visual order stable: rebuild the dict with the key swapped
        self._profiles = {(new if k == old else k): v for k, v in self._profiles.items()}
        if self._current_profile == old:
            self._current_profile = new
        ok = self._save_profiles()
        self.profiles_changed.emit()
        return ok

    def duplicate_profile(self, name: str) -> Optional[str]:
        if name not in self._profiles:
            return None
        new = unique_profile_name(f"{name} copy", self._profiles)
        self._profiles[new] = json.loads(json.dumps(self._profiles[name]))
        self._save_profiles()
        self.profiles_changed.emit()
        return new

    def import_profiles(self, profiles: Dict[str, dict]) -> List[str]:
        """Add profiles, renaming on name clashes. Returns the names used."""
        added = []
        for name, prof in profiles.items():
            nm = unique_profile_name(name, self._profiles)
            self._profiles[nm] = json.loads(json.dumps(prof))
            added.append(nm)
        if added:
            self._save_profiles()
            self.profiles_changed.emit()
        return added

    def delete_profile(self, name: str):
        if name in self._profiles:
            del self._profiles[name]
            if self._current_profile == name:
                self._current_profile = None
            self._save_profiles()
            self.profiles_changed.emit()

    # ── Profile file location ────────────────────────────────────────────────
    # Moved to XDG_CONFIG_HOME to match the other tools in this toolbox
    # (ryzenadj_gui.py etc. all use ~/.config/<tool>/...), instead of a
    # dotfile directly in $HOME. Old installs are migrated once, in-place.
    _OLD_PROFILES_PATH = Path.home() / ".drs_configurator_profiles.json"

    @staticmethod
    def _profiles_path() -> Path:
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "drstool"
        return config_dir / "profiles.json"

    def _load_profiles(self):
        data_file = self._profiles_path()
        try:
            if not data_file.exists() and self._OLD_PROFILES_PATH.exists():
                # One-time migration from the old ~/.drs_configurator_profiles.json
                data_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._OLD_PROFILES_PATH, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                # A top-level non-dict (e.g. a list from a truncated/edited
                # file) would crash every `for name in profiles` later —
                # validate before adopting it.
                if not isinstance(legacy, dict):
                    raise ValueError("legacy profiles file is not a JSON object")
                self._profiles = legacy
                self._save_profiles()
                return
            if data_file.exists():
                with open(data_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("profiles file is not a JSON object")
                self._profiles = loaded
        except Exception:
            # Corrupt or unreadable file. Starting with an empty in-memory set
            # is fine, but the very next save_profile() would rewrite the file
            # and destroy whatever was recoverable in it — so move it aside
            # first instead of merely leaving it "untouched".
            moved = ""
            try:
                if data_file.exists():
                    bad = data_file.with_name(
                        f"profiles.json.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                    os.replace(data_file, bad)
                    moved = f" The unreadable file was kept as {bad.name}."
            except OSError:
                pass
            self._profiles = {}
            msg = (f"Could not read profiles from {data_file} — starting with no "
                   f"profiles this session.{moved}")
            self.startup_notice = msg
            self.profile_save_error.emit(msg)

    # ── Profile history (snapshots of the state each save replaces) ──────────
    _HISTORY_KEEP = 60

    @staticmethod
    def _history_dir() -> Path:
        return SettingsManager._profiles_path().parent / "history"

    def list_snapshots(self) -> List[Path]:
        try:
            return sorted(self._history_dir().glob("profiles-*.json"), reverse=True)
        except OSError:
            return []

    def _snapshot_old(self, data_file: Path, new_text: str):
        """Keep the file we are about to overwrite, unless nothing actually changes."""
        try:
            if not data_file.exists():
                return
            old = data_file.read_bytes()
            if not old.strip() or old == new_text.encode("utf-8"):
                return
            hd = self._history_dir()
            hd.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (hd / f"profiles-{stamp}.json").write_bytes(old)
            for p in sorted(hd.glob("profiles-*.json"))[:-self._HISTORY_KEEP]:
                p.unlink(missing_ok=True)
        except OSError:
            pass

    def restore_profile(self, name: str, data) -> bool:
        if not isinstance(data, dict):
            return False
        self._profiles[name] = json.loads(json.dumps(data))
        ok = self._save_profiles()
        self.profiles_changed.emit()
        return ok

    def restore_all_profiles(self, profiles: Dict) -> bool:
        if not isinstance(profiles, dict):
            return False
        self._profiles = json.loads(json.dumps(profiles))
        if self._current_profile not in self._profiles:
            self._current_profile = None
        ok = self._save_profiles()
        self.profiles_changed.emit()
        return ok

    def reload_from_disk(self, merge: bool) -> str:
        """Adopt an externally modified profiles.json. merge=True keeps our profiles on
        name conflicts and only adds the ones that exist solely on disk."""
        data_file = self._profiles_path()
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if not isinstance(disk, dict):
                raise ValueError("not a JSON object")
        except Exception as ex:
            self.disk_written.emit()
            return f"profiles.json on disk is unreadable ({ex}) — kept the in-memory profiles."
        if merge:
            added = [n for n in disk if n not in self._profiles]
            clash = [n for n in disk if n in self._profiles and disk[n] != self._profiles[n]]
            for n in added:
                self._profiles[n] = disk[n]
            ok = self._save_profiles()
            self.profiles_changed.emit()
            return (f"Merged: {len(added)} profile(s) added from disk, "
                    f"{len(clash)} differing one(s) kept as in DRSTool." + ("" if ok else " (save failed)"))
        self._profiles = disk
        if self._current_profile not in self._profiles:
            self._current_profile = None
        self.disk_written.emit()
        self.profiles_changed.emit()
        return f"Reloaded {len(disk)} profile(s) from disk."

    def _save_profiles(self) -> bool:
        """Write profiles to disk atomically. Returns True on success."""
        data_file = self._profiles_path()
        try:
            data_file.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self._profiles, indent=2)
            # unique temp name: two running instances used to share "<file>.json.tmp"
            fd, tmp_name = tempfile.mkstemp(dir=str(data_file.parent), prefix=f".{data_file.name}.", suffix=".tmp")
            tmp_file = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                if data_file.exists():             # mkstemp is 0600; keep the file's existing mode
                    shutil.copymode(data_file, tmp_file)
                else:
                    os.chmod(tmp_file, 0o644 & ~_umask())
                self._snapshot_old(data_file, text)
                os.replace(tmp_file, data_file)  # atomic on POSIX — no partial-write corruption
            except BaseException:
                tmp_file.unlink(missing_ok=True)
                raise
            self.disk_written.emit()
            return True
        except Exception as e:
            self.profile_save_error.emit(f"Failed to save profiles to {data_file}: {e}")
            return False
