"""lutris-game-tune page: settings and installer."""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from .base import find_bundled_dir, pkexec_auth_failed
from .theme import SCROLLABLE_CONTROL_QSS
from .tool_ui import _tool_btn, _TOOL_GROUP_SS, FLM_FIELD_SS
from .widgets import _NO_SCROLL_FILTER


# ============================================================================
# Privilege model — vk_flip_meter build and lutris-game-tune
# ============================================================================
#
# vk_flip_meter build: UNPRIVILEGED (cmake configure + cmake --build, plain
#   QProcess). Only cmake --install and the manifest path fixup need root —
#   those two steps run through pkexec.
#
# lutris-game-tune: all operations that touch /etc/ or the setuid wrapper
#   run through pkexec. Config is read from /etc/lutris-game-tune.conf at
#   startup and written back atomically via pkexec tee.

# Installation paths (detection)
LGTUNE_WRAPPER = Path("/usr/local/bin/lutris-game-tune-wrapper")
LGTUNE_CONF    = Path("/etc/lutris-game-tune.conf")
LGTUNE_LOG     = Path("/var/log/lutris-game-tune.log")
LGTUNE_LIB_DIR = Path("/usr/local/lib/lutris-game-tune")
LGTUNE_POLICY  = Path("/usr/share/polkit-1/actions/org.drstool.lutris-game-tune.policy")


def lgtune_mode() -> str:
    """How the wrapper is installed: "setuid", "polkit" (no setuid; PRE/POST via pkexec,
    RUN via the cap_sys_nice file capability) or "" (not installed / unknown)."""
    try:
        st = os.stat(LGTUNE_WRAPPER)
    except OSError:
        return ""
    if st.st_mode & 0o4000:
        return "setuid"
    return "polkit" if LGTUNE_POLICY.is_file() else ""


def lgtune_cmd(action: str, wrapper=None) -> str:
    """Command line for PRE / POST in the current install mode."""
    w = str(wrapper or LGTUNE_WRAPPER)
    return f"pkexec {w} {action}" if lgtune_mode() == "polkit" else f"{w} {action}"


# ============================================================================
# lutris-game-tune — Settings + Installer widget
# ============================================================================
#
# All operations that touch /etc/ or /usr/local/bin/ are routed through
# pkexec so the GUI itself never runs as root.
#
# Config key whitelist mirrors load_config() in lutris-game-tune.sh v4.1.
# The script parses KEY=VALUE strictly (no shell source), so we generate
# the same format: one KEY=VALUE per line, comment headers for readability.
#
# Detection heuristic: wrapper binary at /usr/local/bin/lutris-game-tune-wrapper
# must exist and be executable.  Config at /etc/lutris-game-tune.conf is
# optional (populated with defaults when absent).

# ── Config key metadata ───────────────────────────────────────────────────────
# Each entry: (key, widget_type, default, label, description, options/range)
#   widget_type: "check" | "combo" | "spin" | "line" | "line_space"
#   "line_space" = QLineEdit accepting space-separated tokens (proc names etc.)
#   options: list[str] for combo, (min,max) tuple for spin, None otherwise

LGTUNE_KEYS: List[tuple] = [
    # ── CPU Governor / EPP ────────────────────────────────────────────────────
    ("SET_CPU_GOVERNOR", "check", "1",
     "Set CPU governor on game start",
     "Write the chosen CPU_GOVERNOR (and EPP if amd-pstate is active) when a "
     "game launches via the PRE hook, then restore the previous governor on "
     "POST.  Disabling this leaves the governor wherever the system had it."),

    ("CPU_GOVERNOR", "combo", "performance",
     "CPU governor",
     "Frequency-scaling governor applied during gameplay.  'performance' pins "
     "all cores at max boost and also sets the Energy Performance Preference "
     "(EPP) to 'performance' when amd-pstate active mode is active — "
     "critical for Ryzen mobile CPUs where boost is EPP-gated.  'schedutil' "
     "is a good compromise for battery: it reacts to scheduler load signals "
     "rather than a fixed ceiling.",
     ["performance", "powersave", "schedutil", "ondemand", "conservative"]),

    ("SET_EPP_BOOST", "check", "1",
     "Enable amd-pstate per-core EPP boost during gameplay",
     "Writes 1 to /sys/module/amd_pstate/parameters/epp_boost on PRE and "
     "restores the previous value on POST.  This is the per-core epp_boost "
     "mechanism: an update-util hook samples each core's C0 residency every "
     "10 ms and, when a core is at least 50% busy, pins that core's EPP field "
     "to performance (0) until 300 ms pass without another busy sample.  It "
     "targets the case a plain governor change cannot fix — a render thread "
     "that blocks briefly on a futex or GPU fence every frame decays the "
     "hardware's performance signal, so each post-wakeup burst restarts at a "
     "low operating point and inflates the frame-time tail.  Unlike forcing "
     "EPP=performance globally, the boost is per-core and short-lived, so "
     "idle cores are not held at high power.  NOTE: this knob is NOT in "
     "mainline as of this writing — it only exists on kernels built with the "
     "epp_boost patch series.  On kernels without it the script skips the "
     "write silently, so leaving this enabled is harmless either way."),

    # ── AMD 3D V-Cache ────────────────────────────────────────────────────────
    ("SET_X3D_VCACHE_MODE", "check", "1",
     "Set AMD 3D V-Cache core preference on game start",
     "Writes X3D_VCACHE_MODE to the amd_x3d_vcache driver's amd_x3d_mode "
     "sysfs file on PRE and restores the previous value on POST.  AMD 3D "
     "V-Cache CPUs only (dual-CCD parts where one CCD has the extra L3 die "
     "stacked on it).  The driver's instance directory (an ACPI device id, "
     "e.g. AMDI0101:00) is discovered at runtime rather than hardcoded, "
     "since it isn't guaranteed to be the same on every board/BIOS.  "
     "Silently skipped if the driver isn't bound (non-X3D CPU or a kernel "
     "without the driver), so leaving this enabled is harmless either way."),

    ("X3D_VCACHE_MODE", "combo", "frequency",
     "AMD 3D V-Cache core preference",
     "Value written to the amd_x3d_mode sysfs file.  'frequency' asks the "
     "scheduler to prefer the higher-clocking CCD (usually better for "
     "CPU-bound / high-refresh-rate titles).  'cache' asks it to prefer "
     "cores on the CCD with the larger L3 (usually better for games that "
     "are sensitive to cache misses, e.g. large open-world titles).",
     ["frequency", "cache"]),

    # ── PCIe ASPM ─────────────────────────────────────────────────────────────
    ("SET_ASPM", "check", "1",
     "Set PCIe ASPM policy on game start",
     "Write ASPM_POLICY to /sys/module/pcie_aspm/parameters/policy during PRE "
     "and restore the previous policy on POST.  PCIe ASPM (Active State Power "
     "Management) controls link-power transitions between the CPU and "
     "PCIe devices.  In the 'default' or 'powersave' modes the link can "
     "briefly enter low-power states, adding ~100 µs wake-up latency each "
     "time.  Switching to 'performance' prevents those transitions during "
     "gameplay, which can reduce frame-time jitter on GPU-bound titles."),

    ("ASPM_POLICY", "combo", "performance",
     "PCIe ASPM policy",
     "Policy written to /sys/module/pcie_aspm/parameters/policy.  "
     "'performance' disables link power-state transitions entirely — "
     "the PCIe bus stays at full power.  'default' lets the firmware/driver "
     "decide (usually a mild power-save level).  'powersave' maximises "
     "link-power gating (useful for battery, bad for latency).",
     ["performance", "default", "powersave", "powersupersave"]),

    # ── Deep C-states ─────────────────────────────────────────────────────────
    ("DISABLE_DEEP_CSTATES", "check", "0",
     "Disable deep CPU C-states during gameplay",
     "Writes '1' to /sys/devices/system/cpu/cpu*/cpuidle/state{N}/disable for "
     "all states deeper than CSTATE_KEEP_MAX.  Deep C-states (C6/C7/C10 on "
     "Zen 4) have longer exit latencies (up to ~250 µs for CC10) that can "
     "appear as occasional frame-time spikes ('hitches') when the package "
     "wakes from a deep sleep mid-frame.  Disabling them keeps cores in "
     "C1/POLL at the cost of ~3–8 W of extra idle power draw — worth it "
     "while plugged in on a laptop, less so on battery."),

    ("CSTATE_KEEP_MAX", "spin", "1",
     "Deepest C-state to keep enabled",
     "Index of the deepest idle state that stays enabled when "
     "DISABLE_DEEP_CSTATES=1.  0 = POLL (active spin, lowest latency, "
     "highest power), 1 = C1/C1E (light halt, ~1 µs exit, recommended).  "
     "States at index > this value are disabled.  On Zen 4 (7845HX): "
     "state 0=POLL, 1=C1, 2=C6, 3=C7, 4=CC10 — so CSTATE_KEEP_MAX=1 "
     "disables C6/C7/CC10 while keeping the fast C1 path alive.",
     (0, 10)),

    # ── VM ────────────────────────────────────────────────────────────────────
    ("VM_SWAPPINESS", "spin", "10",
     "vm.swappiness during gameplay",
     "Value written to /proc/sys/vm/swappiness via sysctl during PRE, "
     "restored on POST.  Controls how aggressively the kernel reclaims "
     "anonymous pages by swapping them out.  The Linux default is 60; "
     "a lower value (10–20) makes the kernel prefer reclaiming the page "
     "cache instead of swapping game data out, reducing random-access "
     "latency spikes caused by swap I/O during gameplay.  Setting it to 0 "
     "disables swap entirely while in game — only safe if you have enough "
     "RAM for the game plus the OS.",
     (0, 100)),

    # ── Transparent HugePages ─────────────────────────────────────────────────
    ("THP_ENABLED", "combo", "madvise",
     "THP enabled",
     "Value written to /sys/kernel/mm/transparent_hugepage/enabled.  "
     "'always' maps all anonymous allocations to 2 MB huge pages — lowest "
     "TLB pressure but can fragment memory and cause occasional allocation "
     "stalls.  'madvise' (recommended) only uses huge pages for regions "
     "that explicitly call madvise(MADV_HUGEPAGE) — Wine, DXVK, and VKD3D "
     "all do this, so game data gets huge pages while OS metadata doesn't.  "
     "'never' disables THP completely.",
     ["always", "madvise", "never"]),

    ("THP_SHMEM_ENABLED", "combo", "madvise",
     "THP shmem_enabled",
     "Value written to /sys/kernel/mm/transparent_hugepage/shmem_enabled.  "
     "Controls THP for shared-memory (shmem/tmpfs) mappings — used by "
     "Vulkan drivers for command/descriptor buffers and by Wine's shared "
     "memory segments.  'madvise' is the safe default: huge pages are used "
     "only when the allocation explicitly requests them.",
     ["always", "within_size", "advise", "never", "deny", "force"]),

    ("THP_DEFRAG", "combo", "madvise",
     "THP defrag",
     "Value written to /sys/kernel/mm/transparent_hugepage/defrag.  "
     "Controls how hard the kernel tries to compact memory to satisfy "
     "huge-page requests.  'always' defragments synchronously — can stall "
     "allocations for tens of milliseconds.  'defer' defrags asynchronously "
     "via a background khugepaged thread — no stalls but slower promotion.  "
     "'madvise' only defrags when madvise(MADV_HUGEPAGE) was called.  "
     "'never' skips defrag entirely.",
     ["always", "defer", "defer+madvise", "madvise", "never"]),

    # ── CCD/CCX core isolation ────────────────────────────────────────────────
    ("CCD_ISOLATION_ENABLED", "check", "1",
     "Enable CCD/CCX core isolation",
     "When enabled, the script detects the CPU's CCD topology at PRE time "
     "and pins the game (and its child processes) to CCD 0 while confining "
     "background system processes to CCD 1.  On a Ryzen 9 7845HX (1×CCD, "
     "single-CCX) this is automatically skipped — the script silently does "
     "nothing.  On multi-CCD Ryzen desktop parts (e.g. 5900X, 7950X) it "
     "removes cross-CCD memory-latency variance from game threads, which "
     "can reduce frame-time jitter.  Disable if you see scheduling issues "
     "or if you only have one CCD."),

    ("CCD_LAUNCHER", "line", "lutris",
     "Launcher process name",
     "Process name searched for with 'pgrep -x' to identify the launcher "
     "that will be the root of the CCD isolation group.  'lutris' for Lutris, "
     "'steam' for Steam, 'heroic' for Heroic, 'bottles' for Bottles.  "
     "The script pins this process and all of its descendants to CCD 0 "
     "(theGood cgroup)."),

    ("CCD_EXTRA_GOOD_PROCS", "line_space", "",
     "Extra processes for the performance CCD",
     "Space-separated list of process names (pgrep -x) to explicitly add to "
     "theGood (CCD 0 / performance) cgroup in addition to the launcher's "
     "process tree.  Useful for helper processes that are launched by the "
     "game but are not direct children of the launcher — e.g. gamescope, "
     "picom, wine64-preloader.  Leave empty if CCD_ISOLATION_ENABLED=0."),

    ("CCD_MONITOR_SECONDS", "spin", "30",
     "Child-process scan duration (seconds)",
     "How long (in seconds) after the launcher is moved to theGood the "
     "script keeps scanning for newly spawned child processes (shader "
     "compilation threads, DXVK/VKD3D background workers) and adds them "
     "to the performance CCD.  0 = one-shot scan only.  30–60 s covers "
     "most shader-compilation windows at game startup.",
     (0, 300)),

    ("CCD_PROTECTED_PROCS", "line_space",
     "elogind elogind-daemon systemd-logind dbus-daemon dbus-broker "
     "polkitd openrc-user login gdm gdm3 sddm lightdm",
     "Protected process names (never moved)",
     "Space-separated list of process names that must never be swept into "
     "the system CCD even if found sitting directly in the cgroup-v2 root.  "
     "Covers OpenRC (elogind) and systemd (systemd-logind) session-manager "
     "names so the same list works on both init systems.  The default list "
     "is safe to leave unchanged — adding an unknown daemon here has no "
     "cost, missing one risks breaking D-Bus/PolicyKit session tracking."),

    ("CCD_GOOD_PARTITION_TYPE", "combo", "root",
     "theGood cgroup partition type",
     "cpuset partition type for the game's CCD cgroup.  'root' is stable "
     "and allows the kernel's housekeeping tasks to migrate onto those CPUs "
     "if needed.  'isolated' gives stronger isolation (no housekeeping) but "
     "can cause stability issues on some kernels / firmware combinations — "
     "test carefully before deploying.",
     ["root", "isolated"]),

    ("CCD_UGLY_PARTITION_TYPE", "combo", "member",
     "theUgly cgroup partition type",
     "cpuset partition type for the system/background CCD cgroup.  'member' "
     "is the recommended default and matches the script's own fallback: it "
     "leaves this group as a plain (non-exclusive) cgroup.  If BOTH theGood "
     "and theUgly claim their CPUs as exclusive partition roots ('root' or "
     "'isolated'), the top-level cgroup can be left with no CPUs of its "
     "own, and the kernel silently marks the partition invalid — i.e. no "
     "isolation at all, with no error anywhere.  Only pick 'root'/'isolated' "
     "here if you specifically need theUgly to also be an exclusive "
     "partition and understand this trade-off.",
     ["member", "root", "isolated"]),

    # ── PCI latency ───────────────────────────────────────────────────────────
    ("SET_PCI_LATENCY", "check", "1",
     "Set PCI latency timer",
     "Write the optimal PCI latency timer value to all PCI devices on PRE "
     "and restore on POST.  The latency timer limits how long a PCI master "
     "can hold the bus before yielding; older PCI devices (especially legacy "
     "audio or capture cards) sometimes have conservative defaults that add "
     "bus arbitration latency.  Harmless on PCIe-only systems (PCIe doesn't "
     "use the latency timer), but the script still writes it for completeness."),

    # ── Intel CPUs (GenuineIntel only; AMD-only knobs above are skipped there) ──
    ("SET_INTEL_TURBO", "check", "1",
     "Force Intel turbo on during gameplay",
     "Writes 0 to intel_pstate/no_turbo on PRE and restores it on POST.  Intel CPUs only; skipped when intel_pstate is absent."),

    ("SET_INTEL_HWP_BOOST", "check", "1",
     "Enable intel_pstate HWP dynamic boost",
     "Writes 1 to intel_pstate/hwp_dynamic_boost (active mode with HWP only)."),

    ("INTEL_MAX_PERF_PCT", "spin", "100",
     "intel_pstate max_perf_pct",
     "Upper performance limit in percent while gaming.  0 = leave unchanged.",
     (0, 100)),

    ("INTEL_MIN_PERF_PCT", "spin", "0",
     "intel_pstate min_perf_pct",
     "Lower performance limit in percent while gaming.  0 = leave unchanged.",
     (0, 100)),

    ("SET_INTEL_EPB", "check", "1",
     "Set energy_perf_bias to 0 (max performance)",
     "Writes 0 to every cpuN/power/energy_perf_bias on PRE; restored on POST."),

    ("SET_INTEL_ITMT", "check", "1",
     "Enable ITMT favored-core scheduling",
     "Writes 1 to /proc/sys/kernel/sched_itmt_enabled (Turbo Boost Max 3.0 / hybrid CPUs)."),

    ("HYBRID_CORE_ISOLATION", "check", "0",
     "Hybrid P/E-core isolation (opt-in)",
     "Intel hybrid CPUs (Alder Lake and newer): game on the P-cores, rest of the system on the E-cores, via the same cpuset machinery as CCD isolation.  Skipped when the P-cores expose fewer than HYBRID_MIN_P_THREADS logical CPUs."),

    ("HYBRID_MIN_P_THREADS", "spin", "8",
     "Minimum P-core threads for hybrid isolation",
     "Hybrid isolation is skipped when the P-cores expose fewer logical CPUs than this.",
     (1, 512)),

    # ── Proton / Wine friendliness ───────────────────────────────────────────
    ("VM_MAX_MAP_COUNT", "spin", "2147483642",
     "vm.max_map_count (raise-only)",
     "Raised to this value while gaming, never lowered.  Some Proton titles crash on the stock 65530.  0 = leave alone.",
     (0, 2147483647)),

    ("SET_VM_DIRTY", "check", "1",
     "Limit dirty-page window during gameplay",
     "Writes smaller dirty_bytes / dirty_background_bytes limits (smooths shader-cache and save-file write bursts).  All four ratio/bytes originals are saved and restored."),

    ("VM_DIRTY_BACKGROUND_BYTES", "spin", "67108864",
     "vm.dirty_background_bytes",
     "Background writeback threshold in bytes (must be smaller than dirty_bytes).",
     (1048576, 2147483647)),

    ("VM_DIRTY_BYTES", "spin", "268435456",
     "vm.dirty_bytes",
     "Hard dirty-page limit in bytes.  Skipped when dirty_bytes is already at or below this.",
     (1048576, 2147483647)),

    ("SET_NUMA_BALANCING", "check", "1",
     "Disable NUMA auto-balancing during gameplay",
     "Writes 0 to kernel.numa_balancing (background page scanning/migration adds page-fault noise)."),

    # ── IRQ affinity ─────────────────────────────────────────────────────────
    ("SET_IRQ_AFFINITY", "check", "0",
     "Move device IRQs to the system CPUs (opt-in)",
     "Moves the IRQs of the chosen PCI classes onto the system CPUs (IRQ_AFFINITY_CPUS, or the second CCD group).  Managed IRQs such as NVMe queues cannot be moved and are skipped.  Stop irqbalance, or it undoes this."),

    ("IRQ_AFFINITY_CLASSES", "line_space", "gpu nvme net usb",
     "IRQ classes to move",
     "Space-separated, any of: gpu nvme net usb audio.",
     None),

    ("IRQ_AFFINITY_CPUS", "line", "",
     "IRQ target CPUs (cpulist)",
     "Explicit target cpulist such as 8-15,24-31.  Empty = use the second CCD/CCX group (needs a multi-CCD CPU).",
     None),

    # ── GPU / network / storage ──────────────────────────────────────────────
    ("SET_NVIDIA_PERSISTENCE", "check", "0",
     "Enable NVIDIA persistence mode (opt-in)",
     "Turns persistence mode on for GPUs where it is currently off, and back off on POST.  Small effect while a game already holds the GPU."),

    ("SET_AMDGPU_PERF_LEVEL", "check", "0",
     "Force amdgpu performance level (opt-in)",
     "Writes AMDGPU_PERF_LEVEL to power_dpm_force_performance_level on every amdgpu card.  Raises power draw."),

    ("AMDGPU_PERF_LEVEL", "combo", "high",
     "amdgpu performance level",
     "Value for power_dpm_force_performance_level.",
     ["high", "auto", "low", "manual", "profile_standard", "profile_min_sclk", "profile_min_mclk", "profile_peak"]),

    ("SET_WIFI_POWERSAVE", "check", "1",
     "Turn Wi-Fi power save off during gameplay",
     "Runs 'iw dev <if> set power_save off' on interfaces where it is on; restored on POST.  Needs the iw tool."),

    ("SET_NVME_SCHED", "check", "1",
     "Set NVMe I/O scheduler during gameplay",
     "Writes NVME_IO_SCHEDULER to every NVMe queue/scheduler that offers it."),

    ("NVME_IO_SCHEDULER", "combo", "none",
     "NVMe I/O scheduler",
     "none is the usual best choice for NVMe; the others are for experiments.",
     ["none", "mq-deadline", "kyber", "bfq"]),

    ("SET_SCX_SCHED", "check", "0",
     "Run a sched_ext (scx) scheduler during gameplay",
     "Starts the scheduler chosen below while games run and stops it on POST. Needs a kernel with "
     "sched_ext (CachyOS ships one) and the scx_* binary. Skipped if another sched_ext scheduler is active."),

    ("SCX_SCHEDULER", "combo", "scx_lavd",
     "sched_ext scheduler",
     "lavd: latency-aware, a good default for games. bpfland: favours interactive tasks. "
     "rusty: general purpose. flash: low-latency, experimental.",
     ["scx_lavd", "scx_bpfland", "scx_rusty", "scx_flash"]),

    # ── Logging ───────────────────────────────────────────────────────────────
    ("LOG_LEVEL", "combo", "INFO",
     "Log level",
     "Verbosity of /var/log/lutris-game-tune.log.  "
     "'DEBUG' logs every sysfs read/write and cgroup operation — very noisy, "
     "use only when diagnosing a specific issue.  "
     "'INFO' logs PRE/POST lifecycle events and all tuning steps applied.  "
     "'WARN' logs only unexpected conditions (skipped steps, deprecated keys).  "
     "'ERROR' logs only fatal failures.",
     ["DEBUG", "INFO", "WARN", "ERROR"]),
]


class LgtuneWidget(QWidget):
    """
    Right-panel widget for the lutris-game-tune sub-tab inside Extra Tools.
    Source is bundled at _SCRIPT_DIR/lutris-game-tune-main/ — no zip needed.

    Layout (scrollable):
      ┌─ How lutris-game-tune works ─────────────────────────────────────────┐
      │  Usage explanation: PRE / POST / RUN / STATUS, Lutris wiring        │
      └──────────────────────────────────────────────────────────────────────┘
      ┌─ Installation ───────────────────────────────────────────────────────┐
      │  [detected / not detected]  Install / Uninstall (pkexec) buttons    │
      └──────────────────────────────────────────────────────────────────────┘
      ┌─ System-wide Settings  (/etc/lutris-game-tune.conf) ─────────────────┐
      │  [per-key rows: label + description + control]                       │
      │  [ Save to /etc/ (pkexec) ]  [ Run STATUS ]  [ View Log ]           │
      └──────────────────────────────────────────────────────────────────────┘
      console (QPlainTextEdit — install/status/log output)
    """

    # Bundled source path (repo layout: DRSTool/lutris-game-tune-main/)
    _BUNDLED_SRC = find_bundled_dir("lutris-game-tune-main", "install.sh")
    _out_buf: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None

        # Master layout (no margins — scroll area provides padding)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(14)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        header = QLabel("lutris-game-tune — System-wide Game Tuner")
        header.setStyleSheet("color:#eceff4; font-size:18px; font-weight:700;")
        layout.addWidget(header)

        # ── How it works / usage ─────────────────────────────────────────────
        usage_group = QGroupBox("How lutris-game-tune works")
        usage_group.setStyleSheet(_TOOL_GROUP_SS)
        usage_layout = QVBoxLayout(usage_group)
        usage_layout.setSpacing(8)

        usage_text = QLabel(
            "<b>lutris-game-tune</b> is a Bash script + setuid-root C wrapper that "
            "applies system-level performance tweaks at game launch and reverts them "
            "cleanly when the game exits.  The wrapper (<code>lutris-game-tune-wrapper</code>) "
            "is installed as setuid root so Lutris (running as your user) can trigger it "
            "without a password prompt each time.<br><br>"

            "<b>Modes / commands:</b><br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper PRE</code> — run before the game starts: "
            "saves current system state, then applies CPU governor, PCIe ASPM, C-state, "
            "THP, swappiness, PCI latency, and CCD/CCX isolation settings from "
            "<code>/etc/lutris-game-tune.conf</code>.<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper POST</code> — run after the game exits: "
            "restores every setting that PRE changed back to its original value.  Safe to "
            "call even if PRE didn't run (idempotent).<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper STATUS</code> — prints the current "
            "active state (which settings are applied, how many games are running) to stdout.  "
            "Useful to confirm the tuner is active during a game session.<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper RUN &lt;nice&gt; [--] &lt;command…&gt;</code> — "
            "starts the game command with the given nice value (e.g. <code>-5</code>) using "
            "root privilege for the <code>nice()</code> syscall, then immediately drops to your "
            "user — the game itself never runs as root.  Goes in Lutris's "
            "<i>Command prefix</i> field.<br><br>"

            "<b>Wiring in Lutris:</b><br>"
            "Go to <i>Configure → System options</i> (per-game) or "
            "<i>Preferences → System options</i> (global) and set:<br>"
            "&nbsp;&nbsp;<b>Pre-game script:</b> <code>/usr/local/bin/lutris-game-tune-wrapper PRE</code><br>"
            "&nbsp;&nbsp;<b>Post-game script:</b> <code>/usr/local/bin/lutris-game-tune-wrapper POST</code><br>"
            "Optionally, to start the game at a higher CPU priority:<br>"
            "&nbsp;&nbsp;<b>Command prefix:</b> <code>/usr/local/bin/lutris-game-tune-wrapper RUN -5</code><br><br>"

            "<b>Security model:</b>  The wrapper binary is setuid root; it validates "
            "arguments strictly, drops root immediately after the privileged syscall "
            "(for RUN mode), and delegates all logic to the Bash script at "
            "<code>/usr/local/lib/lutris-game-tune/lutris-game-tune.sh</code> — "
            "which in turn reads <code>/etc/lutris-game-tune.conf</code> in a "
            "safe, no-<code>source</code> parser (whitelist-only KEY=VALUE).  "
            "The config file must be root-owned and not group/other writable "
            "(mode 644 or 640) — the script refuses to load it otherwise.<br><br>"

            "<b>Log file:</b> <code>/var/log/lutris-game-tune.log</code> — rotated "
            "automatically at 1 MB.  Use the <i>View Log</i> button below to tail it."
        )
        usage_text.setWordWrap(True)
        usage_text.setTextFormat(Qt.RichText)
        usage_text.setStyleSheet(
            "color:#b3bac7; font-size:12px; line-height:155%; "
            "background:transparent;"
        )
        usage_layout.addWidget(usage_text)
        layout.addWidget(usage_group)

        # ── Installation status + installer ──────────────────────────────────
        install_group = QGroupBox("Installation")
        install_group.setStyleSheet(_TOOL_GROUP_SS)
        install_layout = QVBoxLayout(install_group)
        install_layout.setSpacing(8)

        self._status_banner = QLabel()
        self._status_banner.setWordWrap(True)
        self._status_banner.setStyleSheet("font-size:12px; font-weight:700; padding:4px;")
        install_layout.addWidget(self._status_banner)

        # Bundled source path banner (read-only)
        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        src_lbl = QLabel("📁 Source:")
        src_lbl.setStyleSheet("color:#8f9bb0; font-size:12px; font-weight:600;")
        self._lgt_src_lbl = QLabel(str(self._BUNDLED_SRC))
        self._lgt_src_lbl.setStyleSheet(
            "color:#76b900; font-size:12px; font-family:monospace;"
        )
        lgt_src_ok = (self._BUNDLED_SRC / "install.sh").is_file()
        self._lgt_src_status = QLabel("✔ found" if lgt_src_ok else "✘ not found")
        self._lgt_src_status.setStyleSheet(
            f"color:{'#76b900' if lgt_src_ok else '#c04040'}; "
            "font-size:12px; font-weight:700;"
        )
        src_row.addWidget(src_lbl)
        src_row.addWidget(self._lgt_src_lbl, 1)
        src_row.addWidget(self._lgt_src_status)
        install_layout.addLayout(src_row)

        # Action buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._install_btn = _tool_btn(
            "Install (pkexec)", "#76b900", "#76b900", "#0c0e12", bold=True
        )
        self._install_btn.clicked.connect(self._on_install)
        self._uninstall_btn = _tool_btn("Uninstall (pkexec)", "#3a1010", "#7a2020", "#e07070")
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        self._switch_btn = _tool_btn("Switch install mode (pkexec)")
        self._switch_btn.setToolTip("Re-install in the other mode (setuid ⇄ polkit); settings are kept")
        self._switch_btn.clicked.connect(self._on_switch_mode)
        btn_row.addWidget(self._install_btn)
        btn_row.addWidget(self._uninstall_btn)
        btn_row.addWidget(self._switch_btn)
        btn_row.addStretch()
        install_layout.addLayout(btn_row)

        self._polkit_chk = QCheckBox("Install without a setuid binary (polkit + file capability)")
        self._polkit_chk.setChecked(bool(shutil.which("pkexec")))
        self._polkit_chk.setToolTip(
            "PRE / POST run through pkexec; a polkit rule lets the user at the machine do that without a "
            "password, remote and inactive sessions need an admin password. RUN gets only CAP_SYS_NICE "
            "from a file capability instead of full root. Lutris configs written by DRSTool are switched "
            "to the matching command on the next Apply.")
        self._polkit_chk.setStyleSheet("color:#b3bac7; font-size:12px;")
        install_layout.addWidget(self._polkit_chk)

        install_note = QLabel(
            "Install runs <code>lutris-game-tune-main/install.sh</code> (bundled in "
            "the DRSTool repository) via pkexec.  "
            "Uninstall runs <code>uninstall.sh</code> from the installed lib dir via pkexec.  "
            "The config file at <code>/etc/lutris-game-tune.conf</code> is "
            "<b>not removed</b> on uninstall — your settings are preserved."
        )
        install_note.setWordWrap(True)
        install_note.setTextFormat(Qt.RichText)
        install_note.setStyleSheet("color:#7b8496; font-size:13px; font-style:italic;")
        install_layout.addWidget(install_note)

        layout.addWidget(install_group)

        # ── Settings group ────────────────────────────────────────────────────
        self._settings_group = QGroupBox(
            "System-wide Settings  —  /etc/lutris-game-tune.conf  (root-owned)"
        )
        self._settings_group.setStyleSheet(_TOOL_GROUP_SS)
        settings_layout = QVBoxLayout(self._settings_group)
        settings_layout.setSpacing(10)

        # Build per-key widgets
        self._key_widgets: Dict[str, QWidget] = {}  # key → control widget
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(1, 1)

        for row_idx, entry in enumerate(LGTUNE_KEYS):
            key, wtype, default, label, desc, *rest = entry
            opts_or_range = rest[0] if rest else None

            # Label (key name, bold)
            key_lbl = QLabel(f"<b>{key}</b>")
            key_lbl.setStyleSheet("color:#c8cdd8; font-size:13px;")
            key_lbl.setToolTip(desc)
            grid.addWidget(key_lbl, row_idx * 2, 0, Qt.AlignTop)

            # Control widget
            if wtype == "check":
                ctrl = QCheckBox(label)
                ctrl.setChecked(default == "1")
                ctrl.setStyleSheet(
                    "QCheckBox{ color:#dfe3ea; font-size:13px; spacing:6px; }"
                    "QCheckBox::indicator{ width:14px; height:14px; "
                    "border:1px solid #2a313d; border-radius:3px; background:#181c24; }"
                    "QCheckBox::indicator:checked{ background:#76b900; border:1px solid #76b900; }"
                )

            elif wtype == "combo":
                ctrl = QComboBox()
                ctrl.addItems(opts_or_range or [])
                idx_default = opts_or_range.index(default) if opts_or_range and default in opts_or_range else 0
                ctrl.setCurrentIndex(idx_default)
                ctrl.setStyleSheet("""
QComboBox{ background:#181c24; border:1px solid #2a313d; border-radius:4px;
           color:#dfe3ea; font-size:13px; padding:3px 8px; min-height:26px; }
QComboBox:hover{ border:1px solid #76b900; }
QComboBox QAbstractItemView{ background:#12151b; color:#dfe3ea; selection-background-color:#1d222c; }
""")
                ctrl.installEventFilter(_NO_SCROLL_FILTER)

            elif wtype == "spin":
                ctrl = QSpinBox()
                lo, hi = opts_or_range if opts_or_range else (0, 9999)
                ctrl.setRange(lo, hi)
                ctrl.setValue(int(default))
                ctrl.setStyleSheet("""
QSpinBox{ background:#181c24; border:1px solid #2a313d; border-radius:4px;
          color:#dfe3ea; font-size:13px; padding:3px 6px; }
QSpinBox:hover{ border:1px solid #76b900; }
QSpinBox::up-button, QSpinBox::down-button{ width:16px; }
""")
                ctrl.installEventFilter(_NO_SCROLL_FILTER)

            elif wtype in ("line", "line_space"):
                ctrl = QLineEdit(default)
                ctrl.setStyleSheet(FLM_FIELD_SS)
                if wtype == "line_space":
                    ctrl.setPlaceholderText("space-separated names")

            else:
                ctrl = QLineEdit(default)
                ctrl.setStyleSheet(FLM_FIELD_SS)

            ctrl.setToolTip(desc)
            grid.addWidget(ctrl, row_idx * 2, 1)
            self._key_widgets[key] = ctrl

            # Description (smaller, muted, spans full row)
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                "color:#b4bcc9; font-size:13px; line-height:150%; padding:1px 0 6px 0;"
            )
            grid.addWidget(desc_lbl, row_idx * 2 + 1, 0, 1, 2)

        # ── Runtime availability hint for SET_EPP_BOOST ───────────────────────
        # epp_boost is not upstream yet, so annotate the checkbox with whether
        # the knob actually exists on the running kernel. Purely informational:
        # the option stays writable either way, since the user may be
        # configuring for a patched kernel they haven't booted yet.
        _epp_ctrl = self._key_widgets.get("SET_EPP_BOOST")
        if isinstance(_epp_ctrl, QCheckBox):
            _epp_present = Path("/sys/module/amd_pstate/parameters/epp_boost").exists()
            if _epp_present:
                _epp_ctrl.setText(_epp_ctrl.text() + "   [kernel: supported]")
            else:
                _epp_ctrl.setText(_epp_ctrl.text() + "   [kernel: not present]")
                _epp_ctrl.setToolTip(
                    _epp_ctrl.toolTip()
                    + "\n\nThis kernel does not expose "
                      "/sys/module/amd_pstate/parameters/epp_boost — the setting "
                      "will be saved but skipped at PRE time until you boot a "
                      "kernel with the epp_boost patch series applied."
                )

        # ── Runtime availability hint for SET_X3D_VCACHE_MODE ─────────────────
        # amd_x3d_mode only exists when the amd_x3d_vcache driver is bound (AMD
        # 3D V-Cache CPU + supporting kernel). The instance directory name is an
        # ACPI device id (e.g. AMDI0101:00) that isn't guaranteed to be the same
        # on every board/BIOS, so it is globbed for rather than hardcoded.
        _x3d_ctrl = self._key_widgets.get("SET_X3D_VCACHE_MODE")
        if isinstance(_x3d_ctrl, QCheckBox):
            _x3d_matches = list(Path("/sys/bus/platform/drivers/amd_x3d_vcache").glob("*/amd_x3d_mode")) \
                if Path("/sys/bus/platform/drivers/amd_x3d_vcache").is_dir() else []
            if _x3d_matches:
                _x3d_ctrl.setText(_x3d_ctrl.text() + "   [kernel: supported]")
            else:
                _x3d_ctrl.setText(_x3d_ctrl.text() + "   [kernel: not present]")
                _x3d_ctrl.setToolTip(
                    _x3d_ctrl.toolTip()
                    + "\n\nNo amd_x3d_vcache driver instance was found under "
                      "/sys/bus/platform/drivers/amd_x3d_vcache — either this "
                      "isn't an AMD 3D V-Cache CPU, or the running kernel doesn't "
                      "have the driver.  The setting will be saved but skipped at "
                      "PRE time until the driver is bound."
                )

        settings_layout.addLayout(grid)

        # Save / status / log button row
        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self._save_btn = _tool_btn(
            "💾  Save to /etc/ (pkexec)", "#76b900", "#76b900", "#0c0e12", bold=True
        )
        self._save_btn.clicked.connect(self._on_save)
        self._status_btn = _tool_btn("▶  Run STATUS")
        self._status_btn.clicked.connect(self._on_run_status)
        self._log_btn = _tool_btn("📄  View Log")
        self._log_btn.clicked.connect(self._on_view_log)
        action_row.addWidget(self._save_btn)
        action_row.addWidget(self._status_btn)
        action_row.addWidget(self._log_btn)
        action_row.addStretch()
        settings_layout.addLayout(action_row)

        save_note = QLabel(
            "Writes <code>/etc/lutris-game-tune.conf</code> as root via pkexec.  "
            "Changes take effect on the <b>next PRE run</b> — no restart needed."
        )
        save_note.setWordWrap(True)
        save_note.setTextFormat(Qt.RichText)
        save_note.setStyleSheet("color:#7b8496; font-size:13px; font-style:italic;")
        settings_layout.addWidget(save_note)

        layout.addWidget(self._settings_group)

        # ── Console ───────────────────────────────────────────────────────────
        console_hdr = QHBoxLayout()
        console_lbl = QLabel("Output")
        console_lbl.setStyleSheet("color:#8f9bb0; font-size:12px; font-weight:700;")
        clear_btn = _tool_btn("Clear")
        clear_btn.clicked.connect(lambda: self._console.clear())
        console_hdr.addWidget(console_lbl)
        console_hdr.addStretch()
        console_hdr.addWidget(clear_btn)
        layout.addLayout(console_hdr)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setFixedHeight(160)
        self._console.setStyleSheet("""
QPlainTextEdit{
    background:#08090c; border:1px solid #1d222c; border-radius:6px;
    color:#9be238; font-family:monospace; font-size:13px; padding:6px;
}
""")
        layout.addWidget(self._console)
        layout.addStretch()

        # Populate from /etc/ on first show
        self._refresh_install_state()
        self._load_conf()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _log(self, text: str):
        self._console.appendPlainText(text)
        self._console.verticalScrollBar().setValue(
            self._console.verticalScrollBar().maximum()
        )

    def _is_installed(self) -> bool:
        return LGTUNE_WRAPPER.exists() and os.access(str(LGTUNE_WRAPPER), os.X_OK)

    def _refresh_install_state(self):
        src_ok = (self._BUNDLED_SRC / "install.sh").is_file()
        self._lgt_src_status.setText("✔ found" if src_ok else "✘ not found")
        self._lgt_src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; "
            "font-size:12px; font-weight:700;"
        )
        if self._is_installed():
            mode = lgtune_mode()
            how = {"setuid": "setuid root", "polkit": "polkit + cap_sys_nice, no setuid"}.get(
                mode, "neither setuid nor polkit — PRE/POST will fail; re-install")
            self._status_banner.setText(f"✔  Installed ({how})  —  {LGTUNE_WRAPPER}")
            self._status_banner.setStyleSheet(
                f"color:{'#76b900' if mode else '#e3a73f'}; font-size:12px; font-weight:700; padding:4px;"
            )
            self._install_btn.hide()
            self._uninstall_btn.show()
            self._switch_btn.show()
            self._switch_btn.setText("Switch to setuid mode (pkexec)" if mode == "polkit"
                                     else "Switch to polkit mode (pkexec)")
            self._polkit_chk.hide()
            self._settings_group.setEnabled(True)
        else:
            self._switch_btn.hide()
            self._polkit_chk.show()
            self._status_banner.setText(
                "✘  Not installed  —  wrapper not found at "
                f"{LGTUNE_WRAPPER}.  "
                "Click Install to build and install from the bundled source."
            )
            self._status_banner.setStyleSheet(
                "color:#c04040; font-size:12px; font-weight:700; padding:4px;"
            )
            self._install_btn.show()
            self._uninstall_btn.hide()
            self._settings_group.setEnabled(False)

    def _load_conf(self):
        """Parse /etc/lutris-game-tune.conf and populate the UI widgets."""
        if not LGTUNE_CONF.is_file():
            return
        try:
            text = LGTUNE_CONF.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        values: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")

        for key, ctrl in self._key_widgets.items():
            val = values.get(key)
            if val is None:
                continue
            if isinstance(ctrl, QCheckBox):
                ctrl.setChecked(val in ("1", "true", "yes"))
            elif isinstance(ctrl, QComboBox):
                idx = ctrl.findText(val, Qt.MatchFixedString)
                if idx >= 0:
                    ctrl.setCurrentIndex(idx)
            elif isinstance(ctrl, QSpinBox):
                try:
                    ctrl.setValue(int(val))
                except ValueError:
                    pass
            elif isinstance(ctrl, QLineEdit):
                ctrl.setText(val)

    def _build_conf_text(self) -> str:
        """Generate the full /etc/lutris-game-tune.conf content from UI state."""
        lines = [
            "# /etc/lutris-game-tune.conf — written by DRSTool",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "#",
            "# This file is read by lutris-game-tune.sh (whitelist-only KEY=VALUE parser).",
            "# Must be owned by root and not group/other writable (mode 644 or 640).",
            "# Changes take effect on the next PRE run.",
            "",
            "# --- CPU Governor / EPP -------------------------------------------",
        ]
        def _val(key: str) -> str:
            ctrl = self._key_widgets.get(key)
            if ctrl is None:
                return ""
            if isinstance(ctrl, QCheckBox):
                return "1" if ctrl.isChecked() else "0"
            elif isinstance(ctrl, QComboBox):
                return ctrl.currentText()
            elif isinstance(ctrl, QSpinBox):
                return str(ctrl.value())
            elif isinstance(ctrl, QLineEdit):
                # Root-read KEY=VALUE file: a pasted CR/LF or '"' would start a
                # new key / break out of the quoted value.
                return re.sub(r'[\r\n"\\]', " ", ctrl.text()).strip()
            return ""

        lines += [
            f"SET_CPU_GOVERNOR={_val('SET_CPU_GOVERNOR')}",
            f"CPU_GOVERNOR={_val('CPU_GOVERNOR')}",
            "",
            "# amd-pstate per-core EPP boost (kernel module parameter; only present",
            "# on kernels built with the epp_boost patch series — skipped silently",
            "# otherwise). 1=on, 0=off.",
            f"SET_EPP_BOOST={_val('SET_EPP_BOOST')}",
            "",
            "# AMD 3D V-Cache core preference (amd_x3d_vcache driver; AMD 3D",
            "# V-Cache CPUs only). frequency = prefer higher-clocking CCD,",
            "# cache = prefer cores on the CCD with the larger L3. Skipped",
            "# silently if the driver isn't bound.",
            f"SET_X3D_VCACHE_MODE={_val('SET_X3D_VCACHE_MODE')}",
            f"X3D_VCACHE_MODE={_val('X3D_VCACHE_MODE')}",
            "",
            "# --- PCIe ASPM ---------------------------------------------------",
            f"SET_ASPM={_val('SET_ASPM')}",
            f"ASPM_POLICY={_val('ASPM_POLICY')}",
            "",
            "# --- Deep C-state disabling (optional) ---------------------------",
            f"DISABLE_DEEP_CSTATES={_val('DISABLE_DEEP_CSTATES')}",
            f"CSTATE_KEEP_MAX={_val('CSTATE_KEEP_MAX')}",
            "",
            "# --- VM ----------------------------------------------------------",
            f"VM_SWAPPINESS={_val('VM_SWAPPINESS')}",
            "",
            "# --- Transparent HugePages ---------------------------------------",
            f"THP_ENABLED={_val('THP_ENABLED')}",
            f"THP_SHMEM_ENABLED={_val('THP_SHMEM_ENABLED')}",
            f"THP_DEFRAG={_val('THP_DEFRAG')}",
            "",
            "# --- CCD/CCX core isolation ---------------------------------------",
            f"CCD_ISOLATION_ENABLED={_val('CCD_ISOLATION_ENABLED')}",
            f"CCD_LAUNCHER={_val('CCD_LAUNCHER')}",
            f'CCD_EXTRA_GOOD_PROCS="{_val("CCD_EXTRA_GOOD_PROCS")}"',
            f"CCD_MONITOR_SECONDS={_val('CCD_MONITOR_SECONDS')}",
            f"CCD_GOOD_PARTITION_TYPE={_val('CCD_GOOD_PARTITION_TYPE')}",
            f"CCD_UGLY_PARTITION_TYPE={_val('CCD_UGLY_PARTITION_TYPE')}",
            f'CCD_PROTECTED_PROCS={_val("CCD_PROTECTED_PROCS")}',
            "",
            "# --- PCI latency -------------------------------------------------",
            f"SET_PCI_LATENCY={_val('SET_PCI_LATENCY')}",
            "",
            "# --- Intel CPUs (GenuineIntel only) --------------------------------",
            f"SET_INTEL_TURBO={_val('SET_INTEL_TURBO')}",
            f"SET_INTEL_HWP_BOOST={_val('SET_INTEL_HWP_BOOST')}",
            f"INTEL_MAX_PERF_PCT={_val('INTEL_MAX_PERF_PCT')}",
            f"INTEL_MIN_PERF_PCT={_val('INTEL_MIN_PERF_PCT')}",
            f"SET_INTEL_EPB={_val('SET_INTEL_EPB')}",
            f"SET_INTEL_ITMT={_val('SET_INTEL_ITMT')}",
            f"HYBRID_CORE_ISOLATION={_val('HYBRID_CORE_ISOLATION')}",
            f"HYBRID_MIN_P_THREADS={_val('HYBRID_MIN_P_THREADS')}",
            "",
            "# --- Proton / Wine friendliness ------------------------------------",
            f"VM_MAX_MAP_COUNT={_val('VM_MAX_MAP_COUNT')}",
            f"SET_VM_DIRTY={_val('SET_VM_DIRTY')}",
            f"VM_DIRTY_BACKGROUND_BYTES={_val('VM_DIRTY_BACKGROUND_BYTES')}",
            f"VM_DIRTY_BYTES={_val('VM_DIRTY_BYTES')}",
            f"SET_NUMA_BALANCING={_val('SET_NUMA_BALANCING')}",
            "",
            "# --- IRQ affinity (opt-in) -----------------------------------------",
            f"SET_IRQ_AFFINITY={_val('SET_IRQ_AFFINITY')}",
            f'IRQ_AFFINITY_CLASSES="{_val("IRQ_AFFINITY_CLASSES")}"',
            f"IRQ_AFFINITY_CPUS={_val('IRQ_AFFINITY_CPUS')}",
            "",
            "# --- GPU / network / storage ---------------------------------------",
            f"SET_NVIDIA_PERSISTENCE={_val('SET_NVIDIA_PERSISTENCE')}",
            f"SET_AMDGPU_PERF_LEVEL={_val('SET_AMDGPU_PERF_LEVEL')}",
            f"AMDGPU_PERF_LEVEL={_val('AMDGPU_PERF_LEVEL')}",
            f"SET_WIFI_POWERSAVE={_val('SET_WIFI_POWERSAVE')}",
            f"SET_NVME_SCHED={_val('SET_NVME_SCHED')}",
            f"NVME_IO_SCHEDULER={_val('NVME_IO_SCHEDULER')}",
            f"SET_SCX_SCHED={_val('SET_SCX_SCHED')}",
            f"SCX_SCHEDULER={_val('SCX_SCHEDULER')}",
            "",
            "# --- Logging -----------------------------------------------------",
            f"LOG_LEVEL={_val('LOG_LEVEL')}",
            "",
        ]
        return "\n".join(lines)

    # ── action handlers ───────────────────────────────────────────────────────

    def _on_install(self):
        install_sh = self._BUNDLED_SRC / "install.sh"
        if not install_sh.is_file():
            QMessageBox.warning(
                self, "Bundled source not found",
                f"install.sh not found at:\n  {install_sh}\n\n"
                "Make sure the lutris-game-tune-main/ directory is present "
                "next to DRSTool.py (it is part of the DRSTool repository)."
            )
            return
        args = ["bash", str(install_sh)] + (["--polkit"] if self._polkit_chk.isChecked() else [])
        self._log(f"==> Running pkexec {' '.join(args)}")
        self._run_proc("pkexec", args, tag="install")

    def _on_switch_mode(self):
        cur = lgtune_mode()
        new = "setuid" if cur == "polkit" else "polkit"
        if QMessageBox.question(
                self, "Switch install mode",
                f"Re-install lutris-game-tune in {new} mode?\n\n"
                + ("PRE/POST will go through pkexec; Lutris games configured by DRSTool need one more "
                   "Apply on the Lutris tab to pick up the new command." if new == "polkit" else
                   "The wrapper becomes setuid root again and the polkit action is removed."),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._polkit_chk.setChecked(new == "polkit")
        self._on_install()

    def _on_uninstall(self):
        reply = QMessageBox.question(
            self, "Confirm uninstall",
            "Uninstall lutris-game-tune?\n\n"
            "/etc/lutris-game-tune.conf will be kept (your settings are preserved).\n"
            "The wrapper binary and lib directory will be removed.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        uninstall_sh = str(LGTUNE_LIB_DIR / "uninstall.sh")
        if not Path(uninstall_sh).is_file():
            self._log(f"ERROR: uninstall.sh not found at {uninstall_sh}")
            return
        self._log(f"==> Running pkexec bash {uninstall_sh}")
        self._run_proc("pkexec", ["bash", uninstall_sh], tag="uninstall")

    def _on_save(self):
        conf_text = self._build_conf_text()
        # Content goes through stdin. It is written to a temp file next to the
        # target and renamed over it, so an interrupted write can never leave
        # an empty/truncated /etc/lutris-game-tune.conf behind.
        self._log("==> Writing /etc/lutris-game-tune.conf via pkexec …")
        script = (
            "set -e; "
            "tmp=$(mktemp /etc/.lutris-game-tune.conf.XXXXXX); "
            "trap 'rm -f \"$tmp\"' EXIT; "
            "cat > \"$tmp\"; "
            "chmod 644 \"$tmp\"; chown root:root \"$tmp\"; "
            "mv -f \"$tmp\" /etc/lutris-game-tune.conf; "
            "trap - EXIT; "
            "echo 'Saved: /etc/lutris-game-tune.conf'"
        )
        self._run_proc(
            "pkexec",
            ["bash", "-c", script],
            tag="save",
            stdin_data=conf_text.encode()
        )

    def _on_run_status(self):
        if not self._is_installed():
            self._log("lutris-game-tune is not installed.")
            return
        self._log(f"==> {LGTUNE_WRAPPER} STATUS")
        self._run_proc("pkexec", [str(LGTUNE_WRAPPER), "STATUS"], tag="status")

    def _on_view_log(self):
        if not LGTUNE_LOG.is_file():
            self._log(f"Log file not found: {LGTUNE_LOG}")
            return
        self._log(f"==> tail -80 {LGTUNE_LOG}")
        self._run_proc("tail", ["-n", "80", str(LGTUNE_LOG)], tag="log")

    # ── QProcess plumbing ─────────────────────────────────────────────────────

    def _run_proc(self, program: str, args: List[str], tag: str,
                  stdin_data: Optional[bytes] = None):
        # Callers that need root pass "pkexec" as the program themselves.
        if self._proc and self._proc.state() != QProcess.NotRunning:
            self._log("A process is already running — please wait.")
            return
        self._out_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_output(p))
        proc.finished.connect(lambda code, st, t=tag: self._on_finished(code, st, t))
        proc.errorOccurred.connect(
            lambda err, p=proc: self._on_proc_error(err, p)
        )
        self._proc = proc
        proc.start(program, args)
        if stdin_data is not None:
            proc.write(stdin_data)
            proc.closeWriteChannel()

    def _on_output(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        self._out_buf = (self._out_buf + data)[-4000:]
        if data.strip():
            self._log(data.rstrip("\n"))

    def _on_proc_error(self, error, proc: QProcess):
        if error == QProcess.FailedToStart:
            self._log(
                f"ERROR: could not start '{proc.program()}' — is pkexec installed?"
            )

    def _on_finished(self, exit_code: int, exit_status, tag: str):
        if exit_status == QProcess.CrashExit:
            self._log(f"ERROR: '{tag}' process crashed.")
            return
        if tag != "log" and pkexec_auth_failed(exit_code, self._out_buf):
            self._log("Authorization cancelled or denied (pkexec).")
            return
        if exit_code != 0 and tag not in ("log", "status"):
            self._log(f"ERROR: '{tag}' failed (exit code {exit_code}).")
            return
        if tag == "install":
            self._log("==> Installation complete.")
            self._refresh_install_state()
            self._load_conf()
        elif tag == "uninstall":
            self._log("==> Uninstall complete.")
            self._refresh_install_state()
        elif tag == "save":
            self._log("==> Config saved successfully.")
