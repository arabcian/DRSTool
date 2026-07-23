                                                              SCREENSHOTS

<img width="1970" height="1467" alt="Screenshot_20260721_125100" src="https://github.com/user-attachments/assets/8e3e741d-17a7-4ce1-818b-3bedc1da8c81" />
<img width="1970" height="1467" alt="Screenshot_20260721_125043" src="https://github.com/user-attachments/assets/e21120ab-af87-454a-a086-5f3ea99cec29" />
<img width="1970" height="1467" alt="Screenshot_20260721_125017" src="https://github.com/user-attachments/assets/a29ad67f-4a9b-47f4-a44a-1ac8916d458d" />
<img width="1970" height="1467" alt="Screenshot_20260721_124954" src="https://github.com/user-attachments/assets/8a5467e1-75b9-4f8e-ac8a-9cd526e87b30" />
<img width="1970" height="1467" alt="Screenshot_20260721_124937" src="https://github.com/user-attachments/assets/0db5a47d-2f13-4af8-913a-7bdec1d239b1" />
<img width="1970" height="1467" alt="Screenshot_20260721_124900" src="https://github.com/user-attachments/assets/51dc442c-b37c-4476-95d9-506fd12ffb56" />


# DRSTool

**DRSTool** is a PySide6 desktop GUI for building `DXVK_NVAPI_DRS_SETTINGS` strings on Linux — the DXVK-NVAPI equivalent of NVIDIA Profile Inspector on Windows. It exists because tuning NVIDIA driver behavior for a game running through Proton/DXVK on Linux normally means hand-writing long, error-prone environment-variable strings from memory or scattered wiki pages. DRSTool turns that into a searchable, documented, point-and-click editor and lets you save the result as a reusable profile per game.

## What it does

DRSTool lets you:

- Browse and set **NVIDIA Driver Settings (DRS)** — the same low-level settings NVIDIA Profile Inspector exposes on Windows, reimplemented here for `dxvk-nvapi`'s `DXVK_NVAPI_DRS_SETTINGS` environment variable.
- Pick a **GPU architecture** so DRSTool generates the correct `DXVK_NVAPI_GPU_ARCH` value for your card.
- Configure **DXVK**, **VKD3D-Proton**, and NVIDIA `__GL_*` environment variables through the same searchable, documented interface, instead of memorizing variable names and valid values.
- Configure the **vk_flip_meter (FLM)** frame-pacing layer's runtime variables and build/install the layer itself from source.
- Save and reload **profiles** (a full snapshot of DRS settings + GPU arch + env vars) per game, and copy the final combined `KEY=VALUE ...` string ready to paste into a launch script, Steam launch options, or a Lutris config.

In short: point, click, describe, copy — instead of writing hex-coded driver settings by hand.

## Why it exists

On Windows, NVIDIA Profile Inspector is the standard tool for tweaking per-game driver behavior beyond what the NVIDIA Control Panel exposes. On Linux there was no equivalent GUI for the `dxvk-nvapi` settings that let you replicate that same fine-grained control for Proton/Wine games — you had to know the hex setting IDs and valid values ahead of time. DRSTool fills that gap with human-readable names, descriptions, and per-setting editors, built specifically for the Linux gaming stack (DXVK, VKD3D-Proton, dxvk-nvapi, vk_flip_meter).

## Requirements

- Python ≥ 3.7
- PySide6 ≥ 6.10
- For the vk_flip_meter build/install feature: `cmake`, a C++ compiler, and `pkexec` (PolicyKit) available on the system

## Running

```bash
python3 DRSTool.py
```

## Interface overview

The app is a single window split into a left sidebar (list/navigation) and a right editor panel, with a persistent output bar across the top showing the currently generated environment string. Five tabs switch what the sidebar/editor show:

### 1. DRS Settings
A searchable, categorized list of **117 driver settings** across categories such as OpenGL, Anti-Aliasing, Texture Filtering, VSync/Flip, Frame Rate, Power, SLI, Stereo, VRR/G-Sync, DLSS/NGX, Ansel, FXAA, AO, Optimus, and Misc. Each setting has a short description and a longer detailed description, plus the correct control type — enum dropdown, numeric spinner, or bitfield checkboxes — matching how the underlying value is actually encoded. Selecting a setting opens its editor on the right; setting a value updates the output bar and highlights the setting green in the sidebar list.

### 2. GPU Arch
A list of NVIDIA GPU architecture families (Maxwell through Blackwell, i.e. GeForce 900-series through RTX 50-series) with example cards for each. Selecting one sets `DXVK_NVAPI_GPU_ARCH` in the output string, since some DRS settings only apply correctly when the driver knows which architecture it's dealing with.

### 3. DXVK / VKD3D / NV / FLM
A single combined, categorized, searchable list covering:
- **DXVK** environment variables (HUD flags, logging, device/frame-related options, etc.) — 15 variables
- **VKD3D-Proton** environment variables, including the `VKD3D_CONFIG` flag grid — 16 variables
- **NVIDIA `__GL_*`** variables — 31 variables
- **vk_flip_meter (FLM)** runtime variables (`FLM_MODE`, `FLM_TARGET_FPS`, `FLM_MFG_MULTIPLIER`, etc.) — 16 variables

Each variable is typed (string, enum, bool, integer, or flag-set) and gets the matching editor control — text field, dropdown, checkbox, or a checkbox grid for multi-flag variables like `DXVK_HUD` and `VKD3D_CONFIG`. Values you set here are merged into the same combined output string as the DRS settings.

### 4. Profiles
Save the entire current state — DRS settings, GPU architecture, and all env vars — under a name, then reload or delete it later. Profiles are stored as JSON under `$XDG_CONFIG_HOME/drstool/profiles.json` (falling back to `~/.config/drstool/profiles.json`), written atomically to avoid corruption on crash/power-loss. Existing installs using the old `~/.drs_configurator_profiles.json` location are migrated automatically on first run.

### 5. vk_flip_meter
A build/install panel for the vk_flip_meter Vulkan layer, bundled as a subproject in this repository. It locates or lets you browse to the layer's source, then runs an unprivileged `cmake` configure + build, and only escalates to `pkexec` for the two steps that actually need root: `cmake --install` and a manifest library-path fixup. This keeps almost the entire build pipeline running as your normal user, only prompting for a password (via a graphical polkit dialog) at the last possible moment.

Runtime tuning of the layer (`FLM_MODE`, `FLM_TARGET_FPS`, etc.) is not done on this tab — it's one click away on the "DXVK / VKD3D / NV / FLM" tab, using the same editor as everything else, and is folded into the combined output string automatically.

## Output bar

Across the top of the window, DRSTool continuously shows the combined environment string generated from your current DRS settings, GPU arch, and env vars, along with a "Copy" action — ready to drop directly into a Lutris/Steam launch-options field or a shell script.

## Design notes

- **Signal-driven state**: a central `SettingsManager` (a `QObject`) is the single source of truth for DRS settings, GPU arch, and profiles, emitting distinct Qt signals (`settings_changed`, `arch_changed`, `profiles_changed`, `profile_loaded`) so UI widgets only rebuild what actually changed — e.g. the profile list only refreshes on `profiles_changed`, not on every single setting edit.
- **Atomic profile writes**: profiles are written to a temp file and `os.replace()`'d into place, with an `fsync()` beforehand, so a crash mid-save can't corrupt the profiles file.
- **Shell-safe output**: the combined env string is built with `shlex.quote()`, so values containing spaces or special characters are quoted correctly instead of silently breaking when pasted into a shell.
- **Dark, NVIDIA-green-accented UI** styled consistently across all tabs (list headers, selection highlighting, scrollbars) via shared Qt stylesheets.
