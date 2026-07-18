// ============================================================================
// FLM — Vulkan Flip Meter / Frame Pacing Layer  (v2.4 — "gerçek etki")
//
// TASARIM ÖZETİ
// -------------
// İki bağımsız yol:
//
//   1) LIMITER  — presentWait GEREKTİRMEZ. QueuePresent'te salt yerel saatle
//      absolute-timeline FPS sınırlayıcı (libstrangle mantığı). Her zaman
//      çalışır, MangoHud grafiğinde anında düz çizgi olarak görünür. Gözle
//      görülür, güvenli, deterministik etki. FLM_TARGET_FPS ister.
//
//   2) PACER    — presentWait VARSA. Ölçüm thread'i gerçek flip zamanlarını
//      okur, timeline tahmini kurar; MFG (frame-gen) çarpanı varsa üretilmiş
//      kareleri flip aralığına EŞİT dağıtır (slot pacing). VRR panelde düzgün
//      frametime için.
//
// ANTI-STUTTER KURALLARI (v1 placebo/stutter sebeplerinin çözümü):
//   * TEK KAPI: pacing yalnız TEK noktada (varsayılan Present). v1 hem Acquire
//     hem Present'te bekletiyordu → çift gecikme.
//   * GPU-BOUND BEKÇİSİ: oyun GPU-limitliyse CPU'da bekletmek kuyruğu boşaltıp
//     kötüleştirir. Ardışık kareler hedefi aşınca pacing otomatik kapanır.
//   * FIFO/vsync ATLAMA: FIFO zaten vsync'e kilitli; üstüne pacing = compositor
//     ile kavga. Yalnız MAILBOX/IMMEDIATE (VRR) pace edilir. Küçük yardımcı
//     swapchain'ler hiç pace edilmez.
//   * SOFT SLEW: timeline sapması hard-rebase yerine yumuşak düzeltilir.
//   * LEAD-BASED PRESENT: present tahmini flip'ten FLM_PRESENT_LEAD_NS önce
//     bırakılır (v1'in "+1 kare" hatalı formülü kaldırıldı).
//
// Kalıcı düzeltmeler (v1'den):
//   [FIX-1]  Hot-path shared_ptr kopyası (UAF).
//   [FIX-2]  DestroyDevice tüm state'leri stop+join.
//   [FIX-13] CreateDevice fallback'inde loader zinciri restore (MangoHud crash).
//   [FIX-14] pNext'te olan feature'lar tekrar enjekte edilmez.
//   [FIX-15] Oyunun kendi presentId'leri takip edilir (DXVK uyumu).
//
// v2.1 düzeltmeleri (performans / gecikme / akıcılık):
//   [FIX-16] SLOT ARALIĞI = TÜM aralıkların EMA'sı. m kare toplamda T sürer →
//            ortalama aralık = T/m; hem paced hem unpaced durumda doğru slot
//            genişliğini verir. v2 fake-filtreli EMA (≈T) kullanıyordu → MFG'de
//            pacer FPS'i m kat DÜŞÜRÜYORDU. Kaldırıldı.
//   [FIX-17] MFG autodetect: eşik artık slot-EMA'ya göre (interval < 0.7*ema).
//            v2'nin kabul-medyanı tabanlı eşiği matematiksel olarak hiç
//            tetiklenmiyordu (p≈0 → mhat=1). Kapı aktifken tespit DONDURULUR
//            (paced uniform aralıklar tespiti zehirler → salınım engeli).
//   [FIX-18] GPU-bound bekçisi yalnız FLM_TARGET_FPS>0 iken ve ham aralık değil
//            slot-EMA üzerinden çalışır. v2'de MFG'nin bimodal aralıkları
//            bekçiyi anında tetikleyip pacing'i kapatıyordu. fps=0 doğal
//            cadence'ta hedef zaten ölçümden türetildiği için bekçi anlamsız.
//   [FIX-19] "interval > 2.5*avg → is_fake" dalı kaldırıldı: büyük HITCH'ler
//            fake sayılıp hitch tespitinden kaçıyordu → hitch sırasında pacing
//            devam ediyordu (görünür stutter).
//   [FIX-20] Kapı bekleme tavanı artık aralığa göreli (max(20ms, 1.5*iv)).
//            Sabit 20ms tavan FPS<=50 hedeflerde limiter'ı tamamen NO-OP
//            yapıyordu.
//   [FIX-21] Canlı ayar GERÇEK: FLM_CONFIG=<dosya> (KEY=VALUE) + SIGUSR1.
//            v2 handler'ı getenv okuyorduk — çalışan sürecin ortamı dışarıdan
//            değişemez (işlevsiz) ve getenv async-signal-safe değil (UB).
//            Handler artık yalnız atomik bayrak set eder; reload ölçüm/present
//            thread'inde yapılır.
//   [FIX-22] vkAcquireNextImage2KHR intercept edildi (bu yolu kullanan
//            motorlarda warmup sayacı hiç ilerlemiyordu → kapı hiç açılmıyordu).
//   [FIX-23] Ölü state temizliği (gate_target_ns / base_flip_ns).
//
// v2.2 düzeltmeleri (üç bağımsız kod incelemesinin süzülmüş sonuçları):
//   [FIX-24] PACER lead klempi: lead >= iv/2 olunca hedef geçmişe düşüp kapı
//            sessizce no-op oluyordu (örn. yüksek FPS + varsayılan 1ms lead).
//            Artık lead = min(FLM_PRESENT_LEAD_NS, iv/2).
//   [FIX-25] Config dosyasında değerin BAŞINDAKİ boşluk trim edilmiyordu:
//            "FLM_MODE= present" parse edilemiyordu (string karşılaştırma).
//   [FIX-26] Reload artık getenv çağırmaz: env init'te bir kez snapshot'lanır
//            (POSIX getenv reload thread'lerinde teorik veri yarışı + env
//            zaten dışarıdan değişemez). Semantik aynı: snapshot + dosya,
//            dosya kazanır; satır silinirse env değerine geri döner.
//   [FIX-27] Ölü state temizliği: timeline_target_ns, app_owns_present_id,
//            filtered_interval_ns EMA'sı (yalnız di_count==0 fallback'inde
//            okunuyordu — o anda hiç güncellenmemiş = sabit). stat_fake →
//            stat_fake_hitch (fake+hitch topluyor, isim yanıltıcıydı).
//   [FIX-28] Hot-path false sharing: limiter_next_ns (present thread) ile
//            ölçüm thread'inin her karede yazdığı alanlar aynı cache-line'ı
//            paylaşıyordu. İki blok alignas(64) ile ayrıldı.
//   [FIX-29] Log: fflush yalnız INFO ve üzeri. DEBUG tam tamponlu (64KB) —
//            DEBUG açıkken kare başına flush maliyeti kalktı. Not: crash'te
//            son DEBUG satırları tamponda kalabilir (normal çıkışta flush olur).
//   [FIX-30] CSV: 1MB stdio tamponu + flush'ta fflush yok → csv_flush artık
//            salt bellek formatlama; disk write() ancak tampon dolunca (~26k
//            satır) gerçekleşir. Ölçüm thread'inin zamanlaması I/O'dan korunur.
//   [FIX-31] CSV'ye telemetri kolonları: eff_mfg, slot_mean_ns, pacing —
//            MFG tespiti ve GPU-bound bekçisinin regresyon analizi için.
//   [FIX-32] FLM_STATS_INTERVAL=<sn> (hot-reload edilebilir, varsayılan 5).
//   [FIX-33] FLM_TARGET_FPS [0,1000] klempi (atoi taşması / iv=0 koruması) ve
//            map'lere başlangıç reserve()'i.
//
// v2.3 düzeltmeleri (akıcılık + input lag):
//   [FIX-37] FLOOR-PACING DONMA/FREN SARMALI. real_win yalnız NON-FAKE
//            aralıklarla besleniyordu; floor etkin ve m>1 iken TÜM aralıklar
//            (uniform ≈T/m ve real'in kalan payı) fake eşiğinin (≈0.75T)
//            ALTINA düşer → real_win + kabul-medyanı tamamen DONAR → slot_iv
//            eski T₀'da kilitlenir. VRR'de FPS yükselince floor bayat kalır,
//            kapı her present'i frenler; frenlenmiş aralıklar da fake sınıfında
//            kaldığı için tahmin kendini asla düzeltemez (pozitif kilit) —
//            FIX-36'nın yok etmeye çalıştığı "mutlak grid freni" kalıcı geri
//            geliyordu. ÇÖZÜM: T tahmini artık fake filtresinden bağımsız,
//            faz-duyarsız DÖNGÜ TOPLAMI ile: son m HAM aralığın toplamı ≈ T
//            (paced/unpaced/bimodal her durumda; ε+(T-ε)=T). Her flip'te
//            güncellenir → FPS değişimini frenlemeden takip eder, fren
//            oluşursa negatif geri besleme ile kendini bırakır. Fake sınıfı
//            yalnız istatistik/CSV için kaldı. display_intervals medyanı
//            (tek tüketicisi buydu) kaldırıldı; hitch eşiği ve fake split de
//            bu canlı T tahminine bağlandı (bayat medyanla kaçan hitch'ler).
//   [FIX-38] FIX-36 false-sharing regresyonu: real_win/real_idx/real_count
//            present-thread cache-line'ına (limiter_next_ns yanına) konmuştu
//            ama bu alanlara HER karede ÖLÇÜM thread'i yazıyor → FIX-28'in
//            çözdüğü cache-line ping-pong'u geri gelmişti. Ölçüm bloğuna
//            taşındı; present satırında yalnız present-thread alanları kaldı.
//   [FIX-39] ADAPTİF SPİN: kernel uykusunun gerçek uyanma gecikmesi (oversleep)
//            sönümlü-maksimum ile izlenir; spin payı buna göre ayarlanır.
//            Yüklü sistemde sabit 150µs pay yetmeyince kapı GEÇ kalıyordu
//            (floor kaçırılır → jitter spike); hassas sistemde ise her karede
//            boşa spin yakılıyordu. FLM_SPIN_ADAPT=0 → eski sabit davranış,
//            FLM_SPIN_NS=0 → saf uyku (değişmedi).
//   [FIX-40] Düşük-FPS ısınma kilidi: hitch eşiği varsayılan 16.6ms tabanla
//            başladığından ~30 FPS oyunlarda İLK kareler hitch sayılıp tahmin
//            penceresi hiç ısınamıyor, pacing kalıcı kapalı kalabiliyordu.
//            Pencere ısınana kadar (4 örnek) hitch sınıflandırması bastırılır.
//
// v2.4 düzeltmeleri (akıcılık — konsept doğrulama incelemesi):
//   [FIX-42] fps>0 iken floor yolu LIMITER'ı deliyordu: floor dalı yalnız
//            !limiter_mode'a bakıyordu. AUTO + presentWait + FLM_TARGET_FPS=120
//            → slot=8.33ms, floor=7.08ms → oyun hedefin %117'sine (≈141 FPS)
//            kadar kaçar; hedefe kilit yok → dalgalı frametime. Floor artık
//            YALNIZ fps==0 (doğal cadence) yolunda; fps>0'da klasik timeline
//            pacer (lead'li, tam kilit) kullanılır. README ile tutarlı.
//   [FIX-43] ÖLÇÜM TAZELİK BEKÇİSİ. presentWait ölçümü hiç örnek üretmezse
//            (id=0 gönderen oyun → sürekli TIMEOUT) slot_interval_ns 16.6ms
//            varsayılanında kalır → floor≈14.2ms → 240Hz oyun ~70 FPS'e
//            FRENLENİR. Alt-tab/OUT_OF_DATE sonrası bayat T ile aynı sınıf.
//            Ölçüm thread'i her başarılı flip'te last_flip_ns yayınlar; pacer
//            ve floor kapıları (limiter DEĞİL) örnek yoksa ya da son flip
//            MEAS_FRESH_NS'ten (250ms) eskiyse kendini kapatır ve anchor'ları
//            sıfırlar. Ölçüm akmadan pacing OLMAZ.
//   [FIX-44] FLOOR RATIO AUTOTUNE (kapalı çevrim). ratio=850, m=2'de kararlı
//            durum aralıkları 0.425T/0.575T ALTERNASYONU yapar (CoV ~%15) —
//            "iyileşti ama tam akıcı değil" hissinin yapısal kaynağı. İdeal
//            ratio çoğu zaman 1000'e yakın ama sabit yüksek ratio erken gelen
//            real kareyi frenler. Çözüm: geçen present'lerin headroom'u
//            (since-floor) bolsa ratio YAVAŞ sıkılır (+1/kare), headroom
//            incelirse ya da ardışık >=max(2,m) present tutulursa (fren
//            belirtisi) HIZLI gevşetilir. Delta [-150,+150], taban ratio ve
//            MFG-adapt üstüne biner, [500,1000] klempi korunur.
//            FLM_FLOOR_AUTOTUNE=0 → eski sabit-ratio davranışı.
//   [FIX-45] Temizlik: floor yolundaki ölü tavan (left < floor*2 — since>=0
//            iken left<=floor olduğundan hiç tetiklenmez) sadeleşti; hitch
//            dalı floor anchor'ını (last_present_ns) ve autotune fren
//            sayacını da açıkça sıfırlar (örtük yerine belirgin re-anchor).
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
static std::atomic<int> g_log_level{(int)LogLevel::ERR};   // [item 15] atomik
static FILE*            g_log_file = stderr;

// [FIX-29] fflush yalnız INFO+ — DEBUG spam'i tamponlu kalır (stderr zaten
// tamponsuzdur; bu yalnız FLM_LOG_FILE için anlamlı).
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
    constexpr int      SLOT_WINDOW         = 12;   // [FIX-16] 12 = ekok(1..4) →
                                                   // her MFG çarpanında tam döngü,
                                                   // faz kaynaklı ortalama sapması yok
    // [FIX-36] VRR + MFG floor-pacing: real-frame periyot medyanı için kısa,
    // FPS değişimine hızlı tepki veren pencere. SLOT_WINDOW (mean, 12) FPS
    // 150↔220 dalgalanırken gerçek anlık periyodun gerisinde kalıyordu.
    // [FIX-37] Pencere artık ham aralık değil DÖNGÜ-TOPLAMI (son m aralığın
    // toplamı ≈ T) tahminleri tutar — paced/unpaced fark etmeksizin her
    // flip'te güncellenir, fake filtresine bağımlı değildir.
    constexpr int      REAL_WINDOW         = 8;    // son N adet T tahmini
    constexpr int      CYC_RING            = 4;    // [FIX-37] son ham aralıklar (maks MFG çarpanı)
    constexpr int64_t  MIN_FLOOR_NS        = 500'000LL;   // 2000 FPS tavanı: floor asla bunun altına inmez
    constexpr int      MFG_DETECT_WINDOW   = 64;   // [item 7]
    constexpr int      MIN_SC_WIDTH        = 640;  // [item 11]
    constexpr int      MIN_SC_HEIGHT       = 480;
    constexpr int      CSV_BUFFER          = 256;  // [item 12]
    constexpr int64_t  STATS_INTERVAL_NS   = 5'000'000'000LL;  // [FIX-32]
    constexpr int64_t  MEAS_FRESH_NS       = 250'000'000LL;    // [FIX-43] ölçüm tazelik penceresi
    constexpr size_t   CSV_STDIO_BUF       = 1u << 20;         // [FIX-30]
    constexpr size_t   LOG_STDIO_BUF       = 64u << 10;        // [FIX-29]
}

enum class PaceMode  { AUTO = 0, PRESENT, LIMITER, OFF };
enum class PacePoint { PRESENT = 0, ACQUIRE, BOTH };

// ============================================================================
// CONFIG (hot-reload edilebilenler atomik — [item 15])
// ============================================================================
struct FLMConfig {
    // Yapısal (reload dışı)
    int         mfg_mult_env = 0;   // 0 = otomatik; >0 = zorla
    int         rt_priority  = 0;
    std::string measure_cpu;        // [item 13]
    bool        stats        = false;
    std::string csv_path;
    std::string config_path;        // [FIX-21] FLM_CONFIG canlı ayar dosyası

    // Hot-reload edilebilir
    std::atomic<int>     target_fps {0};
    std::atomic<int64_t> spin_ns    {FlmConst::DEFAULT_SPIN_NS};
    std::atomic<int64_t> lead_ns    {FlmConst::DEFAULT_LEAD_NS};
    std::atomic<int64_t> drift_tol  {0};
    std::atomic<int>     mode       {(int)PaceMode::AUTO};
    std::atomic<int>     pace_point {(int)PacePoint::PRESENT};
    std::atomic<int64_t> stats_interval_ns {FlmConst::STATS_INTERVAL_NS}; // [FIX-32]

    // [FIX-36] VRR + MFG floor-pacing ayarları — HEPSİ hot-reload.
    // pace_mode: grid (eski mutlak-grid davranışı) | floor (yeni slew-limit).
    // floor modu VRR'de değişken FPS'i frenlemez; yalnız ε-burst'ün çok erken
    // çıkmasını engelleyip generated/real uçurumunu yumuşatır.
    std::atomic<bool>    floor_pacing {true};   // FLM_FLOOR_PACING=1 (varsayılan açık)
    // floor = slot_iv * (floor_ratio/1000). 850 = 0.85 → present öncekinden en az
    // %85 slot sonra çıkar. Düşük = daha gevşek (jitter geçer), yüksek = daha sıkı
    // (daha düz ama geç kalırsa hitch). Hisle ayarlanacak asıl knob budur.
    std::atomic<int>     floor_ratio {850};     // FLM_FLOOR_RATIO (500-1000)

    // [FIX-41] MFG-adaptif ratio gevşetmesi. Ada (40-serisi) GPU, çarpan (m)
    // arttıkça ekstra üretilen kareleri aynı GPU tavanına sıkıştırmak zorunda
    // kalıyor -> gerçek slot süresi (T/m civarı) daha büyük varyansla dağılıyor.
    // Sabit floor_ratio, m=2'de iyi çalışsa da m=3/4'te real kareyi floor
    // içinde tutmaya çalışıp gereksiz bekleme + fren yaratabilir (hitch% ve
    // cov%'ın m ile katlanarak artmasının bir parçası). Bu yüzden m arttıkça
    // ratio'yu kademeli gevşetiyoruz: her ekstra çarpan adımı için ratio'dan
    // FLM_FLOOR_MFG_STEP kadar düş (varsayılan 40/1000 birim). m=1'de etkisiz.
    std::atomic<bool>     floor_mfg_adapt {true};   // FLM_FLOOR_MFG_ADAPT (varsayılan 1/açık)
    std::atomic<int>      floor_mfg_step  {40};     // FLM_FLOOR_MFG_STEP (0-200), ratio birimi/adım

    // [FIX-44] Kapalı-çevrim ratio ayarı: headroom bolsa sık, fren belirtisinde
    // gevşet. Taban FLM_FLOOR_RATIO + MFG-adapt üstüne [-150,+150] delta.
    std::atomic<bool>     floor_autotune  {true};   // FLM_FLOOR_AUTOTUNE (varsayılan 1/açık)

    // [FIX-39] Uyanma gecikmesine göre spin payını otomatik ayarla (hot-reload).
    // 0 = eski davranış: FLM_SPIN_NS ne diyorsa sabit o kadar spin.
    std::atomic<bool>    spin_adapt {true};     // FLM_SPIN_ADAPT (varsayılan 1)
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

// [FIX-21] Tek KV uygulayıcı — hem env hem config dosyası buradan geçer.
static void apply_dynamic_kv(const char* key, const char* val) {
    if (!key || !val || !*val) return;
    // [FIX-33] fps klempi: iv = 1e9/fps hesabında iv=0 ve atoi taşması koruması.
    if      (!strcmp(key, "FLM_TARGET_FPS"))         g_config.target_fps.store(std::clamp(atoi(val), 0, 1000));
    else if (!strcmp(key, "FLM_STATS_INTERVAL"))     g_config.stats_interval_ns.store(   // [FIX-32] saniye
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

// [FIX-21] FLM_CONFIG dosyası: '#' yorum, KEY=VALUE satırları.
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

// [FIX-26] Env init'te BİR KEZ snapshot'lanır: getenv reload thread'lerinde
// POSIX'e göre yarışabilir ve çalışan sürecin ortamı zaten dışarıdan
// değişemez. Revert semantiği korunur: reload = snapshot + dosya (dosya
// kazanır; dosyadan satır silinirse env değerine geri dönülür).
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

// Önce env snapshot (statik), sonra dosya (canlı) — dosya kazanır.
static void reload_dynamic_config() {
    for (const auto& [k, v] : g_env_snapshot)
        apply_dynamic_kv(k.c_str(), v.c_str());
    if (!g_config.config_path.empty())
        load_config_file(g_config.config_path.c_str());
}

// [FIX-21] Handler async-signal-safe: yalnız bayrak. Reload thread bağlamında.
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

static void reserve_global_maps();  // [FIX-33] tanım map bildirimlerinden sonra

#ifdef FLM_PGO_INSTRUMENTED
static void sigusr2_handler(int);  // [FIX-34] tanım now_ns()'den sonra (ileri bildirim)
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
                // [FIX-29] DEBUG hacmi için tam tamponlama (INFO+ zaten flush eder).
                setvbuf(f, nullptr, _IOFBF, FlmConst::LOG_STDIO_BUF);
                g_log_file = f;
            }
        }

        snapshot_dynamic_env();     // [FIX-26]
        reload_dynamic_config();

        // [item 15] SIGUSR1 yalnız kimse kullanmıyorsa kur.
        struct sigaction old{};
        if (sigaction(SIGUSR1, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr1_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR1, &sa, nullptr);
        }

#ifdef FLM_PGO_INSTRUMENTED
        // [FIX-34] SIGUSR2: oyunu kapatmadan "kill -USR2 <pid>" ile anında
        // .gcda flush. atexit'e güvenilemeyen launcher'lar için tek yol.
        if (sigaction(SIGUSR2, nullptr, &old) == 0 && old.sa_handler == SIG_DFL) {
            struct sigaction sa{};
            sa.sa_handler = sigusr2_handler;
            sigemptyset(&sa.sa_mask);
            sigaction(SIGUSR2, &sa, nullptr);
        }
        FLM_LOG(LogLevel::WARN,
                "PGO ENSTRUMANLI derleme aktif — periyodik (60sn) + SIGUSR2 ile .gcda flush");
#endif

        reserve_global_maps();  // [FIX-33]

        FLM_LOG(LogLevel::INFO,
                "Config: mode=%d fps=%d mfg_env=%d spin=%lldns lead=%lldns rt=%d",
                g_config.mode.load(), g_config.target_fps.load(), g_config.mfg_mult_env,
                (long long)g_config.spin_ns.load(), (long long)g_config.lead_ns.load(),
                g_config.rt_priority);
    });
}

// [FIX-34] PGO ENSTRÜMANLI derlemede atexit()'e güvenilemez: Steam/Proton
// çoğu zaman süreci _exit()/exit_group ile kapatır, atexit handler'ları HİÇ
// çalışmaz → .gcda asla yazılmaz. Bu blok yalnız ebuild PGO "generate" fazında
// -DFLM_PGO_INSTRUMENTED ile derlerken aktif; normal/PGO-use derlemede yok.
#ifdef FLM_PGO_INSTRUMENTED
extern "C" void __gcov_dump(void);
extern "C" void __gcov_reset(void);
static std::atomic<int64_t> g_last_gcov_dump_ns{0};
static std::atomic<bool>    g_gcov_dump_flag{false};
// SIGUSR2: talep üzerine ANINDA dump (oyunu kapatmadan profil almak için).
static void sigusr2_handler(int) { g_gcov_dump_flag.store(true, std::memory_order_relaxed); }
#endif


static inline int64_t now_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1'000'000'000LL + ts.tv_nsec;
}

#ifdef FLM_PGO_INSTRUMENTED
// now_ns()'e bağımlı olduğu için tanımı burada (bildirimi yukarıda).
static inline void flm_gcov_periodic_dump() {
    int64_t t = now_ns();
    int64_t last = g_last_gcov_dump_ns.load(std::memory_order_relaxed);
    bool due = g_gcov_dump_flag.exchange(false, std::memory_order_relaxed);
    if (!due && (t - last) < 60'000'000'000LL) return;   // 60 sn periyot
    if (g_last_gcov_dump_ns.compare_exchange_strong(last, t, std::memory_order_relaxed)) {
        __gcov_dump();
        FLM_LOG(LogLevel::INFO, "PGO: gcov profili diske yazildi (.gcda)");
    }
}
#endif


// Kaba kısım ABSTIME kernel uykusu (sinyal bölünmesine dayanıklı), son
// spin kadar pause-spin. FLM_SPIN_NS=0 → tamamen uyku (min CPU).
//
// [FIX-39] ADAPTİF SPİN. clock_nanosleep'in gerçek uyanma gecikmesi
// (oversleep = uyanılan an - istenen an; timer slack + scheduler kuyruğu)
// sönümlü-MAKSİMUM ile izlenir:
//   est = max(ölçülen, est - est/256)     → büyüme anında, sönüm ~256 örnek
// ve spin payı est*1.5 + 20µs olarak seçilir. Sabit 150µs payın iki
// başarısızlık modu vardı:
//   * Yüklü sistem: oversleep > 150µs → kapı hedefi KAÇIRIR → present geç
//     çıkar → floor/limiter'ın düzelttiği jitter'ı kapının kendisi üretir.
//   * Boş/RT sistem: oversleep ~5-30µs → her karede ~120µs boşa spin
//     (240 FPS'te çekirdek zamanının ~%3'ü ısıya gider).
// Adaptif pay iki modu da çözer; present her zaman TAM hedefte bırakılır
// (akıcılık) ve asla gereksiz erken spin'e girilmez (CPU → oyuna kalır).
static std::atomic<int64_t> g_oversleep_est{100'000};  // ns; ılımlı başlangıç

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
            int64_t os = now_ns() - wake;   // sinyal kesmesi → negatif → atla
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

    // [item 11] Bağlam
    VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    uint32_t         width  = 0;
    uint32_t         height = 0;
    bool             pace_allowed = false;   // create anında karar (FIFO/küçük → false)

    std::jthread    measure_thread;

    // Hot-path atomikleri — ayrı cache-line (false sharing engeli)
    alignas(64) std::atomic<uint64_t> next_present_id{1};
    alignas(64) std::atomic<int64_t>  slot_interval_ns{FlmConst::DEFAULT_INTERVAL_NS};
                std::atomic<int64_t>  last_flip_ns{0};   // [FIX-43] son başarılı flip anı
                                                         // (aynı satır: ikisini de ölçüm
                                                         // thread'i yazar, present okur)
    alignas(64) std::atomic<int64_t>  last_gate_wait_ns{0};     // [FIX-17] tespit dondurma
    alignas(64) std::atomic<uint32_t> present_seq{0};           // [item 4]
    alignas(64) std::atomic<int>      eff_mfg{1};               // [item 7] efektif çarpan
    alignas(64) std::atomic<int>      frame_count{0};
    alignas(64) std::atomic<bool>     hitch_active{false};
    alignas(64) std::atomic<int>      hitch_recovery_frames{0};
    alignas(64) std::atomic<bool>     pacing_enabled{true};     // [item 8] GPU-bound bekçisi

    // [FIX-28] LIMITER timeline'ı — YALNIZ QueuePresent thread'i, her karede
    // yazar. Kendi cache-line'ında olmalı; aksi halde ölçüm thread'inin her
    // karede yazdığı blokla (aşağısı) ping-pong yapar.
    // [FIX-36] Aynı cache-line'da present-side floor-pacing durumu (yalnız
    // present thread dokunur, kilitsiz). last_present_ns present ritmini
    // ölçüm gecikmesi olmadan anchor'lar; floor'un tabanı slot_interval_ns'ten
    // (ölçüm thread'i yayınlar) okunur.
    alignas(64) int64_t limiter_next_ns   = 0;
    int64_t             last_present_ns    = 0;   // [FIX-36] önceki present anı
    int                 ratio_auto         = 0;   // [FIX-44] öğrenilen ratio deltası [-150,150]
    int                 held_run           = 0;   // [FIX-44] ardışık tutulan present sayısı

    // [FIX-28][FIX-38] Yalnız ölçüm thread'i dokunur → kilitsiz; present-
    // thread alanlarından ayrı cache-line'da başlar. (FIX-36 real_win'i
    // yanlışlıkla yukarıdaki present satırına koymuştu; bu alanlara her
    // karede ölçüm thread'i yazdığından FIX-28'in çözdüğü false sharing
    // geri gelmişti — buraya taşındı.)
    // [FIX-37] Döngü halkası: son CYC_RING HAM aralık. Son m tanesinin
    // toplamı ≈ T (real periyot) — paced/unpaced/bimodal fark etmez.
    alignas(64) int64_t cyc_win[FlmConst::CYC_RING] = {};
    int     cyc_idx     = 0;
    int     cyc_count   = 0;
    // [FIX-36/37] T (real-frame periyodu) tahmin penceresi — medyanı floor
    // pacing'in tabanıdır. Artık her flip'te döngü toplamıyla beslenir.
    int64_t real_win[FlmConst::REAL_WINDOW] = {};
    int     real_idx    = 0;
    int     real_count  = 0;
    // [FIX-16] Slot penceresi: TÜM aralıkların kayan ortalaması (MFG'nin
    // bimodal ε/T desenini per-sample EMA'nın aksine tam doğru ortalar).
    int64_t slot_win[FlmConst::SLOT_WINDOW] = {};
    int     slot_idx    = 0;
    int     slot_count  = 0;
    int64_t slot_sum    = 0;
    int64_t slot_mean_ns = FlmConst::DEFAULT_INTERVAL_NS;
    // [item 8] GPU-bound penceresi
    int     over_target_run  = 0;
    int     under_target_run = 0;
    // [item 7] MFG algılama
    int     mfg_small_cnt = 0;
    int     mfg_total_cnt = 0;
    // [item 12] istatistik
    int64_t stat_last_ns    = 0;
    int64_t stat_sum_ns     = 0;
    int64_t stat_max_ns     = 0;
    int     stat_frames     = 0;
    int     stat_fake_hitch = 0;   // [FIX-27] fake + hitch toplamı (eski adı stat_fake)
    // [item 12] CSV — [FIX-31] telemetri kolonları eklendi
    FILE*   csv_fp = nullptr;
    struct CsvRow {
        int64_t  flip_ns, interval_ns;
        int      is_fake, is_hitch;
        uint32_t slot;
        int      mfg;            // efektif MFG çarpanı
        int64_t  slot_mean_ns;   // yayınlanan slot ortalaması
        int      pacing;         // GPU-bound bekçisi durumu
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

    // [FIX-37] Real-frame periyodu (T) medyanı — döngü-toplam tahminlerinden.
    // display_intervals medyanının yerini aldı: m=1'de birebir aynı semantik
    // (döngü toplamı = ham aralık), m>1'de fake filtresiz ve faz-duyarsız.
    int64_t real_period_median() const {
        int n = std::min(real_count, FlmConst::REAL_WINDOW);
        if (n == 0) return FlmConst::DEFAULT_INTERVAL_NS;
        int64_t tmp[FlmConst::REAL_WINDOW];
        std::copy(real_win, real_win + n, tmp);
        std::sort(tmp, tmp + n);
        return tmp[n / 2];
    }

    // [FIX-30] fflush YOK: fopen sonrası 1MB _IOFBF tampon kuruluyor; buradaki
    // fprintf'ler salt bellek formatlamadır. Gerçek write() ancak stdio tamponu
    // dolunca (≈20k+ satır) olur — ölçüm thread'inin zamanlaması korunur.
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
// [item 2] dispatch_key(gpu/instance) → InstanceDispatch bulmak için
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

// [FIX-33] İlk insert'lerde rehash olmasın diye init'te bir kez.
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
    return (it != g_sc_map.end()) ? it->second : nullptr;  // [FIX-1] kopya
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
// GÖREV: gerçek flip aralığını ölç, doğal cadence'ı (fake-filtreli) yayınla,
// GPU-bound / hitch durumunu işaretle. GATING BURADA YAPILMAZ — kapı present
// thread'inde yerel timeline ile çalışır (cross-thread mutlak hedef devri
// v1'deki stutter'ın kaynağıydı; kaldırıldı).
// ============================================================================
static void apply_thread_policies() {
    if (g_config.rt_priority > 0) {
        sched_param sp{};
        sp.sched_priority = g_config.rt_priority;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0)
            FLM_LOG(LogLevel::WARN, "SCHED_FIFO ayarlanamadi (CAP_SYS_NICE?)");
    }
    // [item 13] ölçüm thread affinity: "0-3" veya "5"
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
                FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU affinity ayarlanamadi");
        } else {
            FLM_LOG(LogLevel::WARN, "FLM_MEASURE_CPU parse hatasi: %s", s.c_str());
        }
    }
    pthread_setname_np(pthread_self(), "flm-measure");
}

static void measurement_thread_fn(std::stop_token stoken, std::shared_ptr<SwapchainState> st) {
    apply_thread_policies();

    // [item 12] CSV aç
    if (!g_config.csv_path.empty()) {
        st->csv_fp = fopen(g_config.csv_path.c_str(), "w");
        if (st->csv_fp) {
            // [FIX-30] Büyük stdio tamponu: csv_flush disk'e dokunmaz.
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
        maybe_reload();   // [FIX-21] SIGUSR1 → burada (AS-safe) uygulanır
#ifdef FLM_PGO_INSTRUMENTED
        flm_gcov_periodic_dump();   // [FIX-34] atexit'e güvenme, elle flush et
#endif
        if (!st->disp || !st->disp->has_present_wait) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
            continue;
        }

        // [FIX-5] Geride kaldıysak ileri sar (eski id anında döner → ~0 interval)
        uint64_t latest = st->next_present_id.load(std::memory_order_relaxed);
        if (latest > 2 && wait_id + 2 < latest) {
            wait_id    = latest - 1;
            last_valid = false;
        }

        VkResult r = st->disp->WaitForPresentKHR(st->device, st->swapchain,
                                                 wait_id, FlmConst::WAIT_TIMEOUT_NS);
        if (r == VK_TIMEOUT) { last_valid = false; continue; }
        // [item 9] resize/alt-tab: swapchain yaşıyor, thread ÖLMEMELİ.
        if (r == VK_ERROR_OUT_OF_DATE_KHR || r == VK_SUBOPTIMAL_KHR ||
            r == VK_ERROR_SURFACE_LOST_KHR) {
            last_valid = false;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            continue;
        }
        if (r != VK_SUCCESS) {
            FLM_LOG(LogLevel::DEBUG, "WaitForPresentKHR fatal: %d", (int)r);
            break;  // yalnız DEVICE_LOST / bilinmeyen
        }

        int64_t tnow = now_ns();
        st->last_flip_ns.store(tnow, std::memory_order_relaxed);   // [FIX-43]

        if (last_valid) {
            int64_t interval_ns = tnow - last_display_ns;

            // Efektif çarpan
            int m = st->eff_mfg.load(std::memory_order_relaxed);

            // [FIX-37] Önceki T tahmini (döngü-toplam medyanı). Fake split ve
            // hitch eşiği artık buna bağlı — pacing altında donan eski
            // kabul-medyanına değil.
            const int64_t T_prev = st->real_period_median();
            // [FIX-40] Pencere ısınmadan (4 tahmin) hitch/fake sınıflandırma
            // yapılmaz: T_prev henüz 16.6ms varsayılanındayken düşük-FPS
            // oyunlarda her kare hitch sayılıp tahmin hiç ısınamıyordu.
            const bool warm = st->real_count >= 4;

            // [FIX-37] HITCH ÖNCE ve HAM aralıktan. Hitch aralığı uzundur;
            // fake (kısa) sınıfına düşemez → [FIX-19] koruması aynen geçerli.
            bool is_hitch = warm &&
                            interval_ns > st->get_hitch_threshold(T_prev);
            if (is_hitch) {
                st->hitch_active.store(true, std::memory_order_relaxed);
                st->hitch_recovery_frames.store(FlmConst::HITCH_RECOVERY,
                                                std::memory_order_relaxed);
                // Hitch'i kapsayan döngü toplamları T'yi zehirler → halka reset.
                st->cyc_count = 0;
                st->cyc_idx   = 0;
            } else {
                if (st->hitch_active.load(std::memory_order_relaxed)) {
                    if (st->hitch_recovery_frames.fetch_sub(1, std::memory_order_relaxed) <= 1)
                        st->hitch_active.store(false, std::memory_order_relaxed);
                }

                // [FIX-37] DÖNGÜ TOPLAMI ile T tahmini. Ham aralığı halkaya it;
                // son m aralığın toplamı, present'ler NASIL dağılmış olursa
                // olsun ≈ T'dir:
                //   unpaced bimodal : ε + (T-ε)            = T
                //   floor-paced     : floor + (T-floor)     = T
                //   uniform paced   : m * (T/m)             = T
                // Yani tahmin pacing'in kendi etkisine KÖR — eski non-fake
                // beslemesinin donma/fren kilidi (v2.2) yapısal olarak yok.
                st->cyc_win[st->cyc_idx] = interval_ns;
                st->cyc_idx = (st->cyc_idx + 1) % FlmConst::CYC_RING;
                if (st->cyc_count < FlmConst::CYC_RING) st->cyc_count++;

                const int mm = std::clamp(m, 1, FlmConst::CYC_RING);
                if (st->cyc_count >= mm) {
                    int64_t T_est = 0;
                    for (int k = 0; k < mm; k++)
                        T_est += st->cyc_win[(st->cyc_idx - 1 - k +
                                              FlmConst::CYC_RING) % FlmConst::CYC_RING];
                    // Isındıktan sonra tek örnek tahmini en çok 2x/0.25x
                    // oynatabilsin (hitch dışı anomali/clock koruması; FPS
                    // sıçramalarında yine ~2-3 flip'te yakalar).
                    if (warm)
                        T_est = std::clamp(T_est, T_prev / 4, T_prev * 2);
                    st->real_win[st->real_idx] = T_est;
                    st->real_idx = (st->real_idx + 1) % FlmConst::REAL_WINDOW;
                    if (st->real_count < FlmConst::REAL_WINDOW) st->real_count++;
                }
            }

            // [FIX-37] Fake sınıflandırma — artık YALNIZ istatistik/CSV için
            // (pacing tahmini fake filtresine bağımlı değil). Split canlı T
            // tahmininden türetilir.
            bool is_fake = false;
            if (m > 1 && warm && !is_hitch) {
                int64_t split_ns = (T_prev * (m + 1)) / (2LL * m);
                is_fake = (interval_ns < split_ns);
            }

            // [FIX-16] SLOT ORTALAMASI — TÜM aralıklar üzerinden kayan pencere.
            // m present toplamda bir gerçek kare süresi (T) alır → ortalama
            // aralık = T/m; bu, paced (uniform T/m) ve unpaced (ε,...,T-Σε)
            // durumların İKİSİNDE de doğru slot genişliğidir. Pencere
            // ortalaması bimodal deseni tam ortalar (EMA'nın aksine faz
            // sırasından etkilenmez). Hitch zehirlenmesine karşı klemp 4x.
            {
                int64_t safe_iv = std::clamp<int64_t>(interval_ns, 100'000LL,
                                                      st->slot_mean_ns * 4);
                st->slot_sum += safe_iv - st->slot_win[st->slot_idx];
                st->slot_win[st->slot_idx] = safe_iv;
                st->slot_idx = (st->slot_idx + 1) % FlmConst::SLOT_WINDOW;
                if (st->slot_count < FlmConst::SLOT_WINDOW) st->slot_count++;
                st->slot_mean_ns = st->slot_sum / st->slot_count;
            }

            // [FIX-17] MFG algılama: eşik slot-EMA'ya göre (ema ≈ T/m).
            //   m=1: interval ≈ ema      → 0.7*ema altına düşmez  → p≈0 → m=1
            //   m>1: fake ≈ ε << ema, gerçek ≈ m*ema             → p≈(m-1)/m
            // Kapı yakın zamanda beklettiyse (paced uniform aralıklar tespiti
            // zehirler) tespit DONDURULUR — m küçülüp slot'un aniden
            // büyümesinden doğan salınım engellenir.
            if (g_config.mfg_mult_env > 0) {
                if (m != g_config.mfg_mult_env)
                    st->eff_mfg.store(g_config.mfg_mult_env, std::memory_order_relaxed);
            } else {
                bool gate_hot = (tnow - st->last_gate_wait_ns.load(std::memory_order_relaxed))
                                < 1'000'000'000LL;
                if (gate_hot && m > 1) {
                    st->mfg_small_cnt = 0;   // donuk pencere: temiz başla
                    st->mfg_total_cnt = 0;
                } else {
                    if (interval_ns * 10 < st->slot_mean_ns * 7) st->mfg_small_cnt++;
                    st->mfg_total_cnt++;
                    if (st->mfg_total_cnt >= FlmConst::MFG_DETECT_WINDOW) {
                        double p = (double)st->mfg_small_cnt / (double)st->mfg_total_cnt;
                        int mhat = (p < 0.99) ? (int)std::lround(1.0 / (1.0 - p)) : 4;
                        mhat = std::clamp(mhat, 1, 4);
                        if (mhat != m)
                            FLM_LOG(LogLevel::INFO, "MFG carpani: %d -> %d", m, mhat);
                        st->eff_mfg.store(mhat, std::memory_order_relaxed);
                        st->mfg_small_cnt = 0;
                        st->mfg_total_cnt = 0;
                    }
                }
            }

            // [FIX-16/36/37] Yayınlanacak slot aralığı:
            //   fps>0        → sabit hedef (limiter/cap yolu, değişmedi)
            //   floor_pacing → median(T)/m — T döngü-toplam tahmini: pacing
            //                  altında DONMAZ, FPS değişimini flip hızında
            //                  takip eder; fren oluşursa ölçüm frenlenmiş
            //                  aralığı görür → floor kısalır → fren çözülür
            //                  (negatif geri besleme; v2.2'de pozitif kilitti).
            //   klasik pacer → slot_mean (eski davranış, geri dönüş)
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

            // [FIX-18] GPU-bound bekçisi: yalnız AÇIK hedef (fps>0) varken ve
            // ham aralık değil slot-EMA üzerinden. fps=0 doğal cadence'ta hedef
            // zaten ölçümden türetilir → bekçi anlamsız (v2'de MFG'nin bimodal
            // ham aralıkları bekçiyi anında tetikleyip pacing'i kapatıyordu).
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

            // [item 12] istatistik + CSV
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

            // [FIX-32] Aralık FLM_STATS_INTERVAL ile ayarlanabilir (sn).
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
    FLM_LOG(LogLevel::DEBUG, "Olcum thread'i durdu");
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
    // [item 2] core 1.1 fonksiyonu; yoksa KHR türevini dene.
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

// [item 2] presentId + presentWait feature'larını gerçekten destekliyor mu?
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
    if (!inst.GetPhysicalDeviceFeatures2) return false; // sorgulayamıyoruz → güvenli taraf

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

    // [FIX-13] Retry için zincir konumunu SAKLA (loader'ın paylaşılan mutable
    // struct'ı; alt katmanlar da ilerletir, restore etmeden 2. çağrı = crash).
    VkLayerDeviceLink* next_link = chain->u.pLayerInfo;

    // Oyunun uzantı listesi
    std::vector<const char*> exts(pCreateInfo->ppEnabledExtensionNames,
                                  pCreateInfo->ppEnabledExtensionNames +
                                  pCreateInfo->enabledExtensionCount);
    bool app_has_id = false, app_has_wait = false;
    for (auto& e : exts) {
        if (!strcmp(e, VK_KHR_PRESENT_ID_EXTENSION_NAME))   app_has_id   = true;
        if (!strcmp(e, VK_KHR_PRESENT_WAIT_EXTENSION_NAME)) app_has_wait = true;
    }

    // [item 2] presentWait yalnız sürücü gerçekten destekliyorsa enjekte et.
    bool want_inject = query_present_features(gpu);
    if (!want_inject)
        FLM_LOG(LogLevel::INFO, "presentId/Wait desteklenmiyor; PACER kapali (LIMITER hala kullanilabilir)");

    bool injected = false;
    if (want_inject) {
        if (!app_has_id)   exts.push_back(VK_KHR_PRESENT_ID_EXTENSION_NAME);
        if (!app_has_wait) exts.push_back(VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
        injected = true;
    }

    // [FIX-14] pNext'te zaten olan feature struct'larını tekrar ekleme.
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
        FLM_LOG(LogLevel::WARN, "presentWait ile CreateDevice basarisiz (%d), fallback", (int)res);
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

    // [item 1] presentWait'i yalnız GÜVENLE enable edildiğinde kullan.
    // Fallback yolunda uzantı enable EDİLMEDİ; WaitForPresentKHR non-null
    // dönebilir ama çağırmak UB. created_with_wait bunu garanti eder; ayrıca
    // oyunun kendisi ikisini de enable etmişse yine güvenli.
    bool safe_wait = created_with_wait || (app_has_id && app_has_wait);
    d.has_present_wait = (d.WaitForPresentKHR != nullptr) && safe_wait;

    std::unique_lock lk(g_dev_lock);
    g_dev_map[*pDevice] = d;
    return VK_SUCCESS;
}

VK_LAYER_EXPORT void VKAPI_CALL FLM_vkDestroyDevice(
    VkDevice device, const VkAllocationCallbacks* pAllocator)
{
    // [FIX-2] Bu device'a ait tüm swapchain state'lerini durdur+join.
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

// [FIX-9] Queue map'i create anında doldur.
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

    // [item 11] Pacing kararı:
    //  - Küçük yardımcı swapchain (launcher/overlay) → hiç pace etme.
    //  - FIFO/FIFO_RELAXED zaten vsync'e kilitli → present pacing gereksiz
    //    (yalnız ACQUIRE pace point'i seçilirse acquire kapısına izin var,
    //     bu QueuePresent tarafında kontrol ediliyor).
    //  - MAILBOX/IMMEDIATE (VRR senaryosu) → present pacing serbest.
    bool too_small = (st->width  < (uint32_t)FlmConst::MIN_SC_WIDTH ||
                      st->height < (uint32_t)FlmConst::MIN_SC_HEIGHT);
    st->pace_allowed = !too_small;

    if (too_small) {
        FLM_LOG(LogLevel::DEBUG, "Kucuk swapchain %ux%u — pacing atlandi",
                st->width, st->height);
    }

    // Ölçüm thread'i yalnız presentWait varsa ve pace edilebilir swapchain için.
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
// GATE — present thread'inde çalışan TEK kapı (limiter + pacer birleşik)
// ----------------------------------------------------------------------------
// Yerel timeline (st->limiter_next_ns) kullanır. KRİTİK ÖZELLİK: kapı hedefi
// geçmişteyse HİÇ beklemez — yani pacing yalnızca GECİKTİREBİLİR, asla
// hızlandırmaz. Bu yüzden oyunu "kaçırmaya" zorlayamaz; en kötü ihtimalle
// hiçbir şey yapmaz. Stutter üretmemesinin temel garantisi budur.
// ============================================================================
// [FIX-35] advance: timeline'ı ilerlet + slew uygula. BOTH modunda karede iki
// çağrı olur; yalnız BİRİ (present) ilerletmeli, aksi halde kare başına 2*iv
// dayatılır → limiter'da fps/2, pacer'da (iv=ölçüm) pozitif geri besleme
// sarmalı: kare=2*iv → ölçüm=2*iv → iv katlanır; 50ms WaitForPresent
// timeout'u + hitch kesintileri sarmalı ~15-17 FPS'te "kilitler".
static void apply_gate(SwapchainState* st, bool limiter_mode, bool advance) {
    // Hitch veya toparlanma: pace etme, timeline'ı sıfırla (temiz yeniden anchor).
    if (st->hitch_active.load(std::memory_order_relaxed) ||
        st->hitch_recovery_frames.load(std::memory_order_relaxed) > 0) {
        st->limiter_next_ns = 0;
        st->last_present_ns = 0;   // [FIX-45] floor da temiz re-anchor yapsın
        st->held_run        = 0;   // [FIX-44] hitch dizisi fren belirtisi değildir
        return;
    }
    // [item 8] GPU-bound → pace etme.
    if (!st->pacing_enabled.load(std::memory_order_relaxed)) {
        st->limiter_next_ns = 0;
        st->last_present_ns = 0;   // [FIX-45]
        return;
    }

    const int fps = g_config.target_fps.load(std::memory_order_relaxed);

    // ========================================================================
    // [FIX-43] ÖLÇÜM TAZELİK BEKÇİSİ — yalnız ölçüme dayanan yollar (pacer +
    // floor). LIMITER ölçüm istemez, ona dokunma. Ölçüm hiç örnek üretmediyse
    // (id=0 gönderen oyun → sürekli TIMEOUT) ya da bayatsa (alt-tab,
    // OUT_OF_DATE döngüsü) slot_interval_ns varsayılan/eski T'de kalır ve
    // kapı oyunu O değere frenler (16.6ms varsayılan → 240Hz oyun ~70 FPS'e
    // kilitlenirdi). Taze veri yoksa pacing YOK; anchor'lar sıfırlanır ki
    // ölçüm dönünce temiz başlansın.
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
    // [FIX-36] FLOOR PACING — VRR + MFG için asıl yol.
    // ------------------------------------------------------------------------
    // Neden mutlak-grid DEĞİL: VRR'de doğru frametime sabit değil; FPS
    // 150↔220 dalgalanıyor. Mutlak grid, FPS artarken kareleri frenler
    // (görünür titreme). Bunun yerine present çıkışına ÖNCEKİ present'e göreli
    // bir TABAN (floor) koyarız: bir present, öncekinden en az floor kadar
    // sonra çıkabilir. Bu, Ada MFG'nin ε aralıklı generated karelerinin çok
    // erken çıkmasını engeller (onları floor'a kadar bekletir), ama real
    // karenin (uzun aralık sonrası) geç gelmesine DOKUNMAZ. Sonuç: bimodal
    // ε/T deseni düzleşir, değişken FPS frenlenmez.
    //
    // floor = (slot_iv) * floor_ratio.  slot_iv = T/m (ölçümden, m dahil).
    // last_present_ns present thread'inde her karede güncellenir → ölçüm
    // gecikmesi grid'i kaydırmaz (mutlak-grid'in 1-kare bayat sorunu yok).
    // ========================================================================
    // [FIX-42] Floor YALNIZ fps==0 (doğal cadence). fps>0'da klasik timeline
    // pacer hedefe TAM kilitler; floor ise göreli taban olduğundan hedefin
    // %117'sine kadar kaçırıyordu (ratio=850 → 1/0.85). README zaten "fps=0
    // yolunda" diyordu; kod artık uyuyor.
    if (!limiter_mode && fps == 0 &&
        g_config.floor_pacing.load(std::memory_order_relaxed)) {
        int64_t slot_iv = st->slot_interval_ns.load(std::memory_order_relaxed);
        if (slot_iv <= 0) return;
        int     ratio   = g_config.floor_ratio.load(std::memory_order_relaxed);
        // [FIX-41] m arttıkça ratio'yu kademeli gevşet. GPU zaten tavandaysa
        // (40-serisi MFG donanım-hızlandırmasız) m=3/4'te generated kare
        // üretim süresi daha büyük varyansla dağılır; sabit sıkı ratio real
        // kareyi de floor içinde gereksiz bekletip fren/hitch üretebiliyor.
        // Yalnız m>1 (MFG aktif) iken devrede, m=1'de davranış değişmez.
        int m_now = st->eff_mfg.load(std::memory_order_relaxed);
        if (g_config.floor_mfg_adapt.load(std::memory_order_relaxed)) {
            if (m_now > 1) {
                int step = g_config.floor_mfg_step.load(std::memory_order_relaxed);
                ratio = std::clamp(ratio - (m_now - 1) * step, 500, 1000);
            }
        }
        // [FIX-44] Öğrenilen delta taban+adapt üstüne biner; klemp korunur.
        const bool autotune = g_config.floor_autotune.load(std::memory_order_relaxed);
        if (autotune)
            ratio = std::clamp(ratio + st->ratio_auto, 500, 1000);
        int64_t floor   = std::max<int64_t>((slot_iv * ratio) / 1000, FlmConst::MIN_FLOOR_NS);

        int64_t t = now_ns();
        if (st->last_present_ns == 0) {          // ilk present: yalnız anchor
            st->last_present_ns = t;
            return;
        }

        int64_t since = t - st->last_present_ns; // önceki present'ten bu yana

        // [FIX-44] KAPALI ÇEVRİM (yalnız advance=true, yani asıl present
        // kapısı; BOTH'un acquire ayağı ölçmeye karışmasın). Bu karenin
        // gözlemi bir SONRAKİ karenin ratio'sunu ayarlar:
        //   * since <  floor  → present tutulacak. Normal MFG'de karede en çok
        //     m-1 present tutulur; ardışık >= max(2,m) tutulma REAL karenin de
        //     frenlendiğini gösterir → hızlı gevşet (-4).
        //   * since >= floor  → headroom = since - floor.
        //       headroom > slot/12 → aralıklar hâlâ dengesiz (alternasyon
        //                            yaşıyoruz) → yavaşça sık (+1).
        //       headroom < slot/50 → floor real kareyi sıyırıyor → gevşet (-2).
        // Böylece ratio=850'nin yapısal 0.425T/0.575T alternasyonu, fren
        // belirtisi çıkmadığı sürece kendiliğinden ~0.5T/0.5T'ye düzleşir;
        // FPS düşüp real kare erken gelmeye başlarsa delta anında geri çekilir.
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

        // advance=false (BOTH'ta acquire kapısı): yalnız erken kalmayı önle,
        // last_present_ns'i present dalı günceller (çift ilerletme olmasın).
        int64_t target = st->last_present_ns + floor;

        // Present floor'un içinde mi? (çok erken generated kare) → beklet.
        // Floor'u aşmışsa (real kare / geç kare) → hiç bekleme, hemen geç.
        // [FIX-45] Eski "left < floor*2" tavanı ölü koddu: since>=0 iken
        // left = floor - since <= floor, tavan hiç tetiklenemez. Kaldırıldı.
        if (since < floor) {
            int64_t left = target - t;
            if (left > 0) {
                st->last_gate_wait_ns.store(t, std::memory_order_relaxed);  // [FIX-17]
                precise_wait_absolute(target);
                t = now_ns();
            }
        }
        if (advance) st->last_present_ns = t;    // present ritmini anchor'la
        return;
    }

    int64_t iv, lead;
    if (limiter_mode) {
        if (fps <= 0) return;
        iv   = 1'000'000'000LL / fps;
        lead = 0;  // limiter tam hedefe kilitler
    } else {
        // [item 3] PACER: doğal cadence'a uniform aralık, flip'ten lead önce.
        iv = st->slot_interval_ns.load(std::memory_order_relaxed);
        if (iv <= 0) return;
        // [FIX-24] lead >= iv olursa target = next - lead geçmişe düşer ve
        // kapı sessizce no-op olur (örn. yüksek FPS'te varsayılan 1ms lead,
        // ya da FLM_PRESENT_LEAD_NS=8ms + 240Hz). Aralığın yarısıyla sınırla.
        lead = std::min(g_config.lead_ns.load(std::memory_order_relaxed), iv / 2);
    }

    int64_t t = now_ns();
    if (st->limiter_next_ns == 0) {
        st->limiter_next_ns = t + iv;   // ilk kare: bekletme, timeline'ı kur
        return;
    }
    // [FIX-35] İlerletme + slew yalnız karede TEK çağrıda. advance=false olan
    // ikinci kapı (BOTH'ta acquire) mevcut hedefe yalnız "erken kalma"
    // kontrolü yapar — hedef geçmişteyse no-op.
    if (advance) {
        st->limiter_next_ns += iv;      // [item 4] uniform slot ilerlemesi

        // [item 10] Soft slew: hard-rebase yalnız aşırı sapmada; küçük borcu
        // ~8 karede yumuşakça kapat (görünür faz sıçraması yok).
        int64_t drift = st->limiter_next_ns - t;
        int64_t tol   = g_config.drift_tol.load(std::memory_order_relaxed);
        if (tol <= 0) tol = std::clamp<int64_t>(iv / 4, 1'000'000LL, 4'000'000LL);
        if (drift < -2 * iv || drift > 4 * iv) {
            st->limiter_next_ns = t + iv;   // stall / clock jump
        } else if (drift < -tol) {
            st->limiter_next_ns -= drift / 8;  // drift<0 → hedefi öne çek
        }
    }

    int64_t target = st->limiter_next_ns - lead;
    int64_t left   = target - t;
    // [FIX-20] Tavan aralığa göreli: sabit 20ms tavan fps<=50 hedeflerde
    // (iv>=20ms) limiter'ı tamamen no-op yapıyordu.
    int64_t max_wait = std::max<int64_t>(FlmConst::MAX_PACE_WAIT_NS, iv + iv / 2);
    if (left > 0 && left < max_wait) {
        st->last_gate_wait_ns.store(t, std::memory_order_relaxed);  // [FIX-17]
        precise_wait_absolute(target);
    }
}

// Etkin modu çöz. limiter_mode çıktısı: gate limiter mantığı mı kullanacak.
// return: pace edilecek mi.
static bool resolve_gate(const SwapchainState* st, bool has_wait, bool& limiter_mode) {
    if (!st->pace_allowed) return false;
    PaceMode mode = (PaceMode)g_config.mode.load(std::memory_order_relaxed);
    int      fps  = g_config.target_fps.load(std::memory_order_relaxed);

    // [item 11] FIFO/FIFO_RELAXED zaten vsync'e kilitli → PACER (uniform
    // cadence tahmini) gereksiz ve compositor ile kavga eder. LIMITER (daha
    // düşük fps'e cap) bu modlarda YİNE geçerli ve faydalı, ona dokunma.
    bool is_fifo = (st->present_mode == VK_PRESENT_MODE_FIFO_KHR ||
                    st->present_mode == VK_PRESENT_MODE_FIFO_RELAXED_KHR);

    switch (mode) {
        case PaceMode::OFF:     return false;
        case PaceMode::LIMITER: limiter_mode = true;  return fps > 0;
        case PaceMode::PRESENT:
            if (has_wait && !is_fifo) { limiter_mode = false; return true; }
            limiter_mode = true; return fps > 0;   // FIFO veya wait yok → limiter
        case PaceMode::AUTO:
        default:
            if (has_wait && !is_fifo) { limiter_mode = false; return true; }
            limiter_mode = true; return fps > 0;
    }
}

// [FIX-22] Ortak acquire kapısı — hem AcquireNextImageKHR hem 2KHR yolundan.
static inline void acquire_gate(VkSwapchainKHR swapchain, bool has_wait)
{
    // [item 6] Yalnız pace_point ACQUIRE/BOTH ise burada kapı uygula.
    PacePoint pp = (PacePoint)g_config.pace_point.load(std::memory_order_relaxed);
    if (pp == PacePoint::ACQUIRE || pp == PacePoint::BOTH) {
        if (auto st = find_sc_state(swapchain)) {   // [FIX-1] shared_ptr kopya
            if (st->frame_count.load(std::memory_order_relaxed) >= FlmConst::WARMUP_FRAMES) {
                bool limiter_mode = false;
                if (resolve_gate(st.get(), has_wait, limiter_mode))
                    // [FIX-35] BOTH: timeline'ı present ilerletir; acquire
                    // yalnız mevcut hedefe karşı erken kalmayı engeller.
                    apply_gate(st.get(), limiter_mode,
                               /*advance=*/pp == PacePoint::ACQUIRE);
            }
            st->frame_count.fetch_add(1, std::memory_order_relaxed);
        }
    } else {
        // Pacing present'te; yine de frame_count ilerlesin (warmup için).
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

// [FIX-22] Bu yolu kullanan motorlarda warmup sayacı hiç ilerlemiyordu →
// kapı asla açılmıyordu (limiter+pacer sessizce no-op).
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
    maybe_reload();   // [FIX-21] ölçüm thread'i yoksa (salt limiter) da çalışsın
#ifdef FLM_PGO_INSTRUMENTED
    flm_gcov_periodic_dump();   // [FIX-34] measurement thread yoksa buradan da dene
#endif

    QueueData qdata{};
    {
        std::shared_lock qlk(g_queue_lock);
        auto qit = g_queue_map.find(queue);
        if (qit != g_queue_map.end()) qdata = qit->second;
    }
    if (!qdata.disp) {   // yedek: dispatch-key ile çöz+cache
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

    // [FIX-15] Oyunun kendi VkPresentIdKHR'ı var mı?
    const VkPresentIdKHR* app_pid = nullptr;
    for (const VkBaseInStructure* p = (const VkBaseInStructure*)pPresentInfo->pNext; p; p = p->pNext)
        if (p->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) { app_pid = (const VkPresentIdKHR*)p; break; }
    const bool app_has_present_id = (app_pid != nullptr);

    // [FIX-4] Stack dizileri (≤8 swapchain → heap yok)
    uint64_t ids_stack[FlmConst::STACK_PRESENT_IDS];
    std::vector<uint64_t> ids_heap;
    uint64_t* present_ids = ids_stack;
    if (sc_count > FlmConst::STACK_PRESENT_IDS) { ids_heap.resize(sc_count, 0); present_ids = ids_heap.data(); }
    else std::fill(ids_stack, ids_stack + sc_count, 0ULL);

    // [item 6] present kapısı yalnız pace_point PRESENT/BOTH ise.
    PacePoint pp = (PacePoint)g_config.pace_point.load(std::memory_order_relaxed);
    bool gate_here = (pp == PacePoint::PRESENT || pp == PacePoint::BOTH);

    bool any_id = false;
    for (uint32_t i = 0; i < sc_count; i++) {
        auto st = find_sc_state(pPresentInfo->pSwapchains[i]);
        if (!st) continue;

        if (app_has_present_id) {
            // [FIX-15] Oyunun id'sini takip et: next_present_id = app_id + 1.
            if (app_pid->pPresentIds && i < app_pid->swapchainCount) {
                uint64_t id = app_pid->pPresentIds[i];
                if (id) st->next_present_id.store(id + 1, std::memory_order_relaxed);
            }
        }

        // TEK KAPI (yalnız ilk/primary swapchain'de; çoklu swapchain nadir)
        if (gate_here && i == 0 &&
            st->frame_count.load(std::memory_order_relaxed) >= FlmConst::WARMUP_FRAMES) {
            st->present_seq.fetch_add(1, std::memory_order_relaxed);  // [item 4]
            bool limiter_mode = false;
            if (resolve_gate(st.get(), has_wait, limiter_mode))
                apply_gate(st.get(), limiter_mode, /*advance=*/true);  // [FIX-35]
        }

        // presentWait için id enjekte et (oyun kendisi göndermiyorsa).
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
    // [FIX-11] GIPA üzerinden istenen device-level fonksiyonlar da katmandan geçmeli.
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
// README — ENV DEĞİŞKENLERİ
// ----------------------------------------------------------------------------
//  FLM_MODE=auto|present|limiter|off   (varsayılan auto)
//     auto     : presentWait varsa PACER, yoksa (fps set ise) LIMITER
//     present  : PACER'ı zorla (presentWait yoksa limiter'a düşer)
//     limiter  : salt FPS limiter (presentWait gerekmez, FLM_TARGET_FPS ister)
//     off      : hiçbir şey yapma (A/B testi taban çizgisi)
//  FLM_TARGET_FPS=<n>          limiter/pacer hedef fps (0 = doğal cadence)
//  FLM_PACE_POINT=present|acquire|both  (varsayılan present) — TEK kapı noktası
//  FLM_FLOOR_PACING=1          [FIX-36] VRR+MFG floor-pacing (varsayılan 1/açık).
//                              fps=0 (doğal cadence) + pacer yolunda devreye
//                              girer. Mutlak-grid yerine present'e göreli TABAN:
//                              present öncekinden en az floor kadar sonra çıkar.
//                              Ada (40-serisi, HW flip metering YOK) MFG'nin
//                              ε aralıklı generated karelerini eşitler, real
//                              kareyi frenlemez, değişken FPS'i bozmaz.
//                              0 = eski mutlak-grid pacer'a dön.
//  FLM_FLOOR_RATIO=850         [FIX-36] floor = (T/m) * ratio/1000  (500-1000).
//                              Asıl "hisle ayarlanan" knob. Düşük (700) = gevşek,
//                              jitter geçer ama daha az düzeltme. Yüksek (950) =
//                              sıkı, daha düz ama geç kalırsa hitch riski.
//                              CANLI: FLM_CONFIG dosyasına yaz + kill -USR1 <pid>.
//  FLM_FLOOR_MFG_ADAPT=1       [FIX-41] m (MFG çarpanı) arttıkça FLOOR_RATIO'yu
//                              kademeli gevşet (yalnız m>1 iken). Ada'da (40-
//                              serisi) GPU tavanda iken m=3/4'te generated kare
//                              üretim varyansı büyür; sabit sıkı ratio real
//                              kareyi de gereksiz bekletip hitch/cov artırabilir.
//                              0 = eski davranış (ratio tüm m'lerde sabit).
//  FLM_FLOOR_MFG_STEP=40       [FIX-41] her (m-1) adımı için ratio'dan düşülen
//                              miktar (0-1000 birim ölçeğinde). Örn. ratio=850,
//                              step=40 → m=2:810, m=3:770, m=4:730. Yüksek step
//                              = daha agresif gevşetme (daha az fren, biraz daha
//                              gevşek ε-eşitleme). CANLI ayarlanabilir.
//  FLM_FLOOR_AUTOTUNE=1        [FIX-44] ratio'yu kapalı çevrimle ayarla:
//                              geçen present'lerin headroom'u bolsa yavaşça
//                              sık (aralıklar düzleşir), ardışık tutulma /
//                              ince headroom görülünce hızla gevşet (fren
//                              önlenir). Delta [-150,+150], taban ratio ve
//                              MFG-adapt üstüne biner. 0 = sabit ratio.
//  FLM_PRESENT_LEAD_NS=1000000 flip'ten ne kadar önce present (ns)
//  FLM_SPIN_NS=150000          son N ns pause-spin (0 = tamamen uyku, min CPU)
//  FLM_SPIN_ADAPT=1            [FIX-39] spin payını ölçülen uyanma gecikmesine
//                              göre otomatik ayarla (30µs-2ms; hot-reload).
//                              1 iken FLM_SPIN_NS yalnız açık/kapalı anlamlıdır;
//                              0 → FLM_SPIN_NS kadar sabit spin (eski davranış).
//  FLM_DRIFT_TOLERANCE_NS=0    0 = otomatik (iv/4)
//  FLM_MFG_MULTIPLIER=0        0 = otomatik algıla, 1-4 = zorla
//  FLM_RT_PRIORITY=0           ölçüm thread SCHED_FIFO önceliği (CAP_SYS_NICE)
//  FLM_MEASURE_CPU=0-3         ölçüm thread affinity
//  FLM_STATS=1                 periyodik özet log (INFO)
//  FLM_STATS_INTERVAL=5        özet periyodu, saniye (1-3600; hot-reload) [FIX-32]
//  FLM_CSV=/tmp/flm.csv        kare bazlı ölçüm dökümü — kolonlar [FIX-31]:
//                              flip_ns,interval_ns,is_fake,is_hitch,slot,
//                              mfg,slot_mean_ns,pacing
//  FLM_CONFIG=/tmp/flm.conf    canlı ayar dosyası (KEY=VALUE, '#' yorum)
//  FLM_LOG_LEVEL=DEBUG|INFO|WARN|ERROR
//  FLM_LOG_FILE=/path          log dosyası (varsayılan stderr)
//  SIGUSR1                     FLM_CONFIG dosyasını yeniden oku (env statiktir;
//                              canlı değişiklik yalnız dosya üzerinden mümkün)
//
//  HIZLI DOĞRULAMA:
//    FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud <oyun>
//      → MangoHud'da düz 60 FPS çizgisi = katman çalışıyor.
//    A/B:  FLM_MODE=off FLM_CSV=/tmp/off.csv   vs
//          FLM_MODE=present FLM_CSV=/tmp/on.csv
//      → on.csv'de interval stddev ve p99 düşük, 1% low yüksek olmalı.
// ============================================================================
