# DRSTool – İkinci tur mühendislik teslim raporu (03.11.2026)

Bu kaynak paket önceki `DRSTool-Improved-2026-11-03.zip` üzerine yapılan **ek değişiklikleri** içerir; önceki güvenlik düzeltmeleri korunmuştur.

## Bağımlılık ve çalışma ortamı

- Kurulu ve kullanılabilir: Python 3.13, PyYAML, ruamel.yaml 0.18.17, gcc, g++, clang++, cmake, bash.
- **Kurulamadı:** PySide6. PyPI erişimi DNS / ağ hatası verdi. Ağ erişimi olmadığı için paket indirme gerçekleşmedi. Qt GUI testleri bu ortamda yürütülemedi.
- **Kurulamadı:** Vulkan geliştirme başlıkları ve CMake için Vulkan dev paketi. Sistemde Vulkan çalışma zamanı var, ancak `vulkan/vulkan.h` bulunmuyor. FLM ASAN/UBSAN/TSAN derleme kontrolü `find_package(Vulkan)` aşamasında durdu.
- Gerçek NVIDIA GPU, oyun ya da Wine/Proton kurulumları bu ortamda bulunmuyor. Donanım testleri yapılmadı.

## Bu turda yapılanlar

1. **Qt'den bağımsız benchmark çekirdeği:** Ham frametime dizileri için FPS/1% low/p99 ve takılma istatistikleri; 3+ tekrarlı testleri birbiriyle karşılaştıran `drstool_core/benchmark.py` ve `scripts/compare_captures.py` eklendi. Farklı sahne ve süreler için uyarılar var, bilimsel anlamda otomatik eş-sahne tekrar oynatma yok.
2. **Profil risk sınıflandırması:** `drstool_core/risk.py` ve Linter entegrasyonu; `standard/experimental/advanced/incompatible` seviyeleri. Ayarları otomatik silmez; yalnızca uyarır.
3. **Salt-okunur teşhis:** `drstool_core/diagnostics.py`, yeni Diagnostics GUI sekmesi ve Qt gerektirmeyen `scripts/diagnose.py`; sürücü/probe, araç varlığı, prefix/Proton araç sayısı, profil ve işlem günlüğü özetlerini kişisel yolları içermeden gösterir.
4. **Gerçek güvenli crash recovery:** Tuner `RECOVER` komutu ve isteğe bağlı root izleme servisi eklendi. Hâlâ çalışan PID+başlangıç zamanı eşleşmesi veya belirsiz/ankorsuz oyun kaydı varsa otomatik restore ETMEZ. OpenRC ve systemd servis şablonları, installer/uninstaller ve Gentoo ebuild bağlantıları eklendi. Her sistemde güvenli çalıştığını garanti eden bir gerçek donanım/oturum testi yok.
5. **Profil kaydetme veri bütünlüğü:** Başarısız disk yazmalarında bellek içi profil değişikliği geri alınıyor; yanlış başarı bildirimi engellendi. Kaydetme öncesi fark önizlemesi (ekleme/silme/değiştirme) ve risk özeti eklendi.
6. **Dile hazırlık ve kısmi Türkçe:** Görünüm ekranında `auto/en/tr` seçimi, yeniden başlatma ve yaklaşık 100 yaygın arayüz ifadesi için katalog; ana sekmeler ve bazı kontrol adları Türkçeleştirildi. **Tüm UI metinlerinin çevirisi ve Qt Linguist kataloglarına eksiksiz geçiş yapılmadı**; teknik isimler özellikle çevrilmiyor.
7. **Yapı doğrulama:** `scripts/verify-full.sh`, Python/Bash testlerine yeni senaryolar ve paket wheel oluşturma doğrulaması eklendi.

## Önceki 25 maddelik listede hâlâ tamamen kapanmayanlar

- **11:** Güvenli RECOVER ve isteğe bağlı izleme servisi var; gerçek oyun kapanışı, uyku/uyanma ve servis yöneticisi entegrasyonu donanımda test edilmedi.
- **16:** Çekirdek mantık Qt dışına taşınmaya devam etti; `main_window.py`, `dll_manager.py`, `tool_manager.py` gibi büyük GUI sınıfları eksiksiz yeniden bölünmedi.
- **17:** Extra Tools lazy-loading önceki kodda bulunuyor; tüm ana sekmeler lazy yüklemeye taşınmadı ve başlangıç performans ölçümü yapılamadı.
- **18:** FLM sanitizer CI betikleri mevcut; Vulkan dev paketinin yokluğu nedeniyle yerel derleme ve GPU runtime race testleri başarısız/çalıştırılamadı.
- **20:** 3+ tekrarlı CSV A/B analizi uygulanıyor; aynı oyun sahnesini otomatik tekrarlayan yürütücü yok.
- **21:** Profil kaydetme ve bazı tool/Lutris işlemlerinde önizleme var; bütün disk-yazma yolları tek ortak GUI sayfasında birleştirilmedi.
- **22:** Mevcut sistem probu, linter ve yeni risk kontrolü var; tüm ekran kartı/sürücü/oyun kombinasyonlarını kapsayan güncel uzman öneri motoru yok.
- **24:** Türkçe kataloğu kısmi; tam Qt Linguist dönüşümü bitmiş değil.
- **25:** Tanılama özeti var; tüm üçüncü parti araçların ayrıntılı olay kayıtlarını ilişkilendiren eksiksiz zaman çizelgesi henüz yok.

Bu eksikleri **tamamlandı** diye sunmak doğru değildir. Özellikle Qt/Vulkan bağımlılığı kurulamadığı ve gerçek GPU olmadığı için donanım üzerinde tam doğrulama mümkün olmadı.

## Doğrulama sonuçları

- `python -m compileall -q drstool_core drstool_gui tests` — başarılı.
- `python -m unittest discover -s tests -q` — **124 test, 82 başarılı, 42 GUI testi atlandı**.
- `bash lutris-game-tune-main/tests/run-tests.sh` — **81 başarılı, 0 başarısız**.
- `gcc -Wall -Wextra -Werror -fsyntax-only lutris-game-tune-main/lutris-game-tune-wrapper.c` — başarılı.
- `python -m pip wheel --no-deps --no-build-isolation .` — başarılı, tekrardan son kaynakla oluşturulacak.
- `bash scripts/test-flm-sanitizers.sh` — Vulkan include/library bulunamadı, derleme başlatılamadı.

## Gentoo makinede tam doğrulama

Kaynak paketi açtıktan sonra PySide6, PyYAML, ruamel.yaml, C/C++ derleyici, CMake, Vulkan header/loader ve gerekli Qt platform plugin'leri sistemde hazır olduğunda:

```bash
QT_QPA_PLATFORM=offscreen bash scripts/verify-full.sh
python scripts/diagnose.py
python scripts/compare_captures.py --a run-a1.csv run-a2.csv run-a3.csv --b run-b1.csv run-b2.csv run-b3.csv --same-scene
```

`--same-scene` yalnızca kullanıcı gerçekten aynı sahneyi ve koşulları tekrarladıysa verilmelidir. Gerçek GPU oyun testleri ayrıca yapılmalıdır.

OpenRC kurtarma servisi **isteğe bağlıdır**; doğrudan etkinleştirilmez:

```bash
sudo rc-service lutris-game-tune-recovery start
sudo rc-update add lutris-game-tune-recovery default
```

Servisin etkinleşmesi için tuner daha önce sistem kurulumundan geçmiş olmalıdır. Bilinmeyen bir oyun/launcher durumunda otomatik geri alma yapmaz; `RESTORE` komutu manuel kullanılabilir.
