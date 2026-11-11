# DRSTool Experimental — Performans ve Hata İyileştirme Planı Entegrasyonu

**Tarih:** 11 Kasım 2026
**Kaynak:** `DRSTool-experimental(1)(1).zip`
**İş planı:** `DRSTool_performans_ve_hata_iyilestirme_plani(2).txt`

## Uygulanan iyileştirmeler

| Plan | Durum | Kod / karşılık |
|---|---|---|
| P0-A Tool Manager temizliği | Uygulandı | Başarılı kurulumda `ok = True`, parça ve HTTP yan dosyasını sil; iptal/başarısızlıkta kısmi veriyi tut. `drstool_gui/tool_manager.py`. |
| P0-B GUI'yi kilitleyen cache silme | Uygulandı | Qt worker, per-file iptal ve progress, güvenli `lstat`, symlink takip etmeme, kısmi başarısızlık detayları, silme sonrasında asenkron yeniden tarama. `drstool_core/cache_ops.py`, `drstool_gui/cache_manager.py`. |
| P1-A Sonsuz kapanış beklemesi | Uygulandı | `TaskCenter` iptal sözleşmesi, sınırlı bekleme, kapanış için Qt olay döngüsüyle yoklama, 12 sn sonra açık pencereye dönerek QThread'i çalışan halde yok etmeyi engelleme. `tasks.py`, `main_window.py`, `sysinfo.py`, `dll_manager.py`, `game_card.py`. |
| P1-B Steam VDF atomik işlem | Uygulandı | Her iki VDF'yi yazmadan önce hesaplama, ikinci yazma başarısızlığında rollback, SHA-256 ve dışarıdan değişiklik tespiti, kalıcı işlem günlüğü ve yedek. `launchers.py` ve mevcut `drstool_core/file_transactions.py`. |
| P1-C HTTP Range / resume | Uygulandı | `Content-Range` ofseti, doğrulayıcı `If-Range`, 200/206/416, kesik yanıt, URL yan dosyası ve mevcut checksum doğrulaması. Sağlayıcı checksum sunmuyorsa `UNVERIFIED` raporlanır. `drstool_core/http_download.py`. |
| P1-D Lutris senkron YAML taraması | Uygulandı | Arka plan işçisi, 180 ms debounce, `(mtime_ns,ctime_ns,size,inode)` metadata cache, bozuk/silinen YAML toleransı ve tarama iptali. `lutris_config_scan.py`, `lutris_sync.py`. |
| P1-E YAML yorum/quote kaybı | Uygulandı | `ruamel.yaml` round-trip zorunlu bağımlılık, kullanıcı yorumları ve tırnaklarını koruyan merge; mevcut `owned` sahiplik sınırları korundu. Kütüphane yoksa kaydetme reddedilir. `yaml_merge.py`. |
| P1-P Launcher taraması / preview | Uygulandı | Steam/Heroic/Bottles scan Qt thread'ine taşındı; Steam preview VDF metadata bazlı sınırlı cache. Var olan yalnızca görünür önizleme/debounce korunuyor. |
| P2-P DLL taraması | Uygulandı | 64 oyun girdisi kapasiteli, dizin ve eşleşen DLL stat kontrolleri olan bellekiçi cache; `Force full rescan`. Mevcut 9 seviye / 250.000 giriş sınırları korunuyor. |
| P2-P Cache tarama maliyeti | Kısmen | Worker iptal ve file-progress eklendi; isteğe bağlı kalıcı disk snapshot yerine güvenilir tam tarama devam ediyor. |
| P2-P Qt liste / arama profili | Ölçüm altyapısı | `DRSTOOL_PROFILE_GUI=1` ile `MainWindow.startup/switch_tab`, Ctrl+K palette, DRS ve Env var liste oluşturma süreleri `QElapsedTimer` ile loglanır. Model/view kapsamlı dönüşümü yapılmadı. |
| P2-P FLM sıcak yol | Algoritma korunuyor | Pacing/driver davranışı değişmedi. Mevcut `scripts/test-flm-sanitizers.sh` için CI derleme işi tanımlandı. Vulkan/GPU donanımında ölçüm yapılamadı. |
| Test ve CI | Uygulandı / ortam kısıtlı | Qt-free / Qt-enabled / native FLM / wheel smoke ayrı CI işleri; 25 yeni regresyon testi, `verify-core.sh`, `verify-full.sh`, sentetik benchmark betiği. |
| Paketleme | Güncellendi | `pyproject.toml`, Gentoo ebuild, Arch PKGBUILD içinde gerekli `ruamel.yaml` bağımlılığı. Kaynak arşivi ve git patch üretildi. |

## Doğrulama — bu çalışma ortamında

- `python -m compileall -q drstool_core drstool_gui tests`: başarılı.
- `python -m unittest discover -s tests -p test_core.py -v`: **49 başarılı**.
- `python -m unittest discover -s tests -p test_integration_plan.py -v`: **25 başarılı**.
- Diğer Qt-bağımsız geçmiş testler `test_v3.py` **9**, `test_safety.py` **17**, `test_enhancements.py` **17**: başarılı.
- `lutris-game-tune-main/tests/run-tests.sh`: **81 başarılı, 0 başarısız**.
- C wrapper `gcc -Wall -Wextra -Werror -fsyntax-only`: başarılı.
- `git diff --check`: başarılı.
- `scripts/benchmark-scan.py`: 50/250/1000 sahte Lutris YAML ve 800 sentetik oyun dosyasıyla cold/warm karşılaştırma üretildi. Bu değerler gerçek Gentoo diski, GPU veya oyun frametime ölçümleri değildir.

**Doğrulanmayanlar:** Bu ortamda **PySide6**, Vulkan SDK header'ları ve tam paket derleyicisi olmadığı için Qt offscreen GUI testleri, kurulmuş wheel üzerinden smoke, FLM ASan/UBSan/TSan derlemesi ve gerçek GPU frametime/Xid/driver testleri çalıştırılamadı. CI tanımları bu kontrolleri uygun ortamda koşturacak şekilde hazırlandı; CI henüz burada yürütülmedi. GPU tarafında kararlılık iddiası yoktur.

## Kurulum / ek doğrulama (Gentoo / KDE Wayland)

```bash
cd DRSTool-experimental
python -m pip install -e .
bash scripts/verify-core.sh
# PySide6, Vulkan SDK ve derleyici mevcutsa:
bash scripts/verify-full.sh
# Sentetik test, kendi F2FS veya NTFS yolunda da kullanılabilir:
python scripts/benchmark-scan.py --out /tmp/drstool-perf.json
# Opsiyonel GUI gecikme kaydı:
DRSTOOL_PROFILE_GUI=1 drstool
```

*Root yetkisi gerektirmeyen Python geliştirme kurulumu için Gentoo'ya uygun bir venv kullanın. Ebuild/PKGBUILD paketlerini önce test kurulumunda derleyin.*

## Geri alma / bakım

- Güncellemeler için kaynak arşivi ve orijinal arşiv birbirinden bağımsızdır; orijinal sürüme geçmek için eski kaynak kopyasını kullanın ya da oluşturulan patch'i `git apply -R` ile geri alın.
- Steam VDF işlemleri için kalıcı günlük ve dosya yedekleri `$XDG_STATE_HOME/drstool/steam-transactions/` altında (değişken yoksa `~/.local/state/drstool/steam-transactions/`) tutulur. `transaction.json` ve `files/` içeriğini birlikte saklayın. Mevcut `drstool_core.file_transactions.restore()` yalnızca checksum'ı eşleşen hedefleri geri yükler, dışarıdan değişmiş dosyayı ezmez.
- Lutris kullanıcı YAML dosyalarını düzenlemeden önce program içinde önizleme yapın; geçersiz YAML veya eksik ruamel bağımlılığında kaydetme reddedilir.
- Başlangıçta `DRSTOOL_PROFILE_GUI` kapalıdır; üretim ortamında ek profilleme maliyeti yoktur.
- FLM pacing, adaptive spin ve swapchain lookup alanlarına dokunulmadı. **Xid 154 / UVM korelasyonu neden-sonuç kanıtı değildir**; Gentoo üzerinde validation layer, frametime CSV ve kernel driver logları ile ayrıca sınanmalıdır.

## Açık doğrulama kapıları

1. Gentoo/OpenRC + KDE Plasma/Wayland'de GUI açılış, sekme, Ctrl+K, kapanış ve cache iptal akışını gerçek PySide6 ile sınayın.
2. Hata enjeksiyonuyla Steam iki VDF'nin ikinci yazma hatasını, dış düzenlemeyi ve Undo akışını gerçek Steam kapalıyken sınayın.
3. F2FS ve NTFS/FUSE üzerinde sentetik benchmark'ı çalıştırıp, örnek projedeki otomatik karşılaştırma betiğiyle sıcak/soğuk sonuçları kıyaslayın.
4. Vulkan SDK sağlanınca ASan/UBSan/TSan derlemelerini ve mümkünse FLM runtime validation/VRR/MFG frametime deneylerini tamamlayın.
5. Gentoo ebuild, Arch PKGBUILD, wheel build + smoke ve gerçek launcher konfigürasyonlarıyla paket testi yapın.
