# FLM — Vulkan Flip Meter / Frame Pacing Layer
## Türkçe Kullanım Kılavuzu (v2.7)

---

## İçindekiler

1. [FLM Nedir?](#1-flm-nedir)
2. [Kurulum](#2-kurulum)
3. [İki Temel Mod: LIMITER ve PACER](#3-i̇ki-temel-mod-limiter-ve-pacer)
4. [Hızlı Başlangıç ve Doğrulama](#4-hızlı-başlangıç-ve-doğrulama)
5. [Senaryo 1 — VRR Panel + Frame Generation (MFG)](#5-senaryo-1--vrr-panel--frame-generation-mfg)
6. [Senaryo 2 — VRR Panel, MFG Kapalı](#6-senaryo-2--vrr-panel-mfg-kapalı)
7. [Senaryo 3 — Sabit Hz Panel, FPS Cap](#7-senaryo-3--sabit-hz-panel-fps-cap)
8. [Senaryo 4 — FIFO/vsync-on Motor](#8-senaryo-4--fifovSync-on-motor)
9. [Senaryo 5 — Launcher ve Overlay Pencereleri](#9-senaryo-5--launcher-ve-overlay-pencereleri)
10. [Canlı Ayar (SIGUSR1 + Config Dosyası)](#10-canlı-ayar-sigusr1--config-dosyası)
11. [Teşhis ve Sorun Giderme](#11-teşhis-ve-sorun-giderme)
12. [A/B Testi — CSV Ölçümü](#12-ab-testi--csv-ölçümü)
13. [İleri Düzey Ayarlar](#13-i̇leri-düzey-ayarlar)
14. [Tüm Değişkenlerin Referans Tablosu](#14-tüm-değişkenlerin-referans-tablosu)
15. [Karar Ağacı](#15-karar-ağacı)

---

## 1. FLM Nedir?

FLM, Vulkan uygulamalarına transparent biçimde eklenen bir **frame pacing katmanı**dır. Vulkan'ın katman altyapısını kullanır — oyunu değiştirmenize, kaynak kodunu derlemenize gerek yoktur; yalnızca bir ortam değişkeni ile aktifleşir.

**İki şey yapar:**

- **LIMITER:** Belirttiğiniz FPS'e sabit üst sınır koyar. `presentWait` gerektirmez, her sürücü ve GPU'da çalışır.
- **PACER:** Gerçek flip timestamp'lerini ölçerek frame aralıklarını düzler. `presentWait` gerektirir (NVIDIA/AMD yeni sürücülerinde mevcut). Özellikle VRR + MFG (Frame Generation) kombinasyonunda göze çarpan titremeleri giderir.

**Neden gerekli?** RTX 40-serisi gibi donanım flip metering'i olmayan GPU'larda, DLSS-FG veya FSR-FG ile üretilen kareler birbirine **eşit aralıklı gelmiyor**: `(ε, ε, ε, T)` deseni oluşuyor (çok kısa, çok kısa, çok kısa, çok uzun). Bu durum VRR panelde gözle fark edilen jitter/titreme olarak ortaya çıkıyor. FLM, bu ε-frame'leri gerçek kareye göre daha dengeli dağıtarak düzeltiyor.

---

## 2. Kurulum

### Gereksinimler (Gentoo)

```bash
sudo emerge -av media-libs/vulkan-loader   # Vulkan headers
sudo emerge -av media-libs/vulkan-layers   # vk_layer.h
```

Diğer dağıtımlarda `vulkan-headers` ve `vulkan-validationlayers` (veya `libvulkan-dev`) paketleri gereklidir.

### Derleme ve Kurulum

```bash
# Standart derleme (/usr/local'e kurar)
./build.sh

# Belirli prefix ile
./build.sh /usr

# Bu makineye özel optimizasyon (taşınamaz, daha hızlı):
FLM_NATIVE_BUILD=ON ./build.sh
```

`FLM_NATIVE_BUILD=ON` seçeneği `-O3 -march=native -mtune=native -flto` ile derler; başka bir makinede çalışmaz ama yerel performansı artırır. Kendi Gentoo kurulumunuz için bu genelde önerilen seçenektir.

### Kurulum Doğrulama

```bash
vulkaninfo --summary | grep -i flip_meter
```

Çıktıda `VK_LAYER_cpu_flip_meter` görünüyorsa kurulum başarılı.

### Katmanın Aktifleştirilmesi

Katman **varsayılan olarak pasif**tir. Her çalıştırma için açıkça aktifleştirmeniz gerekir:

```bash
ENABLE_LAYER_cpu_flip_meter=1 <oyun_komutu>
```

Steam'de launch seçeneklerine eklemek için:
```
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_FLOOR_PACING=1 %command%
```

---

## 3. İki Temel Mod: LIMITER ve PACER

### LIMITER

- `FLM_TARGET_FPS=<n>` verdiğinizde **her zaman** LIMITER devreye girer.
- `presentWait` gerektirmez — eski GPU/sürücüde de çalışır.
- MangoHud'da **düz yatay bir çizgi** olarak görünür.
- `FLM_FLOOR_PACING` bu modda **hiçbir etkisi yoktur**.

### PACER

- `FLM_MODE=present` (veya `auto`) ve `FLM_TARGET_FPS=0` olduğunda aktiftir.
- `presentWait` desteği gereklidir. Sürücünüz desteklemiyorsa PACER hiç devreye girmez.
- Floor-pacing (`FLM_FLOOR_PACING=1`) bu modda çalışır.
- VRR + MFG kombinasyonunun asıl çözümü budur.

> **Önemli kural:** `FLM_TARGET_FPS > 0` ayarı LIMITER'ı zorla devreye sokar. Floor-pacing ve PACER devre dışı kalır. VRR senaryolarında bu değişkeni boş bırakın (0).

---

## 4. Hızlı Başlangıç ve Doğrulama

### Katmanın gerçekten çalıştığını test etme

```bash
# LIMITER ile test — MangoHud'da düz 60 FPS çizgisi görmeli
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud <oyun>
```

Düz bir çizgi görüyorsanız katman aktif. Görmüyorsanız → [Teşhis](#11-teşhis-ve-sorun-giderme) bölümüne bakın.

### Temel başlatma komutu

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_CONFIG=/tmp/flm.conf mangohud <oyun>
```

`FLM_CONFIG` dosyası sayesinde oyunu kapatmadan ayar değiştirebilirsiniz.

---

## 5. Senaryo 1 — VRR Panel + Frame Generation (MFG)

**Durum:** G-Sync/FreeSync panel açık, DLSS-FG veya FSR-FG etkin, FPS cap yok. Özellikle RTX 40-serisi gibi donanım flip metering'i olmayan GPU'larda oluşan `(ε, ε, ε, T)` bimodal jitter sorununu çözmeye çalışıyorsunuz.

### Temel Komut

```bash
ENABLE_LAYER_cpu_flip_meter=1 \
  FLM_MODE=present \
  FLM_FLOOR_PACING=1 \
  FLM_FLOOR_RATIO=850 \
  FLM_CONFIG=/tmp/flm.conf \
  mangohud <oyun>
```

### Neden Bu Ayarlar?

- `FLM_TARGET_FPS` **verilmiyor** — VRR'de sabit FPS kilidi istemiyoruz, sadece frame aralıklarını birbirine yaklaştırmak istiyoruz.
- `FLM_FLOOR_RATIO=850` başlangıç noktası: "bir kare, önceki kareden en az %85 slot genişliği sonra çıkabilir." Bu, ε-frame'leri biraz tutar ve T-frame'e dokunmaz.

### Ayar Tablosu (Hissederek)

| Hissettiğiniz şey | Değişiklik |
|---|---|
| Hâlâ mikro-titreme var, MFG'nin ritmi bozuk | `FLM_FLOOR_RATIO`'yu **yükselt**: 850 → 900 → 950 |
| Görüntü "yapışkan" hissettiriyor, input gecikmesi var | `FLM_FLOOR_RATIO`'yu **düşür**: 850 → 800 → 750 |
| Ani tek seferlik takılmalar (genel stutter, MFG jitter'ından farklı) | `FLM_FLOOR_RATIO` bunu çözmez — shader compilation veya gerçek hitch; oyun tarafı sorun |
| Ayarın hiç etkisi yok gibi | `presentWait` desteklenmiyor olabilir. Log'a bakın (aşağıda) |

### Canlı Ayar (Oyunu Kapatmadan)

```bash
# /tmp/flm.conf dosyasını düzenle:
echo "FLM_FLOOR_RATIO=920" > /tmp/flm.conf

# Yeni ayarı uygula:
kill -USR1 $(pidof <oyun_binary>)
```

Log'da şunu görürseniz değişiklik uygulanmış:
```
[FLM] Config reload: mode=1 fps=0 spin=150000 lead=1000000 floor=1 ratio=920 ...
```

### A/B Karşılaştırma (Aynı Oturumda)

```bash
# /tmp/flm.conf içine yaz:
echo "FLM_MODE=off" > /tmp/flm.conf
kill -USR1 $(pidof <oyun>)
# → FLM tamamen devre dışı

echo "FLM_MODE=present" > /tmp/flm.conf
kill -USR1 $(pidof <oyun>)
# → FLM tekrar devrede
```

### Ek Otomatik Optimizasyonlar (v2.7 ile Gelen)

FLM, MFG çarpanı (m=2,3,4) arttıkça otomatik olarak floor ratio'yu biraz gevşetir (`FLM_FLOOR_MFG_ADAPT=1`, varsayılan açık). Ayrıca closed-loop autotune (`FLM_FLOOR_AUTOTUNE=1`) zamanla ratio'yu sahne koşullarına göre ayarlar — ama hitch oluşmaya başlarsa hemen gevşer. Bu otomatikleri elle kapatmak nadiren gerekir.

---

## 6. Senaryo 2 — VRR Panel, MFG Kapalı

**Durum:** Frame generation yok, GPU doğrudan render ediyor; hafif frametime tutarsızlığı var.

```bash
ENABLE_LAYER_cpu_flip_meter=1 \
  FLM_MODE=present \
  FLM_FLOOR_PACING=1 \
  FLM_FLOOR_RATIO=800 \
  mangohud <oyun>
```

MFG olmadığında bimodal desen yok; floor-pacing çalışır ama etkisi daha hafif. `FLOOR_RATIO` değerini Senaryo 1'den daha düşük tutun (750-800 arası genelde yeter). Buradaki amaç büyük patlak düzeltmek değil, küçük pürüzleri düzleştirmek.

---

## 7. Senaryo 3 — Sabit Hz Panel, FPS Cap

**Durum:** VRR kullanmıyorsunuz, belirli bir FPS tavanı istiyorsunuz (termal/güç yönetimi, ya da MFG jitter'ını cap ile bastırmak).

```bash
ENABLE_LAYER_cpu_flip_meter=1 \
  FLM_MODE=limiter \
  FLM_TARGET_FPS=120 \
  mangohud <oyun>
```

### Hangi FPS Değerini Seçmeli?

Genel strateji: hedeflediğiniz FPS bandının **alt sınırının biraz altına** cap koymak, üst sınırdan koymaktan daha akıcı hissettiriri. Örnek:

- Oyun 150-220 FPS arası dalgalanıyorsa → `FLM_TARGET_FPS=144` veya `165`
- GPU'yu sürekli tavana zorlamak yerine biraz nefes payı bırakmak tercih edilir

### MFG + LIMITER Kombinasyonu

MFG açıkken cap koyduğunuzda, fazla üretilen kareler GPU-bound bekçisi tarafından zaten süzülür. Ekstra ayar gerekmez; sadece oyunun kaldırabileceği bir FPS seçin.

### LIMITER ile Floor-Pacing

`FLM_TARGET_FPS > 0` olduğunda floor-pacing **tamamen devre dışı** kalır. Bu iki sistem birbirini dışlar.

---

## 8. Senaryo 4 — FIFO/vsync-on Motor

**Durum:** Oyun MAILBOX/IMMEDIATE yerine FIFO kullanıyor (zaten vsync'e kilitli).

**Yapmanız gereken şey: Hiçbir şey.**

Kod bunu kendisi tespit eder (`resolve_gate`). FIFO'da PACER otomatik olarak devreye girmez — compositor zaten pacing yapıyor, üstüne FLM eklenmesi kendi jitter'ını yaratır.

LIMITER (`FLM_TARGET_FPS`) ise FIFO'da da çalışır.

### İstisna: FIFO'yu Zorla Pace Etmek

MFG'li ama yalnız FIFO sunan motorlarda veya PACER'ı compositor ile karşılaştırmak istiyorsanız:

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_PACE_FIFO=1 <oyun>
```

Bunu açmadan önce CSV ile ölçüm yapın — normal FIFO içeriğinde bu ayar jitter azaltmak yerine artırabilir.

---

## 9. Senaryo 5 — Launcher ve Overlay Pencereleri

**Durum:** Steam launcher, oyun menüsü, overlay pencereleri FLM'den etkilenmiyor mu?

Bu normaldir. FLM, 640×480 altındaki swapchain'leri (`MIN_SC_WIDTH`, `MIN_SC_HEIGHT`) otomatik olarak pace listesi dışında tutar. Ana oyun penceresi etkilenmeye devam eder.

---

## 10. Canlı Ayar (SIGUSR1 + Config Dosyası)

FLM'nin en güçlü özelliklerinden biri: oyunu kapatmadan ayar değiştirebilirsiniz.

### Kullanım

```bash
# Oyunu config dosyası ile başlat:
ENABLE_LAYER_cpu_flip_meter=1 FLM_CONFIG=/tmp/flm.conf mangohud <oyun>

# Oyun çalışırken dosyayı düzenle:
cat > /tmp/flm.conf << 'EOF'
FLM_FLOOR_RATIO=920
FLM_STATS=1
FLM_STATS_INTERVAL=3
EOF

# Değişiklikleri uygula:
kill -USR1 $(pidof <oyun_binary>)
```

### Reload Neler Yapabilir?

Aşağıdaki değişkenler canlı olarak değiştirilebilir:

- `FLM_MODE`, `FLM_TARGET_FPS`, `FLM_PACE_POINT`
- `FLM_FLOOR_PACING`, `FLM_FLOOR_RATIO`
- `FLM_FLOOR_MFG_ADAPT`, `FLM_FLOOR_MFG_STEP`
- `FLM_FLOOR_AUTOTUNE`, `FLM_FLOOR_AUTOTUNE_MAX`
- `FLM_SPIN_NS`, `FLM_SPIN_ADAPT`, `FLM_PRESENT_LEAD_NS`
- `FLM_DRIFT_TOLERANCE_NS`
- `FLM_STATS`, `FLM_STATS_INTERVAL`
- `FLM_LOG_LEVEL`
- `FLM_PACE_FIFO`
- `FLM_WARMUP_FRAMES`, `FLM_HITCH_RECOVERY`, `FLM_HITCH_THRESHOLD_MS`
- `FLM_PROBE_PERIOD_S`, `FLM_PROBE_FLIPS`

### Reload Neler Yapamaz?

Bu değişkenler **başlangıçta** okunur, sonradan değiştirilemez:

- `FLM_MFG_MULTIPLIER` (swapchain oluşturulurken sabitlenir)
- `FLM_RT_PRIORITY`, `FLM_MEASURE_CPU` (thread başlarken kurulur)
- `FLM_CSV`, `FLM_LOG_FILE`, `FLM_CONFIG` (dosyalar başlangıçta açılır)
- `FLM_STATS` (istatistik belleği başlangıçta tahsis edilir)

### Config Dosyası Sözdizimi

```ini
# Yorum satırı (# ile başlar)
FLM_FLOOR_RATIO=900
FLM_STATS=1
FLM_STATS_INTERVAL=5
# Değişken silinirse, env değişkeni geri yüklenir (varsayılana döner)
```

---

## 11. Teşhis ve Sorun Giderme

### Adım 1: Katman Yükleniyor mu?

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log mangohud <oyun>

# Başka bir terminalde:
tail -f /tmp/flm.log
```

Log'un başında şunu görmelisiniz:
```
[FLM] Config: mode=0 fps=0 floor=1 ratio=850 spin=150000 lead=1000000 ...
```

Bu satırı görmüyorsanız katman yüklenmiyor demektir. `vulkaninfo --summary | grep flip_meter` ile kontrol edin.

### Adım 2: presentWait Destekleniyor mu?

```
[FLM] presentId/Wait desteklenmiyor; PACER kapali
```

Bu mesajı görüyorsanız PACER ve floor-pacing **hiç çalışmaz**. Yalnızca `FLM_TARGET_FPS` ile LIMITER kullanabilirsiniz.

presentWait desteği için NVIDIA'da sürücü 525+ ve **Wayland** (veya Xwayland) önerilir. X11'de desteklemeyebilir.

### Adım 3: FIFO Swapchain mi?

`FLM_MODE=present` ile PACER hiç tetiklenmiyorsa, oyun muhtemelen FIFO kullanıyor. Test:

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_PACE_POINT=acquire FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log <oyun>
```

Hâlâ tetiklenmiyorsa FIFO'dur — bu beklenen davranış.

### Adım 4: Warmup'ı Geçti mi?

İlk **30 kare** (`WARMUP_FRAMES`, varsayılan) hiç pace edilmez. Oyunun başında ilk saniyede fark görmemek normaldir.

### Adım 5: Periyodik Özet İstatistik

```bash
ENABLE_LAYER_cpu_flip_meter=1 \
  FLM_LOG_LEVEL=INFO \
  FLM_STATS=1 \
  FLM_STATS_INTERVAL=5 \
  FLM_LOG_FILE=/tmp/flm.log \
  mangohud <oyun>
```

Her 5 saniyede bir istatistik özetini log'a yazar. Ayar denerken canlı geri bildirim için kullanışlıdır.

### Adım 6: MFG Tespiti Kontrol

MFG çarpanının sürekli değiştiğini düşünüyorsanız:

```bash
FLM_LOG_LEVEL=DEBUG FLM_LOG_FILE=/tmp/flm.log <oyun>
```

Log'da `MFG carpani: X -> Y` satırlarını takip edin. Çok sık değişiyorsa çarpanı elle sabitleyebilirsiniz:

```bash
FLM_MFG_MULTIPLIER=2  # DLSS-FG için genelde 2
```

---

## 12. A/B Testi — CSV Ölçümü

Ayarların gerçekten işe yarıyıp yaramadığını nesnel olarak kanıtlamak için:

```bash
# Kapalı halde kayıt:
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=off FLM_CSV=/tmp/off.csv mangohud <oyun>

# Açık halde kayıt:
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_FLOOR_PACING=1 FLM_CSV=/tmp/on.csv mangohud <oyun>
```

CSV'yi Python veya LibreOffice Calc ile açın. `interval_ns` sütununun **standart sapması** ve **p99** değerine bakın:

- `on.csv`'de stddev daha düşük → frame timing iyileşmiş
- `on.csv`'de p99 daha düşük → büyük anlık gecikmeler azalmış

**Önemli:** İlk 1-2 dakikayı (shader derleme dönemi) analizden çıkarın. Stabil bir sahnede kayıt alın.

CSV sütunları: `timestamp_ns`, `interval_ns`, `eff_mfg`, `slot_mean_ns`, `pacing`

---

## 13. İleri Düzey Ayarlar

### Ölçüm Thread'ini CPU'ya Sabitleme

Birden fazla CCD olan Ryzen sistemlerinde (özellikle sizin 7845HX), ölçüm thread'ini oyunla aynı CCD'de tutmak gecikmeyi azaltabilir:

```bash
FLM_MEASURE_CPU=0-7    # CCD 0 (ilk 8 çekirdek)
FLM_MEASURE_CPU=8-11   # Belirli çekirdekler
```

### Realtime Öncelik

```bash
FLM_RT_PRIORITY=40     # SCHED_FIFO önceliği (root veya CAP_SYS_NICE gerekir)
```

### Spin Penceresi Ayarı

240Hz ve üzerinde kernel wake-up gecikmesi sorunu yaşıyorsanız:

```bash
FLM_SPIN_NS=300000     # Varsayılan: 150000 ns
```

Bu değeri artırmak CPU kullanımını biraz yükseltir ama timing hassasiyetini artırır.

FLM v2.3'ten itibaren spin penceresi **adaptif** olarak ayarlanır (`FLM_SPIN_ADAPT=1`, varsayılan). Kernel uyanma gecikmesini ölçerek spin marjını otomatik optimize eder. Eski sabit davranış için:

```bash
FLM_SPIN_ADAPT=0       # Adaptif spin'i kapat
```

### Present Lead (Gate Önceleme)

Flip tahmininden ne kadar önce present işlemi yapılacağını belirler:

```bash
FLM_PRESENT_LEAD_NS=1500000   # Varsayılan: 1000000 ns (1ms)
```

240Hz gibi çok yüksek Hz'de sorun yaşıyorsanız önce `FLM_SPIN_NS`'i deneyin.

### Hitch Eşiği ve Kurtarma

```bash
FLM_HITCH_THRESHOLD_MS=0      # 0 = otomatik (max(1.5*T, T+2ms), T+30ms sınırlı)
FLM_HITCH_RECOVERY=8          # Hitch sonrası kaç kare pas geçilsin (varsayılan: 8)
```

### Warmup Kareleri

```bash
FLM_WARMUP_FRAMES=30           # Varsayılan: 30
```

### MFG Probe Ayarları

FLM, MFG tespitini doğrulamak için periyodik probe yapar. Bunu kapatmak için (yalnızca `FLM_MFG_MULTIPLIER` ile sabit çarpan kullanıyorsanız):

```bash
FLM_PROBE_PERIOD_S=0           # 0 = probing devre dışı
# veya
FLM_PROBE_FLIPS=0
```

Varsayılan: 10 saniyede bir, 24 flip süresince probe.

### Drift Toleransı

```bash
FLM_DRIFT_TOLERANCE_NS=0       # 0 = otomatik (iv/4)
FLM_DRIFT_TOLERANCE_NS=2000000 # Manuel: 2ms
```

### Autotune Detayları

```bash
FLM_FLOOR_AUTOTUNE=1           # Varsayılan: açık
FLM_FLOOR_AUTOTUNE_MAX=300     # Pozitif yön üst sınırı (0-500 arası, varsayılan: 300)
FLM_FLOOR_MFG_ADAPT=1          # MFG çarpanına göre ratio gevşetme (varsayılan: açık)
FLM_FLOOR_MFG_STEP=40          # Her extra m adımı için ratio birimi (0-200)
```

---

## 14. Tüm Değişkenlerin Referans Tablosu

| Değişken | Varsayılan | Aralık/Seçenekler | Canlı? | Açıklama |
|---|---|---|---|---|
| `ENABLE_LAYER_cpu_flip_meter` | (yok) | `1` | Hayır | Katmanı aktifleştirir |
| `FLM_MODE` | `auto` | `auto\|present\|limiter\|off` | Evet | Ana çalışma modu |
| `FLM_TARGET_FPS` | `0` | `0-1000` | Evet | `>0` → LIMITER; `0` → PACER |
| `FLM_FLOOR_PACING` | `1` | `0\|1` | Evet | Floor-pacing açık/kapalı |
| `FLM_FLOOR_RATIO` | `850` | `500-1000` | Evet | **Ana ayar knob'u.** Yüksek=sıkı/düz, düşük=gevşek/az gecikme |
| `FLM_FLOOR_MFG_ADAPT` | `1` | `0\|1` | Evet | MFG çarpanına göre otomatik ratio gevşetme |
| `FLM_FLOOR_MFG_STEP` | `40` | `0-200` | Evet | Her extra m adımı için ratio düşüş miktarı |
| `FLM_FLOOR_AUTOTUNE` | `1` | `0\|1` | Evet | Closed-loop ratio otomatik ayarı |
| `FLM_FLOOR_AUTOTUNE_MAX` | `300` | `0-500` | Evet | Autotune pozitif yön üst sınırı |
| `FLM_PACE_POINT` | `present` | `present\|acquire\|both` | Evet | Gate noktası; varsayılan bırakın |
| `FLM_PACE_FIFO` | `0` | `0\|1` | Evet | FIFO swapchain'lerde PACER'ı zorlama |
| `FLM_PRESENT_LEAD_NS` | `1000000` | `0-8000000` | Evet | Flip öncesi present öncesi (ns) |
| `FLM_SPIN_NS` | `150000` | `0-2000000` | Evet | Spin penceresi genişliği (ns) |
| `FLM_SPIN_ADAPT` | `1` | `0\|1` | Evet | Adaptif spin (ölçülen latency'ye göre) |
| `FLM_DRIFT_TOLERANCE_NS` | `0` | `0+` | Evet | `0` = otomatik (iv/4) |
| `FLM_MFG_MULTIPLIER` | `0` | `0-4` | **Hayır** | `0` = otomatik tespit; `1-4` = zorlama |
| `FLM_RT_PRIORITY` | `0` | `0-99` | **Hayır** | Ölçüm thread'i SCHED_FIFO önceliği |
| `FLM_MEASURE_CPU` | (yok) | `0-3`, `0,4` vb. | **Hayır** | Thread CPU affinity |
| `FLM_STATS` | `0` | `0\|1` | **Hayır** | Periyodik istatistik log'u |
| `FLM_STATS_INTERVAL` | `5` | `1-3600` (sn) | Evet | İstatistik log aralığı |
| `FLM_CSV` | (yok) | dosya yolu | **Hayır** | CSV telemetri çıktısı |
| `FLM_CONFIG` | (yok) | dosya yolu | **Hayır** | Canlı config dosyası |
| `FLM_LOG_LEVEL` | `ERROR` | `DEBUG\|INFO\|WARN\|ERROR` | Evet | Log seviyesi |
| `FLM_LOG_FILE` | stderr | dosya yolu | **Hayır** | Log çıktı dosyası |
| `FLM_WARMUP_FRAMES` | `30` | `0-10000` | Evet | Başlangıçta pace edilmeyen kare sayısı |
| `FLM_HITCH_RECOVERY` | `8` | `0-240` | Evet | Hitch sonrası bekleme kare sayısı |
| `FLM_HITCH_THRESHOLD_MS` | `0` | `0-1000` (ms) | Evet | `0` = otomatik hitch eşiği |
| `FLM_PROBE_PERIOD_S` | `10` | `0-3600` (sn) | Evet | `0` = probe devre dışı |
| `FLM_PROBE_FLIPS` | `24` | `0-240` | Evet | Probe süresi (flip sayısı) |

---

## 15. Karar Ağacı

```
OTURUMU BAŞLATMADAN ÖNCE: Katman aktif mi?
  vulkaninfo --summary | grep flip_meter
  → Çıktı yok: kurulum sorunu

─────────────────────────────────────────────

VRR panel + MFG açık, cap istemiyorsun:
  FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850
  → Hâlâ jitter var        → RATIO yükselt (900, 950)
  → Input gecikir/ağırlaşır → RATIO düşür (800, 750)
  → Hiç etki yok            → Log'a bak (presentWait? FIFO?)

─────────────────────────────────────────────

MFG jitter'ı çok fazla, cap ile bastırmak istiyorsun:
  FLM_MODE=limiter FLM_TARGET_FPS=<alt-sınır, ör. 144 veya 165>

─────────────────────────────────────────────

VRR panel, MFG kapalı, hafif pürüzler:
  FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=800

─────────────────────────────────────────────

FIFO / vsync-on motor:
  Hiçbir şey yapma (kod otomatik doğrusunu seçer)
  LIMITER istiyorsan: FLM_TARGET_FPS=<n>

─────────────────────────────────────────────

Ayar etkisiz görünüyor:
  1. FLM_LOG_LEVEL=INFO → "Config:" satırı görüyor musun?
  2. "presentId/Wait desteklenmiyor" var mı? → Sadece LIMITER kullan
  3. FLM_PACE_POINT=acquire dene → Hâlâ yok → FIFO
  4. Warmup'ı geçti mi? (30 kare)
  5. FLM_CSV ile ölç, shader-cache bitince karşılaştır

─────────────────────────────────────────────

Nesnel kanıt istiyorsun:
  FLM_CSV=/tmp/off.csv + FLM_MODE=off → kayıt al
  FLM_CSV=/tmp/on.csv  + FLM_MODE=present → kayıt al
  interval_ns sütunu: stddev ve p99 karşılaştır
```

---

## Hızlı Kopya-Yapıştır Komutları

```bash
# Teşhis — katman ve presentWait kontrolü
ENABLE_LAYER_cpu_flip_meter=1 FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log mangohud <oyun>

# VRR + MFG (RTX 40-serisi, DLSS-FG/FSR-FG)
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850 FLM_CONFIG=/tmp/flm.conf mangohud <oyun>

# Sabit FPS cap
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=limiter FLM_TARGET_FPS=165 mangohud <oyun>

# A/B baseline (kapalı)
ENABLE_LAYER_cpu_flip_meter=1 FLM_MODE=off FLM_CSV=/tmp/off.csv mangohud <oyun>

# Canlı ayar değişikliği
echo "FLM_FLOOR_RATIO=920" > /tmp/flm.conf && kill -USR1 $(pidof <oyun>)
```
