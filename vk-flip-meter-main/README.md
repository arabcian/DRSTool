# ⚠️ DISCLAIMER

This layer is part of a project developed with AI assistance. Use at your own risk. By using FLM, you accept responsibility for any system instability, GPU hangs, or display corruption that may result. See DISCLAIMER.md in the parent project directory for full details.

---

# FLM — Vulkan Flip Meter / Frame Pacing Layer (v3.0 — "auto")

A Vulkan layer that evens out frame delivery on VRR panels, especially with
frame generation (DLSS-FG / FSR-FG / MFG), and doubles as a precise FPS cap.

v3.0 configures itself. There is nothing to tune for the common case:

```bash
ENABLE_LAYER_cpu_flip_meter=1 %command%
```

## What it does

| Situation | What FLM does |
|---|---|
| `FLM_TARGET_FPS` > 0 | **Limiter**: absolute-timeline FPS cap. Needs no presentWait. |
| VRR, MAILBOX/IMMEDIATE | **Floor pacer**: generated/runt frames are held until a minimum spacing after the previous one; real frames and VRR rate changes pass untouched. |
| FIFO (vsync on) | Cadence is measured. Continuous intervals = VRR → paced. Intervals locked to refresh multiples = fixed refresh → left alone. |
| Tiny swapchains (<640×480) | Ignored (launchers, overlays). |

Detected automatically: the frame-generation multiplier (1–4x), the floor
ratio (closed loop, per multiplier), the sleep/spin margin, hitch threshold
and recovery, fixed-refresh vs VRR on FIFO.

## Variables

| Variable | Meaning |
|---|---|
| `FLM_MODE` | `auto` (default) · `latency` (looser floor, faster hitch recovery) · `present` (also pace FIFO classified as fixed-refresh) · `cap` (limiter only) · `off` (A/B baseline). Hot-reloadable. |
| `FLM_TARGET_FPS` | `>0` = FPS cap. `0` = natural cadence. Hot-reloadable. |
| `FLM_FLOOR_RATIO` | Optional 500–1000. Overrides the base floor ratio (auto 850, latency 780); the closed loop still adjusts around it. Hot-reloadable. |
| `FLM_MFG_MULTIPLIER` | `0` auto (default), `1`–`4` force. Load-time. |
| `FLM_RT_PRIORITY` / `FLM_MEASURE_CPU` | Measurement thread SCHED_FIFO priority / CPU list (`0-3,8`). Load-time. |
| `FLM_LOG_LEVEL` / `FLM_LOG_FILE` | `DEBUG`/`INFO`/`WARN` (default)/`ERROR`; log file (default stderr). |
| `FLM_STATS=1` | Every 5 s at INFO: avg, p99, max, fake/hitch counts, multiplier, effective ratio, FIFO verdict. |
| `FLM_CSV=/path` | Per-flip dump: `flip_ns,interval_ns,is_fake,is_hitch,slot,mfg,slot_mean_ns,pacing`. |
| `FLM_CONFIG=/path` | `KEY=VALUE` file re-read on `SIGUSR1` (hot-reloadable keys only). |

`FLM_PROFILE=vrr|mfg|latency|cap|off` and `FLM_MODE=limiter` still work as
aliases. Every other v2.x `FLM_*` variable was removed; if one is still set
it is reported once in the log (`removed in v3 … ignored`) — delete it.

## Checking it works

```bash
FLM_LOG_LEVEL=INFO FLM_STATS=1 FLM_LOG_FILE=/tmp/flm.log %command%
tail -f /tmp/flm.log
```

* `STATS … mfg=4 ratio=9xx` — pacer active, multiplier detected, floor near a full slot.
* `ratio=0` — the pacer is not running on this swapchain (FIFO judged fixed-refresh, no presentWait, or a cap is set).
* `fifo=fixed` on a VRR panel — the game is sitting at the panel's maximum refresh or at a perfectly steady rate; nothing to smooth. `FLM_MODE=present` forces pacing anyway.
* `presentId/Wait not supported` — only the limiter is available on this driver.

A/B on the same scene (skip the first minute of shader compilation):

```bash
FLM_MODE=off FLM_CSV=/tmp/off.csv %command%
FLM_CSV=/tmp/on.csv %command%
```

Compare the stddev / p99 of `interval_ns`.

## Live tuning

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_CONFIG=/tmp/flm.conf %command%
echo 'FLM_MODE=latency' > /tmp/flm.conf
kill -USR1 $(pidof <game_binary>)
```

Each reload starts from the built-in defaults, then the environment, then the
file — removing a line reverts that key.

## Migrating from v2.x

| v2.x | v3.0 |
|---|---|
| `FLM_PROFILE=mfg` / `vrr` | nothing (default) |
| `FLM_MODE=limiter FLM_TARGET_FPS=N` | `FLM_TARGET_FPS=N` |
| `FLM_PACE_FIFO=1` | automatic (VRR cadence detection); `FLM_MODE=present` to force |
| `FLM_FLOOR_*`, `FLM_SPIN_*`, `FLM_HITCH_*`, `FLM_PROBE_*`, `FLM_WARMUP_FRAMES`, `FLM_PRESENT_LEAD_NS`, `FLM_DRIFT_TOLERANCE_NS`, `FLM_PACE_POINT`, `FLM_STATS_INTERVAL`, `FLM_CSV_SYNC_S` | delete — internal now |
