# DRSTool — Kasım 2026 bakım turu

Odak: stabilite, performans ve ölü kod. Güvenlik denetimlerinin (AUDIT_*) kapsadığı alanlara dokunulmadı. Her değişikliğin regresyon testi `tests/test_maintenance.py` içindedir (eski kodda 15 testin 7'si başarısız oluyor).

## Stabilite

| Yer | Sorun | Düzeltme |
| --- | --- | --- |
| `game_scan._game_root_from_exe` | UE3 düzeni (`Oyun/Binaries/Win32/x.exe`: Borderlands 2, BioShock Infinite, XCOM) UE4 sanılıp iki seviye yukarı çıkılıyordu, bu da **bütün oyun kütüphanesi** klasörünü bu oyunun klasörü yapıyordu. DLL taraması ve DLL Manager diğer oyunların DLL'lerini bu oyuna bağlayabiliyordu. | UE4/5 kökü yalnızca yanında `Engine/` varsa seçiliyor; yoksa `Binaries`'in üst klasörü kullanılıyor. |
| `game_scan._yaml_game_section` | Lutris YAML'daki `exe: null` / `working_dir: ~` değerleri `"None"` adlı göreli bir yola dönüşüyordu. | `None` değerleri atlanıyor. |
| Kapanış (`TaskCenter`) | Oyun kartı yenileme, umu-database araması, log ve frametime yükleyicileri kapanış beklemesine dahil değildi. Bunlar çalışırken pencere kapanırsa Qt çalışan bir QThread'i yok edip süreci abort ediyordu. | Kısa işler için listede görünmeyen `TASKS.retain()`; umu araması `TASKS.track` ile görünür ve tekrar girişe kapalı. |
| `GameCardWidget.reload` | Aynı oyun için iki yenileme üst üste binerse, geç biten eski sonuç yeni kartın üzerine yazılıyordu (oyun anahtarı aynı olduğundan kontrol yakalamıyordu). | Yalnızca en son worker'ın sonucu uygulanıyor. |
| `SessionStore.recent` | Sıralama sırasında bir oturum dosyası silinirse tüm liste boş dönüyordu. | Kaybolan dosya sadece atlanıyor. |
| `nip_import.parse_nip` | Bozuk XML'de belgelenen `ValueError` yerine `ParseError` çıkıyordu. | `ValueError`'a çevriliyor. |

## Performans (ölçülerek)

| Yer | Önce | Sonra |
| --- | --- | --- |
| `LinkStore` sorguları (`get`, `all`, `counts`…): her çağrıda `game_links.json` baştan okunup ayrıştırılıyordu; Game hints oyun listesi bunu oyun başına çağırıyor (GUI thread). 300 oyun × 40 kayıt (2.3 MB): | 6000 ms | 62 ms |
| Aynı, 60 oyun × 5 kayıt: | 43 ms | 2 ms |
| Crash Lab adaptif arama adımı (`answer()`), 40 ayar: | 130–230 ms | 70–90 ms |

- `LinkStore`: dosya `(cihaz, inode, mtime_ns, ctime_ns, boyut)` imzası değişmediyse yeniden ayrıştırılmıyor. Tüm yazımlar atomik `replace` olduğu için her sürüm yeni inode alır; diğer pencerelerin değişiklikleri görülmeye devam eder. Yazma ve kurtarma yolları her zaman diskten okur.
- Crash Lab: hipotezler bir kez "VEYA-of-VE" kloz kümelerine derlenip alt küme testiyle değerlendiriliyor. 12 senaryoda öneriler, süreler, posterior ve sonuç eskisiyle birebir aynı.
- Ölçülüp kazanç bulunamayanlar geri alındı: DLL tarama döngüsündeki mikro değişiklikler (gürültü içinde). Başlangıç (~0,9 s) ve arayüz filtreleri (<25 ms) zaten hızlıydı.

## Ölü kod

- `CrashModel.best_test` (yerini `best_plan` almıştı; hiçbir yerde çağrılmıyordu) ve modül açıklamasındaki anılışı kaldırıldı.
- `_log.debug(...)` sonrasında kalan 21 gereksiz `pass` kaldırıldı.
- Hiç okunmayan öznitelikler kaldırıldı: `_pfx_btn`, `_exe_lbl`, `_args_lbl`, `PlanDialog.result_label`.
- Testlerdeki ruff uyarıları (kullanılmayan import/değişken) temizlendi. `ruff check .` temiz.
- Bilerek bırakılanlar: `to_json`, `clear_game_files_cache`, `describe`, `select_key`, `wait_all`, `set_checked` yalnızca testlerden çağrılıyor ama testlerin kullandığı API'ler.

## Doğrulama

- `python -m unittest discover -s tests`: 405 test, hepsi başarılı (1 atlanan, önceden de vardı).
- `scripts/verify-core.sh` (Lutris Game Tune shell testleri 92/92, wrapper 10/10, C wrapper `-Werror`): başarılı. `test_maintenance.py` buraya eklendi.
- 16 ayrıştırıcıda 48 bin girdilik fuzz turu: beklenmeyen istisna yok.
- Gerçek GPU, Wine/Proton ve gerçek bir oyun kütüphanesi üzerinde test edilmedi. UE3 düzeltmesi bilinen klasör yapılarına dayanıyor.

---

# 2. tur — arayüz ve kod

Yöntem: uygulama ekransız (offscreen) çalıştırılıp her sekme ve 17 araç sayfası 980×640, 1024×700, 1280×800, 1400×900 ve 1920×1080 boyutlarında, İngilizce ve Türkçe arayüzle görüntülendi. Ayrıca her görünür düğmenin metninden dar çizilip çizilmediği ya da kaydırma alanının sağından taşıp taşmadığı ölçüldü.

## Kök neden

`MainWindow.setMinimumSize(980, 640)`: üst düzey pencerede açıkça verilen minimum, layout'ların kendi minimumunu geçersiz kılar. Satırı bu genişliğe sığmayan sayfalarda düğmeler metinlerinin altına eziliyordu ("ɔancel scar", "lear selecte") ya da sayfa sağdan kesiliyordu (DLL Manager, Tool Manager, lutris-game-tune). 1024 px'te 8 sayfada ~60 kontrol ve ~150 etiket etkileniyordu.

## Arayüz değişiklikleri

| Yer | Değişiklik |
| --- | --- |
| `widgets.FlowLayout` (yeni) | Yer kalmayınca alt satıra kayan düğme satırı. Öğeler hiçbir zaman sizeHint'in altına inmiyor; `add_stretch()` sonraki öğeleri sağa yaslıyor. |
| Cache, Upstream drift, History, DLL Manager, Tool Manager, Crash Lab, Launch line, Community tips, Games kenar çubuğu, filtre çipleri, oyun kartı kutucukları | Taşan satırlar FlowLayout'a taşındı. |
| DLL Manager "Update to" | 4 sabit sütunlu ızgara yerine kayan hücreler. |
| MangoHud | Hazır ayarlar ayrı, kayan satırda. 880 px altında açıklama sütunu gizleniyor (metin onay kutusunun tooltip'inde, filtre açıklamada aramaya devam ediyor). Uzun "(+ MANGOHUD_CONFIGFILE)" düğme metni tooltip'e taşındı (vkBasalt da aynı). |
| Upstream drift | Sayfa kaydırma alanına alındı; dar ekranda açıklama metni dikeyde eziliyordu. |
| lutris-game-tune | "[kernel: …]" notu onay kutusundan açıklama satırının başına taşındı. Anahtar adları yalnızca gerektiğinde `_` sonrasında bölünüyor. |
| vk_flip_meter, lutris-game-tune | Kaynak klasör yolu ortadan kısaltılıyor (tam yol tooltip'te, tıklayınca kopyalanır). pipx kurulumundaki uzun yol sayfayı genişletiyordu. |
| vk_flip_meter | Koda gömülü "7845HX" CPU adı kaldırıldı; metin artık genel. |
| Karşılama penceresi | Düğme metinleri pencere boyutlanmadan önce çevriliyor (Türkçede eziliyordu). |
| Çeviri | 5 yeni Türkçe karşılık; bir Türkçe metin sığması için kısaltıldı. |

Sonuç: İngilizce arayüzde 980–1920 px arasındaki tüm boyutlarda ezilen ya da kesilen kontrol yok. Türkçede yalnızca 980 px'te .nip sekmesinde 3 px'lik (görünmez düzeyde) bir fark ve arka plan görev düğmesinin geçici sıkışması kalıyor.

## Kalıcı koruma

`tests/test_maintenance.py::NarrowWindowLayoutTests` her sekmeyi ve araç sayfasını 1024×700'de açıp ezilen ya da kesilen düğme olmadığını doğruluyor. Eski kodda ~60 sorunla başarısız oluyor. Tam test setinde (dolu oyun kartı vb.) iki ek sorunu da yakaladı; ikisi de düzeltildi. FlowLayout için birim testi de eklendi.

## Doğrulama

- 407 test başarılı (1 atlanan, önceden de vardı); 4 ardışık tam çalıştırmada kararlı.
- `ruff`, `compileall`, `verify-core.sh`, fuzz turu: temiz.
