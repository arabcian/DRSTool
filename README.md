<img width="1863" height="1546" alt="Screenshot_20260711_193903" src="https://github.com/user-attachments/assets/149e3bfc-a0d0-4a83-931b-18dd785aab8d" />
<img width="1863" height="1546" alt="Screenshot_20260711_193922" src="https://github.com/user-attachments/assets/30add834-9353-444f-a7dd-f0fa281c1316" />
<img width="1863" height="1546" alt="Screenshot_20260711_193942" src="https://github.com/user-attachments/assets/da7eb3c2-9d7e-49c0-81b4-e3ff26259e90" />

# DRSTool

**DRSTool** is a PySide6 desktop GUI for tuning NVIDIA driver behavior, frame pacing, and system-level performance for Linux gaming — bundling three tools in a single unified interface:

- **DRS Settings editor** — the Linux equivalent of NVIDIA Profile Inspector, for `dxvk-nvapi`'s `DXVK_NVAPI_DRS_SETTINGS`
- **vk_flip_meter** — a Vulkan frame-pacing / cadence-modulation implicit layer (bundled source, built from within the app)
- **lutris-game-tune** — a system-wide performance tuner for Lutris/Wine games (CPU governor, PCIe ASPM, C-states, THP, CCD/CCX isolation, managed via a setuid-root wrapper)

It exists because tuning NVIDIA driver behavior for a Proton/Wine game on Linux normally means hand-writing long hex-coded environment variable strings from memory or scattered wiki pages. DRSTool turns that into a searchable, documented, point-and-click editor — and extends that same philosophy to every related tool in the Linux gaming stack.

## Repository layout

```
DRSTool/
├── DRSTool.py                    # Main application (single file, PySide6)
├── assets/
│   └── drstool.png               # Application icon
├── vk-flip-meter-main/           # Bundled vk_flip_meter source (C++/CMake)
│   ├── CMakeLists.txt
│   ├── build.sh
│   ├── src/flip_meter.cpp
│   └── manifest/
│       └── VkLayer_cpu_flip_meter.json.in
└── lutris-game-tune-main/        # Bundled lutris-game-tune source (Bash + C)
    ├── install.sh
    ├── uninstall.sh
    ├── lutris-game-tune.sh
    ├── lutris-game-tune-wrapper.c
    └── lutris-game-tune.conf
```

Both sub-projects are part of this repository. There are no external downloads at install time.

## Requirements

- Python ≥ 3.12
- PySide6 ≥ 6.10 (`dev-python/pyside` on Gentoo)
- `pkexec` (PolicyKit) — for privileged install/save operations
- For vk_flip_meter build: `cmake` ≥ 3.20 and a C++20-capable compiler
- For lutris-game-tune install: `gcc` and `flock` (part of `util-linux`)

Optional but recommended:
- `dev-python/setproctitle` — prevents the KDE/GNOME Wayland taskbar icon from falling back to a generic icon when launched via python-exec2c

## Running

```bash
python3 DRSTool.py
```

### Gentoo (ebuild)

A `drstool-9999.ebuild` is provided in the repository root for live-git installation via Portage:

```bash
# Copy the ebuild into a local overlay, then:
emerge -av games-util/drstool
```

USE flags:

| Flag | Default | Description |
|---|---|---|
| `flip-meter` | ON | Build and install the vk_flip_meter Vulkan layer |
| `lutris-tune` | ON | Build and install the lutris-game-tune setuid wrapper |
| `lto` | OFF | Link-time optimisation for the vk_flip_meter layer |
| `pgo` | OFF | Two-pass profile-guided optimisation for vk_flip_meter (see pkg_postinst for workflow) |

## Interface overview

The app is a single window split into a left sidebar and a right editor panel, with a persistent output bar across the top showing the currently generated environment string. Five tabs switch what the sidebar and editor show:

### 1. DRS Settings

A searchable, categorized list of **117 driver settings** — the same low-level settings NVIDIA Profile Inspector exposes on Windows, reimplemented here for `dxvk-nvapi`'s `DXVK_NVAPI_DRS_SETTINGS` environment variable.

Categories include: OpenGL, Anti-Aliasing, Texture Filtering, VSync/Flip, Frame Rate, Power, SLI, Stereo, VRR/G-Sync, DLSS/NGX, Ansel, FXAA, AO, Optimus, and Misc.

Each setting has a short description, a longer detailed description, and the correct control type — enum dropdown, numeric spinner, or bitfield checkbox grid — matching how the underlying value is actually encoded. Selecting a setting opens its editor on the right; setting a value updates the output bar and highlights the setting green in the sidebar.

### 2. GPU Arch

A list of NVIDIA GPU architecture families (Maxwell through Blackwell — GeForce 900 through RTX 50-series) with example cards for each. Selecting one sets `DXVK_NVAPI_GPU_ARCH` in the output string, since some DRS settings only apply correctly when the driver knows which architecture it's dealing with.

### 3. DXVK / VKD3D / NV / FLM

A single combined, categorized, searchable list of environment variables across ten categories:

| Category | Variables | Notes |
|---|---|---|
| DXVK | 15 | HUD flags, logging, device/frame options |
| VKD3D-Proton | 16 | Includes the full `VKD3D_CONFIG` 41-flag checkbox grid |
| NVIDIA `__GL_*` | 31 | OpenGL thread opt, VRR, shader cache, G-Sync, etc. |
| NVIDIA PRIME | 4 | `__NV_PRIME_RENDER_OFFLOAD`, `DRI_PRIME`, etc. |
| Proton | ~18 | Sync (ntsync/fsync/esync), HDR, Wayland, NVAPI, NGX, DLSS |
| Wine | ~10 | `WINEFSYNC`, `WINEESYNC`, `WINEDEBUG`, etc. |
| DXVK-NVAPI | ~10 | Reflex, DRS overrides, logging, driver-version spoof |
| NVIDIA Smooth Motion | 4 | `VK_LAYER_NV_present` (RTX 50 frame interpolation) |
| System / Loader | 5 | `VK_LOADER_DEBUG`, `LD_PRELOAD`, `SDL_VIDEODRIVER`, etc. |
| Gamescope | 22 | Output geometry, upscaling (FSR/NIS), HDR, VRR, Steam integration |
| vk_flip_meter | 20 | All FLM_* runtime variables, hot-reloadable via FLM_CONFIG + SIGUSR1 |

Each variable is typed (string, enum, bool, integer, or flag-set) and gets the matching editor control. Values set here merge into the same combined output string as the DRS settings.

### 4. Profiles

Save the entire current state — DRS settings, GPU architecture, and all env vars — under a name, then reload or delete it later. Profiles are stored as JSON under `$XDG_CONFIG_HOME/drstool/profiles.json` (falling back to `~/.config/drstool/profiles.json`), written atomically to avoid corruption on crash or power loss. Existing installs using the old `~/.drs_configurator_profiles.json` path are migrated automatically on first run.

The **Lutris Sync** sub-tab inside Profiles lets you write the current DRS + env var string directly into a Lutris game's `system.env` block, with automatic timestamped backup.

### 5. Extra Tools

A tabbed panel for the two bundled sub-projects:

#### vk_flip_meter sub-tab

Builds and installs the vk_flip_meter Vulkan implicit layer from the bundled `vk-flip-meter-main/` source.

- Source path is displayed (read-only) — no picker needed, it's part of the repository
- `cmake configure` + `cmake --build` run unprivileged as your normal user
- Only `cmake --install` escalates via pkexec (graphical polkit password prompt)
- `FLM_LIB_PATH` is injected at configure time so the manifest template gets the correct library path (no post-install sed patching)
- **Verify** button checks `vulkaninfo --summary` for the layer
- **Live Tuning** section: writes currently-set `FLM_*` vars from the Env Vars tab into an `FLM_CONFIG` file and sends `SIGUSR1` to a running game, enabling hot-reload of frame-pacing parameters without restarting

Runtime `FLM_*` variables are configured on the **DXVK / VKD3D / NV / FLM** tab (one click away) and automatically included in the Copy All output.

#### lutris-game-tune sub-tab

**How it works:** lutris-game-tune applies system-level performance tweaks at game launch (PRE) and reverts them cleanly when the game exits (POST). A small setuid-root C wrapper (`lutris-game-tune-wrapper`) handles privilege escalation without requiring a passwordless sudo setup or leaving a terminal open.

**Tuning applied by PRE:**
- CPU frequency governor + EPP (via amd-pstate on Ryzen, critical for boost on laptop CPUs)
- PCIe ASPM policy (disabling link power-state transitions eliminates ~100 µs wake-up latency spikes)
- Deep C-state disabling (optional — reduces frame-time hitches at the cost of ~3–8 W idle power)
- `vm.swappiness` adjustment (keeps game data in RAM, avoids swap I/O during gameplay)
- Transparent HugePage policy (`enabled`, `shmem_enabled`, `defrag`)
- PCI latency timer
- CCD/CCX core isolation (pins the game to CCD 0, confines background processes to CCD 1 on multi-CCD Ryzen desktops; auto-skipped on single-CCD systems like the 7845HX)

**Commands / Lutris wiring:**

| Field | Value |
|---|---|
| Pre-game script | `/usr/local/bin/lutris-game-tune-wrapper PRE` |
| Post-game script | `/usr/local/bin/lutris-game-tune-wrapper POST` |
| Command prefix (optional) | `/usr/local/bin/lutris-game-tune-wrapper RUN -5` |

`RUN -5` starts the game with `nice -5` (higher CPU priority) using root privilege for the `nice()` syscall, then immediately drops to your user — the game itself never runs as root.

`STATUS` (`pkexec lutris-game-tune-wrapper STATUS`) prints the current active state.

**DRSTool integration:**
- Installation status is detected automatically on startup (checks for the wrapper binary)
- **Install** button runs `lutris-game-tune-main/install.sh` via pkexec (bundled, no download needed)
- **Uninstall** button runs `uninstall.sh` from the installed lib dir via pkexec
- All 19 config keys are exposed as labeled, documented UI controls (QCheckBox, QComboBox, QSpinBox, QLineEdit)
- **Save to /etc/** writes `/etc/lutris-game-tune.conf` atomically via pkexec (stdin-piped `cat >`, then `chmod 644 + chown root:root`)
- **Run STATUS** and **View Log** buttons for at-a-glance diagnostics

Config is stored system-wide in `/etc/lutris-game-tune.conf` (root-owned, 644) and is intentionally **not** part of DRSTool profiles — it applies globally to all games.

## Output bar

Across the top of the window, DRSTool continuously shows the combined environment string generated from your current DRS settings, GPU arch, and env vars, with a Copy All button — ready to paste directly into a Lutris/Steam launch-options field or a shell script.

## Design notes

- **Signal-driven state**: a central `SettingsManager` (`QObject`) is the single source of truth for DRS settings, GPU arch, and profiles, emitting distinct Qt signals (`settings_changed`, `arch_changed`, `profiles_changed`, `profile_loaded`) so UI widgets only rebuild what actually changed.
- **Atomic profile writes**: profiles are written to a temp file, `fsync()`'d, then `os.replace()`'d into place — a crash mid-save cannot corrupt the profiles file.
- **Shell-safe output**: the combined env string is built with `shlex.quote()`, so values containing spaces or special characters are quoted correctly instead of silently breaking when pasted into a shell.
- **Privilege separation**: no part of DRSTool runs as root. All privileged operations (vk_flip_meter install, lutris-game-tune install/save/status) are delegated to pkexec with a graphical polkit password prompt. The lutris-game-tune wrapper binary is the only setuid-root binary, and it validates arguments strictly before executing anything.
- **Bundled sub-projects**: vk_flip_meter and lutris-game-tune ship as subdirectories of the DRSTool repository. No external downloads at runtime.
