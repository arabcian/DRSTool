// ============================================================================
// FLM — Vulkan Flip Meter / Frame Pacing Layer  (v2.4 — "real impact")
//
// DESIGN SUMMARY
// --------------
// Two independent paths:
//
//   1) LIMITER  — does NOT require presentWait. Pure local-clock
//      absolute-timeline FPS cap at QueuePresent (libstrangle logic). Always
//      active, shows as an instant flat line in MangoHud. Visible, safe,
//      deterministic. Requires FLM_TARGET_FPS.
//
//   2) PACER    — requires presentWait. Measurement thread reads real flip
//      timestamps and builds a timeline estimate; if an MFG (frame-gen)
//      multiplier is detected, generated frames are distributed EVENLY across
//      the flip interval (slot pacing). For smooth frametimes on a VRR panel.
//
// ANTI-STUTTER RULES (fixes for v1 placebo/stutter causes):
//   * SINGLE GATE: pacing only at ONE point (default: Present). v1 stalled at
//     both Acquire and Present → double latency.
//   * GPU-BOUND GUARD: stalling on the CPU when the game is GPU-limited drains
//     the queue and makes things worse. Pacing disables automatically when
//     consecutive frames exceed the target.
//   * FIFO/vsync BYPASS: FIFO is already locked to vsync; pacing on top fights
//     the compositor. Only MAILBOX/IMMEDIATE (VRR) is paced. Small auxiliary
//     swapchains are never paced.
//   * SOFT SLEW: timeline drift is corrected gradually instead of hard-rebasing.
//   * LEAD-BASED PRESENT: present is submitted FLM_PRESENT_LEAD_NS before the
//     predicted flip (v1's wrong "+1 frame" formula removed).
//
// Permanent fixes (from v1):
//   [FIX-1]  Hot-path shared_ptr copy (UAF).
//   [FIX-2]  DestroyDevice stops+joins all state.
//   [FIX-13] Loader chain restore on CreateDevice fallback (MangoHud crash).
//   [FIX-14] Features already in pNext are not re-injected.
//   [FIX-15] App's own presentIds are tracked (DXVK compatibility).
//
// v2.1 fixes (performance / latency / smoothness):
//   [FIX-16] SLOT INTERVAL = sliding mean of ALL intervals. m frames take T
//            total → average interval = T/m; correct slot width in both paced
//            and unpaced modes. v2 used a fake-filtered EMA (≈T) → in MFG the
//            pacer DIVIDED FPS by m. Removed.
//   [FIX-17] MFG autodetect: threshold now relative to slot-EMA
//            (interval < 0.7*ema). v2's accept-median threshold was
//            mathematically never triggered (p≈0 → mhat=1). Detection is
//            FROZEN while the gate is active (paced uniform intervals poison
//            detection → oscillation guard).
//   [FIX-18] GPU-bound guard only when FLM_TARGET_FPS>0, and uses slot-EMA
//            rather than raw intervals. In v2 MFG's bimodal intervals
//            immediately triggered the guard and killed pacing. At fps=0 the
//            target is derived from measurements anyway, so the guard is
//            meaningless there.
//   [FIX-19] "interval > 2.5*avg → is_fake" branch removed: large HITCHes
//            were classified as fake and escaped hitch detection → pacing
//            continued during a hitch (visible stutter).
//   [FIX-20] Gate wait cap is now interval-relative (max(20ms, 1.5*iv)).
//            The fixed 20ms cap made the limiter a complete NO-OP at FPS<=50.
//   [FIX-21] Live config is REAL: FLM_CONFIG=<file> (KEY=VALUE) + SIGUSR1.
//            v2 read getenv in the handler — the running process's environment
//            can't be changed externally (no-op) and getenv is not
//            async-signal-safe (UB). Handler now only sets an atomic flag;
//            reload happens in the measurement/present thread context.
//   [FIX-22] vkAcquireNextImage2KHR is now intercepted (engines using this
//            path never advanced the warmup counter → gate never opened).
//   [FIX-23] Dead state cleanup (gate_target_ns / base_flip_ns).
//
// v2.2 fixes (distilled from three independent code reviews):
//   [FIX-24] PACER lead clamp: when lead >= iv/2 the target fell into the past
//            and the gate silently became a no-op (e.g. high FPS + default
//            1ms lead). Now lead = min(FLM_PRESENT_LEAD_NS, iv/2).
//   [FIX-25] Leading whitespace in config file values was not trimmed:
//            "FLM_MODE= present" could not be parsed (string comparison).
//   [FIX-26] Reload no longer calls getenv: env is snapshotted once at init
//            (POSIX getenv has a theoretical data race in reload threads, and
//            the running process's env can't change externally anyway). Same
//            semantics: snapshot + file, file wins; if a line is removed the
//            env value is restored.
//   [FIX-27] Dead state cleanup: timeline_target_ns, app_owns_present_id,
//            filtered_interval_ns EMA (only read in di_count==0 fallback —
//            never updated at that point = constant). stat_fake →
//            stat_fake_hitch (was accumulating both fake and hitch, misleading
//            name).
//   [FIX-28] Hot-path false sharing: limiter_next_ns (present thread) and
//            fields written every frame by the measurement thread shared a
//            cache line. Separated with alignas(64).
//   [FIX-29] Log: fflush only at INFO+. DEBUG is fully buffered (64 KB) —
//            per-frame flush overhead eliminated while DEBUG is on. Note: on
//            crash the last DEBUG lines may remain in the buffer (flushed on
//            normal exit).
//   [FIX-30] CSV: 1 MB stdio buffer + no fflush in csv_flush → csv_flush is
//            now pure in-memory formatting; disk write() only when the buffer
//            fills (~26k rows). Measurement thread timing is protected from I/O.
//   [FIX-31] CSV telemetry columns: eff_mfg, slot_mean_ns, pacing —
//            for regression analysis of MFG detection and GPU-bound guard.
//   [FIX-32] FLM_STATS_INTERVAL=<sec> (hot-reloadable, default 5).
//   [FIX-33] FLM_TARGET_FPS [0,1000] clamp (atoi overflow / iv=0 guard) and
//            initial reserve() on maps.
//
// v2.3 fixes (smoothness + input lag):
//   [FIX-37] FLOOR-PACING FREEZE/BRAKE LOOP. real_win was fed only NON-FAKE
//            intervals; with floor active and m>1 ALL intervals (uniform ≈T/m
//            and the real frame's remainder) fall BELOW the fake threshold
//            (≈0.75T) → real_win + accept-median fully FREEZE → slot_iv locks
//            on the old T₀. On VRR, when FPS rises the floor becomes stale and
//            brakes every present; braked intervals also stay in the fake class
//            so the estimate can never self-correct (positive lock) — the
//            "absolute grid brake" that FIX-36 tried to eliminate came back
//            permanently. FIX: T estimation is now fake-filter-independent and
//            phase-insensitive via CYCLE SUM: the sum of the last m RAW
//            intervals ≈ T (in paced/unpaced/bimodal cases alike; ε+(T-ε)=T).
//            Updated every flip → tracks FPS changes without braking; if a
//            brake forms, negative feedback releases it. Fake class kept for
//            stats/CSV only. display_intervals median (its sole consumer)
//            removed; hitch threshold and fake split now tied to this live T
//            estimate (hitches that escaped with the stale median).
//   [FIX-38] FIX-36 false-sharing regression: real_win/real_idx/real_count
//            had been placed on the present-thread cache line (next to
//            limiter_next_ns) but the MEASUREMENT thread writes them every
//            frame → the cache-line ping-pong fixed by FIX-28 came back.
//            Moved to the measurement block; only present-thread fields remain
//            on the present line.
//   [FIX-39] ADAPTIVE SPIN: the kernel sleep's actual wakeup latency
//            (oversleep) is tracked with a damped maximum; spin margin is
//            adjusted accordingly. On a loaded system the fixed 150 µs margin
//            caused the gate to MISS its target (floor missed → jitter spike);
//            on a quiescent/RT system it burned ~120 µs of pointless spin every
//            frame (≈3% of core time at 240 FPS). FLM_SPIN_ADAPT=0 → old fixed
//            behaviour; FLM_SPIN_NS=0 → pure sleep (unchanged).
//   [FIX-40] Low-FPS warmup lock: hitch threshold starts at the 16.6 ms
//            default, so at ~30 FPS the FIRST frames are classified as hitches
//            and the estimation window never warms up, leaving pacing
//            permanently disabled. Hitch classification is suppressed until the
//            window is warm (4 samples).
//
// v2.4 fixes (smoothness — concept-validation review):
//   [FIX-42] With fps>0 the floor path was BYPASSING the LIMITER: the floor
//            branch only checked !limiter_mode. AUTO + presentWait +
//            FLM_TARGET_FPS=120 → slot=8.33ms, floor=7.08ms → game could run
//            up to 117% of the target (≈141 FPS); no hard lock → wavy
//            frametime. Floor is now ONLY for fps==0 (natural cadence); at
//            fps>0 the classic lead-based timeline pacer is used (full lock).
//            Consistent with the README.
//   [FIX-43] MEASUREMENT FRESHNESS GUARD. If presentWait measurement never
//            produces samples (game sends id=0 → continuous TIMEOUT)
//            slot_interval_ns stays at the 16.6ms default → floor≈14.2ms →
//            a 240Hz game gets CAPPED at ~70 FPS. Same class of problem after
//            alt-tab / OUT_OF_DATE. Measurement thread publishes last_flip_ns
//            on every successful flip; pacer and floor gates (NOT the limiter)
//            shut themselves off and reset anchors if there are no samples or
//            the last flip is older than MEAS_FRESH_NS (250ms). No measurement
//            flow → no pacing.
//   [FIX-44] FLOOR RATIO AUTOTUNE (closed loop). At ratio=850, m=2 the
//            steady-state intervals ALTERNATE 0.425T / 0.575T (CoV ≈15%) —
//            the structural cause of "better but not quite smooth". The ideal
//            ratio is usually close to 1000 but a fixed high ratio brakes
//            early-arriving real frames. Fix: if recent presents have abundant
//            headroom (since-floor), ratio is tightened SLOWLY (+1/frame); if
//            headroom narrows or consecutive >= max(2,m) presents are held
//            (brake sign), it is QUICKLY loosened. Delta [-150,+150] stacks on
//            top of the base ratio and MFG-adapt; [500,1000] clamp preserved.
//            FLM_FLOOR_AUTOTUNE=0 → old fixed-ratio behaviour.
//   [FIX-45] Cleanup: dead cap in floor path (left < floor*2 — since>=0 means
//            left<=floor, cap was never reachable) simplified; hitch and
//            GPU-bound branches now explicitly reset the floor anchor
//            (last_present_ns) and autotune brake counter (explicit re-anchor
//            instead of implicit).
// ============================================================================

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#ifndef VK_LAYER_EXPORT
#  define VK_LAYER_EXPORT __attribute__((visibility("default")))
#endif

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <time.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>

#if defined(__x86_64__) || defined(__i386__)
#  include <immintrin.h>
#  define FLM_CPU_PAUSE() _mm_pause()
#else
#  define FLM_CPU_PAUSE() std::this_thread::yield()
#endif

// ============================================================================
// LOGGING
// ============================================================================
enum class LogLevel { DEBUG = 0, INFO, WARN, ERR };
static std::atomic<int> g_log_level{(int)LogLevel::ERR};   // [item 15] atomic
static FILE*            g_log_file = stderr;

// [FIX-29] fflush only at INFO+ — DEBUG spam stays buffered (stderr is already
// unbuffered; this only matters for FLM_LOG_FILE).
#define FLM_LOG(level, ...) do { \
    if ((int)(level) >= g_log_level.load(std::memory_order_relaxed)) { \
        fprintf(g_log_file, "[FLM] " __VA_ARGS__); \
        fputc('\n', g_log_file); \
        if ((int)(level) >= (int)LogLevel::INFO) fflush(g_log_file); \
    } \
} while (0)

// ============================================================================
// CONSTANTS
// ============================================================================
namespace FlmConst {
    constexpr int64_t  DEFAULT_INTERVAL_NS = 16'666'666LL;
    constexpr int64_t  DEFAULT_SPIN_NS     = 150'000LL;
    constexpr int64_t  DEFAULT_LEAD_NS     = 1'000'000LL;
    constexpr int      HITCH_RECOVERY      = 8;
    constexpr int      WARMUP_FRAMES       = 30;
    constexpr uint64_t WAIT_TIMEOUT_NS     = 50'000'000ULL;
    constexpr int64_t  MAX_PACE_WAIT_NS    = 20'000'000LL;
    constexpr uint32_t STACK_PRESENT_IDS   = 8;
    constexpr int      GPU_BOUND_WINDOW    = 16;   // [item 8]
    constexpr int      SLOT_WINDOW         = 12;   // [FIX-16] 12 = lcm(1..4) →
                                                   // full cycle for every MFG
                                                   // multiplier, no phase-
                                                   // induced mean bias
    // [FIX-36] VRR + MFG floor-pacing: short window for real-frame period
    // median, fast response to FPS changes. SLOT_WINDOW (mean, 12) lagged
    // behind the true instantaneous period during 150↔220 FPS swings.
    // [FIX-37] Window now holds CYCLE-SUM estimates (sum of last m raw
    // intervals ≈ T) — updated every flip regardless of pacing state, no
    // dependency on the fake filter.
    constexpr int      REAL_WINDOW         = 8;    // last N T estimates
    constexpr int      CYC_RING            = 4;    // [FIX-37] last raw intervals (max MFG mult)
    constexpr int64_t  MIN_FLOOR_NS        = 500'000LL;   // 2000 FPS ceiling: floor never goes below this
    constexpr int      MFG_DETECT_WINDOW   = 64;   // [item 7]
    constexpr int      MIN_SC_WIDTH        = 640;  // [item 11]
    constexpr int      MIN_SC_HEIGHT       = 480;
    constexpr int      CSV_BUFFER          = 256;  // [item 12]
    constexpr int64_t  STATS_INTERVAL_NS   = 5'000'000'000LL;  // [FIX-32]
    constexpr int64_t  MEAS_FRESH_NS       = 250'000'000LL;    // [FIX-43] measurement freshness window
    constexpr size_t   CSV_STDIO_BUF       = 1u << 20;         // [FIX-30]
    constexpr size_t   LOG_STDIO_BUF       = 64u << 10;        // [FIX-29]
}

enum class PaceMode  { AUTO = 0, PRESENT, LIMITER, OFF };
enum class PacePoint { PRESENT = 0, ACQUIRE, BOTH };

// ============================================================================
// CONFIG (hot-reloadable fields are atomic — [item 15])
// ============================================================================
struct FLMConfig {
    // Structural (not reloadable)
    int         mfg_mult_env = 0;   // 0 = auto-detect; >0 = force
    int         rt_priority  = 0;
    std::string measure_cpu;        // [item 13]
    bool        stats        = false;
    std::string csv_path;
    std::string config_path;        // [FIX-21] FLM_CONFIG live config file

    // Hot-reloadable
    std::atomic<int>     target_fps {0};
    std::atomic<int64_t> spin_ns    {FlmConst::DEFAULT_SPIN_NS};
    std::atomic<int64_t> lead_ns    {FlmConst::DEFAULT_LEAD_NS};
    std::atomic<int64_t> drift_tol  {0};
    std::atomic<int>     mode       {(int)PaceMode::AUTO};
    std::atomic<int>     pace_point {(int)PacePoint::PRESENT};
    std::atomic<int64_t> stats_interval_ns {FlmConst::STATS_INTERVAL_NS}; // [FIX-32]

    // [FIX-36] VRR + MFG floor-pacing settings — ALL hot-reloadable.
    // floor mode does not brake variable FPS on VRR; it only prevents
    // ε-bursts from exiting too early, smoothing the generated/real gap.
    std::atomic<bool>    floor_pacing {true};   // FLM_FLOOR_PACING=1 (default on)
    // floor = slot_iv * (floor_ratio/1000). 850 = 0.85 → present exits at least
    // 85% of a slot after the previous one. Low = looser (jitter passes through),
    // high = tighter (flatter but risks hitch if late). Main hand-tuning knob.
    std::atomic<int>     floor_ratio {850};     // FLM_FLOOR_RATIO (500-1000)

    // [FIX-41] MFG-adaptive ratio relaxation. Ada (40-series) GPU must pack
    // extra generated frames into the same GPU budget → real slot duration
    // (≈T/m) spreads with higher variance as m grows. A fixed floor_ratio that
    // works at m=2 may hold real frames inside the floor at m=3/4, causing
    // unnecessary stalls and braking (part of why hitch% and cov% multiply with
    // m). Ratio is gradually relaxed per extra multiplier step: subtract
    // FLM_FLOOR_MFG_STEP per (m-1) increment (default 40/1000 units). No effect
    // at m=1.
    std::atomic<bool>     floor_mfg_adapt {true};   // FLM_FLOOR_MFG_ADAPT (default 1/on)
    std::atomic<int>      floor_mfg_step  {40};     // FLM_FLOOR_MFG_STEP (0-200), ratio units/step

    // [FIX-44] Closed-loop ratio adjustment: tighten when headroom is ample,
    // loosen on brake signs. Delta [-150,+150] stacks on base ratio + MFG-adapt.
    std::atomic<bool>     floor_autotune  {true};   // FLM_FLOOR_AUTOTUNE (default 1/on)

    // [FIX-39] Auto-adjust spin margin based on measured wakeup latency (hot-reload).
    // 0 = old behaviour: fixed spin of exactly FLM_SPIN_NS.
    std::atomic<bool>    spin_adapt {true};     // FLM_SPIN_ADAPT (default 1)
};

static FLMConfig      g_config;
static std::once_flag g_config_flag;

static PaceMode parse_mode(const char* s) {
    if (!s) return PaceMode::AUTO;
    if (!strcmp(s, "present")) return PaceMode::PRESENT;
    if (!strcmp(s, "limiter")) return PaceMode::LIMITER;
    if (!strcmp(s, "off"))     return PaceMode::OFF;
    return PaceMode::AUTO;
}
static PacePoint parse_pace_point(const char* s) {
    if (!s) return PacePoint::PRESENT;
    if (!strcmp(s, "acquire")) return PacePoint::ACQUIRE;
    if (!strcmp(s, "both"))    return PacePoint::BOTH;
    return PacePoint::PRESENT;
}

// [FIX-21] Single KV applier — both env and config file go through here.
static void apply_dynamic_kv(const char* key, const char* val) {
    if (!key || !val || !*val) return;
    // [FIX-33] fps clamp: guards against iv=0 and atoi overflow in iv=1e9/fps.
    if      (!strcmp(key, "FLM_TARGET_FPS"))         g_config.target_fps.store(std::clamp(atoi(val), 0, 1000));
    else if (!strcmp(key, "FLM_STATS_INTERVAL"))     g_config.stats_interval_ns.store(   // [FIX-32] seconds
                                                         std::clamp<int64_t>(atoll(val), 1, 3600) * 1'000'000'000LL);
    else if (!strcmp(key, "FLM_SPIN_NS"))            g_config.spin_ns.store(std::clamp<int64_t>(atoll(val), 0, 2'000'000LL));
    else if (!strcmp(key, "FLM_PRESENT_LEAD_NS"))    g_config.lead_ns.store(std::clamp<int64_t>(atoll(val), 0, 8'000'000LL));
    else if (!strcmp(key, "FLM_DRIFT_TOLERANCE_NS")) g_config.drift_tol.store(std::max<int64_t>(0, atoll(val)));
    else if (!strcmp(key, "FLM_MODE"))               g_config.mode.store((int)parse_mode(val));
    else if (!strcmp(key, "FLM_PACE_POINT"))         g_config.pace_point.store((int)parse_pace_point(val));
    else if (!strcmp(key, "FLM_FLOOR_PACING"))       g_config.floor_pacing.store(atoi(val) != 0);   // [FIX-36]
    else if (!strcmp(key, "FLM_FLOOR_RATIO"))        g_config.floor_ratio.store(std::clamp(atoi(val), 500, 1000)); // [FIX-36]
    else if (!strcmp(key, "FLM_FLOOR_MFG_ADAPT"))    g_config.floor_mfg_adapt.store(atoi(val) != 0);   // [FIX-41]
    else if (!strcmp(key, "FLM_FLOOR_MFG_STEP"))     g_config.floor_mfg_step.store(std::clamp(atoi(val), 0, 200)); // [FIX-41]
    else if (!strcmp(key, "FLM_FLOOR_AUTOTUNE"))     g_config.floor_autotune.store(atoi(val) != 0);   // [FIX-44]
    else if (!strcmp(key, "FLM_SPIN_ADAPT"))         g_config.spin_adapt.store(atoi(val) != 0);   // [FIX-39]
    else if (!strcmp(key, "FLM_LOG_LEVEL")) {
        if      (!strcmp(val, "DEBUG")) g_log_level.store((int)LogLevel::DEBUG);
        else if (!strcmp(val, "INFO"))  g_log_level.store((int)LogLevel::INFO);
        else if (!strcmp(val, "WARN"))  g_log_level.store((int)LogLevel::WARN);
        else if (!strcmp(val, "ERROR")) g_log_level.store((int)LogLevel::ERR);
    }
}

// [FIX-21] FLM_CONFIG file: '#' comments, KEY=VALUE lines.
static void load_config_file(const char* path) {
    FILE* f = fopen(path, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof line, f)) {
        char* p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '#' || *p == '\n' || *p == '\0') continue;
        char* eq = strchr(p, '=');
        if (!eq) continue;
        *eq = '\0';
        char* key = p;
        char* ke  = eq;
        while (ke > key && (ke[-1] == ' ' || ke[-1] == '\t')) *--ke = '\0';
        char* val = eq + 1;
        while (*val == ' ' || *val == '\t') val++;   // [FIX-25] "KEY= value"
        size_t n = strlen(val);
        while (n && (val[n-1] == '\n' || val[n-1] == '\r' ||
                     val[n-1] == ' '  || val[n-1] == '\t')) val[--n] = '\0';
        apply_dynamic_kv(key, val);
    }
    fclose(f);
}

// [FIX-26] Snapshotted once at init: getenv has a theoretical data race in
// reload threads under POSIX, and the running process's env can't be changed
// externally anyway. Revert semantics preserved: reload = snapshot + file
// (file wins; if a line is removed, env value is restored).
static std::vector<std::pair<std::string, std::string>> g_env_snapshot;

static void snapshot_dynamic_env() {
    static const char* keys[] = {
        "FLM_TARGET_FPS", "FLM_STATS_INTERVAL", "FLM_SPIN_NS",
        "FLM_PRESENT_LEAD_NS", "FLM_DRIFT_TOLERANCE_NS",
        "FLM_MODE", "FLM_PACE_POINT", "FLM_LOG_LEVEL",
        "FLM_FLOOR_PACING", "FLM_FLOOR_RATIO",   // [FIX-36]
        "FLM_FLOOR_MFG_ADAPT", "FLM_FLOOR_MFG_STEP",   // [FIX-41]
        "FLM_FLOOR_AUTOTUNE",                    // [FIX-44]
        "FLM_SPIN_ADAPT",                        // [FIX-39]
    };
    for (const char* k : keys)
        if (const char* e = getenv(k)) g_env_snapshot.emplace_back(k, e);
}

// Env snapshot first (static), then file (live) — file wins.
static void reload_dynamic_config() {
    for (const auto& [k, v] : g_env_snapshot)
        apply_dynamic_kv(k.c_str(), v.c_str());
    if (!g_config.config_path.empty())
        load_config_file(g_config.config_path.c_str());
}

// [FIX-21] Handler is async-signal-safe: only sets a flag. Applied in thread context.
static std::atomic<bool> g_reload_flag{false};
static void sigusr1_handler(int) { g_reload_flag.store(true, std::memory_order_relaxed); }

static inline void maybe_reload() {
    if (g_reload_flag.load(std::memory_order_relaxed) &&
        g_reload_flag.exchange(false, std::memory_order_relaxed)) {
        reload_dynamic_config();
        FLM_LOG(LogLevel::INFO, "Config reload: mode=%d fps=%d spin=%lld lead=%lld",
                g_config.mode.load(), g_config.target_fps.load(),
                (long long)g_config.spin_ns.load(), (long long)g_config.lead_ns.load());
    }
}

static void reserve_global_maps();  // [FIX-33] defined after map declarations

#ifdef FLM_PGO_INSTRUMENTED
static void sigusr2_handler(int);  // [FIX-34] defined after now_ns() (forward decl)
#endif

static void init_config() {
    std::call_once(g_config_flag, []() {
        const char* e;
        if ((e = getenv("FLM_MFG_MULTIPLIER"))) g_config.mfg_mult_env = std::clamp(atoi(e), 0, 4);
        if ((e = getenv("FLM_RT_PRIORITY")))    g_config.rt_priority  = std::clamp(atoi(e), 0, 99);
        if ((e = getenv("FLM_MEASURE_CPU")))    g_config.measure_cpu  = e;
        if ((e = getenv("FLM_STATS")))          g_config.stats        = (atoi(e) != 0);
        if ((e = getenv("FLM_CSV")))            g_config.csv_path     = e;
        if ((e = getenv("FLM_CONFIG")))         g_config.config_path  = e;  // [FIX-21]
        if ((e = getenv("FLM_LOG_FILE"))) {
            if (FILE* f = fopen(e, "a")) {
                // [FIX-29] Full buffering for DEBUG volume (INFO+ flushes anyway).
                setvbuf(f, nullptr, _IOFBF, FlmConst::LOG_STDIO_BUF);
                g_log_file = f;
            }
        }

        snapshot_dynamic_env();     // [FIX-26]
        reload_dynamic_config();

        // [item 15] Install SIGUSR1 only if no one else has claimed it.
        struct sigaction old{};
        if (sigaction(SIGUSR1, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr1_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR1, &sa, nullptr);
        }

#ifdef FLM_PGO_INSTRUMENTED
        // [FIX-34] SIGUSR2: instant .gcda flush via "kill -USR2 <pid>" without
        // stopping the game. The only option for launchers where atexit is
        // unreliable.
        if (sigaction(SIGUSR2, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr2_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR2, &sa, nullptr);
        }
        FLM_LOG(LogLevel::WARN,
                "PGO INSTRUMENTED build active — periodic (60s) + SIGUSR2 .gcda flush");
#endif

        reserve_global_maps();  // [FIX-33]

        FLM_LOG(LogLevel::INFO,
                "Config: mode=%d fps=%d mfg_env=%d spin=%lldns lead=%lldns rt=%d",
                g_config.mode.load(), g_config.target_fps.load(), g_config.mfg_mult_env,
                (long long)g_config.spin_ns.load(), (long long)g_config.lead_ns.load(),
                g_config.rt_priority);
    });
}

// [FIX-34] atexit() is unreliable in PGO instrumented builds: Steam/Proton
// usually kills the process with _exit()/exit_group, so atexit handlers never
// run → .gcda is never written. This block is only active when built with
// -DFLM_PGO_INSTRUMENTED during the ebuild PGO "generate" phase; absent in
// normal and PGO-use builds.
#ifdef FLM_PGO_INSTRUMENTED
extern "C" void __gcov_dump(void);
extern "C" void __gcov_reset(void);
static std::atomic<int64_t> g_last_gcov_dump_ns{0};
static std::atomic<bool>    g_gcov_dump_flag{false};
// SIGUSR2: on-demand immediate dump (for profiling without closing the game).
static void sigusr2_handler(int) { g_gcov_dump_flag.store(true, std::memory_order_relaxed); }
#endif


static inline int64_t now_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1'000'000'000LL + ts.tv_nsec;
}

#ifdef FLM_PGO_INSTRUMENTED
// Defined here because it depends on now_ns() (declared above).
static inline void flm_gcov_periodic_dump() {
    int64_t t = now_ns();
    int64_t last = g_last_gcov_dump_ns.load(std::memory_order_relaxed);
    bool due = g_gcov_dump_flag.exchange(false, std::memory_order_relaxed);
    if (!due && (t - last) < 60'000'000'000LL) return;   // 60s period
    if (g_last_gcov_dump_ns.compare_exchange_strong(last, t, std::memory_order_relaxed)) {
        __gcov_dump();
        FLM_LOG(LogLevel::INFO, "PGO: gcov profile written to disk (.gcda)");
    }
}
#endif


// Bulk ABSTIME kernel sleep (signal-interrupt-resistant), spin for the last
// spin_ns. FLM_SPIN_NS=0 → pure sleep (min CPU).
//
// [FIX-39] ADAPTIVE SPIN. The actual wakeup latency of clock_nanosleep
// (oversleep = wakeup time - requested time; timer slack + scheduler queue)
// is tracked with a damped maximum:
//   est = max(measured, est - est/256)   → grows instantly, decays ≈256 samples
// and spin margin = est*1.5 + 20µs. The fixed 150µs margin had two failure
// modes:
//   * Loaded system: oversleep > 150µs → gate MISSES its target → late present
//     → the gate itself produces the jitter that floor/limiter tries to fix.
//   * Idle/RT system: oversleep ≈5-30µs → ~120µs of wasted spin every frame
//     (≈3% of core time at 240 FPS goes to heat).
// Adaptive margin fixes both: present always lands EXACTLY on target
// (smoothness) and never spins unnecessarily early (CPU stays with the game).
static std::atomic<int64_t> g_oversleep_est{100'000};  // ns; conservative start

static void precise_wait_absolute(int64_t target) {
    if (target <= 0) return;
    const int64_t spin_cfg = g_config.spin_ns.load(std::memory_order_relaxed);
    const bool    adapt    = spin_cfg > 0 &&
                             g_config.spin_adapt.load(std::memory_order_relaxed);
    int64_t spin = spin_cfg;
    if (adapt) {
        int64_t est = g_oversleep_est.load(std::memory_order_relaxed);
        spin = std::clamp<int64_t>(est + est / 2 + 20'000, 30'000, 2'000'000);
    }
    for (;;) {
        int64_t left = target - now_ns();
        if (left <= spin) break;
        int64_t wake = target - spin;
        timespec ts;
        ts.tv_sec  = wake / 1'000'000'000LL;
        ts.tv_nsec = wake % 1'000'000'000LL;
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr);
        if (adapt) {
            int64_t os = now_ns() - wake;   // signal interrupt → negative → skip
            if (os > 0) {
                int64_t est = g_oversleep_est.load(std::memory_order_relaxed);
                g_oversleep_est.store(std::max(os, est - est / 256),
                                      std::memory_order_relaxed);
            }
        }
    }
    while (now_ns() < target) FLM_CPU_PAUSE();
}

// ============================================================================
// DISPATCH
// ============================================================================
struct InstanceDispatch {
    PFN_vkGetInstanceProcAddr                     GetInstanceProcAddr      = nullptr;
    PFN_vkDestroyInstance                         DestroyInstance          = nullptr;
    PFN_vkGetPhysicalDeviceFeatures2              GetPhysicalDeviceFeatures2 = nullptr; // [item 2]
};

struct DeviceDispatch {
    PFN_vkGetDeviceProcAddr           GetDeviceProcAddr           = nullptr;
    PFN_vkDestroyDevice               DestroyDevice               = nullptr;
    PFN_vkQueuePresentKHR             QueuePresentKHR             = nullptr;
    PFN_vkAcquireNextImageKHR         AcquireNextImageKHR         = nullptr;
    PFN_vkAcquireNextImage2KHR        AcquireNextImage2KHR        = nullptr;  // [FIX-22]
    PFN_vkWaitForPresentKHR           WaitForPresentKHR           = nullptr;
    PFN_vkCreateSwapchainKHR          CreateSwapchainKHR          = nullptr;
    PFN_vkDestroySwapchainKHR         DestroySwapchainKHR         = nullptr;
    PFN_vkGetDeviceQueue              GetDeviceQueue              = nullptr;
    PFN_vkGetDeviceQueue2             GetDeviceQueue2             = nullptr;
    bool                              has_present_wait            = false;
};

// ============================================================================
// SWAPCHAIN STATE
// ============================================================================
struct SwapchainState {
    VkDevice        device    = VK_NULL_HANDLE;
    VkSwapchainKHR  swapchain = VK_NULL_HANDLE;
    DeviceDispatch* disp      = nullptr;

    // [item 11] Context
    VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    uint32_t         width  = 0;
    uint32_t         height = 0;
    bool             pace_allowed = false;   // decided at create time (FIFO/small → false)

    std::jthread    measure_thread;

    // Hot-path atomics — separate cache lines (false sharing guard)
    alignas(64) std::atomic<uint64_t> next_present_id{1};
    alignas(64) std::atomic<int64_t>  slot_interval_ns{FlmConst::DEFAULT_INTERVAL_NS};
                std::atomic<int64_t>  last_flip_ns{0};   // [FIX-43] timestamp of last successful flip
                                                         // (same line: both written by measurement
                                                         // thread, read by present thread)
    alignas(64) std::atomic<int64_t>  last_gate_wait_ns{0};     // [FIX-17] detection freeze
    alignas(64) std::atomic<uint32_t> present_seq{0};           // [item 4]
    alignas(64) std::atomic<int>      eff_mfg{1};               // [item 7] effective multiplier
    alignas(64) std::atomic<int>      frame_count{0};
    alignas(64) std::atomic<bool>     hitch_active{false};
    alignas(64) std::atomic<int>      hitch_recovery_frames{0};
    alignas(64) std::atomic<bool>     pacing_enabled{true};     // [item 8] GPU-bound guard

    // [FIX-28] LIMITER timeline — written ONLY by the QueuePresent thread,
    // every frame. Must live on its own cache line; otherwise it ping-pongs
    // with the measurement thread's per-frame writes (below).
    // [FIX-36] Same cache line holds present-side floor-pacing state (only
    // the present thread touches it, lock-free). last_present_ns anchors the
    // present rhythm without measurement lag; floor base is read from
    // slot_interval_ns (published by the measurement thread).
    alignas(64) int64_t limiter_next_ns   = 0;
    int64_t             last_present_ns    = 0;   // [FIX-36] previous present timestamp
    int                 ratio_auto         = 0;   // [FIX-44] learned ratio delta [-150,150]
    int                 held_run           = 0;   // [FIX-44] consecutive held presents

    // [FIX-28][FIX-38] Only the measurement thread touches these → lock-free;
    // starts on a separate cache line from the present-thread fields. (FIX-36
    // had mistakenly placed real_win on the present line; the measurement
    // thread writes these every frame, so FIX-28's fix regressed — moved here.)
    // [FIX-37] Cycle ring: last CYC_RING raw intervals. Sum of the last m
    // entries ≈ T (real period) regardless of pacing mode.
    alignas(64) int64_t cyc_win[FlmConst::CYC_RING] = {};
    int     cyc_idx     = 0;
    int     cyc_count   = 0;
    // [FIX-36/37] T (real-frame period) estimation window — median is the
    // floor-pacing base. Now fed by cycle sums every flip.
    int64_t real_win[FlmConst::REAL_WINDOW] = {};
    int     real_idx    = 0;
    int     real_count  = 0;
    // [FIX-16] Slot window: sliding mean over ALL intervals (correctly centres
    // MFG's bimodal ε/T pattern, unlike per-sample EMA which is phase-dependent).
    // 4x hitch-poisoning clamp.
    int64_t slot_win[FlmConst::SLOT_WINDOW] = {};
    int     slot_idx    = 0;
    int     slot_count  = 0;
    int64_t slot_sum    = 0;
    int64_t slot_mean_ns = FlmConst::DEFAULT_INTERVAL_NS;
    // [item 8] GPU-bound window
    int     over_target_run  = 0;
    int     under_target_run = 0;
    // [item 7] MFG detection
    int     mfg_small_cnt = 0;
    int     mfg_total_cnt = 0;
    // [item 12] stats
    int64_t stat_last_ns    = 0;
    int64_t stat_sum_ns     = 0;
    int64_t stat_max_ns     = 0;
    int     stat_frames     = 0;
    int     stat_fake_hitch = 0;   // [FIX-27] fake + hitch combined (old name stat_fake was misleading)
    // [item 12] CSV — [FIX-31] telemetry columns added
    FILE*   csv_fp = nullptr;
    struct CsvRow {
        int64_t  flip_ns, interval_ns;
        int      is_fake, is_hitch;
        uint32_t slot;
        int      mfg;            // effective MFG multiplier
        int64_t  slot_mean_ns;   // published slot mean
        int      pacing;         // GPU-bound guard state
    };
    CsvRow  csv_buf[FlmConst::CSV_BUFFER];
    int     csv_n = 0;

    SwapchainState(VkDevice dev, VkSwapchainKHR sc, DeviceDispatch* d)
        : device(dev), swapchain(sc), disp(d) {}

    ~SwapchainState() {
        if (csv_n && csv_fp) csv_flush();
        if (csv_fp) fclose(csv_fp);
    }

    int64_t get_hitch_threshold(int64_t avg_ns) const {
        int64_t adaptive = std::max<int64_t>((avg_ns * 3) / 2, avg_ns + 2'000'000LL);
        return std::min<int64_t>(adaptive, avg_ns + 30'000'000LL);
    }

    // [FIX-37] Real-frame period (T) median — from cycle-sum estimates.
    // Replaces display_intervals median: identical semantics at m=1 (cycle
    // sum = raw interval), and at m>1 it is fake-filter-free and phase-insensitive.
    int64_t real_period_median() const {
        int n = std::min(real_count, FlmConst::REAL_WINDOW);
        if (n == 0) return FlmConst::DEFAULT_INTERVAL_NS;
        int64_t tmp[FlmConst::REAL_WINDOW];
        std::copy(real_win, real_win + n, tmp);
        std::sort(tmp, tmp + n);
        return tmp[n / 2];
    }

    // [FIX-30] No fflush here: 1 MB _IOFBF buffer set after fopen; fprintf
    // calls are pure in-memory formatting. Actual write() only when the stdio
    // buffer fills (≈20k+ rows) — measurement thread timing is protected.
    void csv_flush() {
        if (!csv_fp) return;
        for (int i = 0; i < csv_n; i++) {
            fprintf(csv_fp, "%lld,%lld,%d,%d,%u,%d,%lld,%d\n",
                    (long long)csv_buf[i].flip_ns, (long long)csv_buf[i].interval_ns,
                    csv_buf[i].is_fake, csv_buf[i].is_hitch, csv_buf[i].slot,
                    csv_buf[i].mfg, (long long)csv_buf[i].slot_mean_ns,
                    csv_buf[i].pacing);
        }
        csv_n = 0;
    }
    void csv_push(int64_t flip, int64_t interval, bool fake, bool hitch, uint32_t slot,
                  int mfg, int64_t slot_mean, bool pacing) {
        if (!csv_fp) return;
        csv_buf[csv_n++] = {flip, interval, fake ? 1 : 0, hitch ? 1 : 0, slot,
                            mfg, slot_mean, pacing ? 1 : 0};
        if (csv_n >= FlmConst::CSV_BUFFER) csv_flush();
    }
};

// ============================================================================
// GLOBAL MAPS
// ============================================================================
static std::shared_mutex g_inst_lock;
static std::unordered_map<VkInstance, InstanceDispatch> g_inst_map;
// [item 2] dispatch_key(gpu/instance) → locate InstanceDispatch
static std::unordered_map<void*, VkInstance>            g_instkey_map;

static std::shared_mutex g_dev_lock;
static std::unordered_map<VkDevice, DeviceDispatch> g_dev_map;

struct QueueData {
    VkDevice        device = VK_NULL_HANDLE;
    DeviceDispatch* disp   = nullptr;
};
static std::shared_mutex g_queue_lock;
static std::unordered_map<VkQueue, QueueData> g_queue_map;

static std::shared_mutex g_sc_lock;
static std::unordered_map<VkSwapchainKHR, std::shared_ptr<SwapchainState>> g_sc_map;

static inline void* dispatch_key(void* handle) { return *(void**)handle; }

// [FIX-33] Reserve once at init to avoid rehash on first inserts.
static void reserve_global_maps() {
    { std::unique_lock lk(g_inst_lock);  g_inst_map.reserve(4);  g_instkey_map.reserve(4); }
    { std::unique_lock lk(g_dev_lock);   g_dev_map.reserve(4); }
    { std::unique_lock lk(g_queue_lock); g_queue_map.reserve(16); }
    { std::unique_lock lk(g_sc_lock);    g_sc_map.reserve(8); }
}

static DeviceDispatch* find_device_dispatch(VkDevice device) {
    std::shared_lock lk(g_dev_lock);
    auto it = g_dev_map.find(device);
    return (it != g_dev_map.end()) ? &it->second : nullptr;
}

static std::shared_ptr<SwapchainState> find_sc_state(VkSwapchainKHR sc) {
    std::shared_lock lk(g_sc_lock);
    auto it = g_sc_map.find(sc);
    return (it != g_sc_map.end()) ? it->second : nullptr;  // [FIX-1] copy
}

static void stop_and_join(std::shared_ptr<SwapchainState>& st) {
    if (st && st->measure_thread.joinable()) {
        st->measure_thread.request_stop();
        st->measure_thread.join();
    }
}

// ============================================================================
// MEASUREMENT THREAD
// ----------------------------------------------------------------------------
// PURPOSE: measure real flip intervals, publish the natural cadence
// (fake-filtered), flag GPU-bound / hitch state. NO GATING HERE — the gate
// runs in the present thread against a local timeline (passing absolute targets
// across threads was the source of v1's stutter; removed).
// ============================================================================
static void apply_thread_policies() {
    if (g_config.rt_priority > 0) {
        sched_param sp{};
        sp.sched_priority = g_config.rt_priority;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0)
            FLM_LOG(LogLevel::WARN, "SCHED_FIFO failed (CAP_SYS_NICE?)");
    }
    // [item 13] measurement thread affinity: "0-3" or "5"
    if (!g_config.measure_cpu.empty()) {
        cpu_set_t set; CPU_ZERO(&set);
        const std::string& s = g_config.measure_cpu;
        size_t dash = s.find('-');
        bool ok = false;
        try {
            if (dash != std::string::npos) {
                int a = std::stoi(s.substr(0, dash));
                int b = std::stoi(s.substr(dash + 1));
                if (a >= 0 && b >= a && b < CPU_SETSIZE) {
                    for (int c = a; c <= b; c++) CPU_SET(c, &set);
                    ok = true;
                }
            } else {
                int c = std::stoi(s);
                if (c >= 0 && c < CPU_SETSIZE) { CPU_SET(c, &set); ok = true; }
            }
        } catch (...) { ok = false; }
        if (ok) {
            if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0)
                FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU affinity failed");
        } else {
            FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU parse error: %s", s.c_str());
        }
    }
    pthread_setname_np(pthread_self(), "flm-measure");
}

static void measurement_thread_fn(std::stop_token stoken, std::shared_ptr<SwapchainState> st) {
    apply_thread_policies();

    // [item 12] Open CSV
    if (!g_config.csv_path.empty()) {
        st->csv_fp = fopen(g_config.csv_path.c_str(), "w");
        if (st->csv_fp) {
            // [FIX-30] Large stdio buffer: csv_flush never touches disk.
            setvbuf(st->csv_fp, nullptr, _IOFBF, FlmConst::CSV_STDIO_BUF);
            fprintf(st->csv_fp,
                    "flip_ns,interval_ns,is_fake,is_hitch,slot,mfg,slot_mean_ns,pacing\n");
        }
    }

    uint64_t wait_id         = st->next_present_id.load(std::memory_order_relaxed);
    if (wait_id == 0) wait_id = 1;
    int64_t  last_display_ns = 0;
    bool     last_valid      = false;
    st->stat_last_ns = now_ns();

    while (!stoken.stop_requested()) {
        maybe_reload();   // [FIX-21] SIGUSR1 → applied here (AS-safe)
#ifdef FLM_PGO_INSTRUMENTED
        flm_gcov_periodic_dump();   // [FIX-34] don't rely on atexit, flush manually
#endif
        if (!st->disp || !st->disp->has_present_wait) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        // [FIX-5] If we've fallen behind, fast-forward (old id returns
        // immediately → ~0 interval)
        uint64_t latest = st->next_present_id.load(std::memory_order_relaxed);
        if (latest > 2 && wait_id + 2 < latest) {
            wait_id    = latest - 1;
            last_valid = false;
        }

        VkResult r = st->disp->WaitForPresentKHR(st->device, st->swapchain,
                                                 wait_id, FlmConst::WAIT_TIMEOUT_NS);
        if (r == VK_TIMEOUT) { last_valid = false; continue; }
        // [item 9] resize/alt-tab: swapchain is alive, thread MUST NOT die.
        if (r == VK_ERROR_OUT_OF_DATE_KHR || r == VK_SUBOPTIMAL_KHR ||
            r == VK_ERROR_SURFACE_LOST_KHR) {
            last_valid = false;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }
        if (r != VK_SUCCESS) {
            FLM_LOG(LogLevel::DEBUG, "WaitForPresentKHR fatal: %d", (int)r);
            break;  // only DEVICE_LOST / unknown
        }

        int64_t tnow = now_ns();
        st->last_flip_ns.store(tnow, std::memory_order_relaxed);   // [FIX-43]

        if (last_valid) {
            int64_t interval_ns = tnow - last_display_ns;

            // Effective multiplier
            int m = st->eff_mfg.load(std::memory_order_relaxed);

            // [FIX-37] Previous T estimate (cycle-sum median). Fake split and
            // hitch threshold are now tied to this — not to the stale
            // accept-median that froze under pacing.
            const int64_t T_prev = st->real_period_median();
            // [FIX-40] No hitch/fake classification until the window is warm
            // (4 estimates): at ~30 FPS with T_prev still at 16.6ms every
            // first frame counted as a hitch and the window never warmed up.
            const bool warm = st->real_count >= 4;

            // [FIX-37] HITCH FIRST, from the raw interval. Hitch intervals are
            // long and cannot fall into the fake (short) class → [FIX-19]
            // protection still applies.
            bool is_hitch = warm &&
                            interval_ns > st->get_hitch_threshold(T_prev);
            if (is_hitch) {
                st->hitch_active.store(true, std::memory_order_relaxed);
                st->hitch_recovery_frames.store(FlmConst::HITCH_RECOVERY,
                                                std::memory_order_relaxed);
                // Cycle sums covering the hitch would poison T → reset the ring.
                st->cyc_count = 0;
                st->cyc_idx   = 0;
            } else {
                if (st->hitch_active.load(std::memory_order_relaxed)) {
                    if (st->hitch_recovery_frames.fetch_sub(1, std::memory_order_relaxed) <= 1)
                        st->hitch_active.store(false, std::memory_order_relaxed);
                }

                // [FIX-37] T estimation via CYCLE SUM. Push raw interval into
                // the ring; sum of the last m entries ≈ T regardless of how
                // presents are distributed:
                //   unpaced bimodal : ε + (T-ε)            = T
                //   floor-paced     : floor + (T-floor)     = T
                //   uniform paced   : m * (T/m)             = T
                // Estimate is BLIND to pacing's own effect — the freeze/brake
                // lock of the v2.2 non-fake feed is structurally eliminated.
                st->cyc_win[st->cyc_idx] = interval_ns;
                st->cyc_idx = (st->cyc_idx + 1) % FlmConst::CYC_RING;
                if (st->cyc_count < FlmConst::CYC_RING) st->cyc_count++;

                const int mm = std::clamp(m, 1, FlmConst::CYC_RING);
                if (st->cyc_count >= mm) {
                    int64_t T_est = 0;
                    for (int k = 0; k < mm; k++)
                        T_est += st->cyc_win[(st->cyc_idx - 1 - k +
                                              FlmConst::CYC_RING) % FlmConst::CYC_RING];
                    // After warmup, clamp single-sample estimate to 2x/0.25x
                    // (anomaly/clock protection outside of hitches; still
                    // catches FPS jumps within ≈2-3 flips).
                    if (warm)
                        T_est = std::clamp(T_est, T_prev / 4, T_prev * 2);
                    st->real_win[st->real_idx] = T_est;
                    st->real_idx = (st->real_idx + 1) % FlmConst::REAL_WINDOW;
                    if (st->real_count < FlmConst::REAL_WINDOW) st->real_count++;
                }
            }

            // [FIX-37] Fake classification — now ONLY for stats/CSV (pacing
            // estimate no longer depends on the fake filter). Split derived
            // from live T estimate.
            bool is_fake = false;
            if (m > 1 && warm && !is_hitch) {
                int64_t split_ns = (T_prev * (m + 1)) / (2LL * m);
                is_fake = (interval_ns < split_ns);
            }

            // [FIX-16] SLOT MEAN — sliding window over ALL intervals.
            // m presents take one real-frame duration (T) total → average
            // interval = T/m; correct slot width for both paced (uniform T/m)
            // and unpaced (ε,...,T-Σε) modes. Window mean correctly centres
            // the bimodal pattern (unlike EMA, which is phase-order-dependent).
            // 4x clamp against hitch poisoning.
            {
                int64_t safe_iv = std::clamp<int64_t>(interval_ns, 100'000LL,
                                                      st->slot_mean_ns * 4);
                st->slot_sum += safe_iv - st->slot_win[st->slot_idx];
                st->slot_win[st->slot_idx] = safe_iv;
                st->slot_idx = (st->slot_idx + 1) % FlmConst::SLOT_WINDOW;
                if (st->slot_count < FlmConst::SLOT_WINDOW) st->slot_count++;
                st->slot_mean_ns = st->slot_sum / st->slot_count;
            }

            // [FIX-17] MFG detection: threshold relative to slot-EMA (ema ≈ T/m).
            //   m=1: interval ≈ ema      → never drops below 0.7*ema → p≈0 → m=1
            //   m>1: fake ≈ ε << ema, real ≈ m*ema                  → p≈(m-1)/m
            // If the gate has recently waited (paced uniform intervals poison
            // detection), detection is FROZEN — prevents oscillation caused by
            // m shrinking and slot suddenly growing.
            if (g_config.mfg_mult_env > 0) {
                if (m != g_config.mfg_mult_env)
                    st->eff_mfg.store(g_config.mfg_mult_env, std::memory_order_relaxed);
            } else {
                bool gate_hot = (tnow - st->last_gate_wait_ns.load(std::memory_order_relaxed))
                                < 1'000'000'000LL;
                if (gate_hot && m > 1) {
                    st->mfg_small_cnt = 0;   // frozen window: start clean
                    st->mfg_total_cnt = 0;
                } else {
                    if (interval_ns * 10 < st->slot_mean_ns * 7) st->mfg_small_cnt++;
                    st->mfg_total_cnt++;
                    if (st->mfg_total_cnt >= FlmConst::MFG_DETECT_WINDOW) {
                        double p = (double)st->mfg_small_cnt / (double)st->mfg_total_cnt;
                        int mhat = (p < 0.99) ? (int)std::lround(1.0 / (1.0 - p)) : 4;
                        mhat = std::clamp(mhat, 1, 4);
                        if (mhat != m)
                            FLM_LOG(LogLevel::INFO, "MFG multiplier: %d -> %d", m, mhat);
                        st->eff_mfg.store(mhat, std::memory_order_relaxed);
                        st->mfg_small_cnt = 0;
                        st->mfg_total_cnt = 0;
                    }
                }
            }

            // [FIX-16/36/37] Slot interval to publish:
            //   fps>0        → fixed target (limiter/cap path, unchanged)
            //   floor_pacing → median(T)/m — T is the cycle-sum estimate: does
            //                  NOT freeze under pacing, tracks FPS changes at
            //                  flip speed; if a brake forms, measurement sees
            //                  the braked interval → floor shortens → brake
            //                  releases (negative feedback; v2.2 was positive).
            //   classic pacer → slot_mean (old behaviour, fallback)
            int fps = g_config.target_fps.load(std::memory_order_relaxed);
            int64_t slot_iv;
            if (fps > 0) {
                slot_iv = 1'000'000'000LL / fps;
            } else if (g_config.floor_pacing.load(std::memory_order_relaxed)) {
                int mm2 = std::max(1, m);
                slot_iv = std::max<int64_t>(st->real_period_median() / mm2,
                                            FlmConst::MIN_FLOOR_NS);
            } else {
                slot_iv = st->slot_mean_ns;
            }
            st->slot_interval_ns.store(slot_iv, std::memory_order_relaxed);

            // [FIX-18] GPU-bound guard: only when an explicit target (fps>0) is
            // set, and uses slot-EMA rather than raw intervals. At fps=0 the
            // target is derived from measurements anyway → guard is meaningless
            // (in v2 MFG's bimodal raw intervals immediately triggered it and
            // killed pacing).
            if (fps > 0) {
                if (st->slot_mean_ns > (slot_iv * 105) / 100) {
                    st->over_target_run++; st->under_target_run = 0;
                } else if (st->slot_mean_ns <= (slot_iv * 102) / 100) {
                    st->under_target_run++; st->over_target_run = 0;
                }
                if (st->over_target_run >= FlmConst::GPU_BOUND_WINDOW) {
                    if (st->pacing_enabled.exchange(false, std::memory_order_relaxed))
                        FLM_LOG(LogLevel::DEBUG, "GPU-bound: pacing OFF");
                    st->over_target_run = FlmConst::GPU_BOUND_WINDOW;
                } else if (st->under_target_run >= FlmConst::GPU_BOUND_WINDOW) {
                    if (!st->pacing_enabled.exchange(true, std::memory_order_relaxed))
                        FLM_LOG(LogLevel::DEBUG, "GPU-bound: pacing ON");
                    st->under_target_run = FlmConst::GPU_BOUND_WINDOW;
                }
            } else {
                st->over_target_run = st->under_target_run = 0;
                if (!st->pacing_enabled.load(std::memory_order_relaxed))
                    st->pacing_enabled.store(true, std::memory_order_relaxed);
            }

            // [item 12] stats + CSV
            if (!is_fake) {
                st->stat_sum_ns   += interval_ns;
                st->stat_max_ns    = std::max(st->stat_max_ns, interval_ns);
                st->stat_frames++;
                if (is_hitch) st->stat_fake_hitch++;
            } else {
                st->stat_fake_hitch++;
            }
            st->csv_push(tnow, interval_ns, is_fake, is_hitch,
                         st->present_seq.load(std::memory_order_relaxed),
                         m, st->slot_mean_ns,
                         st->pacing_enabled.load(std::memory_order_relaxed)); // [FIX-31]

            // [FIX-32] Interval configurable via FLM_STATS_INTERVAL (seconds).
            int64_t stats_iv = g_config.stats_interval_ns.load(std::memory_order_relaxed);
            if (g_config.stats && tnow - st->stat_last_ns >= stats_iv &&
                st->stat_frames > 0) {
                double avg_ms = ((double)st->stat_sum_ns / (double)st->stat_frames) / 1e6;
                double max_ms = (double)st->stat_max_ns / 1e6;
                FLM_LOG(LogLevel::INFO,
                    "STATS %llds: n=%d avg=%.2fms max=%.2fms fake/hitch=%d mfg=%d pacing=%d",
                    (long long)(stats_iv / 1'000'000'000LL), st->stat_frames,
                    avg_ms, max_ms, st->stat_fake_hitch,
                    st->eff_mfg.load(), (int)st->pacing_enabled.load());
                st->stat_sum_ns = st->stat_max_ns = 0;
                st->stat_frames = st->stat_fake_hitch = 0;
                st->stat_last_ns = tnow;
            }
        }

        last_display_ns = tnow;
        last_valid      = true;
        wait_id++;
    }

    if (st->csv_n) st->csv_flush();
    FLM_LOG(LogLevel::DEBUG, "Measurement thread stopped");
}

// ============================================================================
// LAYER HOOKS
// ============================================================================
extern "C" {

// Forward
VK_LAYER_EXPORT PFN_vkVoidFunction VKAPI_CALL FLM_vkGetInstanceProcAddr(VkInstance, const char*);
VK_LAYER_EXPORT PFN_vkVoidFunction VKAPI_CALL FLM_vkGetDeviceProcAddr(VkDevice, const char*);

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkCreateInstance(
    const VkInstanceCreateInfo* pCreateInfo,
    const VkAllocationCallbacks* pAllocator, VkInstance* pInstance)
{
    init_config();

    auto* chain = (VkLayerInstanceCreateInfo*)pCreateInfo->pNext;
    while (chain && !(chain->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO &&
                      chain->function == VK_LAYER_LINK_INFO))
        chain = (VkLayerInstanceCreateInfo*)chain->pNext;
    if (!chain) return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;

    auto fn = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    if (!fn) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult res = fn(pCreateInfo, pAllocator, pInstance);
    if (res != VK_SUCCESS) return res;

    InstanceDispatch d{};
    d.GetInstanceProcAddr        = (PFN_vkGetInstanceProcAddr)gipa(*pInstance, "vkGetInstanceProcAddr");
    d.DestroyInstance            = (PFN_vkDestroyInstance)gipa(*pInstance, "vkDestroyInstance");
    // [item 2] core 1.1 function; fall back to KHR variant if absent.
    d.GetPhysicalDeviceFeatures2 = (PFN_vkGetPhysicalDeviceFeatures2)gipa(*pInstance, "vkGetPhysicalDeviceFeatures2");
    if (!d.GetPhysicalDeviceFeatures2)
        d.GetPhysicalDeviceFeatures2 = (PFN_vkGetPhysicalDeviceFeatures2)gipa(*pInstance, "vkGetPhysicalDeviceFeatures2KHR");

    std::unique_lock lk(g_inst_lock);
    g_inst_map[*pInstance]                       = d;
    g_instkey_map[dispatch_key((void*)*pInstance)] = *pInstance;  // [item 2]
    return VK_SUCCESS;
}

VK_LAYER_EXPORT void VKAPI_CALL FLM_vkDestroyInstance(
    VkInstance instance, const VkAllocationCallbacks* pAllocator)
{
    InstanceDispatch d{};
    {
        std::unique_lock lk(g_inst_lock);
        auto it = g_inst_map.find(instance);
        if (it != g_inst_map.end()) { d = it->second; g_inst_map.erase(it); }
        g_instkey_map.erase(dispatch_key((void*)instance));
    }
    if (d.DestroyInstance) d.DestroyInstance(instance, pAllocator);
}

// [item 2] Does the driver actually support presentId + presentWait features?
static bool query_present_features(VkPhysicalDevice gpu) {
    InstanceDispatch inst{};
    {
        std::shared_lock lk(g_inst_lock);
        auto kit = g_instkey_map.find(dispatch_key((void*)gpu));
        if (kit != g_instkey_map.end()) {
            auto it = g_inst_map.find(kit->second);
            if (it != g_inst_map.end()) inst = it->second;
        }
    }
    if (!inst.GetPhysicalDeviceFeatures2) return false; // can't query → safe side

    VkPhysicalDevicePresentIdFeaturesKHR   id_f{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR, nullptr, VK_FALSE};
    VkPhysicalDevicePresentWaitFeaturesKHR wait_f{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR, &id_f, VK_FALSE};
    VkPhysicalDeviceFeatures2 f2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, &wait_f, {}};
    inst.GetPhysicalDeviceFeatures2(gpu, &f2);
    return id_f.presentId && wait_f.presentWait;
}

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkCreateDevice(
    VkPhysicalDevice gpu, const VkDeviceCreateInfo* pCreateInfo,
    const VkAllocationCallbacks* pAllocator, VkDevice* pDevice)
{
    auto* chain = (VkLayerDeviceCreateInfo*)pCreateInfo->pNext;
    while (chain && !(chain->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO &&
                      chain->function == VK_LAYER_LINK_INFO))
        chain = (VkLayerDeviceCreateInfo*)chain->pNext;
    if (!chain) return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr   gdpa = chain->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;

    // [FIX-13] SAVE the chain position for retry (loader's shared mutable
    // struct; sub-layers also advance it — without restoring, a 2nd call
    // crashes).
    VkLayerDeviceLink* next_link = chain->u.pLayerInfo;

    // App's extension list
    std::vector<const char*> exts(pCreateInfo->ppEnabledExtensionNames,
                                  pCreateInfo->ppEnabledExtensionNames +
                                  pCreateInfo->enabledExtensionCount);
    bool app_has_id = false, app_has_wait = false;
    for (auto& e : exts) {
        if (!strcmp(e, VK_KHR_PRESENT_ID_EXTENSION_NAME))   app_has_id   = true;
        if (!strcmp(e, VK_KHR_PRESENT_WAIT_EXTENSION_NAME)) app_has_wait = true;
    }

    // [item 2] Inject presentWait only when the driver genuinely supports it.
    bool want_inject = query_present_features(gpu);
    if (!want_inject)
        FLM_LOG(LogLevel::INFO, "presentId/Wait not supported; PACER disabled (LIMITER still available)");

    bool injected = false;
    if (want_inject) {
        if (!app_has_id)   exts.push_back(VK_KHR_PRESENT_ID_EXTENSION_NAME);
        if (!app_has_wait) exts.push_back(VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
        injected = true;
    }

    // [FIX-14] Don't re-inject feature structs already present in pNext.
    bool chain_id_feat = false, chain_wait_feat = false;
    for (const VkBaseInStructure* p = (const VkBaseInStructure*)pCreateInfo->pNext;
         p; p = p->pNext) {
        if (p->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR)   chain_id_feat   = true;
        if (p->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR) chain_wait_feat = true;
    }

    VkPhysicalDevicePresentIdFeaturesKHR   id_feat{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR, nullptr, VK_TRUE};
    VkPhysicalDevicePresentWaitFeaturesKHR wait_feat{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR, nullptr, VK_TRUE};

    VkDeviceCreateInfo ci = *pCreateInfo;
    ci.ppEnabledExtensionNames = exts.data();
    ci.enabledExtensionCount   = (uint32_t)exts.size();
    const void* tail = ci.pNext;
    if (want_inject && !chain_wait_feat) { wait_feat.pNext = (void*)tail; tail = &wait_feat; }
    if (want_inject && !chain_id_feat)   { id_feat.pNext   = (void*)tail; tail = &id_feat; }
    ci.pNext = tail;

    auto fn = (PFN_vkCreateDevice)gipa(VK_NULL_HANDLE, "vkCreateDevice");
    if (!fn) return VK_ERROR_INITIALIZATION_FAILED;

    VkResult res = fn(gpu, &ci, pAllocator, pDevice);
    bool created_with_wait = injected && (res == VK_SUCCESS);
    if (res != VK_SUCCESS && injected) {
        FLM_LOG(LogLevel::WARN, "CreateDevice with presentWait failed (%d), falling back", (int)res);
        chain->u.pLayerInfo = next_link;               // [FIX-13] restore
        res = fn(gpu, pCreateInfo, pAllocator, pDevice);
        created_with_wait = false;
    }
    if (res != VK_SUCCESS) return res;

    DeviceDispatch d{};
    d.GetDeviceProcAddr   = (PFN_vkGetDeviceProcAddr)gdpa(*pDevice, "vkGetDeviceProcAddr");
    d.DestroyDevice       = (PFN_vkDestroyDevice)gdpa(*pDevice, "vkDestroyDevice");
    d.QueuePresentKHR     = (PFN_vkQueuePresentKHR)gdpa(*pDevice, "vkQueuePresentKHR");
    d.AcquireNextImageKHR  = (PFN_vkAcquireNextImageKHR)gdpa(*pDevice, "vkAcquireNextImageKHR");
    d.AcquireNextImage2KHR = (PFN_vkAcquireNextImage2KHR)gdpa(*pDevice, "vkAcquireNextImage2KHR");
    d.WaitForPresentKHR   = (PFN_vkWaitForPresentKHR)gdpa(*pDevice, "vkWaitForPresentKHR");
    d.CreateSwapchainKHR  = (PFN_vkCreateSwapchainKHR)gdpa(*pDevice, "vkCreateSwapchainKHR");
    d.DestroySwapchainKHR = (PFN_vkDestroySwapchainKHR)gdpa(*pDevice, "vkDestroySwapchainKHR");
    d.GetDeviceQueue      = (PFN_vkGetDeviceQueue)gdpa(*pDevice, "vkGetDeviceQueue");
    d.GetDeviceQueue2     = (PFN_vkGetDeviceQueue2)gdpa(*pDevice, "vkGetDeviceQueue2");

    // [item 1] Only use presentWait when it was safely enabled.
    // On the fallback path the extension was NOT enabled; WaitForPresentKHR
    // may be non-null but calling it is UB. created_with_wait guarantees this;
    // also safe if the app itself enabled both.
    bool safe_wait = created_with_wait || (app_has_id && app_has_wait);
    d.has_present_wait = (d.WaitForPresentKHR != nullptr) && safe_wait;

    std::unique_lock lk(g_dev_lock);
    g_dev_map[*pDevice] = d;
    return VK_SUCCESS;
}

VK_LAYER_EXPORT void VKAPI_CALL FLM_vkDestroyDevice(
    VkDevice device, const VkAllocationCallbacks* pAllocator)
{
    // [FIX-2] Stop+join all swapchain states belonging to this device.
    std::vector<std::shared_ptr<SwapchainState>> orphans;
    {
        std::unique_lock lk(g_sc_lock);
        for (auto it = g_sc_map.begin(); it != g_sc_map.end();) {
            if (it->second->device == device) {
                orphans.push_back(std::move(it->second));
                it = g_sc_map.erase(it);
            } else ++it;
        }
    }
    for (auto& st : orphans) stop_and_join(st);

    {
        std::unique_lock qlk(g_queue_lock);
        std::erase_if(g_queue_map, [device](const auto& kv) {
            return kv.second.device == device;
        });
    }

    DeviceDispatch d{};
    {
        std::unique_lock lk(g_dev_lock);
        auto it = g_dev_map.find(device);
        if (it != g_dev_map.end()) { d = it->second; g_dev_map.erase(it); }
    }
    if (d.DestroyDevice) d.DestroyDevice(device, pAllocator);
}

// [FIX-9] Populate queue map at create time.
VK_LAYER_EXPORT void VKAPI_CALL FLM_vkGetDeviceQueue(
    VkDevice device, uint32_t qf, uint32_t qi, VkQueue* pQueue)
{
    DeviceDispatch* disp = find_device_dispatch(device);
    if (!disp || !disp->GetDeviceQueue) { if (pQueue) *pQueue = VK_NULL_HANDLE; return; }
    disp->GetDeviceQueue(device, qf, qi, pQueue);
    if (pQueue && *pQueue != VK_NULL_HANDLE) {
        std::unique_lock lk(g_queue_lock);
        g_queue_map[*pQueue] = QueueData{device, disp};
    }
}

VK_LAYER_EXPORT void VKAPI_CALL FLM_vkGetDeviceQueue2(
    VkDevice device, const VkDeviceQueueInfo2* pInfo, VkQueue* pQueue)
{
    DeviceDispatch* disp = find_device_dispatch(device);
    if (!disp || !disp->GetDeviceQueue2) { if (pQueue) *pQueue = VK_NULL_HANDLE; return; }
    disp->GetDeviceQueue2(device, pInfo, pQueue);
    if (pQueue && *pQueue != VK_NULL_HANDLE) {
        std::unique_lock lk(g_queue_lock);
        g_queue_map[*pQueue] = QueueData{device, disp};
    }
}

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkCreateSwapchainKHR(
    VkDevice device, const VkSwapchainCreateInfoKHR* pCreateInfo,
    const VkAllocationCallbacks* pAllocator, VkSwapchainKHR* pSwapchain)
{
    DeviceDispatch* disp = find_device_dispatch(device);
    if (!disp || !disp->CreateSwapchainKHR) return VK_ERROR_INITIALIZATION_FAILED;

    VkResult res = disp->CreateSwapchainKHR(device, pCreateInfo, pAllocator, pSwapchain);
    if (res != VK_SUCCESS) return res;

    auto st = std::make_shared<SwapchainState>(device, *pSwapchain, disp);
    st->present_mode = pCreateInfo->presentMode;
    st->width        = pCreateInfo->imageExtent.width;
    st->height       = pCreateInfo->imageExtent.height;
    st->next_present_id.store(1, std::memory_order_relaxed);

    // [item 11] Pacing decision:
    //  - Small auxiliary swapchain (launcher/overlay) → never pace.
    //  - FIFO/FIFO_RELAXED already locked to vsync → present pacing is
    //    unnecessary (if ACQUIRE pace point is selected, acquire gate is still
    //    allowed — checked on the QueuePresent side).
    //  - MAILBOX/IMMEDIATE (VRR scenario) → present pacing allowed.
    bool too_small = (st->width  < (uint32_t)FlmConst::MIN_SC_WIDTH ||
                      st->height < (uint32_t)FlmConst::MIN_SC_HEIGHT);
    st->pace_allowed = !too_small;

    if (too_small) {
        FLM_LOG(LogLevel::DEBUG, "Small swapchain %ux%u — pacing skipped",
                st->width, st->height);
    }

    // Measurement thread only when presentWait is available and swapchain is paceable.
    if (disp->has_present_wait && st->pace_allowed)
        st->measure_thread = std::jthread(measurement_thread_fn, st);

    std::shared_ptr<SwapchainState> stale;
    {
        std::unique_lock lk(g_sc_lock);
        auto it = g_sc_map.find(*pSwapchain);
        if (it != g_sc_map.end()) stale = std::move(it->second);
        g_sc_map[*pSwapchain] = std::move(st);
    }
    stop_and_join(stale);
    return res;
}

VK_LAYER_EXPORT void VKAPI_CALL FLM_vkDestroySwapchainKHR(
    VkDevice device, VkSwapchainKHR swapchain, const VkAllocationCallbacks* pAllocator)
{
    DeviceDispatch* disp = find_device_dispatch(device);

    if (swapchain != VK_NULL_HANDLE) {
        std::shared_ptr<SwapchainState> st;
        {
            std::unique_lock lk(g_sc_lock);
            auto it = g_sc_map.find(swapchain);
            if (it != g_sc_map.end()) { st = std::move(it->second); g_sc_map.erase(it); }
        }
        stop_and_join(st);
    }
    if (disp && disp->DestroySwapchainKHR)
        disp->DestroySwapchainKHR(device, swapchain, pAllocator);
}

// ============================================================================
// GATE — the single gate running in the present thread (limiter + pacer unified)
// ----------------------------------------------------------------------------
// Uses a local timeline (st->limiter_next_ns). CRITICAL PROPERTY: if the gate
// target is in the past it DOES NOT WAIT — pacing can only DELAY, never
// accelerate. This means it cannot force the game to "miss" a frame; at worst
// it does nothing. This is the fundamental no-stutter guarantee.
// ============================================================================
// [FIX-35] advance: advance timeline + apply slew. In BOTH mode two calls
// happen per frame; only ONE (present) should advance, otherwise 2*iv is
// imposed per frame → in limiter mode fps/2; in pacer mode (iv=measurement)
// a positive feedback loop: frame=2*iv → measurement=2*iv → iv doubles;
// the 50ms WaitForPresent timeout + hitch interruptions lock the loop at
// ≈15-17 FPS.
static void apply_gate(SwapchainState* st, bool limiter_mode, bool advance) {
    // Hitch or recovery: don't pace, reset timeline (clean re-anchor).
    if (st->hitch_active.load(std::memory_order_relaxed) ||
        st->hitch_recovery_frames.load(std::memory_order_relaxed) > 0) {
        st->limiter_next_ns = 0;
        st->last_present_ns = 0;   // [FIX-45] let floor re-anchor cleanly too
        st->held_run        = 0;   // [FIX-44] hitch run is not a brake sign
        return;
    }
    // [item 8] GPU-bound → don't pace.
    if (!st->pacing_enabled.load(std::memory_order_relaxed)) {
        st->limiter_next_ns = 0;
        st->last_present_ns = 0;   // [FIX-45]
        return;
    }

    const int fps = g_config.target_fps.load(std::memory_order_relaxed);

    // ========================================================================
    // [FIX-43] MEASUREMENT FRESHNESS GUARD — only for measurement-dependent
    // paths (pacer + floor). LIMITER doesn't need measurement; leave it alone.
    // If measurement never produces samples (game sends id=0 → continuous
    // TIMEOUT) or is stale (alt-tab, OUT_OF_DATE loop), slot_interval_ns sits
    // at the default/old T and the gate brakes the game to that value (16.6ms
    // default → 240Hz game gets capped at ~70 FPS). Without fresh data, no
    // pacing; anchors are reset so measurement restart gives a clean start.
    // ========================================================================
    if (!limiter_mode) {
        int64_t lf = st->last_flip_ns.load(std::memory_order_relaxed);
        if (lf == 0 || now_ns() - lf > FlmConst::MEAS_FRESH_NS) {
            st->limiter_next_ns = 0;
            st->last_present_ns = 0;
            st->held_run        = 0;
            return;
        }
    }

    // ========================================================================
    // [FIX-36] FLOOR PACING — primary path for VRR + MFG.
    // ------------------------------------------------------------------------
    // Why NOT absolute grid: on VRR the correct frametime is not constant; FPS
    // swings 150↔220. An absolute grid brakes frames when FPS rises (visible
    // judder). Instead we place a FLOOR relative to the previous present: a
    // present may not exit until at least floor after the one before it. This
    // prevents Ada MFG's ε-interval generated frames from exiting too early
    // (holds them until floor), but DOES NOT touch real frames (which arrive
    // after a long gap). Result: the bimodal ε/T pattern flattens, variable
    // FPS is not braked.
    //
    // floor = slot_iv * floor_ratio.  slot_iv = T/m (from measurement, incl. m).
    // last_present_ns is updated every frame in the present thread → no
    // measurement lag shifting the grid (unlike the 1-frame-stale problem of
    // absolute grids).
    // ========================================================================
    // [FIX-42] Floor ONLY at fps==0 (natural cadence). At fps>0 the classic
    // timeline pacer locks to the target precisely; floor (being relative) was
    // letting the game run up to 117% of the target (ratio=850 → 1/0.85).
    // README already said "active on the fps=0 path"; code now matches.
    if (!limiter_mode && fps == 0 &&
        g_config.floor_pacing.load(std::memory_order_relaxed)) {
        int64_t slot_iv = st->slot_interval_ns.load(std::memory_order_relaxed);
        if (slot_iv <= 0) return;
        int     ratio   = g_config.floor_ratio.load(std::memory_order_relaxed);
        // [FIX-41] Relax ratio as m grows. On Ada (40-series) GPU with no HW
        // flip metering the generated-frame production time has higher variance
        // at m=3/4; a fixed tight ratio can hold real frames inside the floor
        // unnecessarily, causing stalls and hitches. Only active when m>1;
        // no effect at m=1.
        int m_now = st->eff_mfg.load(std::memory_order_relaxed);
        if (g_config.floor_mfg_adapt.load(std::memory_order_relaxed)) {
            if (m_now > 1) {
                int step = g_config.floor_mfg_step.load(std::memory_order_relaxed);
                ratio = std::clamp(ratio - (m_now - 1) * step, 500, 1000);
            }
        }
        // [FIX-44] Learned delta stacks on top of base + adapt; clamp preserved.
        const bool autotune = g_config.floor_autotune.load(std::memory_order_relaxed);
        if (autotune)
            ratio = std::clamp(ratio + st->ratio_auto, 500, 1000);
        int64_t floor   = std::max<int64_t>((slot_iv * ratio) / 1000, FlmConst::MIN_FLOOR_NS);

        int64_t t = now_ns();
        if (st->last_present_ns == 0) {          // first present: anchor only
            st->last_present_ns = t;
            return;
        }

        int64_t since = t - st->last_present_ns; // time since previous present

        // [FIX-44] CLOSED LOOP (advance=true only, i.e. the real present gate;
        // the acquire leg of BOTH must not interfere with measurement). This
        // frame's observation adjusts the NEXT frame's ratio:
        //   * since <  floor  → present will be held. In normal MFG at most m-1
        //     presents per cycle are held; consecutive >= max(2,m) held presents
        //     means the REAL frame is also being braked → loosen quickly (-4).
        //   * since >= floor  → headroom = since - floor.
        //       headroom > slot/12 → intervals still uneven (alternation) →
        //                            tighten slowly (+1).
        //       headroom < slot/50 → floor is grazing real frames → loosen (-2).
        // Net effect: the structural 0.425T/0.575T alternation at ratio=850
        // self-corrects toward ≈0.5T/0.5T as long as no brake sign appears;
        // if FPS drops and real frames start arriving early, delta is pulled
        // back immediately.
        if (autotune && advance) {
            if (since < floor) {
                if (++st->held_run >= std::max(2, m_now)) {
                    st->ratio_auto -= 4;
                    st->held_run = 0;
                }
            } else {
                st->held_run = 0;
                int64_t head = since - floor;
                if      (head > slot_iv / 12) st->ratio_auto += 1;
                else if (head < slot_iv / 50) st->ratio_auto -= 2;
            }
            st->ratio_auto = std::clamp(st->ratio_auto, -150, 150);
        }

        // advance=false (acquire leg of BOTH): only prevent exiting too early;
        // last_present_ns is updated by the present branch (no double advance).
        int64_t target = st->last_present_ns + floor;

        // Is this present inside the floor? (too-early generated frame) → hold.
        // Past the floor? (real frame / late frame) → no wait, pass through.
        // [FIX-45] Old "left < floor*2" cap was dead code: since>=0 means
        // left = floor - since <= floor, cap was unreachable. Removed.
        if (since < floor) {
            int64_t left = target - t;
            if (left > 0) {
                st->last_gate_wait_ns.store(t, std::memory_order_relaxed);  // [FIX-17]
                precise_wait_absolute(target);
                t = now_ns();
            }
        }
        if (advance) st->last_present_ns = t;    // anchor present rhythm
        return;
    }

    int64_t iv, lead;
    if (limiter_mode) {
        if (fps <= 0) return;
        iv   = 1'000'000'000LL / fps;
        lead = 0;  // limiter locks to the target exactly
    } else {
        // [item 3] PACER: uniform interval matching natural cadence, lead before flip.
        iv = st->slot_interval_ns.load(std::memory_order_relaxed);
        if (iv <= 0) return;
        // [FIX-24] If lead >= iv the target falls into the past and the gate
        // silently becomes a no-op (e.g. high FPS + default 1ms lead, or
        // FLM_PRESENT_LEAD_NS=8ms at 240Hz). Clamp to half the interval.
        lead = std::min(g_config.lead_ns.load(std::memory_order_relaxed), iv / 2);
    }

    int64_t t = now_ns();
    if (st->limiter_next_ns == 0) {
        st->limiter_next_ns = t + iv;   // first frame: don't wait, set up timeline
        return;
    }
    // [FIX-35] Advance + slew only in the ONE call per frame (advance=true).
    // The second gate call (acquire leg of BOTH, advance=false) only checks
    // "not too early" against the current target — no-op if target is past.
    if (advance) {
        st->limiter_next_ns += iv;      // [item 4] uniform slot advance

        // [item 10] Soft slew: hard-rebase only on extreme drift; small debt
        // is closed over ≈8 frames (no visible phase jump).
        int64_t drift = st->limiter_next_ns - t;
        int64_t tol   = g_config.drift_tol.load(std::memory_order_relaxed);
        if (tol <= 0) tol = std::clamp<int64_t>(iv / 4, 1'000'000LL, 4'000'000LL);
        if (drift < -2 * iv || drift > 4 * iv) {
            st->limiter_next_ns = t + iv;   // stall / clock jump
        } else if (drift < -tol) {
            st->limiter_next_ns -= drift / 8;  // drift<0 → pull target forward
        }
    }

    int64_t target = st->limiter_next_ns - lead;
    int64_t left   = target - t;
    // [FIX-20] Cap is interval-relative: fixed 20ms cap made the limiter a
    // complete no-op at FPS<=50 (iv>=20ms).
    int64_t max_wait = std::max<int64_t>(FlmConst::MAX_PACE_WAIT_NS, iv + iv / 2);
    if (left > 0 && left < max_wait) {
        st->last_gate_wait_ns.store(t, std::memory_order_relaxed);  // [FIX-17]
        precise_wait_absolute(target);
    }
}

// Resolve the active mode. limiter_mode output: whether gate uses limiter logic.
// Return: whether to pace at all.
static bool resolve_gate(const SwapchainState* st, bool has_wait, bool& limiter_mode) {
    if (!st->pace_allowed) return false;
    PaceMode mode = (PaceMode)g_config.mode.load(std::memory_order_relaxed);
    int      fps  = g_config.target_fps.load(std::memory_order_relaxed);

    // [item 11] FIFO/FIFO_RELAXED already locked to vsync → PACER (uniform
    // cadence estimate) is unnecessary and fights the compositor. LIMITER
    // (cap to a lower FPS) is still valid and useful on these modes.
    bool is_fifo = (st->present_mode == VK_PRESENT_MODE_FIFO_KHR ||
                    st->present_mode == VK_PRESENT_MODE_FIFO_RELAXED_KHR);

    switch (mode) {
        case PaceMode::OFF:     return false;
        case PaceMode::LIMITER: limiter_mode = true;  return fps > 0;
        case PaceMode::PRESENT:
            if (has_wait && !is_fifo) { limiter_mode = false; return true; }
            limiter_mode = true; return fps > 0;   // FIFO or no wait → limiter
        case PaceMode::AUTO:
        default:
            if (has_wait && !is_fifo) { limiter_mode = false; return true; }
            limiter_mode = true; return fps > 0;
    }
}

// [FIX-22] Shared acquire gate — called from both AcquireNextImageKHR paths.
static inline void acquire_gate(VkSwapchainKHR swapchain, bool has_wait)
{
    // [item 6] Apply gate here only if pace_point is ACQUIRE or BOTH.
    PacePoint pp = (PacePoint)g_config.pace_point.load(std::memory_order_relaxed);
    if (pp == PacePoint::ACQUIRE || pp == PacePoint::BOTH) {
        if (auto st = find_sc_state(swapchain)) {   // [FIX-1] shared_ptr copy
            if (st->frame_count.load(std::memory_order_relaxed) >= FlmConst::WARMUP_FRAMES) {
                bool limiter_mode = false;
                if (resolve_gate(st.get(), has_wait, limiter_mode))
                    // [FIX-35] BOTH: timeline is advanced by the present leg;
                    // acquire only checks "not too early" against current target.
                    apply_gate(st.get(), limiter_mode,
                               /*advance=*/pp == PacePoint::ACQUIRE);
            }
            st->frame_count.fetch_add(1, std::memory_order_relaxed);
        }
    } else {
        // Pacing at present; still advance frame_count for warmup.
        if (auto st = find_sc_state(swapchain))
            st->frame_count.fetch_add(1, std::memory_order_relaxed);
    }
}

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkAcquireNextImageKHR(
    VkDevice device, VkSwapchainKHR swapchain, uint64_t timeout,
    VkSemaphore semaphore, VkFence fence, uint32_t* pImageIndex)
{
    DeviceDispatch* disp = find_device_dispatch(device);
    if (!disp || !disp->AcquireNextImageKHR) return VK_ERROR_DEVICE_LOST;

    acquire_gate(swapchain, disp->has_present_wait);
    return disp->AcquireNextImageKHR(device, swapchain, timeout, semaphore, fence, pImageIndex);
}

// [FIX-22] Engines using this path never advanced the warmup counter →
// gate never opened (limiter+pacer silently no-op).
VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkAcquireNextImage2KHR(
    VkDevice device, const VkAcquireNextImageInfoKHR* pAcquireInfo, uint32_t* pImageIndex)
{
    DeviceDispatch* disp = find_device_dispatch(device);
    if (!disp || !disp->AcquireNextImage2KHR) return VK_ERROR_DEVICE_LOST;

    if (pAcquireInfo)
        acquire_gate(pAcquireInfo->swapchain, disp->has_present_wait);
    return disp->AcquireNextImage2KHR(device, pAcquireInfo, pImageIndex);
}

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkQueuePresentKHR(
    VkQueue queue, const VkPresentInfoKHR* pPresentInfo)
{
    maybe_reload();   // [FIX-21] also runs when there's no measurement thread (pure limiter)
#ifdef FLM_PGO_INSTRUMENTED
    flm_gcov_periodic_dump();   // [FIX-34] also try here if no measurement thread
#endif

    QueueData qdata{};
    {
        std::shared_lock qlk(g_queue_lock);
        auto qit = g_queue_map.find(queue);
        if (qit != g_queue_map.end()) qdata = qit->second;
    }
    if (!qdata.disp) {   // fallback: resolve by dispatch-key and cache
        {
            std::shared_lock lk(g_dev_lock);
            void* key = dispatch_key((void*)queue);
            for (auto& [d, dd] : g_dev_map)
                if (dispatch_key((void*)d) == key) { qdata.disp = &dd; qdata.device = d; break; }
        }
        if (qdata.disp) { std::unique_lock qlk(g_queue_lock); g_queue_map[queue] = qdata; }
    }
    if (!qdata.disp) return VK_ERROR_DEVICE_LOST;

    const uint32_t sc_count = pPresentInfo->swapchainCount;
    const bool has_wait = qdata.disp->has_present_wait;

    // [FIX-15] Does the app have its own VkPresentIdKHR?
    const VkPresentIdKHR* app_pid = nullptr;
    for (const VkBaseInStructure* p = (const VkBaseInStructure*)pPresentInfo->pNext; p; p = p->pNext)
        if (p->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) { app_pid = (const VkPresentIdKHR*)p; break; }
    const bool app_has_present_id = (app_pid != nullptr);

    // [FIX-4] Stack arrays (≤8 swapchains → no heap)
    uint64_t ids_stack[FlmConst::STACK_PRESENT_IDS];
    std::vector<uint64_t> ids_heap;
    uint64_t* present_ids = ids_stack;
    if (sc_count > FlmConst::STACK_PRESENT_IDS) { ids_heap.resize(sc_count, 0); present_ids = ids_heap.data(); }
    else std::fill(ids_stack, ids_stack + sc_count, 0ULL);

    // [item 6] Present gate only if pace_point is PRESENT or BOTH.
    PacePoint pp = (PacePoint)g_config.pace_point.load(std::memory_order_relaxed);
    bool gate_here = (pp == PacePoint::PRESENT || pp == PacePoint::BOTH);

    bool any_id = false;
    for (uint32_t i = 0; i < sc_count; i++) {
        auto st = find_sc_state(pPresentInfo->pSwapchains[i]);
        if (!st) continue;

        if (app_has_present_id) {
            // [FIX-15] Track app's id: next_present_id = app_id + 1.
            if (app_pid->pPresentIds && i < app_pid->swapchainCount) {
                uint64_t id = app_pid->pPresentIds[i];
                if (id) st->next_present_id.store(id + 1, std::memory_order_relaxed);
            }
        }

        // SINGLE GATE (only on the first/primary swapchain; multiple swapchains are rare)
        if (gate_here && i == 0 &&
            st->frame_count.load(std::memory_order_relaxed) >= FlmConst::WARMUP_FRAMES) {
            st->present_seq.fetch_add(1, std::memory_order_relaxed);  // [item 4]
            bool limiter_mode = false;
            if (resolve_gate(st.get(), has_wait, limiter_mode))
                apply_gate(st.get(), limiter_mode, /*advance=*/true);  // [FIX-35]
        }

        // Inject id for presentWait if the app doesn't supply one.
        if (has_wait && !app_has_present_id) {
            present_ids[i] = st->next_present_id.fetch_add(1, std::memory_order_relaxed);
            any_id = true;
        }
    }

    VkPresentIdKHR   present_id_info{};
    VkPresentInfoKHR modified = *pPresentInfo;
    if (any_id && !app_has_present_id && has_wait) {
        present_id_info.sType          = VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
        present_id_info.swapchainCount = sc_count;
        present_id_info.pPresentIds    = present_ids;
        present_id_info.pNext          = pPresentInfo->pNext;
        modified.pNext                 = &present_id_info;
    }

    return qdata.disp->QueuePresentKHR(queue, &modified);
}

// ============================================================================
// PROC ADDR
// ============================================================================
#define INTERCEPT(fn) if (strcmp(pName, "vk" #fn) == 0) return (PFN_vkVoidFunction)FLM_vk##fn

VK_LAYER_EXPORT PFN_vkVoidFunction VKAPI_CALL FLM_vkGetDeviceProcAddr(VkDevice device, const char* pName)
{
    INTERCEPT(GetDeviceProcAddr);
    INTERCEPT(DestroyDevice);
    INTERCEPT(QueuePresentKHR);
    INTERCEPT(AcquireNextImageKHR);
    INTERCEPT(AcquireNextImage2KHR);   // [FIX-22]
    INTERCEPT(CreateSwapchainKHR);
    INTERCEPT(DestroySwapchainKHR);
    INTERCEPT(GetDeviceQueue);
    INTERCEPT(GetDeviceQueue2);

    std::shared_lock lk(g_dev_lock);
    auto it = g_dev_map.find(device);
    if (it == g_dev_map.end() || !it->second.GetDeviceProcAddr) return nullptr;
    return it->second.GetDeviceProcAddr(device, pName);
}

VK_LAYER_EXPORT PFN_vkVoidFunction VKAPI_CALL FLM_vkGetInstanceProcAddr(VkInstance instance, const char* pName)
{
    INTERCEPT(GetInstanceProcAddr);
    INTERCEPT(CreateInstance);
    INTERCEPT(DestroyInstance);
    INTERCEPT(CreateDevice);
    INTERCEPT(GetDeviceProcAddr);
    // [FIX-11] Device-level functions requested via GIPA must also go through the layer.
    INTERCEPT(DestroyDevice);
    INTERCEPT(QueuePresentKHR);
    INTERCEPT(AcquireNextImageKHR);
    INTERCEPT(AcquireNextImage2KHR);   // [FIX-22]
    INTERCEPT(CreateSwapchainKHR);
    INTERCEPT(DestroySwapchainKHR);
    INTERCEPT(GetDeviceQueue);
    INTERCEPT(GetDeviceQueue2);

    if (instance == VK_NULL_HANDLE) return nullptr;
    std::shared_lock lk(g_inst_lock);
    auto it = g_inst_map.find(instance);
    if (it == g_inst_map.end() || !it->second.GetInstanceProcAddr) return nullptr;
    return it->second.GetInstanceProcAddr(instance, pName);
}

#undef INTERCEPT

// ============================================================================
// [item 14] LOADER INTERFACE v2 NEGOTIATION
// ============================================================================
VK_LAYER_EXPORT VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(
    VkNegotiateLayerInterface* pVersionStruct)
{
    if (!pVersionStruct ||
        pVersionStruct->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT)
        return VK_ERROR_INITIALIZATION_FAILED;

    if (pVersionStruct->loaderLayerInterfaceVersion > CURRENT_LOADER_LAYER_INTERFACE_VERSION)
        pVersionStruct->loaderLayerInterfaceVersion = CURRENT_LOADER_LAYER_INTERFACE_VERSION;

    pVersionStruct->pfnGetInstanceProcAddr       = FLM_vkGetInstanceProcAddr;
    pVersionStruct->pfnGetDeviceProcAddr         = FLM_vkGetDeviceProcAddr;
    pVersionStruct->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}

} // extern "C"

// ============================================================================
// README — ENVIRONMENT VARIABLES
// ----------------------------------------------------------------------------
//  FLM_MODE=auto|present|limiter|off   (default: auto)
//     auto     : PACER if presentWait available, else LIMITER if fps is set
//     present  : force PACER (falls back to limiter if no presentWait)
//     limiter  : pure FPS limiter (no presentWait needed, requires FLM_TARGET_FPS)
//     off      : do nothing (A/B test baseline)
//  FLM_TARGET_FPS=<n>          limiter/pacer target fps (0 = natural cadence)
//  FLM_PACE_POINT=present|acquire|both  (default: present) — SINGLE gate point
//  FLM_FLOOR_PACING=1          [FIX-36] VRR+MFG floor-pacing (default 1/on).
//                              Active on the fps=0 (natural cadence) + pacer
//                              path. Relative floor instead of absolute grid:
//                              present exits at least floor after the previous.
//                              Equalises Ada (40-series, no HW flip metering)
//                              MFG's ε-interval generated frames, does not brake
//                              real frames, does not distort variable FPS.
//                              0 = revert to old absolute-grid pacer.
//  FLM_FLOOR_RATIO=850         [FIX-36] floor = (T/m) * ratio/1000  (500-1000).
//                              Main hand-tuning knob. Low (700) = loose, jitter
//                              passes but less correction. High (950) = tight,
//                              flatter but risks hitch if late.
//                              LIVE: write to FLM_CONFIG file + kill -USR1 <pid>.
//  FLM_FLOOR_MFG_ADAPT=1       [FIX-41] relax FLOOR_RATIO as m (MFG multiplier)
//                              grows (only when m>1). On Ada (40-series) GPU at
//                              ceiling with m=3/4, generated-frame production
//                              variance grows; a fixed tight ratio may hold real
//                              frames unnecessarily, raising hitch% and cov%.
//                              0 = old behaviour (ratio fixed for all m).
//  FLM_FLOOR_MFG_STEP=40       [FIX-41] amount subtracted from ratio per (m-1)
//                              increment (0-200 ratio units). E.g. ratio=850,
//                              step=40 → m=2:810, m=3:770, m=4:730. Higher step
//                              = more aggressive relaxation (less braking, slightly
//                              looser ε-equalisation). Live-adjustable.
//  FLM_FLOOR_AUTOTUNE=1        [FIX-44] closed-loop ratio adjustment:
//                              tighten slowly when headroom is ample (flattens
//                              intervals), loosen quickly on consecutive holds /
//                              thin headroom (prevents braking). Delta [-150,+150]
//                              stacks on base ratio and MFG-adapt. 0 = fixed ratio.
//  FLM_PRESENT_LEAD_NS=1000000 how far before the predicted flip to submit present (ns)
//  FLM_SPIN_NS=150000          final N ns of pause-spin (0 = pure sleep, min CPU)
//  FLM_SPIN_ADAPT=1            [FIX-39] auto-adjust spin margin from measured wakeup
//                              latency (30µs–2ms; hot-reloadable).
//                              When 1, FLM_SPIN_NS is only meaningful as on/off;
//                              0 → fixed spin of exactly FLM_SPIN_NS (old behaviour).
//  FLM_DRIFT_TOLERANCE_NS=0    0 = auto (iv/4)
//  FLM_MFG_MULTIPLIER=0        0 = auto-detect, 1-4 = force
//  FLM_RT_PRIORITY=0           measurement thread SCHED_FIFO priority (CAP_SYS_NICE)
//  FLM_MEASURE_CPU=0-3         measurement thread affinity
//  FLM_STATS=1                 periodic summary log (INFO)
//  FLM_STATS_INTERVAL=5        summary period, seconds (1-3600; hot-reload) [FIX-32]
//  FLM_CSV=/tmp/flm.csv        per-frame measurement dump — columns [FIX-31]:
//                              flip_ns,interval_ns,is_fake,is_hitch,slot,
//                              mfg,slot_mean_ns,pacing
//  FLM_CONFIG=/tmp/flm.conf    live config file (KEY=VALUE, '#' comments)
//  FLM_LOG_LEVEL=DEBUG|INFO|WARN|ERROR
//  FLM_LOG_FILE=/path          log file (default: stderr)
//  SIGUSR1                     re-read FLM_CONFIG file (env is static;
//                              live changes only via the file)
//
//  QUICK VERIFICATION:
//    FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud <game>
//      → flat 60 FPS line in MangoHud = layer is active.
//    A/B:  FLM_MODE=off FLM_CSV=/tmp/off.csv   vs
//          FLM_MODE=present FLM_CSV=/tmp/on.csv
//      → on.csv should show lower interval stddev and p99, higher 1% low.
// ============================================================================
