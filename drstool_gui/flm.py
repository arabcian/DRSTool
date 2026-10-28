"""vk_flip_meter page: settings, live tuning and installer."""

import os
import re
import shutil
import signal
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import (
    QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QVBoxLayout, QWidget,
)

from .base import (
    default_flm_conf_path, detect_libdir, find_bundled_dir, pkexec_auth_failed, user_cache_dir,
)
from .tool_ui import _tool_btn, FLM_FIELD_SS


# FLM v2.x variables removed in v3.0 (mirrors k_legacy_keys in
# flip_meter.cpp). Owned by DRSTool → deleted from game YAMLs on Apply.
FLM_LEGACY_ENV_KEYS = (
    "FLM_PACE_POINT", "FLM_PACE_FIFO", "FLM_PRESENT_LEAD_NS", "FLM_SPIN_NS",
    "FLM_SPIN_ADAPT", "FLM_DRIFT_TOLERANCE_NS", "FLM_FLOOR_PACING",
    "FLM_FLOOR_MFG_ADAPT", "FLM_FLOOR_MFG_STEP", "FLM_FLOOR_AUTOTUNE",
    "FLM_FLOOR_AUTOTUNE_MAX", "FLM_WARMUP_FRAMES", "FLM_HITCH_RECOVERY",
    "FLM_HITCH_THRESHOLD_MS", "FLM_PROBE_PERIOD_S", "FLM_PROBE_FLIPS",
    "FLM_STATS_INTERVAL", "FLM_CSV_SYNC_S", "FLM_VERBOSE",
)

# Keys the layer accepts through the FLM_CONFIG file + SIGUSR1 hot-reload.
# Mirrors apply_dynamic_kv() in flip_meter.cpp (v3.0) —
# keep in sync when the layer gains/loses reloadable knobs.
FLM_RELOADABLE_KEYS = (
    "FLM_MODE", "FLM_PROFILE", "FLM_TARGET_FPS", "FLM_FLOOR_RATIO", "FLM_LOG_LEVEL",
)


class FlmInstallWidget(QWidget):
    """
    Right-panel build/install widget for vk_flip_meter.
    Source is bundled at _SCRIPT_DIR/vk-flip-meter-main — no picker needed.
    Unprivileged: cmake configure, cmake --build.
    Privileged (pkexec, graphical password prompt): cmake --install.
    """

    _STEPS = ("configure", "build", "install", "verify")

    # Bundled source path (repo layout: DRSTool/vk-flip-meter-main/)
    _BUNDLED_SRC = find_bundled_dir("vk-flip-meter-main", "CMakeLists.txt")
    _out_buf: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._phase: str = ""
        self._build_dir: str = ""
        self._prefix_used: str = ""
        self._libdir_used: str = "lib64"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QLabel("vk_flip_meter — Build & Install")
        header.setStyleSheet("color:#eceff4; font-size:18px; font-weight:700;")
        layout.addWidget(header)

        desc = QLabel(
            "Source is bundled in the repository at <code>vk-flip-meter-main/</code>.  "
            "The build (cmake configure + build) runs as a normal user.  Only "
            "<code>cmake --install</code> — the step that writes to <code>/usr/local</code> — "
            "runs through pkexec with a graphical password prompt."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("color:#b3bac7; font-size:13px; line-height:140%;")
        layout.addWidget(desc)

        # Source path banner (read-only, bundled)
        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        src_icon = QLabel("📁")
        src_lbl = QLabel("Source:")
        src_lbl.setStyleSheet("color:#8f9bb0; font-size:12px; font-weight:600;")
        self._src_path_lbl = QLabel(str(self._BUNDLED_SRC))
        self._src_path_lbl.setStyleSheet(
            "color:#76b900; font-size:12px; font-family:monospace;"
        )
        src_ok = (self._BUNDLED_SRC / "CMakeLists.txt").is_file()
        self._src_status = QLabel("✔ found" if src_ok else "✘ not found")
        self._src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; font-size:12px; font-weight:700;"
        )
        src_row.addWidget(src_icon)
        src_row.addWidget(src_lbl)
        src_row.addWidget(self._src_path_lbl, 1)
        src_row.addWidget(self._src_status)
        layout.addLayout(src_row)

        form = QGridLayout()
        form.setSpacing(8)

        prefix_lbl = QLabel("Install prefix:")
        prefix_lbl.setStyleSheet("color:#8f9bb0; font-size:12px; font-weight:600;")
        self._prefix_edit = QLineEdit("/usr/local")
        self._prefix_edit.setStyleSheet(FLM_FIELD_SS)
        form.addWidget(prefix_lbl, 0, 0)
        form.addWidget(self._prefix_edit, 0, 1)

        layout.addLayout(form)

        self._native_chk = QCheckBox(
            "FLM_NATIVE_BUILD  (-O3 -march=native -mtune=native -flto — specific to this machine, not portable)"
        )
        self._native_chk.setStyleSheet("""
QCheckBox{ color:#dfe3ea; font-size:12px; spacing:8px; }
QCheckBox::indicator{ width:14px; height:14px; border:1px solid #2a313d; border-radius:3px; background:#181c24; }
QCheckBox::indicator:checked{ background:#76b900; border:1px solid #76b900; }
""")
        layout.addWidget(self._native_chk)

        native_warn = QLabel(
            "If enabled: the .so built for the 7845HX may not run on a different CPU (a different machine)."
        )
        native_warn.setStyleSheet("color:#7b8496; font-size:13px; font-style:italic;")
        layout.addWidget(native_warn)

        btn_row = QHBoxLayout()
        self._build_btn = _tool_btn("Build & Install (pkexec)", "#76b900", "#76b900", "#0c0e12", bold=True)
        self._build_btn.clicked.connect(self._on_build_clicked)

        self._verify_btn = _tool_btn("Verify Installation")
        self._verify_btn.clicked.connect(self._on_verify_clicked)

        clear_btn = _tool_btn("Clear Console")
        clear_btn.clicked.connect(lambda: self._console.clear())

        btn_row.addWidget(self._build_btn)
        btn_row.addWidget(self._verify_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_lbl = QLabel("Ready.")
        self._status_lbl.setStyleSheet("color:#7b8496; font-size:13px;")
        layout.addWidget(self._status_lbl)

        # ── Live tuning: FLM_CONFIG file + SIGUSR1 ───────────────────────────
        # The layer's env is snapshotted once at init [FIX-26]; the ONLY way
        # to retune a running game is FLM_CONFIG file + SIGUSR1. This section
        # turns the manual `echo KEY=VAL > /tmp/flm.conf && kill -USR1 ...`
        # dance into two buttons: dump the currently-set hot-reloadable FLM_*
        # vars (from the Env Vars tab) into the .conf, then signal the game.
        live_group = QGroupBox("Live Tuning  (FLM_CONFIG + SIGUSR1)")
        live_group.setStyleSheet("""
QGroupBox{ color:#8f9bb0; font-size:12px; font-weight:700;
    border:1px solid #1d222c; border-radius:6px;
    margin-top:8px; padding-top:12px; }
QGroupBox::title{ subcontrol-origin: margin; left:8px; padding:0 4px; }
""")
        live_layout = QGridLayout(live_group)
        live_layout.setSpacing(6)

        conf_lbl = QLabel("Config file:")
        conf_lbl.setStyleSheet("color:#8f9bb0; font-size:13px; font-weight:600;")
        self._conf_edit = QLineEdit(str(default_flm_conf_path()))
        self._conf_edit.setStyleSheet(FLM_FIELD_SS)
        self._write_conf_btn = _tool_btn("Write .conf")
        self._write_conf_btn.clicked.connect(self._on_write_conf)
        live_layout.addWidget(conf_lbl, 0, 0)
        live_layout.addWidget(self._conf_edit, 0, 1)
        live_layout.addWidget(self._write_conf_btn, 0, 2)

        proc_lbl = QLabel("Process match:")
        proc_lbl.setStyleSheet("color:#8f9bb0; font-size:13px; font-weight:600;")
        self._proc_edit = QLineEdit()
        self._proc_edit.setStyleSheet(FLM_FIELD_SS)
        self._proc_edit.setPlaceholderText("optional regex on the command line, e.g. GameName.exe")
        self._sigusr1_btn = _tool_btn("Send SIGUSR1")
        self._sigusr1_btn.clicked.connect(self._on_send_sigusr1)
        live_layout.addWidget(proc_lbl, 1, 0)
        live_layout.addWidget(self._proc_edit, 1, 1)
        live_layout.addWidget(self._sigusr1_btn, 1, 2)

        live_hint = QLabel(
            "Only works if the game was launched with FLM_CONFIG pointing at "
            "this file. Init-only vars (FLM_CSV, FLM_RT_PRIORITY, ...) are "
            "written as comments — the layer can't change them at runtime."
        )
        live_hint.setWordWrap(True)
        live_hint.setStyleSheet("color:#7b8496; font-size:13px; font-style:italic;")
        live_layout.addWidget(live_hint, 2, 0, 1, 3)

        layout.addWidget(live_group)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setStyleSheet("""
QPlainTextEdit{
    background:#08090c;
    border:1px solid #1d222c;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:13px;
    padding:6px;
}
""")
        layout.addWidget(self._console, 1)

        self._autocheck_src()

    # ── bundled source check ─────────────────────────────────────────────────

    def _autocheck_src(self):
        """Update the source status banner for the bundled path."""
        src_ok = (self._BUNDLED_SRC / "CMakeLists.txt").is_file()
        self._src_status.setText("✔ found" if src_ok else "✘ not found")
        self._src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; font-size:12px; font-weight:700;"
        )

    # ── console ──────────────────────────────────────────────────────────────

    def _log(self, text: str):
        self._console.appendPlainText(text)
        self._console.verticalScrollBar().setValue(self._console.verticalScrollBar().maximum())

    def _set_busy(self, busy: bool, status: str = ""):
        self._build_btn.setEnabled(not busy)
        self._verify_btn.setEnabled(not busy)
        self._status_lbl.setText(status or ("Working..." if busy else "Ready."))

    # ── live tuning (FLM_CONFIG + SIGUSR1) ───────────────────────────────────

    def _collect_flm_values(self) -> Dict[str, str]:
        """Currently-set FLM_* env vars from the shared Env Vars tab."""
        win = self.window()
        if win and hasattr(win, "_env_widget"):
            return {k: v for k, v in win._env_widget.get_env_dict().items()
                    if k.startswith("FLM_")}
        return {}

    def _on_write_conf(self):
        path_str = self._conf_edit.text().strip()
        if not path_str:
            self._log("ERROR: config file path is empty.")
            return
        values = self._collect_flm_values()
        if not values:
            self._log("Nothing to write — no FLM_* vars are set on the Env Vars tab.")
            return

        # One KEY=VALUE per line: a value carrying a newline (imported profile)
        # would otherwise smuggle extra keys into the layer's reload file.
        values = {k: " ".join(str(v).splitlines()) for k, v in values.items()}
        reloadable = {k: v for k, v in values.items() if k in FLM_RELOADABLE_KEYS}
        init_only  = {k: v for k, v in values.items()
                      if k not in FLM_RELOADABLE_KEYS and k != "FLM_CONFIG"}

        lines = [
            "# vk_flip_meter live config — written by DRSTool",
            f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "# Apply with: kill -USR1 <game pid>  (or the Send SIGUSR1 button)",
            "",
        ]
        lines += [f"{k}={v}" for k, v in reloadable.items()]
        if init_only:
            lines.append("")
            lines.append("# init-only (not hot-reloadable — set in the launch env instead):")
            lines += [f"# {k}={v}" for k, v in init_only.items()]

        try:
            path = Path(os.path.expanduser(path_str))
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unpredictable temp name in the same directory, then an atomic
            # rename: SIGUSR1 mid-write can't see a torn file.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            self._log(f"ERROR: could not write {path_str}: {e}")
            return

        self._log(f"Wrote {len(reloadable)} reloadable key(s) to {path_str}"
                  + (f" ({len(init_only)} init-only key(s) as comments)" if init_only else ""))
        # If the game env doesn't point FLM_CONFIG at this exact path, the
        # reload will read a different file (or nothing) — warn early.
        cfg = values.get("FLM_CONFIG", "")
        if not cfg:
            self._log("NOTE: FLM_CONFIG isn't set on the Env Vars tab — the game must be "
                      f"launched with FLM_CONFIG={path_str} for SIGUSR1 to pick this up.")
        elif Path(os.path.expanduser(cfg)) != path:   # "~/x" vs "/home/u/x" is the same file
            self._log(f"WARNING: Env Vars tab has FLM_CONFIG={cfg}, but this file was "
                      f"written to {path_str} — the running game will reload the former.")

    @staticmethod
    def _find_layer_pids(pattern: str):
        """(pid, cmdline) of this user's processes that have libvk_flip_meter
        mapped, optionally filtered by a regex on the command line. `pkill -f`
        matched ANY command line containing the pattern (Lutris, wrappers,
        DRSTool itself) and SIGUSR1's default action kills a process."""
        rx = None
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error:
                rx = re.compile(re.escape(pattern))
        me, uid = os.getpid(), os.getuid()
        found = []
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == me:
                continue
            pid = int(d)
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                with open(f"/proc/{pid}/maps", "rb") as f:
                    if b"libvk_flip_meter" not in f.read():
                        continue
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                continue
            if rx and not rx.search(cmd):
                continue
            found.append((pid, cmd))
        return found

    def _on_send_sigusr1(self):
        pattern = self._proc_edit.text().strip()
        pids = self._find_layer_pids(pattern)
        if not pids:
            self._log("No process with the vk_flip_meter layer loaded"
                      + (f" matches '{pattern}'" if pattern else "")
                      + " — is the game running with ENABLE_LAYER_cpu_flip_meter=1?")
            self._status_lbl.setText("No process matched.")
            return
        sent = 0
        for pid, cmd in pids:
            try:
                os.kill(pid, signal.SIGUSR1)
                sent += 1
                self._log(f"SIGUSR1 -> {pid}  {cmd[:100]}")
            except OSError as e:
                self._log(f"Could not signal {pid}: {e}")
        if sent:
            self._log("SIGUSR1 sent — the layer will re-read FLM_CONFIG on its next frame.")
            self._status_lbl.setText(f"SIGUSR1 sent to {sent} process(es).")

    # ── build / install pipeline ─────────────────────────────────────────────

    def _on_build_clicked(self):
        src = str(self._BUNDLED_SRC)
        prefix = self._prefix_edit.text().strip() or "/usr/local"

        if not (Path(src) / "CMakeLists.txt").is_file():
            QMessageBox.warning(
                self, "Bundled source not found",
                f"CMakeLists.txt not found at:\n  {src}\n\n"
                "Make sure the vk-flip-meter-main/ directory is present "
                "next to DRSTool.py (it is part of the DRSTool repository)."
            )
            return

        self._prefix_used = prefix
        # Out-of-tree build in the user's cache: the bundled source may be
        # read-only (/usr/share/drstool) and must stay clean when it is not.
        self._build_dir = str(self._prepare_build_dir(Path(src)))
        native = "ON" if self._native_chk.isChecked() else "OFF"
        generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"

        self._console.clear()
        self._log(f"==> Source: {src}")
        self._log(f"==> Configuring  (generator={generator}, FLM_NATIVE_BUILD={native}, prefix={prefix})")
        self._set_busy(True, "Configuring (cmake configure)...")
        self._phase = "configure"

        # FLM_LIB_PATH is injected so the manifest template gets the correct
        # lib64 path; CMake's configure_file handles the manifest (FIX-52/61).
        libdir = detect_libdir()
        self._libdir_used = libdir
        flm_lib_path = f"{prefix}/{libdir}/libvk_flip_meter.so"
        args = [
            "-S", src, "-B", self._build_dir,
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            f"-DCMAKE_INSTALL_LIBDIR={libdir}",
            f"-DFLM_NATIVE_BUILD={native}",
            f"-DFLM_LIB_PATH={flm_lib_path}",
            "-G", generator,
        ]
        self._run_process("cmake", args, use_pkexec=False)

    @staticmethod
    def _prepare_build_dir(src: Path) -> Path:
        build = user_cache_dir() / "flm-build"
        cache = build / "CMakeCache.txt"
        try:
            if cache.is_file():
                m = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$",
                              cache.read_text(errors="replace"), re.M)
                # A cache from a different source path makes cmake refuse to configure.
                if m and Path(m.group(1)) != src:
                    shutil.rmtree(build, ignore_errors=True)
        except OSError:
            pass
        build.mkdir(parents=True, exist_ok=True)
        return build

    def _on_verify_clicked(self):
        self._console.clear()
        self._log("==> vulkaninfo --summary | grep -i flip_meter")
        self._set_busy(True, "Verifying...")
        self._phase = "verify"
        cmd = "vulkaninfo --summary 2>/dev/null | grep -i flip_meter || echo 'flip_meter not found — check whether the layer is loaded / vulkaninfo is installed.'"
        self._run_process("bash", ["-c", cmd], use_pkexec=False)

    def _run_process(self, program: str, args: List[str], use_pkexec: bool):
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self._log("A step is already running — please wait.")
            return
        self._out_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_output(p))
        proc.finished.connect(self._on_proc_finished)
        # QProcess.finished NEVER fires on FailedToStart (binary not found /
        # not executable) — without this handler a missing cmake, ninja, or
        # pkexec left the whole widget stuck on "Working..." with both
        # buttons disabled until app restart.
        proc.errorOccurred.connect(lambda err, p=proc: self._on_proc_error(err, p))
        self._proc = proc
        if use_pkexec:
            proc.start("pkexec", [program] + args)
        else:
            proc.start(program, args)

    def _on_output(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        if data:
            self._out_buf = (self._out_buf + data)[-4000:]
            self._log(data.rstrip("\n"))

    def _on_proc_error(self, error, proc: QProcess):
        if error == QProcess.FailedToStart:
            self._log(f"ERROR: could not start '{proc.program()}' — is it installed and in PATH?")
            self._set_busy(False, f"Error: '{proc.program()}' failed to start.")
        # Crashed is followed by finished(CrashExit) — handled there.

    def _on_proc_finished(self, exit_code: int, exit_status):
        if exit_status == QProcess.CrashExit:
            self._log(f"ERROR: step '{self._phase}' crashed (killed by a signal).")
            self._set_busy(False, f"Error: step '{self._phase}' crashed.")
            return
        if exit_code != 0:
            # 126/127 only mean "auth cancelled" when pkexec says so (a missing
            # program exits 127 as well).
            if self._phase == "install" and pkexec_auth_failed(exit_code, self._out_buf):
                self._log("Authorization cancelled/denied — installation aborted (build output kept).")
                self._set_busy(False, "Install cancelled (pkexec authorization).")
                return
            self._log(f"ERROR: step '{self._phase}' failed (exit code {exit_code}).")
            self._set_busy(False, f"Error: step '{self._phase}' failed.")
            return

        if self._phase == "configure":
            self._log("==> Building...")
            self._set_busy(True, "Building (cmake --build)...")
            self._phase = "build"
            nproc = str(os.cpu_count() or 4)
            self._run_process("cmake", ["--build", self._build_dir, "-j", nproc], use_pkexec=False)

        elif self._phase == "build":
            self._log("==> Requesting pkexec authorization for install (root required)...")
            self._set_busy(True, "Waiting for pkexec password...")
            self._phase = "install"
            # cmake --install writes the manifest with the correct FLM_LIB_PATH
            # baked in at configure time (FIX-52/61) — no post-install sed needed.
            self._run_process("cmake", ["--install", self._build_dir], use_pkexec=True)

        elif self._phase == "install":
            self._log("==> Installation complete.")
            self._log(f"    Layer installed to: {self._prefix_used}/{self._libdir_used}/libvk_flip_meter.so")
            self._log("    Manifest: share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json")
            self._log("    Verify: vulkaninfo --summary | grep -i flip_meter")
            self._set_busy(False, "Installation complete.")

        elif self._phase == "verify":
            self._set_busy(False, "Ready.")
