# DRSTool

> ⚠️ **Riski size aittir.** DRSTool GPU sürücü ayarlarını ve isteğe bağlı olarak sistem ayarlarını değiştirir. Kullanmadan önce [DISCLAIMER.md](DISCLAIMER.md) dosyasını okuyun.

**DRSTool**, [dxvk-nvapi](https://github.com/jp7677/dxvk-nvapi) ve NVIDIA DRS (Driver Registry Settings) profilini yapılandırmak için PySide6 (Qt6) tabanlı bir arayüzdür. Ayrıca Wine, Proton, DXVK, VKD3D-Proton, Gamescope ve NVIDIA'ya özgü ayarlar için ortam değişkeni üreticileri içerir. Sonucu doğrudan bir Lutris, Steam, Heroic veya Bottles oyun yapılandırmasına, bir `dxvk.conf` dosyasına ya da bağımsız bir başlatıcıya yazabilir; ayarların gerçekten işe yaradığını doğrulamak için tanılama araçları (linter, canlı süreç kontrolü, log görüntüleyici, frametime analizi) içerir.

English version: [README.md](README.md)

![DRSTool](assets/drstool.png)

## Bileşenler

| Bileşen | Yol | Açıklama |
|---|---|---|
| DRSTool arayüzü | `DRSTool.py` | Ana uygulama: DRS ayarları, ortam değişkenleri, Gamescope bayrakları, profiller, başlatıcı senkronizasyonu, tanılama |
| vk_flip_meter (FLM) | `vk-flip-meter-main/` | VRR panellerde kare sıralaması (frame pacing) ve hassas FPS sınırlayıcısı sağlayan Vulkan katmanı. Ayrıntılar için [README](vk-flip-meter-main/README.tr.md) |
| lutris-game-tune | `lutris-game-tune-main/` | Lutris için oyun öncesi/sonrası sistem ayarları, CCD/CCX çekirdek izolasyonu ve daha düşük nice değeriyle oyun başlatma (setuid C sarmalayıcı + Bash) |
| Ebuild | `drstool-9999.ebuild` | Yukarıdakilerin tümünü derleyip kuran Gentoo live ebuild (`games-util/drstool`) |

## Özellikler

### DRS Ayarları
- Açıklamalı, kategorilere ayrılmış NVIDIA DRS ayarları
- GPU mimarisi seçici (`DXVK_NVAPI_GPU_ARCH` değerini ayarlar)
- Uygulamadan önce ortaya çıkacak komut/ortamın önizlemesi

### Ortam Değişkenleri
Ortam sekmesi aşağıdaki gruplarda 238 değişkeni kapsar:

- **DXVK** (`DXVK_HUD` bayrak tablosu ve `DXVK_CONFIG` anahtar seçici dahil)
- **DXVK çatalları**: d7vk, dxvk-low-latency, DXVK-Sarek (hangi Proton çatalına ait olduğu etiketli)
- **VKD3D-Proton** (`VKD3D_CONFIG` onay kutusu tablosu ve hata ayıklama/profil değişkenleri)
- **DXVK-NVAPI** (DRS ayarları, Vulkan Reflex katmanı, günlükleme, NGX hata ayıklama seçenekleri)
- **Proton** ve **Wine**; Proton-GE, Proton-EM, Proton-CachyOS ve Proton-DW'ye özgü bayraklar dahil
- **NVIDIA** `__GL_*` ve `__NV_*` değişkenleri, PRIME / hibrit GPU ayarları
- **NVIDIA Smooth Motion** (NVPresent katmanı)
- **Wayland girdi** bayrakları ve AMD dışı oyun optimizasyon bayrakları

Sabit değer kümesi olan değişkenler onay kutusu veya açılır liste olarak, serbest biçimli değerler ise metin alanı olarak gösterilir.

### Gamescope
Bayrak kataloğundan (ölçekleme, kare sıralaması, girdi, oturum seçenekleri) `gamescope` komut satırları üretir.

### Gezinme ve günlük yardımcılar
- **Ctrl+K** tüm DRS ayarlarında, ortam değişkenlerinde, GPU mimarilerinde ve araç sayfalarında arar; **Ctrl+F** geçerli listeyi filtreler; **Ctrl+1…5** üst sekmeleri değiştirir; **Ctrl+D** favori ekler/çıkarır; **Ctrl+S** geçerli profili kaydeder
- "Only set" ve "Favorites" çipleri listeleri ayarlanmış ya da yıldızlanmış olanlarla sınırlar
- **Lint** düğmesi tüm yapılandırmayı birbiriyle çelişen kombinasyonlara karşı kontrol eder (aşağıda Linter)

### Profiller
- Tam profilleri (DRS ayarları + ortam değişkenleri) kaydetme ve yükleme
- XDG uyumlu depolama: `$XDG_CONFIG_HOME/drstool/` (varsayılan `~/.config/drstool/`), atomik yazma
- **Geçmiş** (Profiller → History): `profiles.json` her üzerine yazıldığında veya silindiğinde `drstool/history/` altına anlık görüntü (son 60) alınır. Anlık görüntülerde gezinebilir, bir profili güncel hâliyle karşılaştırabilir, tek profili ya da dosyanın tamamını geri yükleyebilirsiniz. İkinci alt sekme Lutris YAML'larının (ve Steam / Heroic / Bottles dosyalarının) yanındaki `.bak` yedeklerinde diff ile gezinip tek tıkla geri yükler; üçüncüsü iki profili (veya mevcut editör durumunu) karşılaştırır
- **.nip içe aktarma** (Profiller → Import .nip): NVIDIA Profile Inspector dışa aktarımını `DXVK_NVAPI_DRS_SETTINGS` çiftlerine çevirir ve editöre uygular (yalnızca DWORD değerler)
- **Dış değişiklikler**: `profiles.json` veya seçili Lutris YAML'ı DRSTool dışında değiştirilirse (başka bir örnek, bir betik, Lutris'in kendisi) bir şerit çıkar: *Yeniden yükle / Birleştir* (profiller) ya da *Önizlemeyi yenile / DRSTool'a yükle* (Lutris). Tespit içerik tabanlıdır; DRSTool'un kendi yazmaları ve sade bir `touch` şeridi tetiklemez

### Başlatıcılar ve senkronizasyon
Senkronizasyon sekmeleri geçerli yapılandırmayı oyunun başlatıldığı yere yazar. Her yazmada zaman damgalı yedek alınır.

| Sekme | Ne yapar |
|---|---|
| **Launch line** | Çalıştırılabilir `env … gamescope … -- <komut>` satırı. İsteğe bağlı **umu-launcher** modu komutu `umu-run <exe>` olarak sarar ve `PROTONPATH` (GE-Proton / UMU-Latest / kurulu bir Proton), `GAMEID`, `STORE` ve `WINEPREFIX` ekler. Kabuk betiği olarak kaydedilebilir |
| **Lutris** | Bir oyunun YAML'ını okur ve yazar (`system.*` anahtarları, ortam değişkenleri, Gamescope seçenekleri, Lutris Game Tune komutları) ve geri alır ("Seçili oyundan yükle"). Yalnızca DRSTool'un yönettiği anahtarlar değişir; elle yazılmış anahtarlar korunur ve onay penceresi silinecek her şeyi listeler |
| **Steam** | Kurulu bir oyunun başlatma seçeneklerini `localconfig.vdf` içine yazar; isteğe bağlı olarak **oyun başına Proton sürümünü** de (`config.vdf` içindeki `CompatToolMapping`): GE-Proton, Proton-CachyOS ve resmi Valve sürümleri listelenir, "Steam default" override'ı kaldırır. Steam kapalı olmalıdır; yazmadan önce geri okuma kontrolü yapılır |
| **Heroic / Bottles** | Ortam değişkenlerini ve sarmalayıcıları başlatıcının kendi oyun yapılandırmasına yazar |
| **dxvk.conf** | `DXVK_CONFIG` anahtar seçicisini oyun klasöründe gerçek bir `dxvk.conf` dosyasına ya da `~/.config/drstool/dxvk/<ad>.conf` + `DXVK_CONFIG_FILE` olarak yazar. Elle yazılmış satırlar korunur, DRSTool'un anahtarları işaretli tek bir blokta durur, önce diff gösterilir. (vkd3d-proton'un yapılandırma dosyası yoktur; `VKD3D_CONFIG` ortam değişkeni olarak kalır.) |
| **Shortcut** | Bağımsız bir başlatıcı betiği (`~/.local/share/drstool/launchers/`) ve `.desktop` girdisi oluşturur; isteğe bağlı lutris-game-tune ile (PRE → RUN → POST). Betik, oluşturulduğu andaki yapılandırmanın anlık görüntüsüdür |
| **History / Import .nip** | Bkz. Profiller |

### Ek Araçlar
**Derleme ve ayarlama**
- **vk_flip_meter**: FLM ayarlarını düzenleme, canlı ayarlama (`FLM_CONFIG` yazar ve `SIGUSR1` gönderir)
- **lutris-game-tune**: ayarlayıcı yapılandırmasını ve oyun bazlı Lutris ayarlarını düzenleme
- **DLSS Updater**: NVIDIA NGX sunucularındaki tüm DLSS SR / RR / FG ve Streamline sürümlerini (henüz yayınlanmamış staged sürümler dahil) listeler; seçilenleri `nvngx_dlss*.dll` / `sl.*.dll` adlarıyla, sürüm adlı klasöre SHA-256 doğrulamalı indirir
- **MangoHud** ve **vkBasalt**: kaplama / son işlem yapılandırması için seçenek üreticileri
- **Prefix Doctor**: Wine prefix'indeki DXVK, vkd3d-proton ve dxvk-nvapi DLL'lerini en son GitHub sürümüyle karşılaştırır ve eskileri değiştirir (orijinaller yedeklenir)

**Kontrol ve teşhis**
- **Linter**: birbiriyle çelişen ya da bu sistemde çalışamayacak kombinasyonları bulur (ör. WoW64 ile `PROTON_NVIDIA_LIBS`, NVIDIA'da FSR4, çift gecikme yığını); çoğu bulgunun tek tıkla düzeltmesi vardır
- **System**: NVIDIA sürücüsü, Vulkan aygıtları, ntsync, oturum türü ve kurulu araçlar; bir kez sorgulanır, Linter ve kullanılamayan öğelerin grileştirilmesiyle paylaşılır
- **Cache**: NVIDIA GL/compute, Mesa, DXVK ve vkd3d-proton önbelleklerinin boyutları (sürücü önbellekleri, her kütüphanedeki Steam `shadercache/<appid>`, Lutris oyun başı önbellekleri, Env Vars sekmesindeki yollar, eklediğiniz klasörler); aç / temizle işlemleri. NVIDIA sürücü sürümü değişince sürüme bağlı önbellekler geçersiz olarak işaretlenir
- **Upstream drift**: güncel DXVK, vkd3d-proton, dxvk-nvapi, gamescope kaynaklarını ve `NvApiDriverSettings.h` dosyasını indirip kataloğa karşı yeni / kaldırılmış env var, `dxvk.conf` anahtarı, `VKD3D_CONFIG` flag'i, gamescope CLI flag'i ve DRS ID'lerini listeler; satırlar "incelendi" olarak işaretlenebilir. "gone" işaretli env var satırları "yeni sürümde kaldırıldı, fork'lar veya eski sürümler için hâlâ geçerli" anlamına da gelebilir
- **Frametime**: MangoHud CSV logları ve `FLM_CSV` dökümleri: ortalama FPS, %1 / %0.1 low, yüzdelikler, stutter ve hitch sayıları ve frametime grafiği. İkinci bir koşuyu yükleyerek iki yapılandırmayı yan yana karşılaştırın (B, A'ya göre %)
- **Live process**: çalışan oyunu seçin; DRSTool'un ayarladığı ortamı sürecin gerçekten aldığı ortamla (`/proc/<pid>/environ`: ✔ eşleşiyor / ≠ farklı / ✗ eksik) karşılaştırır. Sürece yüklenmiş DXVK, vkd3d-proton, dxvk-nvapi, NGX / Streamline DLL'lerini ve Vulkan katmanlarını da listeler; DRS ayarı yapılmış ama NVAPI DLL'i yüklenmemişse uyarır
- **Logs**: Proton (`steam-<appid>.log`), DXVK ve vkd3d-proton log görüntüleyici: önem filtresi, arama ve takip modu. Yaygın hataları (NVAPI yok, nvcuda, GPU device lost, bellek yetersiz, ntsync, esync dosya tanımlayıcı sınırı, Vulkan aygıt/uzantı hataları, NGX, eksik DLL importu) ipucuyla ve güvenli olduğu yerde tek tıkla düzeltmeyle tanır. "Enable logging vars" `PROTON_LOG`, `PROTON_LOG_DIR` ve `DXVK_LOG_PATH` değerlerini ayarlar
- **Game hints**: bir Steam appid'si arayın; öneriler üç kaynaktan gelir ve her biri *Source* sütununda etiketlenir:
  1. *heuristic*: oyunun grafik API'sine dair basit kurallar (ör. NVIDIA'da Direct3D 11/12 için `PROTON_ENABLE_NVAPI=1`)
  2. *system*: bu makineden türetilir (ntsync aygıtı var, hibrit NVIDIA + iGPU → PRIME offload değişkenleri, Wayland oturumu)
  3. engine ve **ProtonDB tier** notları: Unreal / Unity / native Vulkan / D3D9 dönemi uyarıları ve hangi Proton sürümünün denenmesi gerektiği (gold/platinum için stok Proton, silver için Experimental veya GE-Proton, bronze/borked için GE-Proton / Proton-CachyOS ya da eski bir Valve sürümü, trending tier'dan regresyon veya iyileşme ipuçları ve kurulu sürümleriniz)

  Yalnızca DRSTool kataloğunda bulunan değişkenler önerilir, zaten ayarlı değerler atlanır ve sonuçlar bir hafta önbellekte tutulur.

  **Steam'de olmayan oyunlar** (Lutris, Heroic, Bottles, elle eklenenler): oyunun adını yazın ya da başlatıcılarınızdan seçin; *Find appid* Steam mağazasında arar ve benzerlik puanıyla eşleşmeleri listeler. *Use selected appid* aramayı çalıştırır; *Use + set as umu GAMEID* ayrıca Launch line sekmesindeki `GAMEID` alanını `umu-<appid>` ile doldurur. Steam'de satılmayan oyunun eşleşmesi çıkmaz (`umu-0` kullanın).
- **Community tips ⚠**: ProtonDB *kullanıcı raporlarında* geçen ortam ayarları; bilerek ayrı bir sekmede tutulur. Yalnızca bir raporda geçse bile her bahis listelenir ve yanında raporlayanın GPU'su ve sürücüsü, Proton sürümü, işletim sistemi ve kernel'i, değerlendirmesi ve rapor tarihi gösterilir (CPU ve RAM için satırın üzerine gelin). Filtreler: yalnızca NVIDIA raporları, son 6 / 12 ay ve "yalnızca DRSTool'un ayarlayabildiği değişkenler"; *Seen* sütunu aynı ayarın kaç raporda geçtiğini gösterir. Veri, ProtonDB'nin herkese açık aylık rapor dökümünden gelir (github.com/bdefore/protondb-data, ODbL): *Download / update ProtonDB report data* düğmesine bir kez basın (~70 MB, birkaç saniyede yerel olarak indekslenir, son 12 ay tutulur); sonrasında her oyun çevrimdışı aranır. Denemeden önce raporlayanın kurulumunu kendinizinkiyle karşılaştırın. **Riski size aittir**: bunlar bireysel deneyimlerdir, DRSTool doğrulamamıştır; farklı donanım, sürücü, kernel, Proton sürümü ve Linux kurulumlarında farklı davranabilir, çökmeye veya performans kaybına yol açabilir. Hiçbiri önceden seçilmez, onay kutusunu işaretlemeden Apply düğmesi pasif kalır ve onay penceresi uyarıyı tekrarlar. DRSTool kataloğunda olmayan değişkenler yalnızca bilgi amaçlı gösterilir

### Lutris notları
Lutris de kendi yapılandırmasına yazar; bir oyunu iki yerde de düzenlerseniz yukarıdaki dış değişiklik şeridini ve *Seçili oyundan yükle* düğmesini kullanarak DRSTool'u yeniden eşitleyin.

### Ağ erişimi
DRSTool'un çoğu çevrimdışı çalışır. Şu özellikler yalnızca düğmelerine bastığınızda internete bağlanır: **Upstream drift** (GitHub raw dosyaları), **Prefix Doctor** (GitHub sürümleri), **DLSS Updater** (NVIDIA NGX sunucuları), **Game hints** (ProtonDB özeti ve PCGamingWiki), **Community tips** (GitHub raw üzerinden ProtonDB rapor dökümü, ~70 MB, yalnızca indirme düğmesine bastığınızda).

### DRSTool dosyalarını nerede tutar
| Yol | İçerik |
|---|---|
| `~/.config/drstool/profiles.json`, `history/` | profiller ve anlık görüntüleri |
| `~/.config/drstool/dxvk/` | "config dir" modunda oluşturulan `dxvk.conf` dosyaları |
| `~/.local/share/drstool/launchers/` | Shortcut sekmesinin betikleri (`.desktop` dosyaları `~/.local/share/applications/drstool-*.desktop` altına gider) |
| `~/.cache/drstool/hints/`, `~/.cache/drstool/logs/` | Game hints önbelleği; "Enable logging vars" ile yazılan loglar |
| düzenlenen dosyaların yanı | zaman damgalı `.bak` kopyaları |

(`XDG_CONFIG_HOME`, `XDG_DATA_HOME` ve `XDG_CACHE_HOME` dikkate alınır.)

## Gereksinimler

- NVIDIA GPU ve tescilli sürücü (DRS ayarları için)
- Python 3.12+
- PySide6
- İsteğe bağlı, belirli özellikler için: Lutris, Gamescope, Proton/Wine, FLM'yi derlemek için Vulkan SDK + CMake

## Kaynak koddan çalıştırma

```bash
pip install PySide6
python3 DRSTool.py
```

## Gentoo'ya kurulum

Dahil olan ebuild (`drstool-9999.ebuild`) git'ten çeken bir live ebuild'dir.

USE bayrakları:

| Bayrak | Varsayılan | Etkisi |
|---|---|---|
| `flip-meter` | açık | vk_flip_meter Vulkan katmanını (C++) derler ve kurar |
| `lto` | kapalı | Katmanı `-flto` ile derler (`flip-meter` gerektirir) |
| `pgo` | kapalı | Katman için iki geçişli profil tabanlı optimizasyon (`flip-meter` gerektirir) |
| `lutris-tune` | açık | lutris-game-tune setuid sarmalayıcısını, betiğini ve varsayılan yapılandırmayı kurar |

```bash
emerge --oneshot games-util/drstool
```

Not: lutris-game-tune, setuid-root sarmalayıcı gerektirir. Ebuild bunu ayarlar; `lutris-game-tune-wrapper STATUS` ile kontrol edebilirsiniz.

## Kullanım notları

- Değişiklikler seçili oyunun yapılandırmasına uygulanır. Yazmadan önce önizlemeyi kontrol edin.
- Ayarlar diske yazılır ve bir sonraki oyun başlatmada etkili olur. lutris-game-tune değişiklikleri bir sonraki PRE çalıştırmasında uygulanır.
- Lutris YAML dosyasını yedekleyin. Bir Lutris yapılandırmasına yazmadan önce DRSTool, yanına zaman damgalı bir `.bak` kopyası oluşturur ve en son 5 tanesini tutar.

## Proje yapısı

```
DRSTool.py                 ana arayüz
assets/drstool.png         uygulama simgesi
drstool-9999.ebuild        Gentoo ebuild
vk-flip-meter-main/        FLM Vulkan katmanı (C++, CMake)
lutris-game-tune-main/     Lutris ayarlayıcı (Bash + setuid C sarmalayıcı)
DISCLAIMER.md              risk bildirimi
LICENSE                    MIT
```

## Lisans

MIT. Ayrıntılar için [LICENSE](LICENSE).

## Sorumluluk reddi

Bu proje yapay zekâ desteğiyle geliştirilmiştir ve garanti verilmeden sunulmaktadır. Tam metin için [DISCLAIMER.md](DISCLAIMER.md) dosyasına bakın.
