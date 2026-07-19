# FLM — Vulkan Flip Meter / Frame Pacing Layer (v2.2 + FIX-36 floor-pacing)

Vulkan katmanı olarak çalışan bir frame pacing aracı. İki bağımsız yolu var:

- **LIMITER** — sabit FPS tavanı (presentWait gerekmez, her zaman çalışır)
- **PACER** — doğal cadence'ı düzler (presentWait gerekir); VRR + MFG (özellikle
  40-serisi gibi donanım flip metering'i olmayan GPU'larda) için `FLM_FLOOR_PACING`
  eklendi

Bu README "hangi durumda hangi ayar" sorusuna cevap vermek için yazıldı. Env
değişkenlerinin tam listesi dosyanın en altında; burada senaryo bazlı kullanım var.

---

## Hızlı başlangıç

```bash
FLM_MODE=present FLM_CONFIG=/tmp/flm.conf mangohud <oyun>
```

Bu, presentWait varsa PACER'ı, yoksa otomatik LIMITER'a düşer. `FLM_CONFIG`
dosyası canlı ayar için — oyunu kapatmadan değiştirip `kill -USR1 <pid>` ile
yeniden yükletebilirsin. Bu README boyunca hep bu ikiliyi kullanacağız.

Doğrulama: `FLM_MODE=limiter FLM_TARGET_FPS=60 mangohud <oyun>` çalıştır,
MangoHud'da **düz 60 FPS çizgisi** görüyorsan katman devrede demektir.

---

## Senaryo 1 — VRR panel + Frame Generation (asıl geliştirilme amacı)

**Durum:** G-Sync/FreeSync panel, MFG (DLSS-FG / FSR-FG) açık, FPS cap
koymuyorsun, oyun 100-250 FPS arası dalgalanıyor. Özellikle **donanım flip
metering'i olmayan GPU'larda** (RTX 40-serisi gibi) generated kareler eşit
aralıklı çıkmıyor — kısa/kısa/kısa/uzun deseni (ε,ε,ε,T) panelde titreme
olarak hissediliyor.

**Ne yapılmalı:** PACER + floor-pacing. Bu, `FLM_FLOOR_PACING`'in tam olarak
çözmek için var olduğu durum.

```bash
FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850 \
FLM_CONFIG=/tmp/flm.conf mangohud <oyun>
```

**Neden bu ayarlar:**
- `FLM_TARGET_FPS` **verilmiyor** (0/doğal cadence) — VRR'de sabit FPS'e
  kilitlemek istemiyoruz, sadece kare aralıklarını birbirine yaklaştırmak
  istiyoruz.
- `FLM_FLOOR_RATIO=850` başlangıç noktası. Bu, "bir kare öncekinden en az
  %85 slot-genişliği sonra çıkabilir" demek. ε aralıklı generated kareyi
  bekletir, real kareye dokunmaz.

**Hissederek ayarlama:**

| Hissettiğin şey | Yapılacak değişiklik |
|---|---|
| Hâlâ mikro-titreme var, MFG'nin ritmi bozuk hissediyorsun | `FLM_FLOOR_RATIO`'yu **yükselt** (900 → 950). Taban sıkılaşır, kareler daha düz aralanır. |
| Görüntü "yapışkan" / input gecikmesi hissediyorsun, kontroller ağır | `FLM_FLOOR_RATIO`'yu **düşür** (750 → 700). Taban gevşer, doğal jitter bir miktar geri gelir ama gecikme azalır. |
| Ani, tek seferlik takılmalar (genel stutter, MFG jitter'ından farklı) | Muhtemelen shader-comp veya gerçek hitch — floor-pacing bunu zaten pas geçer (`hitch_active` guard'ı). `FLM_FLOOR_RATIO` bunu düzeltmez, oyun tarafı sorun. |
| Ayarın hiç etkisi yokmuş gibi hissediyorsan | `presentWait` desteklenmiyor olabilir (log'da "presentId/Wait desteklenmiyor" satırına bak — `FLM_LOG_LEVEL=INFO`). O zaman yalnız LIMITER çalışır, floor-pacing hiç devreye girmez. |

**Canlı ayar (oyunu kapatmadan):**
```bash
# /tmp/flm.conf
FLM_FLOOR_RATIO=900
```
```bash
kill -USR1 $(pidof <oyun_binary>)
```
Log'da `Config reload: mode=... fps=... spin=... lead=...` satırını görürsen
uygulanmıştır.

**A/B karşılaştırma (aynı sahnede):**
```bash
# Kapalı:
FLM_MODE=off mangohud <oyun>
# Açık:
FLM_MODE=present FLM_FLOOR_PACING=1 mangohud <oyun>
```
`FLM_MODE=off` yazıp `flm.conf` içine `FLM_MODE=off` koyup `SIGUSR1`
göndererek de aynı oyun oturumunda anında geçiş yapabilirsin.

---

## Senaryo 2 — VRR panel, MFG kapalı, sadece doğal cadence düzeltmesi

**Durum:** Frame generation yok, GPU render doğrudan panele gidiyor, ama
CPU/GPU dalgalanmasından kaynaklı hafif frametime tutarsızlığı var.

```bash
FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=800 mangohud <oyun>
```

MFG yokken `m=1` sabit kalır, floor-pacing yine çalışır ama etkisi daha
hafiftir (zaten ε/T bimodal deseni yok). `FLOOR_RATIO`'yu MFG senaryosuna
göre biraz daha düşük tutmak (750-800) genelde yeterli — burada amaç
titreşimi bastırmak değil, küçük pürüzleri düzleştirmek.

---

## Senaryo 3 — Sabit Hz panel (60/120/144 vsync), FPS cap istiyorsun

**Durum:** VRR yok ya da kullanmıyorsun, belirli bir FPS tavanına oturtmak
istiyorsun (ör. termal/güç nedeniyle, ya da MFG'nin jitter'ını FPS cap ile
bastırmak istiyorsun).

```bash
FLM_MODE=limiter FLM_TARGET_FPS=120 mangohud <oyun>
```

`FLM_TARGET_FPS>0` verildiği an LIMITER devreye girer ve `FLM_FLOOR_PACING`
bu yolda **hiç etkili değildir** — cap yolu ayrı, mutlak-hedef limiter
mantığını kullanır (floor-pacing yalnız `fps=0` pacer yolunda çalışır, bkz.
kod: `FIX-36` bloğu `if (fps > 0)` dalına girmez).

**Sen daha önce şunu söylemiştin:** "bazı oyunlarda MFG çok fazla jitter
üretiyor, o yüzden FPS kilitlemek gerekiyor" — bu tam olarak bu senaryo.
Cap koyduğunda MFG'nin ürettiği fazla kareler zaten GPU-bound bekçisi
tarafından süzülüyor (`over_target_run` → `pacing_enabled=false`), yani
LIMITER + MFG combo'sunda ekstra bir ayar gerekmez; sadece hedef FPS'i
oyunun kaldırabileceği yere çek.

**Hangi FPS'i seçmeli:** Senin stratejin zaten 150-220 FPS bandını
hedeflemek. Eğer bir oyun bu bandı VRR'de tutamıyorsa (çok fazla düşüş
yaşıyorsa), cap'i bandın **alt sınırının biraz altına** (ör. 144 veya 165)
koymak, üst sınırdan cap koymaktan daha akıcı hissettirir — çünkü GPU'yu
sürekli tavana zorlamak yerine biraz payla çalıştırırsın.

---

## Senaryo 4 — FIFO/vsync-on modunda çalışan bir motor

**Durum:** Oyun MAILBOX/IMMEDIATE değil, FIFO kullanıyor (zaten vsync'e
kilitli).

Hiçbir şey yapmana gerek yok — kod bunu kendisi tespit ediyor
(`resolve_gate`): FIFO'da PACER hiç devreye girmez (compositor'la
çakışmasın diye), yalnız LIMITER (varsa `FLM_TARGET_FPS`) çalışır. Floor-
pacing de aynı şekilde FIFO'da pasif kalır.

---

## Senaryo 5 — Küçük/yardımcı swapchain'ler (launcher, overlay pencereleri)

Bunlar otomatik olarak pace edilmez (`MIN_SC_WIDTH=640`, `MIN_SC_HEIGHT=480`
altı → `pace_allowed=false`). Ayar gerektirmez, bilgi amaçlı: ana oyun
penceresi etkilenmeye devam eder.

---

## Genel teşhis: "Hiçbir ayar bir şey değiştirmiyor gibi"

Sırayla kontrol et:

1. **Katman gerçekten yükleniyor mu?**
   ```bash
   FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log mangohud <oyun>
   tail -f /tmp/flm.log
   ```
   `Config: mode=... fps=... ...` satırını görmelisin.

2. **presentWait destekleniyor mu?**
   Log'da `presentId/Wait desteklenmiyor; PACER kapali` varsa, floor-pacing
   dahil hiçbir PACER özelliği çalışmaz — yalnız LIMITER (`FLM_TARGET_FPS`)
   kullanılabilir.

3. **Swapchain FIFO mu?**
   `FLM_MODE=present` iken PACER'ın hiç tetiklenmediğini düşünüyorsan, oyun
   muhtemelen FIFO kullanıyor (Senaryo 4). `FLM_PACE_POINT=acquire` deneyip
   fark var mı bak — hâlâ yoksa FIFO'dur, normal.

4. **Warmup'ı geçmiş mi?**
   İlk 30 kare (`WARMUP_FRAMES`) hiç pace edilmez — oyunun ilk saniyesinde
   fark almazsan endişelenme.

5. **CSV ile ölç (shader-cache gürültüsüne dikkat):**
   ```bash
   FLM_CSV=/tmp/flm.csv FLM_MODE=present FLM_FLOOR_PACING=1 mangohud <oyun>
   ```
   İlk 1-2 dakikayı (shader derleme dönemi) analiz dışı bırak, sonrasında
   `interval_ns` sütununun stddev'ine bak.

---

## Değişkenler — tam referans

| Değişken | Ne zaman değiştirilir |
|---|---|
| `FLM_MODE=auto\|present\|limiter\|off` | `off`: A/B taban çizgisi. `limiter`: sabit FPS cap istiyorsan. `present`: VRR/PACER istiyorsan. `auto` (varsayılan): genelde bunu bırak, kod doğrusunu seçer. |
| `FLM_TARGET_FPS=<n>` | **>0 verirsen LIMITER'a geçer**, floor-pacing devre dışı kalır. VRR + MFG senaryosunda bunu **boş bırak** (0). |
| `FLM_FLOOR_PACING=1\|0` | VRR+MFG'de aç (varsayılan zaten açık). Eski mutlak-grid pacer'a dönmek istersen `0`. |
| `FLM_FLOOR_RATIO=850` | **Asıl hisle ayarlanan knob.** Yüksek=daha düz/daha sıkı, düşük=daha gevşek/daha az gecikme. 700-950 arası mantıklı aralık. |
| `FLM_PACE_POINT=present\|acquire\|both` | Varsayılan `present` bırak. `both` yalnız present'in tek başına yetmediğini CSV ile doğrularsan dene. |
| `FLM_PRESENT_LEAD_NS` | Yüksek Hz'de (240Hz gibi) varsayılan genelde yeterli; sorun yaşarsan `FLM_SPIN_NS`'i önce artır. |
| `FLM_SPIN_NS=150000` | 240Hz gibi çok yüksek Hz'de kernel uyandırma gecikmesi hissedersen artır (ör. 300000). CPU kullanımını bir miktar yükseltir. |
| `FLM_MFG_MULTIPLIER=0` | Otomatik tespit yanılıyorsa (log'da `MFG carpani: X -> Y` sık sık değişiyorsa) çarpanı elle sabitle (1-4). |
| `FLM_RT_PRIORITY` / `FLM_MEASURE_CPU` | Ölçüm thread'i CPU'da rakip görüyorsa (yüksek çekirdek sayılı sistemde genelde gerekmez). |
| `FLM_STATS=1` + `FLM_STATS_INTERVAL=5` | Periyodik özet log; ayar denerken canlı geri bildirim için aç. |
| `FLM_CSV=/tmp/flm.csv` | Kalıcı ölçüm — shader-cache bitmiş, stabil bir sahnede A/B karşılaştırması için. |
| `FLM_CONFIG=/tmp/flm.conf` + `SIGUSR1` | Yukarıdaki her ayarı oyunu kapatmadan değiştirmek için. Ayar denemelerinin normal yolu bu olmalı. |
| `FLM_LOG_LEVEL` / `FLM_LOG_FILE` | Teşhis için `INFO`, MFG geçişlerini izlemek için `DEBUG`. |

---

## Özet karar ağacı

```
VRR panel + MFG açık, cap istemiyorsun
  → FLM_MODE=present FLM_FLOOR_PACING=1 FLM_FLOOR_RATIO=850
    → titriyor  → RATIO yükselt
    → ağırlaşmış → RATIO düşür

MFG'nin jitter'ı çok fazla, cap ile bastırmak istiyorsun
  → FLM_MODE=limiter FLM_TARGET_FPS=<alt-sınır civarı, ör. 144-165>

FIFO/vsync-on motor
  → hiçbir şey yapma, kod otomatik doğrusunu seçer

Ayar etkisiz görünüyor
  → FLM_LOG_LEVEL=INFO ile logla, presentWait + FIFO kontrolü yap
```
