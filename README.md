# ⚠️ DISCLAIMER

**USE AT YOUR OWN RISK.** This software is provided as-is without warranty of any kind. By using DRSTool, you acknowledge that:

1. **You accept all responsibility** for any damage, data loss, system instability, or hardware damage that may result from using this tool.
2. **Modifying driver settings can cause serious issues** including driver crashes, GPU hangs, system freezes, or GPU damage if incorrectly configured.
3. **This project was developed with AI assistance.** While the code has been reviewed and tested, AI-assisted development may contain subtle bugs or edge cases not caught during testing.
4. **No liability.** The developers and contributors assume no liability for direct or indirect damages caused by this software.

**Before using:** Test settings on non-critical systems first, back up your working configuration, and keep a recovery method available. If you experience driver issues, completely unset the environment variables and restart your display server/system.

---

                                                        SCREENSHOTS

<img width="1970" height="1467" alt="Screenshot_20260721_125100" src="https://github.com/user-attachments/assets/8e3e741d-17a7-4ce1-818b-3bedc1da8c81" />
<img width="1970" height="1467" alt="Screenshot_20260721_125043" src="https://github.com/user-attachments/assets/e21120ab-af87-454a-a086-5f3ea99cec29" />
<img width="1970" height="1467" alt="Screenshot_20260721_125017" src="https://github.com/user-attachments/assets/a29ad67f-4a9b-47f4-a44a-1ac8916d458d" />
<img width="1970" height="1467" alt="Screenshot_20260721_124954" src="https://github.com/user-attachments/assets/8a5467e1-75b9-4f8e-ac8a-9cd526e87b30" />
<img width="1970" height="1467" alt="Screenshot_20260721_124937" src="https://github.com/user-attachments/assets/0db5a47d-2f13-4af8-913a-7bdec1d239b1" />
<img width="1970" height="1467" alt="Screenshot_20260721_124900" src="https://github.com/user-attachments/assets/51dc442c-b37c-4476-95d9-506fd12ffb56" />


# DRSTool

**DRSTool** is a PySide6 desktop GUI for building `DXVK_NVAPI_DRS_SETTINGS` strings on Linux — the DXVK-NVAPI equivalent of NVIDIA Profile Inspector on Windows. Tuning NVIDIA driver behavior for a game running through Proton/DXVK on Linux normally means hand-writing long, error-prone environment-variable strings from memory or scattered wiki pages. DRSTool turns that into a searchable, documented, point-and-click editor, and bundles two companion tools (a frame-pacing Vulkan layer and a system-wide game-tuning daemon) so the whole per-game tuning workflow lives in one app.

## Quick start

```bash
python3 DRSTool.py
```

Requirements: Python ≥ 3.7, PySide6 ≥ 6.10, PyYAML (only needed for the Lutris Sync feature). For building/installing vk_flip_meter: `cmake`, a C++ compiler, and `pkexec` (PolicyKit).

**Basic workflow:** pick your GPU architecture → browse/search DRS settings and env vars, setting whatever you need → copy the generated string from the top output bar into a launch script / Steam launch options, or use Lutris Sync to write everything straight into a game's Lutris config → optionally save the whole configuration as a named profile to reuse later.

## The 5 tabs, and what each tool does

The window is a sidebar (browse/search) + editor (configure the selected item) layout, with a single combined output string always visible across the top and a "Copy" button next to it.

### 1. DRS Settings
Browse and set **117 NVIDIA Driver Settings (DRS)** — the same low-level settings NVIDIA Profile Inspector exposes on Windows, reimplemented for `dxvk-nvapi`'s `DXVK_NVAPI_DRS_SETTINGS` variable. Organized into 16 categories: DLSS/NGX, OpenGL, Misc, Texture Filtering, VRR/G-Sync, Anti-Aliasing, Frame Rate, SLI, Stereo, Optimus, Power, VSync/Flip, Shader, FXAA, Ansel, AO. Each setting has a short + detailed description and the correct control type (enum dropdown, numeric spinner, or bitfield checkboxes) for how its value is actually encoded.

### 2. GPU Arch
Pick your NVIDIA GPU's architecture family — Maxwell, Pascal, Turing, Ampere, Ada, or Blackwell (GeForce 900-series through RTX 50-series) — to set `DXVK_NVAPI_GPU_ARCH` correctly, since some DRS settings only apply when the driver knows which architecture it's dealing with.

### 3. DXVK / VKD3D / NV / FLM / Gamescope
One combined, searchable list of **192 environment variables** across 11 categories, each with the matching editor control (text field, dropdown, checkbox, or checkbox grid for multi-flag vars like `DXVK_HUD`/`VKD3D_CONFIG`):
- **NVIDIA `__GL_*`** (39) — driver-level OpenGL/Vulkan tuning variables
- **Proton** (38) — Proton-specific behavior toggles
- **vk_flip_meter / FLM** (24) — runtime tuning for the bundled frame-pacing layer (`FLM_MODE`, `FLM_TARGET_FPS`, `FLM_MFG_MULTIPLIER`, `FLM_FLOOR_AUTOTUNE`, etc.)
- **VKD3D-Proton** (20), including the `VKD3D_CONFIG` flag grid
- **DXVK** (18), including the `DXVK_HUD` flag grid
- **Wine** (18)
- **Gamescope** (11) — genuine `gamescope` environment variables (`STEAM_GAMESCOPE_*`, etc. — not CLI flags)
- **DXVK-NVAPI** (10)
- **System / Loader** (6)
- **NVIDIA PRIME** (4) and **NVIDIA Smooth Motion** (4)

The same tab also has a separate **Gamescope Launch Flags** entry with **60 real `gamescope` command-line arguments** (across Geometry, HDR, Input, Frame Pacing, Upscaling, Debug, Session, ReShade, and Steam Deck groups) — these aren't environment variables, so they're built into their own CLI-flag preview string instead of the combined env output, and are what Lutris Sync (below) reads from.

### 4. Extra Tools
Two bundled sub-projects, each with its own build/config sub-tab:
- **vk_flip_meter** — build/install panel for the frame-pacing Vulkan layer. Runs `cmake` configure and build as your normal user, and only escalates to `pkexec` (graphical polkit password prompt) for the two steps that actually need root: `cmake --install` and a manifest library-path fixup.
- **lutris-game-tune** — install/config panel for a system-wide script + setuid wrapper that handles pre/post-game system tuning for Lutris (plus CCD/CCX core isolation and lower-nice game startup). Lets you install/uninstall it, edit its system-wide settings at `/etc/lutris-game-tune.conf` (written via `pkexec`), run its status check, and view its log — all from the same panel.

### 5. Profiles
Two sub-tabs:
- **Saved Profiles** — save the entire current state (DRS settings + GPU arch + all env vars) under a name, then reload or delete it later. Stored as JSON under `$XDG_CONFIG_HOME/drstool/profiles.json` (falls back to `~/.config/drstool/profiles.json`), written atomically (temp file + `fsync` + `os.replace()`) so a crash mid-save can't corrupt the file. Old installs using `~/.drs_configurator_profiles.json` are migrated automatically on first run.
- **Lutris Sync** — finds your Lutris per-game YAML configs and writes DRSTool's current configuration straight into a chosen game's config: DRS/env vars go into `system.env`; Gamescope CLI flags are mapped onto Lutris's native `gamescope_*` keys where one exists (resolution, FPS limiter, sharpness, HDR, cursor grab, the master gamescope toggle), and anything without a native key is packed into a single raw `gamescope_flags` string. If Lutris Game Tune is enabled here, it also writes the `prelaunch_command` / `postexit_command` / `prefix_command` hooks pointing at the installed wrapper.

## Design notes

- **Signal-driven state**: a central `SettingsManager` (`QObject`) is the single source of truth for DRS settings, GPU arch, and profiles, emitting distinct Qt signals (`settings_changed`, `arch_changed`, `profiles_changed`, `profile_loaded`) so widgets only rebuild what actually changed.
- **Shell-safe output**: the combined env string is built with `shlex.quote()`, so values with spaces/special characters are quoted correctly instead of silently breaking when pasted into a shell.
- **Dark, NVIDIA-green-accented UI**, styled consistently across all tabs via shared Qt stylesheets.
