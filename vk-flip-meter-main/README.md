# FLM — Vulkan Flip Meter / Frame Pacing Layer (v2.2 + FIX-36 floor-pacing)

A Vulkan layer that does frame pacing. Two independent paths:

- **LIMITER** — hard FPS cap (no presentWait needed, always works)
- **PACER** — smooths natural cadence (needs presentWait); `FLM_FLOOR_PACING`
  was added specifically for VRR + MFG (frame generation) on GPUs without
  hardware flip metering (e.g. RTX 40-series)

This README answers "what setting for what situation." The full env-var
reference is at the bottom; scenarios first.

---

## Quick start

```bash
FLM_MODE=present FLM_CONFIG=/tmp/flm.conf mangohud <game>
```

This falls back to LIMITER automatically if presentWait isn't available,
otherwise runs PACER. `FLM_CONFIG` is for live tuning — change the file and
`kill -USR1 <pid>` to reload without restarting the game. We'll use this
pair throughout.

Sanity check: run `FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud <game>` — a
**flat 60 FPS line** in MangoHud means the layer is active.

---

## Scenario 1 — VRR panel + Frame Generation (the primary reason this exists)

**Situation:** G-Sync/FreeSync panel, MFG (DLSS-FG / FSR-FG) enabled, no FPS
cap, game floats between 100-250 FPS. Especially on GPUs **without hardware
flip metering** (RTX 40-series and earlier), generated frames don't come out
evenly spaced — a short/short/short/long pattern (ε,ε,ε,T) that reads as
jitter on the panel.

**What to do:** PACER + floor-pacing. This is exactly what
`FLM_FLOOR_PACING` was built to fix.

```bash
FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850 \
FLM_CONFIG=/tmp/flm.conf mangohud <game>
```

**Why these settings:**
- `FLM_TARGET_FPS` is **left unset** (0 / natural cadence) — on VRR we don't
  want a fixed FPS lock, just tighter spacing between frames.
- `FLM_FLOOR_RATIO=850` is a reasonable starting point: "a present may not
  land sooner than 85% of the slot width after the previous one." This
  delays the too-early generated frame without touching the real frame.

**Tuning by feel:**

| What you're feeling | What to change |
|---|---|
| Still micro-jittery, MFG's rhythm feels off | **Raise** `FLM_FLOOR_RATIO` (900 → 950). Tighter floor, more even spacing. |
| Image feels "sticky" / input feels delayed | **Lower** `FLM_FLOOR_RATIO` (750 → 700). Looser floor, some jitter returns but latency drops. |
| Occasional one-off stutters (unlike the steady MFG jitter) | Likely shader-comp or a real hitch — floor-pacing already skips these (the `hitch_active` guard). `FLM_FLOOR_RATIO` won't fix this; it's game-side. |
| The setting seems to do nothing | presentWait might not be supported (check the log for "presentId/Wait desteklenmiyor" with `FLM_LOG_LEVEL=INFO`). If so only LIMITER runs — floor-pacing never activates. |

**Live tuning (without restarting the game):**
```bash
# /tmp/flm.conf
FLM_FLOOR_RATIO=900
```
```bash
kill -USR1 $(pidof <game_binary>)
```
Confirmed applied when you see `Config reload: mode=... fps=... spin=... lead=...`
in the log.

**A/B comparison (same scene):**
```bash
# Off:
FLM_MODE=off mangohud <game>
# On:
FLM_MODE=present FLM_FLOOR_PACING=1 mangohud <game>
```
You can also flip `FLM_MODE=off` in `flm.conf` and send `SIGUSR1` to switch
live within the same session.

---

## Scenario 2 — VRR panel, MFG off, just smoothing natural cadence

**Situation:** No frame generation, GPU renders straight to the panel, but
there's mild frametime inconsistency from CPU/GPU load variance.

```bash
FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=800 mangohud <game>
```

With MFG off, `m=1` stays fixed; floor-pacing still runs but its effect is
lighter (no bimodal ε/T pattern to correct). Keeping `FLOOR_RATIO` a bit
lower than the MFG scenario (750-800) is usually enough — the goal here is
smoothing small roughness, not suppressing a strong pattern.

---

## Scenario 3 — Fixed-Hz panel (60/120/144 vsync), you want an FPS cap

**Situation:** No VRR, or not using it, and you want a hard ceiling (thermal
/ power reasons, or to suppress MFG jitter via an FPS cap instead).

```bash
FLM_MODE=limiter FLM_TARGET_FPS=120 mangohud <game>
```

The moment `FLM_TARGET_FPS>0` is set, LIMITER takes over and
`FLM_FLOOR_PACING` **has no effect on this path** — the cap path is separate,
uses absolute-target limiter logic (see code: the `FIX-36` block never
enters the `fps > 0` branch).

**You mentioned this yourself:** "MFG produces too much jitter in some
games, so I need to cap FPS" — this is exactly that case. With a cap in
place, MFG's excess frames already get filtered by the GPU-bound guard
(`over_target_run` → `pacing_enabled=false`), so LIMITER + MFG combo needs
no extra tuning — just pick a target FPS the game can actually sustain.

**Which FPS to pick:** your general strategy is already the 150-220 FPS
band. If a game can't hold that band on VRR (too many dips), capping
**slightly below the band's lower bound** (e.g. 144 or 165) tends to feel
smoother than capping near the top — you leave the GPU some headroom
instead of pushing it to the ceiling constantly.

---

## Scenario 4 — Engine running FIFO/vsync-on

**Situation:** The game uses FIFO, not MAILBOX/IMMEDIATE (already locked to
vsync).

Nothing to configure — the code detects this itself (`resolve_gate`): PACER
never activates on FIFO (so it doesn't fight the compositor), only LIMITER
(if `FLM_TARGET_FPS` is set) runs. Floor-pacing stays inactive on FIFO too.

---

## Scenario 5 — Small/helper swapchains (launcher, overlay windows)

These are automatically excluded from pacing (`MIN_SC_WIDTH=640`,
`MIN_SC_HEIGHT=480` and below → `pace_allowed=false`). No configuration
needed — informational only, your main game window keeps being paced.

---

## General troubleshooting: "nothing seems to change"

Check in order:

1. **Is the layer actually loading?**
   ```bash
   FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log mangohud <game>
   tail -f /tmp/flm.log
   ```
   You should see a `Config: mode=... fps=... ...` line.

2. **Is presentWait supported?**
   If the log shows `presentId/Wait desteklenmiyor; PACER kapali`, no PACER
   feature runs, floor-pacing included — only LIMITER (`FLM_TARGET_FPS`) is
   usable.

3. **Is the swapchain FIFO?**
   If you think PACER never triggers with `FLM_MODE=present`, the game is
   likely on FIFO (Scenario 4). Try `FLM_PACE_POINT=acquire` and see if
   anything changes — if still nothing, it's FIFO, that's expected.

4. **Has warmup passed?**
   The first 30 frames (`WARMUP_FRAMES`) are never paced — don't worry if
   you see no effect in the game's first second.

5. **Measure with CSV (watch out for shader-cache noise):**
   ```bash
   FLM_CSV=/tmp/flm.csv FLM_MODE=present FLM_FLOOR_PACING=1 mangohud <game>
   ```
   Exclude the first 1-2 minutes (shader compilation period) from analysis,
   then look at the stddev of the `interval_ns` column.

---

## Variables — full reference

| Variable | When to change it |
|---|---|
| `FLM_MODE=auto\|present\|limiter\|off` | `off`: A/B baseline. `limiter`: for a hard FPS cap. `present`: for VRR/PACER. `auto` (default): usually leave this, the code picks correctly. |
| `FLM_TARGET_FPS=<n>` | **Setting it >0 switches to LIMITER**, floor-pacing is disabled. Leave this unset (0) in the VRR + MFG scenario. |
| `FLM_FLOOR_PACING=1\|0` | Keep on for VRR+MFG (on by default). Set `0` to fall back to the old absolute-grid pacer. |
| `FLM_FLOOR_RATIO=850` | **The main knob you tune by feel.** Higher = flatter/tighter, lower = looser/less latency. 700-950 is the sane range. |
| `FLM_PACE_POINT=present\|acquire\|both` | Leave at default `present`. Only try `both` if you've confirmed via CSV that `present` alone isn't enough. |
| `FLM_PRESENT_LEAD_NS` | The default is usually fine even at high Hz (240Hz); if you hit issues, raise `FLM_SPIN_NS` first. |
| `FLM_SPIN_NS=150000` | At very high Hz (240Hz), raise this (e.g. 300000) if you feel kernel wake latency. Slightly raises CPU usage. |
| `FLM_MFG_MULTIPLIER=0` | If autodetection is flaky (log shows `MFG carpani: X -> Y` flipping often), force the multiplier (1-4). |
| `FLM_RT_PRIORITY` / `FLM_MEASURE_CPU` | If the measurement thread is contending for CPU (rarely needed on systems with many cores). |
| `FLM_STATS=1` + `FLM_STATS_INTERVAL=5` | Periodic summary log; turn on while tuning for live feedback. |
| `FLM_CSV=/tmp/flm.csv` | For persistent measurement — use once shader-cache has settled, in a stable scene, for A/B comparison. |
| `FLM_CONFIG=/tmp/flm.conf` + `SIGUSR1` | The normal way to try any of the above settings without restarting the game. |
| `FLM_LOG_LEVEL` / `FLM_LOG_FILE` | `INFO` for troubleshooting, `DEBUG` to watch MFG transitions. |

---

## Decision tree, summarized

```
VRR panel + MFG on, no cap wanted
  → FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850
    → still jittery   → raise RATIO
    → feels heavy/laggy → lower RATIO

MFG jitter too strong, want to suppress via cap
  → FLM_MODE=limiter FLM_TARGET_FPS=<near lower bound, e.g. 144-165>

FIFO/vsync-on engine
  → do nothing, the code picks the right path automatically

Setting seems to have no effect
  → log with FLM_LOG_LEVEL=INFO, check presentWait + FIFO
```
