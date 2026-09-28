# ⚠️ SORUMLULUK REDDİ

Bu katman yapay zekâ desteğiyle geliştirilmiş bir projenin parçasıdır. Kullanım riski size aittir. FLM'yi kullanarak oluşabilecek sistem kararsızlığı, GPU kilitlenmesi veya görüntü bozulmalarının sorumluluğunu kabul etmiş olursunuz. Ayrıntılar için üst dizindeki DISCLAIMER.md dosyasına bakın.

---

# FLM — Vulkan Flip Meter / Frame Pacing Katmanı (v3.0 — "auto")

VRR panellerde, özellikle frame generation (DLSS-FG / FSR-FG / MFG) açıkken
kare teslimini düzenleyen, aynı zamanda hassas bir FPS sınırlayıcı olan
Vulkan katmanı.

v3.0 kendini yapılandırır; olağan kullanımda ayarlanacak bir şey yoktur:

```bash
ENABLE_LAYER_cpu_flip_meter=1 %command%
```

## Ne yapar

| Durum | FLM'nin yaptığı |
|---|---|
| `FLM_TARGET_FPS` > 0 | **Limiter**: mutlak zaman çizelgeli FPS sınırı. presentWait gerekmez. |
| VRR, MAILBOX/IMMEDIATE | **Floor pacer**: üretilmiş/erken kareleri bir öncekinden asgari aralık geçene kadar tutar; gerçek kareler ve VRR hız değişimleri dokunulmadan geçer. |
| FIFO (vsync açık) | Kadans ölçülür. Sürekli aralıklar = VRR → pacing yapılır. Tazeleme katlarına kilitli aralıklar = sabit tazeleme → dokunulmaz. |
| Küçük swapchain'ler (<640×480) | Yok sayılır (launcher, overlay). |

Otomatik belirlenenler: frame generation çarpanı (1–4x), floor oranı (çarpan
başına kapalı döngü), uyku/spin marjı, hitch eşiği ve toparlanma süresi,
FIFO'da sabit tazeleme / VRR ayrımı.

## Değişkenler

| Değişken | Anlamı |
|---|---|
| `FLM_MODE` | `auto` (varsayılan) · `latency` (daha gevşek floor, daha hızlı hitch toparlanması) · `present` (sabit tazeleme sayılan FIFO'da da pacing) · `cap` (yalnız limiter) · `off` (A/B tabanı). Canlı değiştirilebilir. |
| `FLM_TARGET_FPS` | `>0` = FPS sınırı. `0` = doğal kadans. Canlı değiştirilebilir. |
| `FLM_FLOOR_RATIO` | İsteğe bağlı, 500–1000. Temel floor oranını ezer (auto 850, latency 780); kapalı döngü yine bunun etrafında ayarlar. Canlı değiştirilebilir. |
| `FLM_MFG_MULTIPLIER` | `0` otomatik (varsayılan), `1`–`4` zorla. Yükleme anında. |
| `FLM_RT_PRIORITY` / `FLM_MEASURE_CPU` | Ölçüm thread'i SCHED_FIFO önceliği / CPU listesi (`0-3,8`). Yükleme anında. |
| `FLM_LOG_LEVEL` / `FLM_LOG_FILE` | `DEBUG`/`INFO`/`WARN` (varsayılan)/`ERROR`; log dosyası (varsayılan stderr). |
| `FLM_STATS=1` | 5 sn'de bir INFO: ortalama, p99, max, fake/hitch sayıları, çarpan, efektif oran, FIFO kararı. |
| `FLM_CSV=/yol` | Flip başına döküm: `flip_ns,interval_ns,is_fake,is_hitch,slot,mfg,slot_mean_ns,pacing`. |
| `FLM_CONFIG=/yol` | `SIGUSR1` ile yeniden okunan `ANAHTAR=DEĞER` dosyası (yalnız canlı anahtarlar). |

`FLM_PROFILE=vrr|mfg|latency|cap|off` ve `FLM_MODE=limiter` takma ad olarak
çalışmaya devam eder. Diğer tüm v2.x `FLM_*` değişkenleri kaldırıldı; hâlâ
tanımlıysa logda bir kez raporlanır (`removed in v3 … ignored`) — silin.

## Çalıştığını doğrulama

```bash
FLM_LOG_LEVEL=INFO FLM_STATS=1 FLM_LOG_FILE=/tmp/flm.log %command%
tail -f /tmp/flm.log
```

* `STATS … mfg=4 ratio=9xx` — pacer aktif, çarpan tespit edilmiş, floor tam slota yakın.
* `ratio=0` — bu swapchain'de pacer çalışmıyor (FIFO sabit tazeleme sayıldı, presentWait yok ya da sınır ayarlı).
* VRR panelde `fifo=fixed` — oyun panelin azami tazelemesinde ya da kusursuz sabit bir hızda; düzeltilecek bir şey yok. `FLM_MODE=present` yine de zorlar.
* `presentId/Wait not supported` — bu sürücüde yalnız limiter kullanılabilir.

Aynı sahnede A/B (ilk dakikadaki shader derlemesini dışarıda bırakın):

```bash
FLM_MODE=off FLM_CSV=/tmp/off.csv %command%
FLM_CSV=/tmp/on.csv %command%
```

`interval_ns` sütununun stddev / p99 değerlerini karşılaştırın.

## Canlı ayar

```bash
ENABLE_LAYER_cpu_flip_meter=1 FLM_CONFIG=/tmp/flm.conf %command%
echo 'FLM_MODE=latency' > /tmp/flm.conf
kill -USR1 $(pidof <oyun_binary>)
```

Her reload yerleşik varsayılanlardan başlar, sonra ortam değişkenleri, sonra
dosya uygulanır — bir satırı silmek o anahtarı geri alır.

## v2.x'ten geçiş

| v2.x | v3.0 |
|---|---|
| `FLM_PROFILE=mfg` / `vrr` | hiçbir şey (varsayılan) |
| `FLM_MODE=limiter FLM_TARGET_FPS=N` | `FLM_TARGET_FPS=N` |
| `FLM_PACE_FIFO=1` | otomatik (VRR kadans tespiti); zorlamak için `FLM_MODE=present` |
| `FLM_FLOOR_*`, `FLM_SPIN_*`, `FLM_HITCH_*`, `FLM_PROBE_*`, `FLM_WARMUP_FRAMES`, `FLM_PRESENT_LEAD_NS`, `FLM_DRIFT_TOLERANCE_NS`, `FLM_PACE_POINT`, `FLM_STATS_INTERVAL`, `FLM_CSV_SYNC_S` | silin — artık dahili |
