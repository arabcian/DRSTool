// ============================================================================
// FLM — Vulkan Flip Meter / Frame Pacing Layer  (v2.6 — "observability")
//
// [STYLE-03/STYLE-05] Design summary + anti-stutter rules → ALGORITHM.md.
// Fix-by-fix history (FIX-1..FIX-61, v1 through v2.6) → CHANGELOG.md. Per-fix
// rationale that still matters for reading a specific block of code below
// stays as an inline comment right next to that code (e.g. FIX-51's cache-
// line grouping note is above the fields it explains) — this file is the
// implementation; ALGORITHM.md is "how it works", CHANGELOG.md is "what
// changed and why".
// ============================================================================
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#ifndef VK_LAYER_EXPORT
#  define VK_LAYER_EXPORT __attribute__((visibility("default")))
#endif

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <cerrno>
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

// [PERF-14] The logic used to live entirely inside the FLM_LOG macro body.
// A real function is debugger-friendly (breakpoint/step into it, which you
// can't meaningfully do inside a macro expansion) and — with the printf
// format attribute — gets compile-time type checking of the format string
// against its arguments, which a macro can't provide either. FLM_LOG stays a
// thin macro so the ~50 call sites below don't need to change; the side
// effects (the level check, the actual write) now live in one ordinary
// function instead of being re-expanded at every call site.
#if defined(__GNUC__) || defined(__clang__)
static inline void flm_log_impl(LogLevel level, const char* fmt, ...) __attribute__((format(printf, 2, 3)));
#endif
static inline void flm_log_impl(LogLevel level, const char* fmt, ...) {
    if ((int)level < g_log_level.load(std::memory_order_relaxed)) return;
    fprintf(g_log_file, "[FLM] ");
    va_list args;
    va_start(args, fmt);
    vfprintf(g_log_file, fmt, args);
    va_end(args);
    fputc('\n', g_log_file);
    // [FIX-29] fflush only at INFO+ — DEBUG spam stays buffered (stderr is
    // already unbuffered; this only matters for FLM_LOG_FILE).
    if ((int)level >= (int)LogLevel::INFO) fflush(g_log_file);
}
#define FLM_LOG(level, ...) flm_log_impl(level, __VA_ARGS__)

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
    constexpr int      MFG_DETECT_WINDOW   = 32;   // [item 7][FIX-48] 64→32: halves the
                                                   // mixed-sample transient real_win sees
                                                   // during an m transition; at 32 samples
                                                   // p=(m-1)/m is still well-resolved
    constexpr int64_t  PROBE_PERIOD_NS     = 10'000'000'000LL; // [FIX-47] re-sample every 10s
    constexpr int      PROBE_FLIPS         = 24;               // [FIX-47] probe length (flips)
    constexpr int      MIN_SC_WIDTH        = 640;  // [item 11]
    constexpr int      MIN_SC_HEIGHT       = 480;
    constexpr int      CSV_BUFFER          = 256;  // [item 12]
    constexpr int64_t  STATS_INTERVAL_NS   = 5'000'000'000LL;  // [FIX-32]
    constexpr int      STAT_RING           = 4096; // [FIX-58] interval samples kept per
                                                   // stats window for percentiles; at
                                                   // >819 FPS a 5s window overflows and
                                                   // p99 covers the first 4096 samples —
                                                   // acceptable for a health readout
    constexpr int64_t  MEAS_FRESH_NS       = 250'000'000LL;    // [FIX-43] measurement freshness window
    constexpr size_t   CSV_STDIO_BUF       = 1u << 20;         // [FIX-30]
    constexpr size_t   LOG_STDIO_BUF       = 64u << 10;        // [FIX-29]

    // [ROBUST-04] Named thresholds for values that were inline literals in
    // the algorithm below — same numbers, same behaviour, just findable in
    // one place instead of buried in a comparison.
    // [FIX-17-class] MFG "fake" (generated) frame test: interval below 0.7x
    // the slot mean is classified as a generated/interpolated frame.
    constexpr int64_t  MFG_FAKE_RATIO_NUM  = 7;
    constexpr int64_t  MFG_FAKE_RATIO_DEN  = 10;
    constexpr int      MFG_PROBE_MIN_SAMPLES = 16;   // below this, a probe/detect window is too thin to trust
    // [item 8] GPU-bound guard hysteresis band: >105% of target = over
    // (game can't keep up, back off pacing); <=102% = under (safely pacing).
    // The 3-point gap between them is the hysteresis — avoids flapping
    // right at 100%.
    constexpr int64_t  GPU_BOUND_OVER_PCT  = 105;
    constexpr int64_t  GPU_BOUND_UNDER_PCT = 102;
    // [FIX-44] Floor-ratio autotune headroom bands: tighten (small step) when
    // headroom exceeds 1/12 of the slot interval, loosen (bigger step, see
    // FIX-56) when it drops below 1/50 — asymmetric on purpose, loosen reacts
    // faster than tighten.
    constexpr int64_t  AUTOTUNE_TIGHTEN_HEADROOM_DIV = 12;
    constexpr int64_t  AUTOTUNE_LOOSEN_HEADROOM_DIV  = 50;
    // Hitch threshold formula (get_hitch_threshold): 1.5x average, floored at
    // average+2ms, capped at average+30ms — keeps the threshold sane at both
    // very low and very high frame rates.
    constexpr int64_t  HITCH_MULT_NUM      = 3;
    constexpr int64_t  HITCH_MULT_DEN      = 2;
    constexpr int64_t  HITCH_MIN_ADD_NS    = 2'000'000LL;
    constexpr int64_t  HITCH_MAX_ADD_NS    = 30'000'000LL;
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
    // [THREAD-04] relaxed everywhere: writer is a single reload path
    // (init / SIGUSR1 / config-file poll, never concurrent with itself —
    // maybe_reload() gates on an exchange), readers are present/measurement
    // threads that are DESIGNED to tolerate picking up a live-tuned value
    // up to one reload cycle late (these are user-facing tuning knobs, not
    // synchronization signals gating other memory). No field here is read
    // to decide whether some other piece of state is safe to access.
    std::atomic<int>     target_fps {0};
    std::atomic<int64_t> spin_ns    {FlmConst::DEFAULT_SPIN_NS};
    std::atomic<int64_t> lead_ns    {FlmConst::DEFAULT_LEAD_NS};
    std::atomic<int64_t> drift_tol  {0};
    std::atomic<int>     mode       {(int)PaceMode::AUTO};
    std::atomic<int>     pace_point {(int)PacePoint::PRESENT};
    std::atomic<int64_t> stats_interval_ns {FlmConst::STATS_INTERVAL_NS}; // [FIX-32]

    // [FIX-53] FLM_PACE_FIFO=1: allow PACER on FIFO/FIFO_RELAXED swapchains.
    // Off by default — FIFO is already vsync-locked, so PACER's uniform-
    // cadence estimate normally fights the compositor's own timing (double
    // pacing). Explicit opt-in for cases where that's actually wanted (e.g.
    // an MFG-capable engine that only offers FIFO, or comparing PACER's
    // frametime smoothing against the driver's own FIFO cadence).
    std::atomic<bool>    pace_fifo {false};    // FLM_PACE_FIFO

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
    if (strcmp(s, "auto"))   // [ROBUST-03] anything but a recognized value or "auto" is a typo
        FLM_LOG(LogLevel::WARN, "FLM_MODE: unrecognized value '%s', using auto", s);
    return PaceMode::AUTO;
}
static PacePoint parse_pace_point(const char* s) {
    if (!s) return PacePoint::PRESENT;
    if (!strcmp(s, "acquire")) return PacePoint::ACQUIRE;
    if (!strcmp(s, "both"))    return PacePoint::BOTH;
    if (strcmp(s, "present"))
        FLM_LOG(LogLevel::WARN, "FLM_PACE_POINT: unrecognized value '%s', using present", s);
    return PacePoint::PRESENT;
}

// [API-03][ROBUST-03] atoi/atoll accept garbage silently: atoi("abc") == 0,
// indistinguishable from an explicit "0", and out-of-range values were
// clamped with no way to notice the clamp happened short of comparing
// FLM_STATS-dump output against what you typed. from_chars parses without
// locale/allocation overhead AND reports (a) whether it consumed the WHOLE
// string, so "abc" and "120garbage" are both caught instead of becoming
// 0 / 120, and (b) lets us log when clamping actually changes the value.
// On a bad parse the existing atomic is left untouched (a live-reloadable
// knob shouldn't get silently zeroed by a typo) rather than falling back to 0.
template <typename T>
static bool parse_num(const char* key, const char* val, T& out) {
    const char* end = val + strlen(val);
    auto [ptr, ec] = std::from_chars(val, end, out);
    if (ec != std::errc() || ptr != end) {
        FLM_LOG(LogLevel::WARN, "%s: invalid value '%s' (ignored, keeping previous)", key, val);
        return false;
    }
    return true;
}

template <typename T>
static void store_clamped(std::atomic<T>& field, const char* key, const char* val, T lo, T hi) {
    T v;
    if (!parse_num(key, val, v)) return;
    T c = std::clamp(v, lo, hi);
    if (c != v)
        FLM_LOG(LogLevel::WARN, "%s: %lld out of range [%lld,%lld], clamped to %lld",
                key, (long long)v, (long long)lo, (long long)hi, (long long)c);
    field.store(c);
}

// [API-02] Table-driven KV dispatch. Each entry is {key, setter}; a new
// hot-reloadable key is one table row instead of another strcmp/else-if link
// in a chain that was already ~15 deep. apply_dynamic_kv itself becomes a
// linear scan + one function-pointer call — same cost class as the old
// chain (both are O(#keys) string compares), but the mapping from key name
// to behaviour is data, not control flow, so it's readable and diffable at
// 40-50 keys the way the else-if chain would not have been.
static void set_target_fps(const char* v)        { store_clamped(g_config.target_fps, "FLM_TARGET_FPS", v, 0, 1000); }   // [FIX-33]
static void set_stats_interval(const char* v) {   // [FIX-32] seconds → ns
    int64_t sec;
    if (!parse_num("FLM_STATS_INTERVAL", v, sec)) return;
    int64_t c = std::clamp<int64_t>(sec, 1, 3600);
    if (c != sec)
        FLM_LOG(LogLevel::WARN, "FLM_STATS_INTERVAL: %lld out of range [1,3600]s, clamped to %lld",
                (long long)sec, (long long)c);
    g_config.stats_interval_ns.store(c * 1'000'000'000LL);
}
static void set_spin_ns(const char* v)            { store_clamped<int64_t>(g_config.spin_ns, "FLM_SPIN_NS", v, 0, 2'000'000LL); }
static void set_present_lead_ns(const char* v)    { store_clamped<int64_t>(g_config.lead_ns, "FLM_PRESENT_LEAD_NS", v, 0, 8'000'000LL); }
static void set_drift_tolerance_ns(const char* v) {
    int64_t tol;
    if (!parse_num("FLM_DRIFT_TOLERANCE_NS", v, tol)) return;
    if (tol < 0) { FLM_LOG(LogLevel::WARN, "FLM_DRIFT_TOLERANCE_NS: negative value clamped to 0"); tol = 0; }
    g_config.drift_tol.store(tol);
}
static void set_mode(const char* v)               { g_config.mode.store((int)parse_mode(v)); }
static void set_pace_point(const char* v)         { g_config.pace_point.store((int)parse_pace_point(v)); }
static void set_pace_fifo(const char* v)          { g_config.pace_fifo.store(atoi(v) != 0); }         // [FIX-53]
static void set_floor_pacing(const char* v)       { g_config.floor_pacing.store(atoi(v) != 0); }       // [FIX-36]
static void set_floor_ratio(const char* v)        { store_clamped(g_config.floor_ratio, "FLM_FLOOR_RATIO", v, 500, 1000); }   // [FIX-36]
static void set_floor_mfg_adapt(const char* v)    { g_config.floor_mfg_adapt.store(atoi(v) != 0); }    // [FIX-41]
static void set_floor_mfg_step(const char* v)     { store_clamped(g_config.floor_mfg_step, "FLM_FLOOR_MFG_STEP", v, 0, 200); }   // [FIX-41]
static void set_floor_autotune(const char* v)     { g_config.floor_autotune.store(atoi(v) != 0); }     // [FIX-44]
static void set_spin_adapt(const char* v)         { g_config.spin_adapt.store(atoi(v) != 0); }         // [FIX-39]
static void set_log_level(const char* v) {
    if      (!strcmp(v, "DEBUG")) g_log_level.store((int)LogLevel::DEBUG);
    else if (!strcmp(v, "INFO"))  g_log_level.store((int)LogLevel::INFO);
    else if (!strcmp(v, "WARN"))  g_log_level.store((int)LogLevel::WARN);
    else if (!strcmp(v, "ERROR")) g_log_level.store((int)LogLevel::ERR);
    else FLM_LOG(LogLevel::WARN, "FLM_LOG_LEVEL: unrecognized value '%s' (ignored)", v);   // [ROBUST-03]
}

struct ConfigKey { const char* name; void (*apply)(const char*); };
static constexpr ConfigKey g_config_keys[] = {
    { "FLM_TARGET_FPS",         set_target_fps },
    { "FLM_STATS_INTERVAL",     set_stats_interval },
    { "FLM_SPIN_NS",            set_spin_ns },
    { "FLM_PRESENT_LEAD_NS",    set_present_lead_ns },
    { "FLM_DRIFT_TOLERANCE_NS", set_drift_tolerance_ns },
    { "FLM_MODE",               set_mode },
    { "FLM_PACE_POINT",         set_pace_point },
    { "FLM_PACE_FIFO",          set_pace_fifo },
    { "FLM_FLOOR_PACING",       set_floor_pacing },
    { "FLM_FLOOR_RATIO",        set_floor_ratio },
    { "FLM_FLOOR_MFG_ADAPT",    set_floor_mfg_adapt },
    { "FLM_FLOOR_MFG_STEP",     set_floor_mfg_step },
    { "FLM_FLOOR_AUTOTUNE",     set_floor_autotune },
    { "FLM_SPIN_ADAPT",         set_spin_adapt },
    { "FLM_LOG_LEVEL",          set_log_level },
};

// [FIX-21] Single KV applier — both env and config file go through here.
static void apply_dynamic_kv(const char* key, const char* val) {
    if (!key || !val || !*val) return;
    for (const auto& k : g_config_keys) {
        if (!strcmp(key, k.name)) { k.apply(val); return; }
    }
    // Unknown key: silently ignored, same as before (see FIX-60's note that
    // typo'd keys need the SIGUSR1 state dump to catch).
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
        "FLM_PACE_FIFO",                         // [FIX-53]
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
        // [FIX-60] Log the FULL effective state. The old 4-field line meant
        // that after a floor-ratio / autotune / pace_fifo live retune the only
        // way to confirm the value actually landed (typo'd key names are
        // silently ignored by apply_dynamic_kv) was DEBUG-level spelunking.
        // [PERF-09] Not hot-path (SIGUSR1 is rare), but one load per field
        // into a local reads better than re-touching the same atomic once
        // per printf argument, and guarantees the logged line is a single
        // consistent snapshot rather than N independent reads.
        const int     mode        = g_config.mode.load(std::memory_order_relaxed);
        const int     fps         = g_config.target_fps.load(std::memory_order_relaxed);
        const int64_t spin        = g_config.spin_ns.load(std::memory_order_relaxed);
        const int64_t lead        = g_config.lead_ns.load(std::memory_order_relaxed);
        const bool    floor       = g_config.floor_pacing.load(std::memory_order_relaxed);
        const int     ratio       = g_config.floor_ratio.load(std::memory_order_relaxed);
        const bool    mfg_adapt   = g_config.floor_mfg_adapt.load(std::memory_order_relaxed);
        const int     mfg_step    = g_config.floor_mfg_step.load(std::memory_order_relaxed);
        const bool    autotune    = g_config.floor_autotune.load(std::memory_order_relaxed);
        const bool    spin_adapt  = g_config.spin_adapt.load(std::memory_order_relaxed);
        const bool    pace_fifo   = g_config.pace_fifo.load(std::memory_order_relaxed);
        FLM_LOG(LogLevel::INFO,
                "Config reload: mode=%d fps=%d spin=%lld lead=%lld "
                "floor=%d ratio=%d mfg_adapt=%d step=%d autotune=%d "
                "spin_adapt=%d pace_fifo=%d",
                mode, fps, (long long)spin, (long long)lead,
                (int)floor, ratio, (int)mfg_adapt, mfg_step,
                (int)autotune, (int)spin_adapt, (int)pace_fifo);
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
                "Config: mode=%d fps=%d mfg_env=%d spin=%lldns lead=%lldns rt=%d csv=%s",
                g_config.mode.load(), g_config.target_fps.load(), g_config.mfg_mult_env,
                (long long)g_config.spin_ns.load(), (long long)g_config.lead_ns.load(),
                g_config.rt_priority,
                // [FIX-55] Log the CSV path FLM actually resolved at startup —
                // previously invisible in the config line, so a wrong path
                // (typo, unexpanded var, wrong FLM_CSV name) looked identical
                // to "CSV disabled" in the log. Empty string = not configured.
                g_config.csv_path.empty() ? "(none)" : g_config.csv_path.c_str());
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
// is tracked and used to size the spin margin. The fixed 150µs margin had two
// failure modes:
//   * Loaded system: oversleep > 150µs → gate MISSES its target → late present
//     → the gate itself produces the jitter that floor/limiter tries to fix.
//   * Idle/RT system: oversleep ≈5-30µs → ~120µs of wasted spin every frame
//     (≈3% of core time at 240 FPS goes to heat).
// [FIX-46] The v2.4 damped maximum (est = max(os, est - est/256)) was OUTLIER-
// STICKY: a single 2ms oversleep (page fault, P-state transition, SMI) pinned
// the margin near ~3ms for ≈256 samples — at 240 FPS that is >1s of ~3ms spin
// per frame (≈70% of a core burned as heat, boost-clock backpressure feeding
// straight back into frametime). Estimator is now a 16-sample ring + p75:
// robust to isolated spikes (one outlier can never move p75 of 16), still
// converges to a genuinely loaded system within 4-5 samples. Margin cap
// lowered 2ms → 500µs: beyond that, missing by a little beats burning a core.
namespace FlmSpin {
    constexpr int     RING       = 16;
    constexpr int64_t MARGIN_MIN = 30'000LL;
    constexpr int64_t MARGIN_MAX = 500'000LL;   // [FIX-46] was 2ms
}
static std::atomic<int64_t> g_os_ring[FlmSpin::RING];   // zero-init
static std::atomic<int>     g_os_idx{0};                // one present thread in practice;
static std::atomic<int>     g_os_cnt{0};                // atomics keep the rare multi-
                                                        // present-thread case UB-free
static std::atomic<int64_t> g_spin_margin{100'000};     // cached p75-derived margin (ns)

// Push one oversleep sample and refresh the cached margin. Cost: 16-element
// copy + nth_element — negligible next to the clock_nanosleep it follows.
static void spin_margin_update(int64_t os) {
    int idx = g_os_idx.load(std::memory_order_relaxed);
    g_os_ring[idx].store(os, std::memory_order_relaxed);
    g_os_idx.store((idx + 1) % FlmSpin::RING, std::memory_order_relaxed);
    int cnt = g_os_cnt.load(std::memory_order_relaxed);
    if (cnt < FlmSpin::RING) g_os_cnt.store(++cnt, std::memory_order_relaxed);
    int64_t tmp[FlmSpin::RING];
    const int n = cnt;
    for (int i = 0; i < n; i++) tmp[i] = g_os_ring[i].load(std::memory_order_relaxed);
    const int k = (n * 3) / 4;          // p75
    std::nth_element(tmp, tmp + k, tmp + n);
    int64_t p75 = tmp[k];
    g_spin_margin.store(std::clamp<int64_t>(p75 + p75 / 2 + 20'000,
                                            FlmSpin::MARGIN_MIN, FlmSpin::MARGIN_MAX),
                        std::memory_order_relaxed);
}

static void precise_wait_absolute(int64_t target) {
    if (target <= 0) return;
    const int64_t spin_cfg = g_config.spin_ns.load(std::memory_order_relaxed);
    const bool    adapt    = spin_cfg > 0 &&
                             g_config.spin_adapt.load(std::memory_order_relaxed);
    int64_t spin = spin_cfg;
    if (adapt)
        spin = g_spin_margin.load(std::memory_order_relaxed);   // [FIX-46] one load, p75-derived
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
            if (os > 0) spin_margin_update(os);   // [FIX-46]
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

// [FIX-57] fwd decl — registry lives below, near the measurement thread.
static void csv_release_registered(const std::string& reg_key);

// [PERF-08] std::sort's introsort machinery (partition/heapsort fallback) is
// pure overhead below ~16-ish elements. REAL_WINDOW is 8: insertion sort does
// O(n^2) comparisons/swaps but for n<=8 that is at most 28 compares with no
// branching for pivot selection or recursion — measurably cheaper here, and
// it's already sorted-ish frame-to-frame (one value changes per push), which
// is insertion sort's best case (near O(n)).
static constexpr inline void insertion_sort_i64(int64_t* a, int n) {
    for (int i = 1; i < n; i++) {
        int64_t key = a[i];
        int j = i - 1;
        while (j >= 0 && a[j] > key) { a[j + 1] = a[j]; j--; }
        a[j + 1] = key;
    }
}

// [ARCH-03] Measurement-thread-only state groups, split out of what had
// become a flat, ~30-field SwapchainState. None of this changes behaviour or
// layout intent — same fields, same thread ownership — it just gives each
// group a name instead of only a comment above a block of loose fields.
struct TimelineEstimate {
    // [FIX-37] Cycle ring: last CYC_RING raw intervals. Sum of the last m
    // entries ≈ T (real period) regardless of pacing mode.
    int64_t cyc_win[FlmConst::CYC_RING] = {};
    int     cyc_idx     = 0;
    int     cyc_count   = 0;
    // [FIX-36/37] T (real-frame period) estimation window — median is the
    // floor-pacing base. Fed by cycle sums every flip.
    int64_t real_win[FlmConst::REAL_WINDOW] = {};
    int     real_idx    = 0;
    int     real_count  = 0;
    // [FIX-50] Cached median — recomputed once per push instead of copy+sort
    // twice per flip (T_prev read + slot_iv publish both hit it).
    int64_t median_cache = FlmConst::DEFAULT_INTERVAL_NS;

    // [FIX-37] Replaces the old display_intervals median: identical
    // semantics at m=1 (cycle sum = raw interval), fake-filter-free and
    // phase-insensitive at m>1. [FIX-50] median() is a cache hit; the sort
    // runs once per recompute(), not once per read.
    constexpr int64_t median() const { return median_cache; }

    constexpr void recompute() {
        int n = std::min(real_count, FlmConst::REAL_WINDOW);
        if (n == 0) { median_cache = FlmConst::DEFAULT_INTERVAL_NS; return; }
        int64_t tmp[FlmConst::REAL_WINDOW];
        std::copy(real_win, real_win + n, tmp);
        insertion_sort_i64(tmp, n);   // [PERF-08] n<=8: cheaper than std::sort here
        median_cache = tmp[n / 2];
    }
};

// [FIX-16] Slot window: sliding mean over ALL intervals (correctly centres
// MFG's bimodal ε/T pattern, unlike per-sample EMA which is phase-dependent).
struct SlotWindow {
    int64_t win[FlmConst::SLOT_WINDOW] = {};
    int     idx     = 0;
    int     count   = 0;
    int64_t sum     = 0;
    int64_t mean_ns = FlmConst::DEFAULT_INTERVAL_NS;
};

struct GpuBoundGuard {   // [item 8]
    int over_run  = 0;
    int under_run = 0;
};

struct MfgDetect {   // [item 7]
    int small_cnt = 0;
    int total_cnt = 0;
};

struct ProbeState {   // [FIX-47] MFG re-sample probe bookkeeping
    int64_t last_ns = 0;
    int     left    = 0;
};

// [PERF-11] Split out of SwapchainState: see the comment on SwapchainState's
// csv/stats members for why these are separate, lazily-allocated objects
// instead of inline arrays.
struct CsvState {
    FILE*       fp = nullptr;
    std::string reg_key;   // [FIX-57] registry key to release on destroy
    struct Row {
        int64_t  flip_ns, interval_ns;
        int      is_fake, is_hitch;
        uint32_t slot;
        int      mfg;            // effective MFG multiplier
        int64_t  slot_mean_ns;   // published slot mean
        int      pacing;         // GPU-bound guard state
    };
    Row  buf[FlmConst::CSV_BUFFER];
    int  n = 0;

    ~CsvState() {
        if (n && fp) flush();
        if (fp) fclose(fp);
        csv_release_registered(reg_key);   // [FIX-57]
    }

    // [FIX-30] No fflush here: 1 MB _IOFBF buffer set after fopen. [PERF-07]
    // fprintf still pays for glibc's format-string reparse AND an implicit
    // flockfile/funlockfile pair on EVERY row, even though the underlying
    // write() is already batched by the stdio buffer. Formatting the whole
    // batch into a local buffer with snprintf and handing it to fwrite() in
    // one call collapses that per-row lock/parse cost to once per
    // CSV_BUFFER rows. 128 B/row is a generous bound for this row shape
    // (two int64 fields at 20 digits worst case + separators); measurement
    // thread only, so a ~32 KB stack buffer here is fine.
    void flush() {
        if (!fp) return;
        char out[FlmConst::CSV_BUFFER * 128];
        size_t off = 0;
        for (int i = 0; i < n; i++) {
            int w = snprintf(out + off, sizeof(out) - off,
                    "%lld,%lld,%d,%d,%u,%d,%lld,%d\n",
                    (long long)buf[i].flip_ns, (long long)buf[i].interval_ns,
                    buf[i].is_fake, buf[i].is_hitch, buf[i].slot,
                    buf[i].mfg, (long long)buf[i].slot_mean_ns, buf[i].pacing);
            if (w < 0 || (size_t)w >= sizeof(out) - off) break;   // bound guard, should never trip
            off += (size_t)w;
        }
        if (off) fwrite(out, 1, off, fp);
        n = 0;
    }
    void push(int64_t flip, int64_t interval, bool fake, bool hitch, uint32_t slot,
              int mfg, int64_t slot_mean, bool pacing) {
        if (!fp) return;
        buf[n++] = {flip, interval, fake ? 1 : 0, hitch ? 1 : 0, slot,
                    mfg, slot_mean, pacing ? 1 : 0};
        if (n >= FlmConst::CSV_BUFFER) flush();
    }
};

struct StatsState {
    int64_t last_ns = 0, sum_ns = 0, max_ns = 0;
    int     frames  = 0;
    // [FIX-58] fake and hitch tracked SEPARATELY. FIX-27's combined counter
    // made the one number that matters for MFG tuning — the hitch rate —
    // unreadable from STATS: at m=4 fakes dominate the sum, so a hitch-rate
    // regression (the RE Requiem 5.50% case) was invisible without a full
    // CSV round-trip. Now the STATS line answers it directly.
    int     fake = 0, hitch = 0;
    // [FIX-58] Interval samples for percentiles (ALL presented intervals,
    // fake included — the panel sees the mixed stream, so p99 must too).
    // nth_element runs once per stats window in the measurement thread
    // (32 KB copy per 5s — negligible). Only exists when FLM_STATS=1.
    int64_t ring[FlmConst::STAT_RING];
    int     ring_n = 0;
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

    // [FIX-51] Hot-path atomics grouped BY WRITER THREAD. v2.4 gave every
    // atomic its own alignas(64) line (9 lines ≈ 576B); writer isolation is
    // the actual requirement — fields written by the SAME thread can share a
    // line freely (a reader-only thread never dirties it). Two lines total:
    //
    // [THREAD-04] All loads/stores below are memory_order_relaxed. Why that's
    // enough for every field in both groups, not just "it's the fast one":
    //   * Each field has exactly one writer thread (this is the whole point
    //     of the P/M split above) — no field is ever the target of two
    //     concurrent stores, so there's no read-modify-write race to guard.
    //   * None of these fields gate access to OTHER memory on the reading
    //     side (no "if flag, then dereference pointer set before the flag" —
    //     that pattern is what would need acquire/release). Each is read and
    //     used standalone (a timestamp compared to now(), a counter compared
    //     to a threshold, a bool branched on) — the consumer tolerates
    //     reading last-frame's value and self-corrects next frame regardless
    //     (that tolerance is the basis of the whole pacing design: see the
    //     "no-stutter guarantee" comment above apply_gate).
    //   * Producer publishes monotonically increasing/replacing values
    //     (slot_interval_ns, present_seq, ...); a stale read is a one-frame-
    //     old but still-valid value, never a torn or nonsensical one, because
    //     each individual atomic op is itself indivisible regardless of order.
    // Line P — written by the PRESENT/ACQUIRE thread, read by measurement:
    alignas(64) std::atomic<uint64_t> next_present_id{1};
                std::atomic<int64_t>  last_gate_wait_ns{0};     // [FIX-17] detection freeze
                std::atomic<uint32_t> present_seq{0};           // [item 4]
                std::atomic<int>      frame_count{0};
    //
    // Line M — written by the MEASUREMENT thread, read by present:
    alignas(64) std::atomic<int64_t>  slot_interval_ns{FlmConst::DEFAULT_INTERVAL_NS};
                // relaxed: producer (measurement) publishes a new estimate
                // once per flip and only ever replaces it wholesale; consumer
                // (present/apply_gate) reads it once per gate call and is
                // designed to tolerate a one-frame-stale value (next flip
                // corrects it) — there's no second piece of state that needs
                // to be seen "at least as new as" this one.
                std::atomic<int64_t>  last_flip_ns{0};   // [FIX-43] last successful flip ts
                std::atomic<int>      eff_mfg{1};               // [item 7] effective multiplier
                std::atomic<int>      hitch_recovery_frames{0};
                std::atomic<bool>     hitch_active{false};
                std::atomic<bool>     pacing_enabled{true};     // [item 8] GPU-bound guard
                std::atomic<bool>     probe_active{false};      // [FIX-47] MFG re-sample probe

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

    // [ARCH-03] Grouped measurement-thread-only state. FIX-37/FIX-16/FIX-47
    // originally left these as flat SwapchainState fields; only the
    // measurement thread ever touches any of them (present/acquire never
    // reach in), so grouping changes nothing about the FIX-28/38/51 cross-
    // thread cache-line guarantees on the fields above — it just answers
    // "what is this group for" at the type level instead of only in
    // comments. alignas(64) moves with the first group so the measurement
    // cache line still starts in the same place.
    alignas(64) TimelineEstimate timeline;   // [FIX-37] cycle ring + T-period median
    SlotWindow    slot;         // [FIX-16] sliding mean over ALL intervals
    GpuBoundGuard gpu_guard;    // [item 8]
    MfgDetect     mfg_detect;   // [item 7]
    ProbeState    probe;        // [FIX-47] MFG re-sample probe bookkeeping

    // [PERF-11] Both are cache-cold relative to the per-frame timeline/slot/
    // probe state above: CSV only writes when FLM_CSV is set (normally a
    // short diagnostic capture, not left on), and STAT_RING alone is 32 KB.
    // Keeping them inline meant every swapchain paid that memory whether or
    // not either feature was ever turned on. Heap-allocated, allocated once
    // on first actual use, measurement-thread-only same as before — nullptr
    // costs nothing beyond the pointer itself when the feature is off.
    std::unique_ptr<CsvState>   csv;
    std::unique_ptr<StatsState> stats;

    SwapchainState(VkDevice dev, VkSwapchainKHR sc, DeviceDispatch* d)
        : device(dev), swapchain(sc), disp(d) {}

    // csv/stats clean themselves up via unique_ptr (CsvState's destructor
    // flushes + closes + releases the registry key, same as before).
    ~SwapchainState() = default;

    constexpr int64_t get_hitch_threshold(int64_t avg_ns) const {
        int64_t adaptive = std::max<int64_t>((avg_ns * FlmConst::HITCH_MULT_NUM) / FlmConst::HITCH_MULT_DEN,
                                              avg_ns + FlmConst::HITCH_MIN_ADD_NS);
        return std::min<int64_t>(adaptive, avg_ns + FlmConst::HITCH_MAX_ADD_NS);
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
// [MEM-02] max_load_factor(0.5) forces a bigger bucket array for a given
// element count → shorter average probe chains on find(). These maps are all
// tiny (4-16 elements) and sit on hot paths (dispatch_key, find_sc_state's
// slow path), so a few dozen extra bytes buys a real per-lookup win. Set
// before reserve() so reserve sizes against the lower load factor instead of
// rehashing again on first insert.
static void reserve_global_maps() {
    { std::unique_lock lk(g_inst_lock);
      g_inst_map.max_load_factor(0.5f);    g_instkey_map.max_load_factor(0.5f);
      g_inst_map.reserve(4);               g_instkey_map.reserve(4); }
    { std::unique_lock lk(g_dev_lock);
      g_dev_map.max_load_factor(0.5f);     g_dev_map.reserve(4); }
    { std::unique_lock lk(g_queue_lock);
      g_queue_map.max_load_factor(0.5f);   g_queue_map.reserve(16); }
    { std::unique_lock lk(g_sc_lock);
      g_sc_map.max_load_factor(0.5f);      g_sc_map.reserve(8); }
}

static DeviceDispatch* find_device_dispatch(VkDevice device) {
    std::shared_lock lk(g_dev_lock);
    auto it = g_dev_map.find(device);
    return (it != g_dev_map.end()) ? &it->second : nullptr;
}

// [FIX-50] Hot-path swapchain lookup cache. Games overwhelmingly present one
// swapchain, yet v2.4 paid shared_mutex + hash + shared_ptr copy TWICE per
// frame (acquire + present). thread_local {handle, state} pair short-circuits
// that. Correctness: g_sc_gen bumps on EVERY g_sc_map mutation (create /
// destroy / device destroy); a stale generation forces the slow path, so
// handle reuse after destroy+recreate can never serve the old state.
static std::atomic<uint64_t> g_sc_gen{0};

static std::shared_ptr<SwapchainState> find_sc_state(VkSwapchainKHR sc) {
    thread_local VkSwapchainKHR                 c_sc  = VK_NULL_HANDLE;
    thread_local uint64_t                       c_gen = ~0ULL;
    thread_local std::shared_ptr<SwapchainState> c_st;

    uint64_t gen = g_sc_gen.load(std::memory_order_acquire);
    if (sc == c_sc && gen == c_gen && c_st)
        return c_st;                       // fast path: no lock, no hash

    std::shared_ptr<SwapchainState> st;
    {
        std::shared_lock lk(g_sc_lock);
        auto it = g_sc_map.find(sc);
        if (it != g_sc_map.end()) st = it->second;   // [FIX-1] copy
    }
    c_sc = sc; c_gen = gen; c_st = st;
    return st;
}

static void stop_and_join(std::shared_ptr<SwapchainState>& st) {
    if (st && st->measure_thread.joinable()) {
        st->measure_thread.request_stop();
        st->measure_thread.join();
    }
}

// ============================================================================
// [FIX-57] CSV PATH REGISTRY. Each SwapchainState fopen()'d FLM_CSV with "w":
// every swapchain RECREATION (resolution change, fullscreen toggle, the
// OUT_OF_DATE loop after alt-tab) silently TRUNCATED the CSV mid-session —
// exactly the runs used for A/B hitch analysis lost everything before the
// recreate. The registry remembers which paths this process has already
// written: first open truncates + writes the header, later opens append
// without a header (one continuous file across recreations). If a SECOND
// swapchain opens the same path while the first is still live (rare:
// multi-swapchain engines), it gets "<path>.2" etc. instead — two FILE*
// streams appending to one file would interleave torn rows.
// Function-local static avoids init-order issues; leaked at exit by design
// (games routinely _exit()).
// ============================================================================
struct CsvPathInfo { int active = 0; bool seen = false; };
static std::mutex& csv_registry_lock() { static std::mutex m; return m; }
static std::unordered_map<std::string, CsvPathInfo>& csv_registry() {
    static std::unordered_map<std::string, CsvPathInfo> r; return r;
}

// Returns the FILE* (nullptr on failure) and stores the registry key the
// state must release in its destructor.
static FILE* csv_open_registered(const std::string& base_path, std::string& reg_key_out) {
    std::lock_guard lk(csv_registry_lock());
    CsvPathInfo& info = csv_registry()[base_path];
    std::string path = base_path;
    bool append = false;
    if (info.active > 0) {
        path += "." + std::to_string(info.active + 1);   // concurrent open → own file
    } else if (info.seen) {
        append = true;                                    // recreation → continue file
    }
    FILE* f = fopen(path.c_str(), append ? "a" : "w");
    if (!f) return nullptr;
    setvbuf(f, nullptr, _IOFBF, FlmConst::CSV_STDIO_BUF);   // [FIX-30]
    if (!append)
        fprintf(f, "flip_ns,interval_ns,is_fake,is_hitch,slot,mfg,slot_mean_ns,pacing\n");
    info.active++;
    info.seen = true;
    reg_key_out = base_path;
    if (path != base_path)
        FLM_LOG(LogLevel::WARN, "FLM_CSV '%s' already in use by a live swapchain — "
                "writing to '%s' instead", base_path.c_str(), path.c_str());
    else if (append)
        FLM_LOG(LogLevel::INFO, "FLM_CSV '%s': swapchain recreated — appending "
                "(no header repeat)", base_path.c_str());
    return f;
}

static void csv_release_registered(const std::string& reg_key) {
    if (reg_key.empty()) return;
    std::lock_guard lk(csv_registry_lock());
    auto it = csv_registry().find(reg_key);
    if (it != csv_registry().end() && it->second.active > 0) it->second.active--;
}

// ============================================================================
// MEASUREMENT THREAD
// ----------------------------------------------------------------------------
// PURPOSE: measure real flip intervals, publish the natural cadence
// (fake-filtered), flag GPU-bound / hitch state. NO GATING HERE — the gate
// runs in the present thread against a local timeline (passing absolute targets
// across threads was the source of v1's stutter; removed).
// ============================================================================
// [FIX-59] Parse a cpu list: comma-separated ranges/cores ("0-3", "5",
// "4,5,6", "0-3,8,10-11"). Returns true and fills `set` only if the WHOLE
// spec is valid and selects at least one cpu. Split out of
// apply_thread_policies so the accept/reject behaviour is unit-testable.
static bool parse_cpu_list(const std::string& s, cpu_set_t& set) {
    CPU_ZERO(&set);
    bool any = false;
    size_t pos = 0;
    try {
        while (pos <= s.size()) {
            size_t comma = s.find(',', pos);
            std::string tok = s.substr(pos, (comma == std::string::npos)
                                            ? std::string::npos : comma - pos);
            pos = (comma == std::string::npos) ? s.size() + 1 : comma + 1;
            if (tok.empty()) return false;   // ",," / trailing ","
            size_t dash = tok.find('-');
            if (dash != std::string::npos) {
                int a = std::stoi(tok.substr(0, dash));
                int b = std::stoi(tok.substr(dash + 1));
                if (a < 0 || b < a || b >= CPU_SETSIZE) return false;
                for (int c = a; c <= b; c++) CPU_SET(c, &set);
                any = true;
            } else {
                int c = std::stoi(tok);
                if (c < 0 || c >= CPU_SETSIZE) return false;
                CPU_SET(c, &set);
                any = true;
            }
        }
    } catch (...) { return false; }
    return any;
}

static void apply_thread_policies() {
    if (g_config.rt_priority > 0) {
        sched_param sp{};
        sp.sched_priority = g_config.rt_priority;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0)
            FLM_LOG(LogLevel::WARN, "SCHED_FIFO failed (CAP_SYS_NICE?)");
    }
    // [item 13][FIX-59] measurement thread affinity. The old parser only
    // understood ONE range or ONE core — the documented (and DRSTool-
    // suggested) list form "4,5,6" was silently a parse error, so CCD-
    // isolation setups that pin to a non-contiguous core set (e.g. CCD1
    // minus its SMT siblings) could not be expressed at all.
    if (!g_config.measure_cpu.empty()) {
        cpu_set_t set;
        if (parse_cpu_list(g_config.measure_cpu, set)) {
            if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0)
                FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU affinity failed");
        } else {
            FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU parse error: %s",
                    g_config.measure_cpu.c_str());
        }
    }
    pthread_setname_np(pthread_self(), "flm-measure");
}

static void measurement_thread_fn(std::stop_token stoken, std::shared_ptr<SwapchainState> st) {
    apply_thread_policies();

    // [item 12] Open CSV
    if (!g_config.csv_path.empty()) {
        // [FIX-57] Registry-backed open: append (headerless) across swapchain
        // recreations, suffixed file if another live swapchain already owns
        // the path. Buffering + header handled inside.
        st->csv = std::make_unique<CsvState>();
        st->csv->fp = csv_open_registered(g_config.csv_path, st->csv->reg_key);
        if (!st->csv->fp) {
            // [FIX-54] v2.5 silently dropped CSV logging on fopen failure —
            // no way to tell "CSV disabled" from "CSV path unwritable" (wrong
            // dir, missing perms, or a container/sandbox mount namespace that
            // doesn't share the host's view of the path, e.g. Steam Linux
            // Runtime / Pressure Vessel). Now logged loudly with errno.
            FLM_LOG(LogLevel::WARN, "FLM_CSV fopen('%s') failed: %s",
                    g_config.csv_path.c_str(), strerror(errno));
            st->csv.reset();   // [PERF-11] no FILE*, no point keeping the object
        }
    }
    // [PERF-11] g_config.stats is structural (set once at init, not hot-
    // reloadable) — safe to gate the allocation on it here, once.
    if (g_config.stats) st->stats = std::make_unique<StatsState>();

    uint64_t wait_id         = st->next_present_id.load(std::memory_order_relaxed);
    if (wait_id == 0) wait_id = 1;
    int64_t  last_display_ns = 0;
    bool     last_valid      = false;
    if (st->stats) st->stats->last_ns = now_ns();

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
            const int64_t T_prev = st->timeline.median();
            // [FIX-40] No hitch/fake classification until the window is warm
            // (4 estimates): at ~30 FPS with T_prev still at 16.6ms every
            // first frame counted as a hitch and the window never warmed up.
            const bool warm = st->timeline.real_count >= 4;

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
                st->timeline.cyc_count = 0;
                st->timeline.cyc_idx   = 0;
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
                st->timeline.cyc_win[st->timeline.cyc_idx] = interval_ns;
                st->timeline.cyc_idx = (st->timeline.cyc_idx + 1) % FlmConst::CYC_RING;
                if (st->timeline.cyc_count < FlmConst::CYC_RING) st->timeline.cyc_count++;

                const int mm = std::clamp(m, 1, FlmConst::CYC_RING);
                if (st->timeline.cyc_count >= mm) {
                    int64_t T_est = 0;
                    for (int k = 0; k < mm; k++)
                        T_est += st->timeline.cyc_win[(st->timeline.cyc_idx - 1 - k +
                                              FlmConst::CYC_RING) % FlmConst::CYC_RING];
                    // After warmup, clamp single-sample estimate to 2x/0.25x
                    // (anomaly/clock protection outside of hitches; still
                    // catches FPS jumps within ≈2-3 flips).
                    if (warm)
                        T_est = std::clamp(T_est, T_prev / 4, T_prev * 2);
                    st->timeline.real_win[st->timeline.real_idx] = T_est;
                    st->timeline.real_idx = (st->timeline.real_idx + 1) % FlmConst::REAL_WINDOW;
                    if (st->timeline.real_count < FlmConst::REAL_WINDOW) st->timeline.real_count++;
                    st->timeline.recompute();   // [FIX-50] once per push
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
                                                      st->slot.mean_ns * 4);
                st->slot.sum += safe_iv - st->slot.win[st->slot.idx];
                st->slot.win[st->slot.idx] = safe_iv;
                st->slot.idx = (st->slot.idx + 1) % FlmConst::SLOT_WINDOW;
                if (st->slot.count < FlmConst::SLOT_WINDOW) st->slot.count++;
                st->slot.mean_ns = st->slot.sum / st->slot.count;
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
            } else if (st->probe.left > 0) {
                // ============================================================
                // [FIX-47] PROBE ACTIVE — gate is standing down (apply_gate
                // sees probe_active and passes everything through), so the
                // intervals arriving here are RAW. Detection runs UNFROZEN
                // on exactly these samples.
                // ============================================================
                if (interval_ns * FlmConst::MFG_FAKE_RATIO_DEN < st->slot.mean_ns * FlmConst::MFG_FAKE_RATIO_NUM) st->mfg_detect.small_cnt++;
                st->mfg_detect.total_cnt++;
                if (--st->probe.left == 0) {
                    if (st->mfg_detect.total_cnt >= FlmConst::MFG_PROBE_MIN_SAMPLES) {
                        double p = (double)st->mfg_detect.small_cnt / (double)st->mfg_detect.total_cnt;
                        int mhat = (p < 0.99) ? (int)std::lround(1.0 / (1.0 - p)) : 4;
                        mhat = std::clamp(mhat, 1, 4);
                        if (mhat != m) {
                            FLM_LOG(LogLevel::INFO, "MFG multiplier (probe): %d -> %d", m, mhat);
                            st->eff_mfg.store(mhat, std::memory_order_relaxed);
                        }
                    }
                    st->mfg_detect.small_cnt = 0;
                    st->mfg_detect.total_cnt = 0;
                    st->probe.last_ns = tnow;
                    st->probe_active.store(false, std::memory_order_relaxed);
                }
            } else {
                bool gate_hot = (tnow - st->last_gate_wait_ns.load(std::memory_order_relaxed))
                                < 1'000'000'000LL;
                if (gate_hot && m > 1) {
                    st->mfg_detect.small_cnt = 0;   // frozen window: start clean
                    st->mfg_detect.total_cnt = 0;
                    // ========================================================
                    // [FIX-47] FREEZE + ALWAYS-HOT GATE DEADLOCK. Floor pacing
                    // holds at least one present per cycle at m>1, so gate_hot
                    // never cools → detection stayed frozen FOREVER: an in-game
                    // multiplier change (2→3, or MFG off mid-session) could
                    // never be picked up; 3x content ran with a 2x distribution
                    // (0.425T/0.425T/0.15T) permanently. Fix: every
                    // PROBE_PERIOD, suspend the gate for PROBE_FLIPS flips
                    // (~100-200ms — invisible on VRR) and re-measure m from the
                    // raw stream. m→1 transitions were already self-correcting
                    // via cycle-sum; this closes the upward path too.
                    // ========================================================
                    if (st->probe.last_ns == 0) {
                        st->probe.last_ns = tnow;   // anchor on first frozen flip
                    } else if (tnow - st->probe.last_ns >= FlmConst::PROBE_PERIOD_NS) {
                        st->probe.left = FlmConst::PROBE_FLIPS;
                        st->probe_active.store(true, std::memory_order_relaxed);
                        FLM_LOG(LogLevel::DEBUG, "MFG probe: gate suspended for %d flips",
                                FlmConst::PROBE_FLIPS);
                    }
                } else {
                    if (interval_ns * FlmConst::MFG_FAKE_RATIO_DEN < st->slot.mean_ns * FlmConst::MFG_FAKE_RATIO_NUM) st->mfg_detect.small_cnt++;
                    st->mfg_detect.total_cnt++;
                    if (st->mfg_detect.total_cnt >= FlmConst::MFG_DETECT_WINDOW) {
                        double p = (double)st->mfg_detect.small_cnt / (double)st->mfg_detect.total_cnt;
                        int mhat = (p < 0.99) ? (int)std::lround(1.0 / (1.0 - p)) : 4;
                        mhat = std::clamp(mhat, 1, 4);
                        if (mhat != m)
                            FLM_LOG(LogLevel::INFO, "MFG multiplier: %d -> %d", m, mhat);
                        st->eff_mfg.store(mhat, std::memory_order_relaxed);
                        st->mfg_detect.small_cnt = 0;
                        st->mfg_detect.total_cnt = 0;
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
                slot_iv = std::max<int64_t>(st->timeline.median() / mm2,
                                            FlmConst::MIN_FLOOR_NS);
            } else {
                slot_iv = st->slot.mean_ns;
            }
            st->slot_interval_ns.store(slot_iv, std::memory_order_relaxed);

            // [FIX-18] GPU-bound guard: only when an explicit target (fps>0) is
            // set, and uses slot-EMA rather than raw intervals. At fps=0 the
            // target is derived from measurements anyway → guard is meaningless
            // (in v2 MFG's bimodal raw intervals immediately triggered it and
            // killed pacing).
            if (fps > 0) {
                if (st->slot.mean_ns > (slot_iv * FlmConst::GPU_BOUND_OVER_PCT) / 100) {
                    st->gpu_guard.over_run++; st->gpu_guard.under_run = 0;
                } else if (st->slot.mean_ns <= (slot_iv * FlmConst::GPU_BOUND_UNDER_PCT) / 100) {
                    st->gpu_guard.under_run++; st->gpu_guard.over_run = 0;
                }
                if (st->gpu_guard.over_run >= FlmConst::GPU_BOUND_WINDOW) {
                    if (st->pacing_enabled.exchange(false, std::memory_order_relaxed))
                        FLM_LOG(LogLevel::DEBUG, "GPU-bound: pacing OFF");
                    st->gpu_guard.over_run = FlmConst::GPU_BOUND_WINDOW;
                } else if (st->gpu_guard.under_run >= FlmConst::GPU_BOUND_WINDOW) {
                    if (!st->pacing_enabled.exchange(true, std::memory_order_relaxed))
                        FLM_LOG(LogLevel::DEBUG, "GPU-bound: pacing ON");
                    st->gpu_guard.under_run = FlmConst::GPU_BOUND_WINDOW;
                }
            } else {
                st->gpu_guard.over_run = st->gpu_guard.under_run = 0;
                if (!st->pacing_enabled.load(std::memory_order_relaxed))
                    st->pacing_enabled.store(true, std::memory_order_relaxed);
            }

            // [item 12] stats + CSV — both objects are null unless the
            // corresponding feature is actually on ([PERF-11]).
            if (st->stats) {
                if (!is_fake) {
                    st->stats->sum_ns += interval_ns;
                    st->stats->max_ns  = std::max(st->stats->max_ns, interval_ns);
                    st->stats->frames++;
                    if (is_hitch) st->stats->hitch++;   // [FIX-58] separate counter
                } else {
                    st->stats->fake++;                  // [FIX-58]
                }
                // [FIX-58] Percentile sample. Overflow past STAT_RING is
                // dropped; see the constant's comment.
                if (st->stats->ring_n < FlmConst::STAT_RING)
                    st->stats->ring[st->stats->ring_n++] = interval_ns;
            }
            if (st->csv)
                st->csv->push(tnow, interval_ns, is_fake, is_hitch,
                             st->present_seq.load(std::memory_order_relaxed),
                             m, st->slot.mean_ns,
                             st->pacing_enabled.load(std::memory_order_relaxed)); // [FIX-31]

            // [FIX-32] Interval configurable via FLM_STATS_INTERVAL (seconds).
            int64_t stats_iv = g_config.stats_interval_ns.load(std::memory_order_relaxed);
            if (st->stats && tnow - st->stats->last_ns >= stats_iv &&
                st->stats->frames > 0) {
                double avg_ms = ((double)st->stats->sum_ns / (double)st->stats->frames) / 1e6;
                double max_ms = (double)st->stats->max_ns / 1e6;
                // [FIX-58] p99 over ALL presented intervals in the window —
                // the live equivalent of the offline CSV p99 workflow.
                double p99_ms = 0.0;
                if (st->stats->ring_n > 0) {
                    int64_t tmp[FlmConst::STAT_RING];
                    const int n = st->stats->ring_n;
                    std::copy(st->stats->ring, st->stats->ring + n, tmp);
                    const int k = (n * 99) / 100;
                    std::nth_element(tmp, tmp + k, tmp + n);
                    p99_ms = (double)tmp[k] / 1e6;
                }
                FLM_LOG(LogLevel::INFO,
                    "STATS %llds: n=%d avg=%.2fms p99=%.2fms max=%.2fms "
                    "fake=%d hitch=%d mfg=%d pacing=%d",
                    (long long)(stats_iv / 1'000'000'000LL), st->stats->frames,
                    avg_ms, p99_ms, max_ms, st->stats->fake, st->stats->hitch,
                    st->eff_mfg.load(), (int)st->pacing_enabled.load());
                st->stats->sum_ns = st->stats->max_ns = 0;
                st->stats->frames = st->stats->fake = st->stats->hitch = 0;
                st->stats->ring_n = 0;
                st->stats->last_ns = tnow;
            }
        }

        last_display_ns = tnow;
        last_valid      = true;
        wait_id++;
    }

    if (st->csv) st->csv->flush();
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
    g_sc_gen.fetch_add(1, std::memory_order_release);   // [FIX-50] invalidate caches
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
    g_sc_gen.fetch_add(1, std::memory_order_release);   // [FIX-50] invalidate caches
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
        g_sc_gen.fetch_add(1, std::memory_order_release);   // [FIX-50] invalidate caches
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
    // [FIX-50] One clock read per gate entry. v2.4 called now_ns() up to three
    // times on the floor path (freshness guard, floor anchor, autotune); the
    // vDSO call is cheap but the reads could also disagree by the interleaving
    // time. Single timestamp, re-read only after an actual wait.
    int64_t t = now_ns();

    if (!limiter_mode) {
        int64_t lf = st->last_flip_ns.load(std::memory_order_relaxed);
        if (lf == 0 || t - lf > FlmConst::MEAS_FRESH_NS) {
            st->limiter_next_ns = 0;
            st->last_present_ns = 0;
            st->held_run        = 0;
            return;
        }
        // [FIX-47] MFG re-sample probe: gate stands down so RAW intervals
        // reach the detector. Limiter is measurement-free and unaffected.
        if (st->probe_active.load(std::memory_order_relaxed)) {
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
        // [ARCH-04] floor_ratio and floor_autotune are both read
        // unconditionally on this path (unlike floor_mfg_adapt/step just
        // below, which stay individual .load()s behind their own "only if
        // m>1" branch) — bundled here so they read as one named group.
        struct { int ratio; bool autotune; } floor_cfg = {
            g_config.floor_ratio.load(std::memory_order_relaxed),
            g_config.floor_autotune.load(std::memory_order_relaxed),
        };
        int ratio = floor_cfg.ratio;
        // [FIX-41] Relax ratio as m grows. On Ada (40-series) GPU with no HW
        // flip metering the generated-frame production time has higher variance
        // at m=3/4; a fixed tight ratio can hold real frames inside the floor
        // unnecessarily, causing stalls and hitches. Only active when m>1;
        // no effect at m=1.
        // [FIX-56] Step scales with (m-1)*m/2 (1,3,6 at m=2,3,4), not linearly
        // in (m-1) — each extra generated frame's variance compounds rather
        // than adding a fixed increment (A/B data + full reasoning: CHANGELOG
        // FIX-56).
        int m_now = st->eff_mfg.load(std::memory_order_relaxed);
        if (g_config.floor_mfg_adapt.load(std::memory_order_relaxed)) {
            if (m_now > 1) {
                int step = g_config.floor_mfg_step.load(std::memory_order_relaxed);
                int64_t d = (int64_t)step * (m_now - 1) * m_now / 2;  // 1,3,6.. for m=2,3,4
                ratio = (int)std::clamp<int64_t>(ratio - d, 500, 1000);
            }
        }
        // [FIX-44] Learned delta stacks on top of base + adapt; clamp preserved.
        const bool autotune = floor_cfg.autotune;
        if (autotune)
            ratio = std::clamp(ratio + st->ratio_auto, 500, 1000);
        int64_t floor   = std::max<int64_t>((slot_iv * ratio) / 1000, FlmConst::MIN_FLOOR_NS);

        // [FIX-50] t taken once at gate entry; only re-read after a real wait.
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
                    // [FIX-56] Loosen step scales with m (-4*max(1,m-1)):
                    // at high m the floor fights several stacked interpolated-
                    // frame variances at once, not one (CHANGELOG FIX-56).
                    st->ratio_auto -= 4 * std::max(1, m_now - 1);
                    st->held_run = 0;
                }
            } else {
                st->held_run = 0;
                int64_t head = since - floor;
                if      (head > slot_iv / FlmConst::AUTOTUNE_TIGHTEN_HEADROOM_DIV) st->ratio_auto += 1;
                else if (head < slot_iv / FlmConst::AUTOTUNE_LOOSEN_HEADROOM_DIV) st->ratio_auto -= 2;
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

    // [FIX-50] t from gate entry (single clock read per gate).
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

// [ARCH-04] Named snapshot of the config fields resolve_gate always reads
// together, taken once at the top instead of three independent .load()
// calls with no visible relationship between them. All three were already
// unconditional here (no branch skips any of them), so this changes
// grouping/readability only — same number of atomic loads as before, and
// the three values are now guaranteed to describe one consistent instant
// instead of three independently-timed reads.
struct GateModeConfig {
    PaceMode mode;
    int      fps;
    bool     pace_fifo;
};
static GateModeConfig snapshot_gate_mode_config() {
    return {
        (PaceMode)g_config.mode.load(std::memory_order_relaxed),
        g_config.target_fps.load(std::memory_order_relaxed),
        g_config.pace_fifo.load(std::memory_order_relaxed),
    };
}

// Resolve the active mode. limiter_mode output: whether gate uses limiter logic.
// Return: whether to pace at all.
static bool resolve_gate(const SwapchainState* st, bool has_wait, bool& limiter_mode) {
    if (!st->pace_allowed) return false;
    const GateModeConfig cfg = snapshot_gate_mode_config();

    // [item 11] FIFO/FIFO_RELAXED already locked to vsync → PACER (uniform
    // cadence estimate) is unnecessary and normally fights the compositor.
    // LIMITER (cap to a lower FPS) is still valid and useful on these modes.
    // [FIX-53] FLM_PACE_FIFO=1 lifts this filter: PACER (and floor-pacing,
    // since it lives on the same fps==0 branch in apply_gate) becomes
    // available on FIFO too. Opt-in because it's a real behavior change, not
    // just a default flip — on most FIFO content the compositor's own vsync
    // pacing already does this job, and layering PACER on top can add its own
    // jitter. Intended for opt-in cases: an MFG engine that only offers FIFO,
    // or A/B'ing PACER's smoothing against the driver's native FIFO cadence.
    bool is_fifo = (st->present_mode == VK_PRESENT_MODE_FIFO_KHR ||
                    st->present_mode == VK_PRESENT_MODE_FIFO_RELAXED_KHR) &&
                   !cfg.pace_fifo;

    switch (cfg.mode) {
        case PaceMode::OFF:     return false;
        case PaceMode::LIMITER: limiter_mode = true;  return cfg.fps > 0;
        case PaceMode::PRESENT:  // [FIX-52] PRESENT and AUTO were byte-identical — merged
        case PaceMode::AUTO:
        default:
            if (has_wait && !is_fifo) { limiter_mode = false; return true; }
            limiter_mode = true; return cfg.fps > 0;   // FIFO or no wait → limiter
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

        // [FIX-49] present_seq is TELEMETRY (CSV slot column), not gate state.
        // v2.4 only incremented it inside the gate condition → with
        // FLM_PACE_POINT=acquire the slot column froze at 0 and regression
        // analysis silently broke. Count every primary-swapchain present.
        if (i == 0)
            st->present_seq.fetch_add(1, std::memory_order_relaxed);  // [item 4]

        // SINGLE GATE (only on the first/primary swapchain; multiple swapchains are rare)
        if (gate_here && i == 0 &&
            st->frame_count.load(std::memory_order_relaxed) >= FlmConst::WARMUP_FRAMES) {
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
//  FLM_PACE_FIFO=1             [FIX-53] allow PACER/floor-pacing on FIFO too
//                              (default 0). FIFO is already vsync-locked;
//                              only lift this if you specifically want PACER's
//                              smoothing on top of it (e.g. FIFO-only MFG
//                              engine). LIMITER is unaffected either way.
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
//  FLM_FLOOR_MFG_ADAPT=1       [FIX-41][FIX-56] relax FLOOR_RATIO as m (MFG
//                              multiplier) grows: -step*(m-1)*m/2, i.e.
//                              1x/3x/6x step at m=2/3/4 (was linear (m-1)).
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
//  FLM_MEASURE_CPU=0-3         measurement thread affinity; comma lists of
//                              ranges/cores accepted, e.g. "0-3,8,10-11" [FIX-59]
//  FLM_STATS=1                 periodic summary log (INFO) — n, avg, p99,
//                              max, fake, hitch, mfg, pacing [FIX-58]
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
