// ============================================================================
// FLM — Vulkan Flip Meter / Frame Pacing Layer  (v3.1 — "auto")
//
// DESIGN SUMMARY
// --------------
// One decision per present, made automatically:
//
//   FLM_TARGET_FPS > 0  → LIMITER. Absolute-timeline FPS cap at QueuePresent.
//                         No presentWait needed, works on every driver.
//   FLM_TARGET_FPS = 0  → FLOOR PACER (needs presentWait). A measurement
//                         thread reads real flip timestamps, estimates the
//                         real-frame period T and the frame-generation
//                         multiplier m, and publishes slot = T/m. The present
//                         gate then refuses to let a present leave sooner than
//                         floor = slot * ratio after the previous one. Early
//                         (generated / runt) frames are held, late frames pass
//                         untouched, a variable VRR rate is never braked.
//
// v2.x had grown ~30 environment variables whose interactions silently
// no-op'd each other (TARGET_FPS killed the floor family, FIFO killed the
// pacer, a forced multiplier killed the re-anchor probe, PACE_POINT=both
// halved the frame rate). v3.0 keeps the parts that were proven in the field
// (cycle-sum T estimate, m-scaled relaxation, closed-loop ratio, ratchet
// guard, re-anchor probe) and turns every tuning constant back into an
// internal, self-adjusting value. The v2.x per-fix history lives in git.
//
// v3.0 changes
// ------------
//  [V3-1] CONFIG SURFACE 32 → 11. User-facing: FLM_MODE, FLM_TARGET_FPS,
//         FLM_FLOOR_RATIO (optional). Diagnostics/system: FLM_MFG_MULTIPLIER,
//         FLM_RT_PRIORITY, FLM_MEASURE_CPU, FLM_LOG_LEVEL, FLM_LOG_FILE,
//         FLM_STATS, FLM_CSV, FLM_CONFIG. Removed keys are reported ONCE in the
//         log as ignored, so stale launcher configs are easy to clean up.
//         FLM_PROFILE and FLM_MODE=limiter keep working as aliases.
//  [V3-2] FIFO ≠ FIXED REFRESH. v2 never paced FIFO swapchains, on the
//         assumption FIFO is vsync-locked. On a VRR panel FIFO is the NORMAL
//         mode (G-Sync/FreeSync + vsync-on is the recommended setup and what
//         DXVK uses whenever the game's vsync is on) and frames are shown when
//         ready — the layer was silently a no-op for exactly those users. The
//         measurement thread now classifies FIFO cadence: flip intervals
//         quantised to integer multiples of one refresh period = fixed
//         refresh (don't pace); continuous intervals = VRR (pace). The VRR
//         verdict latches for the swapchain's lifetime (pacing makes the
//         cadence uniform, which must not flip the verdict back). Replaces
//         FLM_PACE_FIFO; FLM_MODE=present still forces pacing on FIFO.
//  [V3-3] PHANTOM FLIPS. WaitForPresentKHR returning without blocking means
//         that id flipped before we started waiting: MAILBOX replaced the
//         image (its id completes together with the next one) or the thread
//         fell behind. v2 recorded those as ~0 ns intervals → counted as
//         generated frames → false MFG detection on MAILBOX above refresh,
//         slot halved, pacer braking a game that has no frame generation.
//         A non-blocking return is now skipped; two in a row mark a backlog
//         and break the interval chain instead.
//  [V3-4] FAST-FORWARD FIRED ON NORMAL QUEUE DEPTH. "wait_id + 2 < latest"
//         compares against SUBMITTED ids; with DXVK's usual 2–3 frames in
//         flight it tripped routinely, discarding samples and mixing
//         non-adjacent intervals into the cycle sum. Real backlog is now
//         detected by [V3-3]; the fast-forward is only a safety net (gap > 8).
//  [V3-5] LEARNED RATIO LEAKED ACROSS MULTIPLIERS. ratio_auto learned at m=4
//         (up to +400 to cancel the -240 static relaxation) was applied as-is
//         after a switch to m=1 → ratio clamped to 1000 → floor pinned to the
//         ratchet cap → every frame held until the loop unwound (~100
//         frames). The learned delta now resets on every m change.
//  [V3-6] m=1 IS NOT MFG. With no frame generation the floor only has one
//         job: stop runt frames. A ratio climbing to ~0.95 of the median
//         turned the pacer into a soft limiter that slowed every FPS rise
//         (~8 % per 4 frames). At m=1 the ratio is capped at 850 (or the
//         user's explicit FLM_FLOOR_RATIO) and a single held frame already
//         counts as a brake sign (v2 required two).
//  [V3-7] HITCH THRESHOLD TOO TIGHT. max(1.5T, T+2ms) sat inside the normal
//         p99 at 150–250 FPS (field data: ~7 ms threshold vs 6.7–11.6 ms
//         p99) → ordinary tail frames were "hitches", each one switching the
//         pacer off for 8 flips (~20 % of frames unpaced). Now max(1.5T,
//         T+6ms) (cap T+30ms). Recovery length scales with m (in flips).
//  [V3-8] LIVE RELOAD REVERT. Deleting a line from FLM_CONFIG and sending
//         SIGUSR1 kept the old value unless the same key was in the env.
//         Every reload now starts from built-in defaults → env → file.
//  [V3-9] SINGLE PRESENT-POINT GATE. PACE_POINT=acquire/both removed: in
//         DXVK/vkd3d acquire and present run on the same presenter thread, so
//         acquire gating bought no latency, and on engines that acquire from
//         another thread it raced the non-atomic gate state. The acquire hooks
//         are gone (two fewer intercepted calls per frame).
//  [V3-10] CLASSIC GRID PACER + GPU-BOUND GUARD REMOVED. The fps>0 pacer path
//         was the limiter with a lead offset; the fps=0 grid pacer braked VRR
//         (the reason the floor pacer exists). One limiter, one pacer.
//  [V3-11] HOT PATH. Queue → dispatch lookup cached thread-locally (was a
//         shared_mutex + hash per present). Default log level WARN (was
//         ERROR, which hid every warning including CSV open failures).
//  [V3-12] CSV LIFECYCLE. The file is opened on the first real flip and
//         closed when the measurement thread exits. v2 opened it at swapchain
//         creation — while the OLD swapchain (oldSwapchain recreate order) and
//         a thread_local cache still held the previous state — which is how
//         recreated swapchains ended up writing to "<path>.2".
//  [V3-13] STATS line reports the effective floor ratio and the FIFO/VRR
//         verdict, so the auto-configuration is observable without CSV.
//
// v3.1 changes (review pass — no new configuration)
// ------------------------------------------------------------------------------
//  [V31-1] FLOOR WATCHED ITS OWN OUTPUT ON A CADENCE RISE. The slot is derived
//          from measured flips, and a held frame's flip is the floor itself, so
//          after any slow phase the pacer kept the old cadence: a 10 FPS
//          loading screen followed by 200 FPS gameplay held every frame ~85 ms
//          for ~2 s (simulated: 72 vs 194 FPS in the 3 s after the load); a
//          60→250 FPS rise ran at ~195 FPS with 15 ms spikes for a second.
//          Three fixes: (a) a single hold is capped at MAX_PACE_WAIT_NS;
//          (b) a SUSTAINED STALE HOLD (m=1: 3 presents in a row arriving before
//          half the floor; m>1: more than two cycles of holds the cap had to
//          clip) means the natural cadence is faster
//          than anything
//          the floor can measure → the frame is released, the
//          next REAL_WINDOW+2m presents pass unpaced (m=1) or at the probe's
//          half floor (m>1, keeps generated frames spaced) and the measurement
//          thread restarts its T/slot windows from them (re-anchor);
//          (c) see V31-2.
//  [V31-2] INTEGRATOR WIND-UP. ratio_auto kept climbing while the ratio sat on
//          its ceiling (m=1: +400 above the 850 cap), so the "one held frame
//          = brake" response (-4) needed ~100 held frames before the effective
//          ratio moved at all. ratio_auto is now clamped to the range that can
//          actually change the clamped ratio.
//  [V31-3] LIMITER: the first frame scheduled the NEXT deadline and the second
//          call added another interval on top (second frame waited 2×iv);
//          after a stall > 2 intervals the re-anchor added a full interval of
//          wait to a frame that was already late. Deadline now means "this
//          present"; a stall re-anchors to now with no wait.
//  [V31-4] VK_SUBOPTIMAL_KHR from vkWaitForPresentKHR is a SUCCESS code (the
//          present completed). It was handled as an error: 50 ms sleep, id not
//          advanced → on a swapchain that stays suboptimal (common when the
//          app ignores SUBOPTIMAL) measurement — and with it the pacer — was
//          dead for the session while the thread woke 20×/s.
//  [V31-5] SWAPCHAIN DESTROY STALL. The measurement thread waited in the driver
//          for the NEXT id, which after the app's last present never comes:
//          vkDestroySwapchainKHR / vkDestroyDevice blocked up to 50 ms on the
//          join (+50 ms on the OUT_OF_DATE sleep) — on every alt-tab/resize
//          recreate. The thread now enters the driver only for ids already
//          handed to QueuePresent and otherwise sleeps on a futex
//          (submit_seq) that the next present or the stop request wakes.
//          Side effect: zero wake-ups while the game isn't presenting.
//  [V31-6] EXIT SAFETY. The measurement thread owned a shared_ptr to its own
//          state; when static destructors ran at exit (Wine calls exit())
//          the maps, config strings and the CSV registry were destroyed under
//          a still-running thread (use-after-free), and if that thread then
//          dropped the last reference, ~jthread joined itself → terminate.
//          The thread now gets a raw pointer (state lifetime ⊇ thread
//          lifetime: ~SwapchainState stops+joins first), and the global
//          tables are intentionally leaked so exit never frees them.
//  [V31-7] CONFIG RELOAD RACE. Reload reset the live atomics to defaults and
//          then re-applied env/file, so the present thread could observe
//          target_fps=0 for one frame (limiter timeline reset → one uncapped
//          frame + re-anchor). Values are now built off-line and published
//          once. Mode/log-level values are case-insensitive; an over-long
//          config line is skipped instead of being re-parsed as a new line.
//  [V31-8] LESS SPIN. The present thread's timer slack (default 50 µs) was
//          most of the measured oversleep, i.e. most of the spin margin burnt
//          with _mm_pause. precise_wait sets it to 1 µs once per thread, which
//          cuts the per-hold busy-wait to roughly a third.
//  [V31-9] STATS ratio=0 whenever the floor is not pacing right now (it kept
//          the last value after mode=off/cap, hitch, FIFO-fixed).
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
#include <cerrno>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <cstdint>
#include <vector>
#include <time.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <strings.h>

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
static std::atomic<int> g_log_level{(int)LogLevel::WARN};  // [V3-11] was ERR
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
// CONSTANTS — everything that used to be an env knob is here now.
// ============================================================================
namespace FlmConst {
    constexpr int64_t  DEFAULT_INTERVAL_NS = 16'666'666LL;
    constexpr uint64_t WAIT_TIMEOUT_NS     = 50'000'000ULL;
    constexpr int64_t  MAX_PACE_WAIT_NS    = 20'000'000LL;
    constexpr uint32_t STACK_PRESENT_IDS   = 8;
    constexpr int      WARMUP_FRAMES       = 30;
    constexpr int      SLOT_WINDOW         = 12;   // lcm(1..4): full cycle for every m
    constexpr int      REAL_WINDOW         = 8;    // cycle-sum T estimates (median)
    constexpr int      CYC_RING            = 4;    // raw intervals, max multiplier
    constexpr int64_t  MIN_FLOOR_NS        = 500'000LL;
    constexpr int64_t  RATCHET_MARGIN_NS   = 50'000LL;
    constexpr int      MFG_DETECT_WINDOW   = 32;
    constexpr int64_t  PROBE_PERIOD_NS     = 10'000'000'000LL;
    constexpr int      PROBE_FLIPS         = 24;
    constexpr int      PROBE_RATIO_CAP     = 600;
    constexpr int      MIN_SC_WIDTH        = 640;
    constexpr int      MIN_SC_HEIGHT       = 480;
    constexpr int      CSV_BUFFER          = 256;
    constexpr int64_t  CSV_SYNC_NS         = 5'000'000'000LL;
    constexpr int64_t  STATS_INTERVAL_NS   = 5'000'000'000LL;
    constexpr int      STAT_RING           = 4096;
    constexpr int64_t  MEAS_FRESH_NS       = 250'000'000LL;
    constexpr size_t   CSV_STDIO_BUF       = 1u << 20;
    constexpr size_t   LOG_STDIO_BUF       = 64u << 10;
    // [V3-3] A WaitForPresentKHR that returns faster than this never blocked.
    constexpr int64_t  IMMEDIATE_NS        = 20'000LL;
    // [V3-4] Only a genuine backlog triggers the fast-forward.
    constexpr uint64_t FF_GAP              = 8;
    // [V3-7] Hitch = interval > max(1.5T, T + HITCH_ABS_NS), capped T + 30ms.
    constexpr int64_t  HITCH_ABS_NS        = 6'000'000LL;
    constexpr int64_t  HITCH_CAP_NS        = 30'000'000LL;
    // [V3-2] FIFO cadence classifier.
    constexpr int      FIFO_WIN            = 64;
    constexpr int      FIFO_QUANT_TOL_PCT  = 6;     // |iv/R - k| < 6 %
    constexpr int      FIFO_VRR_MAX_PCT    = 60;    // quantised share below this → VRR
    constexpr int      FIFO_VRR_CONFIRM    = 2;     // consecutive windows to latch
    constexpr int64_t  FIFO_MIN_REFRESH_NS = 1'800'000LL;   // > any real panel's refresh
    // [V31-8] Present-thread timer slack (ns) used while FLM sleeps on it.
    constexpr unsigned long TIMER_SLACK_NS = 1'000UL;
}

// Floor-pacer tuning. Base ratio and the autotune ceiling depend on the mode;
// everything else is shared. Ratio units: 1000 = one full slot.
namespace FlmTune {
    constexpr int RATIO_AUTO     = 850;
    constexpr int RATIO_LATENCY  = 780;
    constexpr int AUTO_MAX       = 400;   // autotune may climb this far above base
    constexpr int AUTO_MAX_LAT   = 200;
    constexpr int AUTO_MIN       = -150;
    constexpr int MFG_STEP       = 40;    // relax by step*(m-1)*m/2 → 40/120/240
    constexpr int RATIO_CAP_M1   = 850;   // [V3-6]
}

enum class PaceMode { AUTO = 0, PRESENT, LATENCY, CAP, OFF };

// ============================================================================
// CONFIG
// ============================================================================
struct FLMConfig {
    // Load-time (read once at vkCreateInstance)
    int         mfg_mult_env = 0;   // 0 = auto-detect; 1..4 = force
    int         rt_priority  = 0;
    std::string measure_cpu;
    bool        stats        = false;
    std::string csv_path;
    std::string config_path;

    // Hot-reloadable (FLM_CONFIG + SIGUSR1)
    std::atomic<int> mode        {(int)PaceMode::AUTO};
    std::atomic<int> target_fps  {0};
    std::atomic<int> floor_ratio {0};   // 0 = mode default
};

// [V31-6] Leaked on purpose: a still-running measurement thread must never see
// these destroyed by static destructors at exit.
static FLMConfig&     g_config = *new FLMConfig;
static std::once_flag g_config_flag;

static const char* mode_name(int m) {
    switch ((PaceMode)m) {
        case PaceMode::AUTO:    return "auto";
        case PaceMode::PRESENT: return "present";
        case PaceMode::LATENCY: return "latency";
        case PaceMode::CAP:     return "cap";
        case PaceMode::OFF:     return "off";
    }
    return "?";
}

// FLM_MODE and the legacy FLM_PROFILE share one vocabulary. [V31-7] any case.
static PaceMode parse_mode(const char* s) {
    if (!strcasecmp(s, "auto") || !strcasecmp(s, "vrr") || !strcasecmp(s, "mfg"))
                                                              return PaceMode::AUTO;
    if (!strcasecmp(s, "present"))                            return PaceMode::PRESENT;
    if (!strcasecmp(s, "latency"))                            return PaceMode::LATENCY;
    if (!strcasecmp(s, "cap") || !strcasecmp(s, "limiter"))   return PaceMode::CAP;
    if (!strcasecmp(s, "off"))                                return PaceMode::OFF;
    FLM_LOG(LogLevel::WARN, "FLM_MODE='%s' unknown — expected auto|present|latency|cap|off; "
            "using auto", s);
    return PaceMode::AUTO;
}

// [V3-1] v2.x keys that are now internal. Reported once, never applied.
static const char* const k_legacy_keys[] = {
    "FLM_PACE_POINT", "FLM_PACE_FIFO", "FLM_PRESENT_LEAD_NS", "FLM_SPIN_NS",
    "FLM_SPIN_ADAPT", "FLM_DRIFT_TOLERANCE_NS", "FLM_FLOOR_PACING",
    "FLM_FLOOR_MFG_ADAPT", "FLM_FLOOR_MFG_STEP", "FLM_FLOOR_AUTOTUNE",
    "FLM_FLOOR_AUTOTUNE_MAX", "FLM_WARMUP_FRAMES", "FLM_HITCH_RECOVERY",
    "FLM_HITCH_THRESHOLD_MS", "FLM_PROBE_PERIOD_S", "FLM_PROBE_FLIPS",
    "FLM_STATS_INTERVAL", "FLM_CSV_SYNC_S", "FLM_VERBOSE",
};
static const char* const k_loadtime_keys[] = {
    "FLM_MFG_MULTIPLIER", "FLM_RT_PRIORITY", "FLM_MEASURE_CPU", "FLM_STATS",
    "FLM_CSV", "FLM_CONFIG", "FLM_LOG_FILE",
};
template <size_t N>
static bool in_list(const char* k, const char* const (&list)[N]) {
    for (const char* s : list) if (!strcmp(k, s)) return true;
    return false;
}

// Keys already reported, so a SIGUSR1 loop doesn't spam the log.
static std::mutex               g_warned_lock;
static std::vector<std::string>& g_warned_keys = *new std::vector<std::string>;   // [V31-6]
static void warn_key_once(const char* key, const char* why) {
    std::lock_guard lk(g_warned_lock);
    for (const auto& k : g_warned_keys) if (k == key) return;
    g_warned_keys.emplace_back(key);
    FLM_LOG(LogLevel::WARN, "%s: %s", key, why);
}

// [V31-7] Hot-reloadable values are assembled here and published in one go,
// so the present thread never sees the transient built-in defaults.
struct DynCfg {
    int mode        = (int)PaceMode::AUTO;
    int target_fps  = 0;
    int floor_ratio = 0;
    int log_level   = (int)LogLevel::WARN;
};

// Returns true if the key is a hot-reloadable one and was applied.
static bool apply_dynamic_kv(DynCfg& c, const char* key, const char* val) {
    if (!key || !val || !*val) return false;
    if      (!strcmp(key, "FLM_MODE") || !strcmp(key, "FLM_PROFILE"))
        c.mode = (int)parse_mode(val);
    else if (!strcmp(key, "FLM_TARGET_FPS"))
        c.target_fps = std::clamp(atoi(val), 0, 1000);
    else if (!strcmp(key, "FLM_FLOOR_RATIO")) {
        int r = atoi(val);
        c.floor_ratio = r <= 0 ? 0 : std::clamp(r, 500, 1000);
    }
    else if (!strcmp(key, "FLM_LOG_LEVEL")) {
        if      (!strcasecmp(val, "DEBUG")) c.log_level = (int)LogLevel::DEBUG;
        else if (!strcasecmp(val, "INFO"))  c.log_level = (int)LogLevel::INFO;
        else if (!strcasecmp(val, "WARN"))  c.log_level = (int)LogLevel::WARN;
        else if (!strcasecmp(val, "ERROR")) c.log_level = (int)LogLevel::ERR;
    }
    else return false;
    return true;
}

using KvList = std::vector<std::pair<std::string, std::string>>;

// FLM_PROFILE first, so an explicit FLM_MODE in the same source wins.
static void apply_kv_list(DynCfg& c, const KvList& kv, bool from_file) {
    for (const auto& [k, v] : kv)
        if (k == "FLM_PROFILE") apply_dynamic_kv(c, k.c_str(), v.c_str());
    for (const auto& [k, v] : kv) {
        if (k == "FLM_PROFILE") continue;
        if (apply_dynamic_kv(c, k.c_str(), v.c_str())) continue;
        if (in_list(k.c_str(), k_legacy_keys))
            warn_key_once(k.c_str(), "removed in v3 (auto-tuned now) — ignored");
        else if (from_file && in_list(k.c_str(), k_loadtime_keys))
            warn_key_once(k.c_str(), "load-time only — has no effect in FLM_CONFIG");
        else if (!in_list(k.c_str(), k_loadtime_keys))
            warn_key_once(k.c_str(), "unknown FLM key — ignored");
    }
}

// '#' comments, KEY=VALUE lines, surrounding whitespace trimmed.
static KvList read_config_file(const char* path) {
    KvList out;
    FILE* f = fopen(path, "r");
    if (!f) return out;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        // [V31-7] Over-long line: drop the rest instead of parsing it as a new line.
        const size_t len = strlen(line);
        if (len && line[len - 1] != '\n' && !feof(f)) {
            int ch;
            while ((ch = fgetc(f)) != EOF && ch != '\n') {}
            continue;
        }
        char* p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '#' || *p == '\n' || *p == '\0') continue;
        char* eq = strchr(p, '=');
        if (!eq) continue;
        *eq = '\0';
        char* ke = eq;
        while (ke > p && (ke[-1] == ' ' || ke[-1] == '\t')) *--ke = '\0';
        char* val = eq + 1;
        while (*val == ' ' || *val == '\t') val++;
        size_t n = strlen(val);
        while (n && (val[n-1] == '\n' || val[n-1] == '\r' ||
                     val[n-1] == ' '  || val[n-1] == '\t')) val[--n] = '\0';
        out.emplace_back(p, val);
    }
    fclose(f);
    return out;
}

// Snapshotted once at init (getenv is not reload-thread safe, and a running
// process's env can't change from outside anyway). Every FLM_* var is kept so
// unknown/legacy names can be reported.
extern char** environ;
static KvList& g_env_snapshot = *new KvList;   // [V31-6] leaked

static void snapshot_env() {
    for (char** e = environ; e && *e; ++e) {
        if (strncmp(*e, "FLM_", 4) != 0) continue;
        const char* eq = strchr(*e, '=');
        if (!eq) continue;
        g_env_snapshot.emplace_back(std::string(*e, (size_t)(eq - *e)), eq + 1);
    }
}

// [V3-8] defaults → env → file. Removing a line from the file reverts it.
static void reload_dynamic_config() {
    DynCfg c;
    apply_kv_list(c, g_env_snapshot, false);
    if (!g_config.config_path.empty())
        apply_kv_list(c, read_config_file(g_config.config_path.c_str()), true);
    g_config.mode.store(c.mode);
    g_config.target_fps.store(c.target_fps);
    g_config.floor_ratio.store(c.floor_ratio);
    g_log_level.store(c.log_level);
}

static std::atomic<bool> g_reload_flag{false};
static void sigusr1_handler(int) { g_reload_flag.store(true, std::memory_order_relaxed); }

static inline int64_t now_ns();

// [V3-18] SIGUSR1 is unreliable under Wine/Proton: ntdll installs its own
// SIGUSR1 handler before the layer loads, so the SIG_DFL check in init_config
// leaves ours uninstalled and `kill -USR1` never reaches the reload path.
// Second trigger: poll FLM_CONFIG's mtime at <= 1 Hz, same flag, same reload.
static std::atomic<int64_t> g_cfg_next_poll_ns{0};
static std::atomic<int64_t> g_cfg_mtime_ns{-1};   // -1 = baseline not taken yet
static void poll_config_mtime() {
    if (g_config.config_path.empty()) return;
    const int64_t t = now_ns();
    int64_t next = g_cfg_next_poll_ns.load(std::memory_order_relaxed);
    if (t < next ||
        !g_cfg_next_poll_ns.compare_exchange_strong(next, t + 1'000'000'000LL,
                                                    std::memory_order_relaxed))
        return;
    struct stat sb;
    if (stat(g_config.config_path.c_str(), &sb) != 0) return;
    const int64_t mt = (int64_t)sb.st_mtim.tv_sec * 1'000'000'000LL + sb.st_mtim.tv_nsec;
    const int64_t prev = g_cfg_mtime_ns.exchange(mt, std::memory_order_relaxed);
    if (prev != -1 && prev != mt) g_reload_flag.store(true, std::memory_order_relaxed);
}

static inline void maybe_reload() {
    poll_config_mtime();
    if (g_reload_flag.load(std::memory_order_relaxed) &&
        g_reload_flag.exchange(false, std::memory_order_relaxed)) {
        reload_dynamic_config();
        FLM_LOG(LogLevel::INFO, "Config reload: mode=%s fps=%d floor_ratio=%d",
                mode_name(g_config.mode.load()), g_config.target_fps.load(),
                g_config.floor_ratio.load());
    }
}

static void reserve_global_maps();

#ifdef FLM_PGO_INSTRUMENTED
static void sigusr2_handler(int);
#endif

static void init_config() {
    std::call_once(g_config_flag, []() {
        const char* e;
        if ((e = getenv("FLM_MFG_MULTIPLIER"))) g_config.mfg_mult_env = std::clamp(atoi(e), 0, 4);
        if ((e = getenv("FLM_RT_PRIORITY")))    g_config.rt_priority  = std::clamp(atoi(e), 0, 99);
        if ((e = getenv("FLM_MEASURE_CPU")))    g_config.measure_cpu  = e;
        if ((e = getenv("FLM_STATS")))          g_config.stats        = (atoi(e) != 0);
        if ((e = getenv("FLM_CSV")))            g_config.csv_path     = e;
        if ((e = getenv("FLM_CONFIG")))         g_config.config_path  = e;
        if ((e = getenv("FLM_LOG_FILE"))) {
            if (FILE* f = fopen(e, "a")) {
                setvbuf(f, nullptr, _IOFBF, FlmConst::LOG_STDIO_BUF);
                g_log_file = f;
            }
        }

        snapshot_env();
        reload_dynamic_config();

        struct sigaction old{};
        if (sigaction(SIGUSR1, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr1_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR1, &sa, nullptr);
        }

#ifdef FLM_PGO_INSTRUMENTED
        if (sigaction(SIGUSR2, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr2_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR2, &sa, nullptr);
        }
        FLM_LOG(LogLevel::WARN,
                "PGO INSTRUMENTED build active — periodic (60s) + SIGUSR2 .gcda flush");
#endif

        reserve_global_maps();

        FLM_LOG(LogLevel::INFO,
                "Config: mode=%s fps=%d floor_ratio=%d mfg_env=%d rt=%d csv=%s config=%s",
                mode_name(g_config.mode.load()), g_config.target_fps.load(),
                g_config.floor_ratio.load(), g_config.mfg_mult_env, g_config.rt_priority,
                g_config.csv_path.empty()    ? "(none)" : g_config.csv_path.c_str(),
                g_config.config_path.empty() ? "(none)" : g_config.config_path.c_str());
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
// adaptive spin margin (p75 of measured oversleep, 30–500µs).
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
    constexpr int     RING           = 16;
    constexpr int64_t MARGIN_MIN     = 30'000LL;
    constexpr int64_t MARGIN_MAX     = 500'000LL;   // [FIX-46] was 2ms
    constexpr int64_t DEFAULT_MARGIN = 100'000LL;   // [FIX-73] warm-start blend base
    // [FIX-67] Hard bound on the final busy-spin. The loop condition already
    // guarantees at most one margin's worth of spinning, but only if the
    // monotonic clock behaves; under a VM clocksource glitch or a
    // CLOCK_MONOTONIC discontinuity the spin would run inside the game's
    // present call with nothing to stop it. Cheap insurance.
    constexpr int64_t SPIN_CAP_NS    = 2'000'000LL;
}
static std::atomic<int64_t> g_os_ring[FlmSpin::RING];   // zero-init
static std::atomic<int>     g_os_idx{0};                // one present thread in practice;
static std::atomic<int>     g_os_cnt{0};                // atomics keep the rare multi-
                                                        // present-thread case UB-free
static std::atomic<int64_t> g_spin_margin{FlmSpin::DEFAULT_MARGIN};   // cached p75-derived margin (ns)

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
    int64_t want = p75 + p75 / 2 + 20'000;
    // [FIX-73] Warm start. Until the ring is full the p75 is computed over a
    // handful of samples and the margin used to snap from the 100µs default to
    // that estimate in a single step — a one-frame timing discontinuity right
    // at the moment the gate starts working, i.e. exactly where a stutter is
    // most noticeable (level load, alt-tab back in). Blend from the default
    // toward the estimate in proportion to how much evidence we actually have.
    if (n < FlmSpin::RING)
        want = (want * n + FlmSpin::DEFAULT_MARGIN * (FlmSpin::RING - n)) / FlmSpin::RING;
    g_spin_margin.store(std::clamp<int64_t>(want,
                                            FlmSpin::MARGIN_MIN, FlmSpin::MARGIN_MAX),
                        std::memory_order_relaxed);
}

static void precise_wait_absolute(int64_t target) {
    if (target <= 0) return;
    // [V31-8] Default timer slack (50 µs) was most of the measured oversleep —
    // and therefore most of the busy-spin margin. Once per thread; RT threads
    // ignore slack anyway.
    thread_local bool slack_set = false;
    if (!slack_set) {
        slack_set = true;
        prctl(PR_SET_TIMERSLACK, FlmConst::TIMER_SLACK_NS, 0UL, 0UL, 0UL);
    }
    const int64_t spin = g_spin_margin.load(std::memory_order_relaxed);   // p75-derived, always adaptive
    for (;;) {
        int64_t left = target - now_ns();
        if (left <= spin) break;
        int64_t wake = target - spin;
        timespec ts;
        ts.tv_sec  = wake / 1'000'000'000LL;
        ts.tv_nsec = wake % 1'000'000'000LL;
        // [FIX-67] clock_nanosleep returns the error directly (it does NOT set
        // errno). EINTR means a signal arrived and we simply re-arm — that is
        // the normal, intended path. Any OTHER error is persistent by nature
        // (EINVAL from a malformed timespec after a clock anomaly, ENOTSUP
        // from an exotic kernel), and retrying it in a loop would hang the
        // game's present call outright rather than merely stutter. Bail to the
        // bounded spin and let the frame through.
        int rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr);
        if (rc != 0 && rc != EINTR) {
            FLM_LOG(LogLevel::DEBUG, "clock_nanosleep failed (%d) — spin fallback", rc);
            break;
        }
        const int64_t os = now_ns() - wake;   // signal interrupt → negative → skip
        if (os > 0) spin_margin_update(os);
    }
    // [FIX-67] Bounded busy-wait.
    const int64_t spin_deadline = now_ns() + FlmSpin::SPIN_CAP_NS;
    for (;;) {
        int64_t n = now_ns();
        if (n >= target || n >= spin_deadline) break;
        FLM_CPU_PAUSE();
    }
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
    PFN_vkWaitForPresentKHR           WaitForPresentKHR           = nullptr;
    PFN_vkCreateSwapchainKHR          CreateSwapchainKHR          = nullptr;
    PFN_vkDestroySwapchainKHR         DestroySwapchainKHR         = nullptr;
    PFN_vkGetDeviceQueue              GetDeviceQueue              = nullptr;
    PFN_vkGetDeviceQueue2             GetDeviceQueue2             = nullptr;
    bool                              has_present_wait            = false;
};

// [FIX-57] fwd decl — registry lives below, near the measurement thread.
static void csv_release_registered(const std::string& reg_key);

// ============================================================================
// SWAPCHAIN STATE
// ============================================================================
struct SwapchainState {
    VkDevice        device    = VK_NULL_HANDLE;
    VkSwapchainKHR  swapchain = VK_NULL_HANDLE;
    DeviceDispatch* disp      = nullptr;

    VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    uint32_t         width  = 0;
    uint32_t         height = 0;
    bool             pace_allowed = false;   // small helper swapchains → false
    bool             is_fifo      = false;

    std::jthread    measure_thread;

    // Atomics grouped by WRITER thread (a reader never dirties a line).
    //
    // Line P — written by the present thread.
    alignas(64) std::atomic<uint64_t> next_present_id{1};
                std::atomic<int64_t>  last_gate_wait_ns{0};   // MFG detection freeze
                std::atomic<uint32_t> present_seq{0};
                std::atomic<int>      frame_count{0};
                std::atomic<int>      eff_ratio{0};           // [V3-13] observability
                int64_t limiter_next_ns = 0;
                int64_t last_present_ns = 0;
                std::atomic<uint32_t> submit_seq{0};          // [V31-5] futex: id handed to the driver / stop
                std::atomic<bool>     reanchor_req{false};    // [V31-1] restart T/slot windows
                int     ratio_auto      = 0;
                int     ratio_m         = 1;                  // [V3-5] m the delta was learned at
                int     held_run        = 0;
                int     hold_streak     = 0;                  // [V31-1] consecutive DEEP holds
                int     pass_left       = 0;                  // [V31-1] unpaced presents left (re-anchor)
    //
    // Line M — written by the measurement thread.
    alignas(64) std::atomic<int64_t>  slot_interval_ns{FlmConst::DEFAULT_INTERVAL_NS};
                std::atomic<int64_t>  last_flip_ns{0};
                std::atomic<int>      eff_mfg{1};
                std::atomic<int>      hitch_left{0};          // flips of suspended pacing
                std::atomic<bool>     probe_active{false};
                std::atomic<bool>     fifo_vrr{false};        // [V3-2] latched verdict
                std::atomic<bool>     detect_ready{false};    // [V3-14] first m estimate done

    // Measurement-thread-only state (no atomics needed).
    alignas(64) int64_t cyc_win[FlmConst::CYC_RING] = {};
    int     cyc_idx         = 0;
    int     cyc_count       = 0;
    int     cyc_since_hitch = 0;
    int64_t real_win[FlmConst::REAL_WINDOW] = {};
    int     real_idx        = 0;
    int     real_count      = 0;
    int64_t real_median_cache = FlmConst::DEFAULT_INTERVAL_NS;
    int64_t probe_last_ns   = 0;
    int     probe_left      = 0;
    int64_t slot_win[FlmConst::SLOT_WINDOW] = {};
    int     slot_idx        = 0;
    int     slot_count      = 0;
    int64_t slot_sum        = 0;
    int64_t slot_mean_ns    = FlmConst::DEFAULT_INTERVAL_NS;
    int     mfg_small_cnt   = 0;
    int     mfg_total_cnt   = 0;
    // [V3-2] FIFO classifier
    int64_t fifo_win[FlmConst::FIFO_WIN] = {};
    int     fifo_n          = 0;
    int     fifo_vrr_votes  = 0;
    // stats
    int64_t stat_last_ns    = 0;
    int64_t stat_sum_ns     = 0;
    int64_t stat_max_ns     = 0;
    int     stat_frames     = 0;
    int     stat_fake       = 0;
    int     stat_hitch      = 0;
    std::unique_ptr<int64_t[]> stat_ring;
    int     stat_ring_n     = 0;
    // CSV — owned by the measurement thread [V3-12]
    FILE*   csv_fp = nullptr;
    std::string csv_reg_key;
    struct CsvRow {
        int64_t  flip_ns, interval_ns;
        int      is_fake, is_hitch;
        uint32_t slot;
        int      mfg;
        int64_t  slot_mean_ns;
        int      pacing;
    };
    CsvRow  csv_buf[FlmConst::CSV_BUFFER];
    int     csv_n = 0;
    int64_t csv_last_sync_ns = 0;

    SwapchainState(VkDevice dev, VkSwapchainKHR sc, DeviceDispatch* d)
        : device(dev), swapchain(sc), disp(d) {}

    // [V31-5/6] Stop the measurement thread: request_stop alone can't reach a
    // thread asleep on submit_seq, so bump + notify it as well.
    void stop_measurement() {
        if (!measure_thread.joinable()) return;
        measure_thread.request_stop();
        submit_seq.fetch_add(1, std::memory_order_seq_cst);
        submit_seq.notify_all();
        measure_thread.join();
    }

    // [V31-6] Join BEFORE touching the CSV: the thread owns it while it runs.
    ~SwapchainState() { stop_measurement(); csv_close(); }

    // [V3-7]
    static int64_t hitch_threshold(int64_t T) {
        int64_t thr = std::max<int64_t>((T * 3) / 2, T + FlmConst::HITCH_ABS_NS);
        return std::min<int64_t>(thr, T + FlmConst::HITCH_CAP_NS);
    }
    static int hitch_recovery(int m, bool latency) {
        return latency ? std::max(2, m + 1) : std::max(4, 2 * m + 2);
    }

    void real_median_recompute() {
        int n = std::min(real_count, FlmConst::REAL_WINDOW);
        if (n == 0) { real_median_cache = FlmConst::DEFAULT_INTERVAL_NS; return; }
        int64_t tmp[FlmConst::REAL_WINDOW];
        std::copy(real_win, real_win + n, tmp);
        std::nth_element(tmp, tmp + n / 2, tmp + n);
        real_median_cache = tmp[n / 2];
    }

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
    // Periodic fflush: small writes every 5s instead of one 1 MB burst, and
    // the file is never more than 5s behind if the game is killed.
    void csv_sync(int64_t t) {
        if (!csv_fp) return;
        if (csv_last_sync_ns == 0) { csv_last_sync_ns = t; return; }
        if (t - csv_last_sync_ns < FlmConst::CSV_SYNC_NS) return;
        if (csv_n) csv_flush();
        fflush(csv_fp);
        csv_last_sync_ns = t;
    }
    void csv_push(int64_t flip, int64_t interval, bool fake, bool hitch, uint32_t slot,
                  int mfg, int64_t slot_mean, bool pacing) {
        if (!csv_fp) return;
        csv_buf[csv_n++] = {flip, interval, fake ? 1 : 0, hitch ? 1 : 0, slot,
                            mfg, slot_mean, pacing ? 1 : 0};
        if (csv_n >= FlmConst::CSV_BUFFER) csv_flush();
    }
    void csv_close() {
        if (csv_fp) {
            if (csv_n) csv_flush();
            fclose(csv_fp);
            csv_fp = nullptr;
        }
        csv_release_registered(csv_reg_key);
        csv_reg_key.clear();
    }
};


// ============================================================================
// GLOBAL MAPS
// ============================================================================
// [V31-6] All tables are leaked on purpose (never destroyed at exit).
static std::shared_mutex g_inst_lock;
static std::unordered_map<VkInstance, InstanceDispatch>& g_inst_map =
    *new std::unordered_map<VkInstance, InstanceDispatch>;
// [item 2] dispatch_key(gpu/instance) → locate InstanceDispatch
static std::unordered_map<void*, VkInstance>& g_instkey_map =
    *new std::unordered_map<void*, VkInstance>;

static std::shared_mutex g_dev_lock;
static std::unordered_map<VkDevice, DeviceDispatch>& g_dev_map =
    *new std::unordered_map<VkDevice, DeviceDispatch>;

struct QueueData {
    VkDevice        device = VK_NULL_HANDLE;
    DeviceDispatch* disp   = nullptr;
};
static std::shared_mutex g_queue_lock;
static std::unordered_map<VkQueue, QueueData>& g_queue_map =
    *new std::unordered_map<VkQueue, QueueData>;
static std::atomic<uint64_t> g_queue_gen{0};   // [V3-11] bumps invalidate find_queue caches

static std::shared_mutex g_sc_lock;
static std::unordered_map<VkSwapchainKHR, std::shared_ptr<SwapchainState>>& g_sc_map =
    *new std::unordered_map<VkSwapchainKHR, std::shared_ptr<SwapchainState>>;

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
    if (st) st->stop_measurement();
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
static std::mutex& csv_registry_lock() { static std::mutex& m = *new std::mutex; return m; }
static std::unordered_map<std::string, CsvPathInfo>& csv_registry() {
    static auto& r = *new std::unordered_map<std::string, CsvPathInfo>; return r;   // [V31-6]
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

// ============================================================================
// MEASUREMENT THREAD
// ----------------------------------------------------------------------------
// Reads real flip timestamps via presentWait and publishes:
//   slot_interval_ns = median(T) / m   (T = cycle sum of the last m intervals)
//   eff_mfg          = frame-generation multiplier (detected or forced)
//   hitch_left       = flips of suspended pacing after a hitch
//   fifo_vrr         = FIFO swapchain shows a continuous (VRR) cadence
// No gating happens here; the gate runs in the present thread.
// ============================================================================

// [V3-2] FIFO cadence classifier. R = the fastest-flip cluster (p5). On a
// fixed-refresh panel every interval is ~k*R; on VRR below the ceiling the
// intervals are continuous. Only ever latches towards VRR: once pacing starts
// the cadence becomes uniform, which on its own would look "quantised".
static void fifo_classify(SwapchainState* st, int64_t interval_ns) {
    st->fifo_win[st->fifo_n++] = interval_ns;
    if (st->fifo_n < FlmConst::FIFO_WIN) return;
    st->fifo_n = 0;

    int64_t tmp[FlmConst::FIFO_WIN];
    std::copy(st->fifo_win, st->fifo_win + FlmConst::FIFO_WIN, tmp);
    const int k5 = FlmConst::FIFO_WIN / 20;
    std::nth_element(tmp, tmp + k5, tmp + FlmConst::FIFO_WIN);
    const int64_t R = tmp[k5];

    // FIFO on a fixed-refresh panel can only flip on vblank, so no interval
    // is shorter than one refresh period. A cluster faster than any real
    // panel (540 Hz ≈ 1.85 ms) proves the display is not fixed-rate — this
    // also keeps a bimodal MFG pattern whose long interval happens to be an
    // integer multiple of the short one from passing as "quantised".
    int quant = 0;
    if (R >= FlmConst::FIFO_MIN_REFRESH_NS) {
        for (int i = 0; i < FlmConst::FIFO_WIN; i++) {
            const int64_t iv  = st->fifo_win[i];
            const int64_t k   = std::max<int64_t>(1, (iv + R / 2) / R);
            const int64_t err = std::llabs(iv - k * R);
            if (err * 100 < R * FlmConst::FIFO_QUANT_TOL_PCT) quant++;
        }
    }
    const int pct = quant * 100 / FlmConst::FIFO_WIN;
    if (pct < FlmConst::FIFO_VRR_MAX_PCT) {
        if (++st->fifo_vrr_votes >= FlmConst::FIFO_VRR_CONFIRM) {
            st->fifo_vrr.store(true, std::memory_order_relaxed);
            FLM_LOG(LogLevel::INFO, "FIFO swapchain shows a VRR cadence "
                    "(%d%% quantised to %.2f ms) — pacing enabled", pct, (double)R / 1e6);
        }
    } else {
        st->fifo_vrr_votes = 0;
    }
}

static void mfg_publish_estimate(SwapchainState* st, int m, const char* tag) {
    double p = (double)st->mfg_small_cnt / (double)st->mfg_total_cnt;
    int mhat = (p < 0.99) ? (int)std::lround(1.0 / (1.0 - p)) : 4;
    mhat = std::clamp(mhat, 1, 4);
    if (mhat != m) {
        FLM_LOG(LogLevel::INFO, "MFG multiplier%s: %d -> %d", tag, m, mhat);
        st->eff_mfg.store(mhat, std::memory_order_relaxed);
        // [V3-16] The T window holds sums of m_old intervals; under m_new they
        // are wrong by an unknown factor (was the multiplier wrong, or did
        // the game change it?). v2 kept them: after 4x→1x the slot stayed at
        // 4T and the floor held every frame (~18 FPS for a second, simulated).
        // Re-bootstrap from the pattern-insensitive slot mean instead.
        st->real_count = st->real_idx = 0;
        st->cyc_since_hitch = 0;
        st->real_median_cache = st->slot_mean_ns * mhat;
    }
    st->mfg_small_cnt = 0;
    st->mfg_total_cnt = 0;
    st->detect_ready.store(true, std::memory_order_relaxed);
}

static void probe_schedule(SwapchainState* st, int64_t tnow) {
    if (st->probe_last_ns == 0) { st->probe_last_ns = tnow; return; }
    if (tnow - st->probe_last_ns >= FlmConst::PROBE_PERIOD_NS) {
        st->probe_left = FlmConst::PROBE_FLIPS;
        st->probe_active.store(true, std::memory_order_relaxed);
        FLM_LOG(LogLevel::DEBUG, "MFG probe: half-floor for %d flips", FlmConst::PROBE_FLIPS);
    }
}
static void probe_end(SwapchainState* st, int64_t tnow) {
    st->probe_last_ns = tnow;
    st->probe_active.store(false, std::memory_order_relaxed);
}

static void csv_open_lazy(SwapchainState* st) {
    if (g_config.csv_path.empty() || st->csv_fp) return;
    st->csv_fp = csv_open_registered(g_config.csv_path, st->csv_reg_key);
    if (!st->csv_fp)
        FLM_LOG(LogLevel::WARN, "FLM_CSV fopen('%s') failed: %s",
                g_config.csv_path.c_str(), strerror(errno));
}

// One real flip interval. All of this used to be inline in the thread loop.
static void process_interval(SwapchainState* st, int64_t tnow, int64_t interval_ns) {
    // [V31-1] The gate saw a sustained hold and is letting frames through
    // unpaced: drop everything the floor shaped and rebuild T and the slot
    // mean from the natural cadence (warm=false → no clamp against old T).
    if (st->reanchor_req.load(std::memory_order_relaxed) &&
        st->reanchor_req.exchange(false, std::memory_order_acquire)) {
        st->real_count = st->real_idx = 0;
        st->cyc_since_hitch = 0;
        st->slot_count = st->slot_idx = 0;
        st->slot_sum = 0;
        std::fill(st->slot_win, st->slot_win + FlmConst::SLOT_WINDOW, 0);
        st->mfg_small_cnt = st->mfg_total_cnt = 0;
    }
    const int     m      = st->eff_mfg.load(std::memory_order_relaxed);
    const int64_t T_prev = st->real_median_cache;
    const bool    warm   = st->real_count >= 4;
    // [V3-15] Reference period for hitch/clamp decisions. The cycle-sum median
    // can be far too small: before m is known (m=1) it is the median of RAW
    // intervals, i.e. ε at 4x. v2 then flagged every real frame as a hitch,
    // a hitch blocks the cycle sums, so T could never recover — the pacer was
    // dead for the whole session. slot_mean*m is pattern-insensitive (mean of
    // a full lcm(1..4) window = T/m) and bounds it from below.
    const int64_t T_ref  = std::max<int64_t>(T_prev, st->slot_mean_ns * std::max(1, m));
    const bool    lat    = g_config.mode.load(std::memory_order_relaxed) ==
                           (int)PaceMode::LATENCY;

    // ── Hitch first, from the raw interval ─────────────────────────────────
    const bool is_hitch = warm && interval_ns > SwapchainState::hitch_threshold(T_ref);
    if (is_hitch) {
        st->hitch_left.store(SwapchainState::hitch_recovery(m, lat), std::memory_order_relaxed);
        st->cyc_since_hitch = 0;   // keep the ring, only suppress sums spanning the hitch
    } else {
        int hl = st->hitch_left.load(std::memory_order_relaxed);
        if (hl > 0) st->hitch_left.store(hl - 1, std::memory_order_relaxed);

        // Cycle sum: the last m raw intervals add up to T regardless of how
        // the pacer distributed them (ε+(T-ε) = floor+(T-floor) = m*T/m = T).
        st->cyc_win[st->cyc_idx] = interval_ns;
        st->cyc_idx = (st->cyc_idx + 1) % FlmConst::CYC_RING;
        if (st->cyc_count < FlmConst::CYC_RING) st->cyc_count++;
        if (st->cyc_since_hitch < FlmConst::CYC_RING) st->cyc_since_hitch++;

        const int mm = std::clamp(m, 1, FlmConst::CYC_RING);
        if (st->cyc_count >= mm && st->cyc_since_hitch >= mm) {
            int64_t T_est = 0;
            for (int k = 0; k < mm; k++)
                T_est += st->cyc_win[(st->cyc_idx - 1 - k + FlmConst::CYC_RING) %
                                     FlmConst::CYC_RING];
            if (warm) T_est = std::clamp(T_est, T_ref / 4, T_ref * 2);
            st->real_win[st->real_idx] = T_est;
            st->real_idx = (st->real_idx + 1) % FlmConst::REAL_WINDOW;
            if (st->real_count < FlmConst::REAL_WINDOW) st->real_count++;
            st->real_median_recompute();
        }
        if (st->is_fifo && !st->fifo_vrr.load(std::memory_order_relaxed))
            fifo_classify(st, interval_ns);
    }

    // Fake split — telemetry only.
    bool is_fake = false;
    if (m > 1 && warm && !is_hitch)
        is_fake = interval_ns < (T_prev * (m + 1)) / (2LL * m);

    // Slot mean over ALL intervals (= T/m): the MFG detector's reference.
    {
        int64_t safe_iv = std::clamp<int64_t>(interval_ns, 100'000LL,
                                              std::max<int64_t>(st->slot_mean_ns * 4, 100'000LL));
        st->slot_sum += safe_iv - st->slot_win[st->slot_idx];
        st->slot_win[st->slot_idx] = safe_iv;
        st->slot_idx = (st->slot_idx + 1) % FlmConst::SLOT_WINDOW;
        if (st->slot_count < FlmConst::SLOT_WINDOW) st->slot_count++;
        st->slot_mean_ns = st->slot_sum / st->slot_count;
    }

    // ── Multiplier ──────────────────────────────────────────────────────────
    // Generated frames flip much sooner than the slot mean (< 0.7*T/m), real
    // frames much later; p = share of short ones → m = 1/(1-p). Detection is
    // frozen while the gate is holding frames (paced intervals are uniform),
    // so a periodic probe re-opens a half-floor window to re-measure.
    const bool small = interval_ns * 10 < st->slot_mean_ns * 7;
    if (g_config.mfg_mult_env > 0) {
        if (m != g_config.mfg_mult_env)
            st->eff_mfg.store(g_config.mfg_mult_env, std::memory_order_relaxed);
        st->detect_ready.store(true, std::memory_order_relaxed);
        // Forced m: detection off, but the re-anchor window stays (it is the
        // only time the cadence is measured from intervals the floor did not
        // shape — the v2.8 ratchet fix depends on it).
        if (st->probe_left > 0) { if (--st->probe_left == 0) probe_end(st, tnow); }
        else probe_schedule(st, tnow);
    } else if (st->probe_left > 0) {
        if (small) st->mfg_small_cnt++;
        st->mfg_total_cnt++;
        if (--st->probe_left == 0) {
            if (st->mfg_total_cnt >= 16) mfg_publish_estimate(st, m, " (probe)");
            st->mfg_small_cnt = st->mfg_total_cnt = 0;
            probe_end(st, tnow);
        }
    } else {
        const bool gate_hot =
            (tnow - st->last_gate_wait_ns.load(std::memory_order_relaxed)) < 1'000'000'000LL;
        // [V3-14] Frozen at m=1 too: a floor holding frames at "m=1" settles
        // into a stable ~0.4T/0.6T split that hides 2x MFG from the detector
        // for good (simulated). The probe re-measures m from half-floor flips.
        if (gate_hot) {
            st->mfg_small_cnt = st->mfg_total_cnt = 0;
            probe_schedule(st, tnow);
        } else if (st->slot_count >= FlmConst::SLOT_WINDOW) {
            // [V3-17] Only against a full slot window: the first samples are
            // measured against a mean still settling (3x was detected as 4x).
            if (small) st->mfg_small_cnt++;
            if (++st->mfg_total_cnt >= FlmConst::MFG_DETECT_WINDOW)
                mfg_publish_estimate(st, m, "");
        }
    }

    // ── Publish slot = T/m for the floor gate ───────────────────────────────
    {
        const int mm = std::max(1, st->eff_mfg.load(std::memory_order_relaxed));
        st->slot_interval_ns.store(std::max<int64_t>(st->real_median_cache / mm,
                                                     FlmConst::MIN_FLOOR_NS),
                                   std::memory_order_relaxed);
    }

    // ── Stats + CSV ─────────────────────────────────────────────────────────
    const bool pacing = st->hitch_left.load(std::memory_order_relaxed) == 0 &&
                        !st->probe_active.load(std::memory_order_relaxed);
    if (!is_fake) {
        st->stat_sum_ns += interval_ns;
        st->stat_max_ns  = std::max(st->stat_max_ns, interval_ns);
        st->stat_frames++;
        if (is_hitch) st->stat_hitch++;
    } else {
        st->stat_fake++;
    }
    if (st->stat_ring && st->stat_ring_n < FlmConst::STAT_RING)
        st->stat_ring[st->stat_ring_n++] = interval_ns;
    st->csv_push(tnow, interval_ns, is_fake, is_hitch,
                 st->present_seq.load(std::memory_order_relaxed),
                 m, st->slot_mean_ns, pacing);
    st->csv_sync(tnow);

    if (g_config.stats && tnow - st->stat_last_ns >= FlmConst::STATS_INTERVAL_NS &&
        st->stat_frames > 0) {
        double avg_ms = ((double)st->stat_sum_ns / (double)st->stat_frames) / 1e6;
        double max_ms = (double)st->stat_max_ns / 1e6;
        double p99_ms = 0.0;
        if (st->stat_ring && st->stat_ring_n > 0) {
            int64_t* ring = st->stat_ring.get();
            const int n = st->stat_ring_n;
            const int k = (n * 99) / 100;
            std::nth_element(ring, ring + k, ring + n);
            p99_ms = (double)ring[k] / 1e6;
        }
        FLM_LOG(LogLevel::INFO,
            "STATS 5s: n=%d avg=%.2fms p99=%.2fms max=%.2fms fake=%d hitch=%d "
            "mfg=%d ratio=%d slot=%.2fms%s",
            st->stat_frames, avg_ms, p99_ms, max_ms, st->stat_fake, st->stat_hitch,
            st->eff_mfg.load(), st->eff_ratio.load(),
            (double)st->slot_interval_ns.load() / 1e6,
            !st->is_fifo ? "" : (st->fifo_vrr.load() ? " fifo=vrr" : " fifo=fixed"));
        st->stat_sum_ns = st->stat_max_ns = 0;
        st->stat_frames = st->stat_fake = st->stat_hitch = 0;
        st->stat_ring_n = 0;
        st->stat_last_ns = tnow;
    }
}

// [V31-6] Raw pointer: the state outlives the thread (~SwapchainState joins
// first), and the thread can never end up holding the last reference.
static void measurement_thread_fn(std::stop_token stoken, SwapchainState* st) {
    apply_thread_policies();
    if (g_config.stats)
        st->stat_ring = std::make_unique<int64_t[]>(FlmConst::STAT_RING);

    uint64_t wait_id         = std::max<uint64_t>(1, st->next_present_id.load(std::memory_order_relaxed));
    int64_t  last_display_ns = 0;
    bool     last_valid      = false;
    int      immediate_run   = 0;
    st->stat_last_ns = now_ns();

    for (;;) {
        // [V31-5] seq BEFORE the stop check and the id load: a present or a
        // stop that lands after this point changes seq, so the futex wait
        // below returns at once instead of missing the wake-up.
        const uint32_t seq = st->submit_seq.load(std::memory_order_acquire);
        if (stoken.stop_requested()) break;
        maybe_reload();
#ifdef FLM_PGO_INSTRUMENTED
        flm_gcov_periodic_dump();
#endif
        const uint64_t latest = st->next_present_id.load(std::memory_order_acquire);
        // [V31-5] Only ids already handed to QueuePresent go to the driver.
        // Waiting there for an id that was never submitted kept every
        // swapchain destroy blocked on the 50 ms timeout.
        if (wait_id >= latest) {
            st->submit_seq.wait(seq, std::memory_order_acquire);
            continue;
        }
        // [V3-4] Safety net only: a backlog is normally caught by [V3-3].
        if (latest > FlmConst::FF_GAP && wait_id + FlmConst::FF_GAP < latest) {
            wait_id    = latest - 1;
            last_valid = false;
        }

        const int64_t t0 = now_ns();
        VkResult r = st->disp->WaitForPresentKHR(st->device, st->swapchain,
                                                 wait_id, FlmConst::WAIT_TIMEOUT_NS);
        if (r == VK_TIMEOUT) {
            // Submitted but not shown within 50 ms (minimised, compositor
            // stall): back off, don't spin.
            last_valid = false;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
        }
        if (r == VK_ERROR_OUT_OF_DATE_KHR || r == VK_ERROR_SURFACE_LOST_KHR) {
            // [V31-5] Dead swapchain: sleep until the next present (or the
            // destroy's stop request) instead of a fixed 50 ms the join had
            // to sit through. One driver call per present at most.
            last_valid = false;
            st->submit_seq.wait(seq, std::memory_order_acquire);
            continue;
        }
        // [V31-4] VK_SUBOPTIMAL_KHR is a success code: the present completed.
        if (r != VK_SUCCESS && r != VK_SUBOPTIMAL_KHR) {
            FLM_LOG(LogLevel::DEBUG, "WaitForPresentKHR fatal: %d", (int)r);
            break;
        }

        const int64_t tnow = now_ns();
        // [V3-3] Non-blocking return = this id flipped before we waited, its
        // real flip time is unknown. One of them: MAILBOX replaced the image
        // (it completes together with the displayed one — the previous
        // timestamp already IS that flip). Two in a row: we are behind.
        if (tnow - t0 < FlmConst::IMMEDIATE_NS) {
            if (++immediate_run >= 2) last_valid = false;
            wait_id++;
            continue;
        }
        immediate_run = 0;

        st->last_flip_ns.store(tnow, std::memory_order_relaxed);
        csv_open_lazy(st);   // [V3-12] after the old swapchain is gone

        if (last_valid) process_interval(st, tnow, tnow - last_display_ns);

        last_display_ns = tnow;
        last_valid      = true;
        wait_id++;
    }

    st->csv_close();   // [V3-12] flush + close + registry release on the owner thread
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
    // [V3-19] Also capture the VALUES the app set: a struct present with
    // VK_FALSE leaves the feature off even though the extension name is enabled.
    bool chain_id_feat = false, chain_wait_feat = false;
    bool app_id_on = false, app_wait_on = false;
    for (const VkBaseInStructure* p = (const VkBaseInStructure*)pCreateInfo->pNext;
         p; p = p->pNext) {
        if (p->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR) {
            chain_id_feat = true;
            app_id_on = ((const VkPhysicalDevicePresentIdFeaturesKHR*)p)->presentId == VK_TRUE;
        }
        if (p->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR) {
            chain_wait_feat = true;
            app_wait_on = ((const VkPhysicalDevicePresentWaitFeaturesKHR*)p)->presentWait == VK_TRUE;
        }
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
    d.WaitForPresentKHR   = (PFN_vkWaitForPresentKHR)gdpa(*pDevice, "vkWaitForPresentKHR");
    d.CreateSwapchainKHR  = (PFN_vkCreateSwapchainKHR)gdpa(*pDevice, "vkCreateSwapchainKHR");
    d.DestroySwapchainKHR = (PFN_vkDestroySwapchainKHR)gdpa(*pDevice, "vkDestroySwapchainKHR");
    d.GetDeviceQueue      = (PFN_vkGetDeviceQueue)gdpa(*pDevice, "vkGetDeviceQueue");
    d.GetDeviceQueue2     = (PFN_vkGetDeviceQueue2)gdpa(*pDevice, "vkGetDeviceQueue2");

    // [item 1] Only use presentWait when it was safely enabled.
    // On the fallback path the extension was NOT enabled; WaitForPresentKHR
    // may be non-null but calling it is UB. created_with_wait guarantees this;
    // also safe if the app itself enabled both.
    // [V3-19] Effective feature state: the app's own value if it supplied the
    // struct, otherwise ours (TRUE only if our injected create succeeded).
    const bool id_on   = chain_id_feat   ? app_id_on   : created_with_wait;
    const bool wait_on = chain_wait_feat ? app_wait_on : created_with_wait;
    const bool ext_ok  = created_with_wait || (app_has_id && app_has_wait);
    bool safe_wait = ext_ok && id_on && wait_on;
    if (ext_ok && !safe_wait)
        FLM_LOG(LogLevel::INFO, "presentId/presentWait feature is off in the application's "
                "device features; PACER disabled (LIMITER still available)");
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
    g_queue_gen.fetch_add(1, std::memory_order_release);   // [V3-11]

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
    st->is_fifo      = (pCreateInfo->presentMode == VK_PRESENT_MODE_FIFO_KHR ||
                        pCreateInfo->presentMode == VK_PRESENT_MODE_FIFO_RELAXED_KHR);

    // Small auxiliary swapchains (launcher/overlay) are never paced. FIFO is
    // decided later from the measured cadence [V3-2].
    bool too_small = (st->width  < (uint32_t)FlmConst::MIN_SC_WIDTH ||
                      st->height < (uint32_t)FlmConst::MIN_SC_HEIGHT);
    st->pace_allowed = !too_small;

    if (too_small) {
        FLM_LOG(LogLevel::DEBUG, "Small swapchain %ux%u — pacing skipped",
                st->width, st->height);
    }

    // Measurement thread only when presentWait is available and swapchain is paceable.
    if (disp->has_present_wait && st->pace_allowed)
        st->measure_thread = std::jthread(measurement_thread_fn, st.get());   // [V31-6]

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
// GATE — runs in the present thread. It can only DELAY a present, never
// accelerate one: a target in the past means no wait at all.
// ============================================================================

// LIMITER: absolute timeline, soft slew on small debt, rebase on stalls.
// limiter_next_ns is the deadline of THIS present (0 = not started).
static void gate_limiter(SwapchainState* st, int fps, int64_t t) {
    const int64_t iv = 1'000'000'000LL / fps;
    // [V31-3] First present: it IS the anchor (v3.0 stored t+iv here and the
    // next call added another iv → the second frame waited two intervals).
    if (st->limiter_next_ns == 0) { st->limiter_next_ns = t; return; }
    st->limiter_next_ns += iv;
    const int64_t drift = st->limiter_next_ns - t;
    const int64_t tol   = std::clamp<int64_t>(iv / 4, 1'000'000LL, 4'000'000LL);
    // [V31-3] Stall / rate change: re-anchor to NOW. v3.0 re-anchored to
    // now+iv, adding a full interval of wait to a frame that was already late.
    if (drift < -2 * iv || drift > 4 * iv) { st->limiter_next_ns = t; return; }
    if (drift < -tol) st->limiter_next_ns -= drift / 8;

    const int64_t left     = st->limiter_next_ns - t;
    const int64_t max_wait = std::max<int64_t>(FlmConst::MAX_PACE_WAIT_NS, iv + iv / 2);
    if (left > 0 && left < max_wait) precise_wait_absolute(st->limiter_next_ns);
}

// FLOOR PACER: a present may not leave sooner than `floor` after the previous
// one. floor = slot * ratio, slot = T/m from the measurement thread.
static void gate_floor(SwapchainState* st, bool latency, int64_t t) {
    auto reset = [st] { st->last_present_ns = 0; st->held_run = 0; st->hold_streak = 0; };

    if (st->hitch_left.load(std::memory_order_relaxed) > 0) { reset(); return; }
    // No fresh measurement (id=0 presents, alt-tab, OUT_OF_DATE loop): the
    // slot is stale and would brake the game to an old cadence.
    const int64_t lf = st->last_flip_ns.load(std::memory_order_relaxed);
    if (lf == 0 || t - lf > FlmConst::MEAS_FRESH_NS) { reset(); return; }

    // [V3-14] Don't shape the cadence before the first unpaced m estimate:
    // pacing with the wrong m poisons the very samples detection needs.
    if (!st->detect_ready.load(std::memory_order_relaxed)) return;
    const int64_t slot = st->slot_interval_ns.load(std::memory_order_relaxed);
    if (slot <= 0) return;
    const int  m       = st->eff_mfg.load(std::memory_order_relaxed);
    // [V31-1] Re-anchor window: the measurement thread is rebuilding T from
    // these presents. m=1: unpaced (any floor would be measured back as T).
    // m>1: the probe's half floor — generated frames stay spaced, real frames
    // are no longer braked, and the cycle sum yields the new T regardless.
    const bool reanchoring = st->pass_left > 0;
    if (reanchoring) {
        st->pass_left--;
        if (m == 1) { reset(); return; }
    }
    const bool probing = reanchoring || st->probe_active.load(std::memory_order_relaxed);

    // [V3-5] A delta learned for another multiplier is meaningless here.
    if (m != st->ratio_m) { st->ratio_m = m; st->ratio_auto = 0; st->held_run = 0; }

    const int user = g_config.floor_ratio.load(std::memory_order_relaxed);
    const int base = user > 0 ? user : (latency ? FlmTune::RATIO_LATENCY : FlmTune::RATIO_AUTO);
    const int amax = latency ? FlmTune::AUTO_MAX_LAT : FlmTune::AUTO_MAX;

    // Relax faster than linearly with m: interpolated frames at m=3/4 share
    // one optical-flow pass and their cost variance stacks (field data: 4x
    // hitch rate 5.5 % → 0.01 % after this change).
    const int fixed = base - ((m > 1) ? FlmTune::MFG_STEP * (m - 1) * m / 2 : 0);
    // [V3-6] No frame generation → only runt frames are the target.
    const int ceil  = (m == 1) ? std::max(FlmTune::RATIO_CAP_M1, user) : 1000;
    // [V31-2] Anti-windup: ratio_auto may only span the range in which it
    // still moves the clamped ratio; anything beyond is invisible credit the
    // brake path would first have to burn off.
    const int a_lo = std::max(FlmTune::AUTO_MIN, 500 - fixed);
    const int a_hi = std::max(a_lo, std::min(amax, ceil - fixed));
    st->ratio_auto = std::clamp(st->ratio_auto, a_lo, a_hi);

    int ratio = fixed + (probing ? 0 : st->ratio_auto);
    ratio = std::clamp(ratio, 500, ceil);
    if (probing) {   // keep generated frames classifiable (< 0.7 slot)
        ratio = std::min(ratio / 2, FlmConst::PROBE_RATIO_CAP);
        st->held_run = 0;
    }
    int64_t floor = std::max<int64_t>((slot * ratio) / 1000, FlmConst::MIN_FLOOR_NS);

    // RATCHET GUARD (hard invariant): floor + overshoot must stay below the
    // slot, otherwise floor → measured interval → slot → floor ratchets the
    // frame rate down one overshoot per cycle (Alan Wake 2: stuck at 45 FPS).
    {
        const int64_t delta = g_spin_margin.load(std::memory_order_relaxed) +
                              FlmConst::RATCHET_MARGIN_NS;
        const int64_t cap = std::max<int64_t>(slot - delta, slot / 2);
        if (floor > cap) {
            floor = cap;
            if (!probing && st->ratio_auto > 0) st->ratio_auto -= 1;
        }
    }
    st->eff_ratio.store((int)((floor * 1000) / slot), std::memory_order_relaxed);

    if (st->last_present_ns == 0) { st->last_present_ns = t; return; }
    const int64_t since = t - st->last_present_ns;
    const bool    hold  = since < floor;

    // [V31-1] SUSTAINED HOLD → RE-ANCHOR. The slot comes from measured flips
    // and a held frame's flip is the floor itself, so once the game speeds up
    // the floor keeps reproducing the old cadence (loading screen → gameplay:
    // every frame held for most of the old frame time).
    //  m=1: a DEEP hold (arrived before half the floor) has no legitimate
    //       cause but a lone runt; 3 in a row = real frames queueing behind
    //       the floor.
    //  m>1: generated frames are deep holds by design, and the relaxed ratio
    //       (<1) follows moderate rises by itself within a few cycles (a
    //       re-anchor there only drains the queue in a burst — simulated).
    //       Only holds the MAX_PACE_WAIT_NS cap had to clip count: the slot
    //       is then stale by more than 20 ms. More than two cycles of them.
    // Release, and let the measurement re-learn T from unshaped presents.
    const bool stale_hold = (m == 1) ? since < floor / 2
                                     : floor - since > FlmConst::MAX_PACE_WAIT_NS;
    if (hold && !probing && stale_hold) {
        if (++st->hold_streak > 2 * m) {
            reset();
            st->pass_left = FlmConst::REAL_WINDOW + 2 * m;
            st->reanchor_req.store(true, std::memory_order_release);
            st->eff_ratio.store(0, std::memory_order_relaxed);
            FLM_LOG(LogLevel::DEBUG, "Floor re-anchor: cadence faster than slot %.2f ms (m=%d)",
                    (double)slot / 1e6, m);
            return;
        }
    } else {
        st->hold_streak = 0;
    }

    // CLOSED LOOP. Per cycle m-1 generated frames are legitimately held; the
    // m-th consecutive hold is the real frame being braked → loosen fast.
    // A pass with plenty of headroom means intervals are still uneven →
    // tighten slowly; a pass that barely clears the floor → loosen.
    if (!probing) {
        if (hold) {
            if (++st->held_run >= m) {
                st->ratio_auto -= 4 * std::max(1, m - 1);
                st->held_run = 0;
            }
        } else {
            st->held_run = 0;
            const int64_t head = since - floor;
            // One pass per cycle at m>1 (vs one per frame at m=1): scale the
            // tightening step so convergence time doesn't grow with m.
            if      (head > slot / 12) st->ratio_auto += std::max(1, m / 2);
            else if (head < slot / 50) st->ratio_auto -= 2;
        }
        st->ratio_auto = std::clamp(st->ratio_auto, a_lo, a_hi);   // [V31-2]
    }

    // Anchor to the actual post-wait time, not to the target: target
    // anchoring doubles per-interval variance (difference of two noise
    // samples) and a relative pacer has no grid to drift from.
    if (hold) {
        // [V31-1] One hold never exceeds MAX_PACE_WAIT_NS, whatever the slot.
        const int64_t target = std::min(st->last_present_ns + floor,
                                        t + FlmConst::MAX_PACE_WAIT_NS);
        if (target > t) {
            st->last_gate_wait_ns.store(t, std::memory_order_relaxed);
            precise_wait_absolute(target);
            t = now_ns();
        }
    }
    st->last_present_ns = t;
}

static void apply_gate(SwapchainState* st, bool has_wait) {
    if (!st->pace_allowed) return;
    st->eff_ratio.store(0, std::memory_order_relaxed);   // [V31-9] gate_floor overwrites
    const PaceMode mode = (PaceMode)g_config.mode.load(std::memory_order_relaxed);
    if (mode == PaceMode::OFF) return;

    const int64_t t   = now_ns();
    const int     fps = g_config.target_fps.load(std::memory_order_relaxed);
    if (fps > 0) { gate_limiter(st, fps, t); return; }
    st->limiter_next_ns = 0;
    if (mode == PaceMode::CAP) return;   // cap without a target: nothing to do

    // [V3-2] FIFO: pace only once the cadence proved to be VRR (or forced).
    if (!has_wait) return;
    if (st->is_fifo && mode != PaceMode::PRESENT &&
        !st->fifo_vrr.load(std::memory_order_relaxed))
        return;
    gate_floor(st, mode == PaceMode::LATENCY, t);
}

// [V3-11] Present-thread queue lookup cache (same generation scheme as the
// swapchain cache). Engines present from one queue on one thread.

static bool find_queue(VkQueue queue, QueueData& out) {
    thread_local VkQueue   c_q   = VK_NULL_HANDLE;
    thread_local uint64_t  c_gen = ~0ULL;
    thread_local QueueData c_qd{};
    const uint64_t gen = g_queue_gen.load(std::memory_order_acquire);
    if (queue == c_q && gen == c_gen && c_qd.disp) { out = c_qd; return true; }

    QueueData qd{};
    {
        std::shared_lock qlk(g_queue_lock);
        auto qit = g_queue_map.find(queue);
        if (qit != g_queue_map.end()) qd = qit->second;
    }
    if (!qd.disp) {   // queue obtained through a path we didn't see: resolve by dispatch key
        {
            std::shared_lock lk(g_dev_lock);
            void* key = dispatch_key((void*)queue);
            for (auto& [d, dd] : g_dev_map)
                if (dispatch_key((void*)d) == key) { qd.disp = &dd; qd.device = d; break; }
        }
        if (qd.disp) {
            std::unique_lock qlk(g_queue_lock);
            g_queue_map[queue] = qd;
        }
    }
    if (!qd.disp) return false;
    c_q = queue; c_gen = gen; c_qd = qd;
    out = qd;
    return true;
}

VK_LAYER_EXPORT VkResult VKAPI_CALL FLM_vkQueuePresentKHR(
    VkQueue queue, const VkPresentInfoKHR* pPresentInfo)
{
    maybe_reload();   // also covers the no-measurement-thread (limiter) case
#ifdef FLM_PGO_INSTRUMENTED
    flm_gcov_periodic_dump();
#endif

    QueueData qdata{};
    if (!find_queue(queue, qdata)) return VK_ERROR_DEVICE_LOST;

    const uint32_t sc_count = pPresentInfo->swapchainCount;
    const bool has_wait = qdata.disp->has_present_wait;

    // App's own VkPresentIdKHR (DXVK / vkd3d-proton pass one when they use
    // presentWait themselves) — track it instead of injecting ours.
    const VkPresentIdKHR* app_pid = nullptr;
    for (const VkBaseInStructure* p = (const VkBaseInStructure*)pPresentInfo->pNext; p; p = p->pNext)
        if (p->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) { app_pid = (const VkPresentIdKHR*)p; break; }

    uint64_t ids_stack[FlmConst::STACK_PRESENT_IDS];
    std::vector<uint64_t> ids_heap;
    uint64_t* present_ids = ids_stack;
    if (sc_count > FlmConst::STACK_PRESENT_IDS) { ids_heap.resize(sc_count, 0); present_ids = ids_heap.data(); }
    else std::fill(ids_stack, ids_stack + sc_count, 0ULL);

    bool any_id = false;
    for (uint32_t i = 0; i < sc_count; i++) {
        auto st = find_sc_state(pPresentInfo->pSwapchains[i]);
        if (!st) continue;

        if (app_pid) {
            // Monotonic: an app that resets/reuses ids must never move the
            // expected id backwards (measurement would wait forever).
            if (app_pid->pPresentIds && i < app_pid->swapchainCount) {
                const uint64_t id = app_pid->pPresentIds[i];
                if (id) {
                    const uint64_t want = id + 1;
                    uint64_t cur = st->next_present_id.load(std::memory_order_relaxed);
                    while (want > cur &&
                           !st->next_present_id.compare_exchange_weak(
                               cur, want, std::memory_order_relaxed)) {}
                }
            }
        }

        // Single gate, primary swapchain only.
        if (i == 0) {
            st->present_seq.fetch_add(1, std::memory_order_relaxed);
            const int fc = st->frame_count.load(std::memory_order_relaxed);
            if (fc < FlmConst::WARMUP_FRAMES)
                st->frame_count.store(fc + 1, std::memory_order_relaxed);
            else
                apply_gate(st.get(), has_wait);
        }

        if (has_wait && !app_pid) {
            present_ids[i] = st->next_present_id.fetch_add(1, std::memory_order_relaxed);
            any_id = true;
        }
        // [V31-5] Wake the measurement thread if it sleeps waiting for this
        // id (a plain load when nobody waits). After the gate, so a held
        // frame's id never reaches the driver wait early.
        if (has_wait && st->measure_thread.joinable()) {
            st->submit_seq.fetch_add(1, std::memory_order_release);
            st->submit_seq.notify_one();
        }
    }

    VkPresentIdKHR   present_id_info{};
    VkPresentInfoKHR modified = *pPresentInfo;
    if (any_id) {
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

// [FIX-72] One list, two consumers. The device-level entries were spelled out
// in both GetDeviceProcAddr and GetInstanceProcAddr; adding a hook meant
// editing both, and forgetting the second is a silent failure (the layer is
// simply bypassed for engines that resolve through that entry point).
#define FLM_DEVICE_INTERCEPTS()      \
    INTERCEPT(GetDeviceProcAddr);    \
    INTERCEPT(DestroyDevice);        \
    INTERCEPT(QueuePresentKHR);      \
    INTERCEPT(CreateSwapchainKHR);   \
    INTERCEPT(DestroySwapchainKHR);  \
    INTERCEPT(GetDeviceQueue);       \
    INTERCEPT(GetDeviceQueue2)

VK_LAYER_EXPORT PFN_vkVoidFunction VKAPI_CALL FLM_vkGetDeviceProcAddr(VkDevice device, const char* pName)
{
    FLM_DEVICE_INTERCEPTS();

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
    // [FIX-11] Device-level functions requested via GIPA must also go through
    // the layer. [FIX-72] Same list as GetDeviceProcAddr, defined once.
    FLM_DEVICE_INTERCEPTS();

    if (instance == VK_NULL_HANDLE) return nullptr;
    std::shared_lock lk(g_inst_lock);
    auto it = g_inst_map.find(instance);
    if (it == g_inst_map.end() || !it->second.GetInstanceProcAddr) return nullptr;
    return it->second.GetInstanceProcAddr(instance, pName);
}

#undef FLM_DEVICE_INTERCEPTS
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
// ENVIRONMENT VARIABLES (v3.1 — unchanged since v3.0)
// ----------------------------------------------------------------------------
//  ENABLE_LAYER_cpu_flip_meter=1   load the implicit layer
//  FLM_MODE=auto|latency|present|cap|off   (default auto; hot-reloadable)
//     auto    : FPS cap if FLM_TARGET_FPS>0, else floor pacer where it helps
//               (presentWait + MAILBOX/IMMEDIATE, or FIFO with a VRR cadence)
//     latency : like auto, looser floor + shorter hitch recovery
//     present : like auto, but also paces FIFO swapchains classified fixed-rate
//     cap     : limiter only (needs FLM_TARGET_FPS)   [alias: limiter]
//     off     : do nothing (A/B baseline)
//     (FLM_PROFILE=vrr|mfg|latency|cap|off still accepted)
//  FLM_TARGET_FPS=<n>     >0 = FPS cap (limiter). 0 = natural cadence.
//  FLM_FLOOR_RATIO=<n>    optional 500-1000: override the base floor ratio
//                         (auto 850, latency 780). Autotune still runs on top.
//  FLM_MFG_MULTIPLIER=0   0 = auto-detect, 1-4 = force        (load-time)
//  FLM_RT_PRIORITY=0      measurement thread SCHED_FIFO prio  (load-time)
//  FLM_MEASURE_CPU=0-3    measurement thread affinity, lists ok (load-time)
//  FLM_LOG_LEVEL=DEBUG|INFO|WARN|ERROR   (default WARN; hot-reloadable)
//  FLM_LOG_FILE=/path     (load-time; default stderr)
//  FLM_STATS=1            5 s summary at INFO: avg/p99/max, fake/hitch,
//                         mfg, effective ratio, FIFO verdict (load-time)
//  FLM_CSV=/path          per-flip dump (load-time): flip_ns,interval_ns,
//                         is_fake,is_hitch,slot,mfg,slot_mean_ns,pacing
//  FLM_CONFIG=/path       KEY=VALUE file re-read on SIGUSR1 (hot keys only)
// ============================================================================
