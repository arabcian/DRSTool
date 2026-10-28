# DRSTool

> ⚠️ **Riski size aittir.** DRSTool GPU sürücü ayarlarını ve isteğe bağlı olarak sistem ayarlarını değiştirir. Kullanmadan önce [DISCLAIMER.md](DISCLAIMER.md) dosyasını okuyun.

**DRSTool**, [dxvk-nvapi](https://github.com/jp7677/dxvk-nvapi) ve NVIDIA DRS (Driver Registry Settings) profilini yapılandırmak için PySide6 (Qt6) tabanlı bir arayüzdür. Ayrıca Wine, Proton, DXVK, VKD3D-Proton, Gamescope ve NVIDIA'ya özgü ayarlar için ortam değişkeni üreticileri içerir. Sonucu doğrudan bir Lutris, Steam, Heroic veya Bottles oyun yapılandırmasına, bir `dxvk.conf` dosyasına ya da bağımsız bir başlatıcıya yazabilir; ayarların gerçekten işe yaradığını doğrulamak için tanılama araçları (linter, canlı süreç kontrolü, log görüntüleyici, frametime analizi) içerir.

English version: [README.md](README.md)

![DRSTool](assets/drstool.png)

## Bileşenler

| Bileşen | Yol | Açıklama |
|---|---|---|
| DRSTool arayüzü | `DRSTool.py`, `drstool_gui/` | Ana uygulama (giriş betiği + PySide6 paketi, her sayfa / özellik için bir modül): DRS ayarları, ortam değişkenleri, Gamescope bayrakları, profiller, başlatıcı senkronizasyonu, tanılama |
| vk_flip_meter (FLM) | `vk-flip-meter-main/` | VRR panellerde kare sıralaması (frame pacing) ve hassas FPS sınırlayıcısı sağlayan Vulkan katmanı. Ayrıntılar için [README](vk-flip-meter-main/README.tr.md) |
| lutris-game-tune | `lutris-game-tune-main/` | Lutris için oyun öncesi/sonrası sistem ayarları, CCD/CCX çekirdek izolasyonu ve daha düşük nice değeriyle oyun başlatma (C sarmalayıcı — setuid ya da polkit, aşağıya bakın — + Bash) |
| drstool_core | `drstool_core/` | Arayüzün ve komut satırının kullandığı Qt'siz kod: katalog (DRS ayarları, ortam değişkenleri, Gamescope bayrakları … `drstool_core/data/` altında JSON), paylaşım kodları, Steam `shortcuts.vdf`, oyun / DLL bulma, yedekli DLL değiştirme (`tests/` altında birim testli) |
| Paketleme | `drstool-9999.ebuild`, `packaging/arch/PKGBUILD`, `pyproject.toml` | Gentoo live ebuild (`games-util/drstool`), Arch `drstool-git` PKGBUILD, pip / pipx kurulumu |

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
- **F1** (veya Ctrl+/) klavye kısayolu sayfasını, **Ctrl+,** *Appearance* penceresini açar. Ctrl+K boşken en son seçtiklerini en üstte gösterir ("son kullanılanlar")
- **Toast bildirimleri**: her durum mesajı (kaydedildi, kopyalandı, uygulandı, geri alındı, hatalar) sağ üstte de belirir; imleç üstündeyken kaybolmaz. Lutris YAML, Steam, Heroic, Bottles veya `dxvk.conf` yazımından sonra toast'ta o yazım için alınan yedeği geri koyan bir **Undo** düğmesi olur (dosya bu arada tekrar değiştiyse reddeder)
- **Arka plan görevleri**: DLL Manager, Upstream drift, Cache taraması, Game hints ve ProtonDB indirmesi üst çubuktaki *⟳ N* düğmesinde (veya ⚙ › Background tasks) son ilerleme satırıyla listelenir; bir satıra tıklayınca ilgili sayfaya gidilir. Birkaç saniyeden uzun süren veya başarısız olan görevler toast, pencere önde değilse masaüstü bildirimi (`notify-send`, yoksa `gdbus`) üretir
- **Oturumu hatırlama**: son profil, *Only set* / *Favorites* çipleri, seçili DRS ayarı / değişken ve açık araç veya sync sekmesi açılışta geri gelir (Appearance'tan kapatılabilir)
- **Appearance** (⚙ › Appearance, Ctrl+,): koyu veya açık tema, vurgu rengi (yeşil, mavi, camgöbeği, mor, turuncu, kırmızı, pembe), liste yoğunluğu (compact / default / spacious) ve %70–200 arası arayüz ölçeği. Değerler açılışta okunur; *Save & restart* kaydedilmemiş değişiklikler için sorduktan sonra yeniden başlatır. Vurgu rengi varsayılanda yeşil olan "ok" durumlarını da boyar
- **Klavye ve ekran okuyucu**: Tab / Shift+Tab üst çubuğa da ulaşır, odaktaki kontrolü bir odak halkası gösterir; kontroller ilk gösterildiklerinde metinlerinden, placeholder'ından, etiketinden veya tooltip'inden erişilebilir ad alır
- "Only set" ve "Favorites" çipleri listeleri ayarlanmış ya da yıldızlanmış olanlarla sınırlar
- **Lint** düğmesi tüm yapılandırmayı birbiriyle çelişen kombinasyonlara karşı kontrol eder (aşağıda Linter). Artık DRS'i de biliyor: RTX 50 öncesi kartta Multi-Frame Generation, RTX 40 öncesinde Frame Generation, GTX'te DLSS, gerçek karttan yeni / onunla aynı GPU arch taklidi, Custom modu olmadan DLSS-SR Scaling, yalnızca Dynamic modda çalışan DLSSG ayarları, Frame Generation üstüne Smooth Motion
- **Geri al / yinele** (Ctrl+Z, Ctrl+Shift+Z / Ctrl+Y, üst çubuktaki ↶ ↷): DRS ayarları, arch, ortam değişkenleri ve gamescope'taki her değişiklik bir adımdır — profil yükleme, içe aktarma, tarifler, lint düzeltmeleri ve *Reset* dahil. Tek alana yazmak tek adımda birleşir. Undo'nun yanındaki ok son 30 adımı listeler; birine tıklayınca oraya atlar
- **Kaydedilmemiş değişiklik işareti artık kesin**: editörü kayıtlı profille karşılaştırır, yani kayıtlı hâle geri alınca işaret kalkar. Üst çubuktaki profil adına tıklayınca farkları görür, *Revert to saved* ile kayıtlı hâle dönersiniz
- **Launch line içe aktarma** (Ctrl+Shift+V, Copy all ▾ menüsü ya da metni pencereye sürükleyip bırakma): Steam launch options, bir shell parçası, `export` satırları veya çıplak bir `DXVK_NVAPI_DRS_SETTINGS` değeri yapıştırın. DRS çiftlerini (sayı yazımı ne olursa olsun), GPU arch'ı, `env -u`'yu, gamescope bayraklarını ve `mangohud`'u çözer; `prime-run` ve `strangle N`'i ortam değişkeni karşılıklarına çevirir; dışarıda bıraktığı sarmalayıcıları ve oyun argümanlarını raporlar. İşaretlenebilir önizleme her öğe için +yeni / ~değişiklik / =aynı gösterir, Birleştir ya da Değiştir seçilir ve uygulamadan önce sonucun **lint tahmini** görünür
- **Launch line'ı açıkla** (Copy all ▾ menüsü, Ctrl+K): mevcut çıktı katalog açıklamalarıyla parça parça çözülür; *Copy explanation* foruma yapıştırılabilecek bir Markdown listesi verir
- **✦ Recipes / Tarifler** (Ctrl+R): hazır paketler — DLSS en yeni modeller, DLAA zorla, Multi-Frame 4x, DLSS göstergesi, Vulkan için Reflex, Smooth Motion, Wayland ya da gamescope ile HDR, daha az shader takılması, hibrit laptopta dGPU, NTSync, düşük gecikmeli DXVK. Her biri bu makinenin gereksinimi karşılayıp karşılamadığını ve ne kadarının zaten uygulandığını gösterir, aynı önizleme + lint tahminiyle açılır. Tarif değerleri çalışma anında katalogla doğrulanır
- Katalog dışındaki değişkenler ve DRS ID'leri (içe aktarma veya elle düzenlenmiş profillerden) kenar çubuğunda kendi gruplarını alır — *Custom (not in catalog)* / *Custom IDs* — böylece düzenlenebilir ve silinebilir kalırlar
- GPU Arch: bu makinedeki kart **◆ your GPU** ile işaretlenir; detay sayfası seçilen taklidin gerçek karttan eski mi, yeni mi, aynı mı olduğunu söyler

### Profiller
- Tam profilleri (DRS ayarları + ortam değişkenleri) kaydetme ve yükleme
- Profile sağ tık (veya *More ▾*): Yeniden adlandır, Çoğalt, Editörle karşılaştır, Dosyaya dışa aktar, Dosyadan içe aktar (tek profil dışa aktarımı ya da bütün bir `profiles.json`; isim çakışmalarına sonek eklenir)
- **Paylaşım kodları**: *Copy share code* (profil menüsü ya da editörün o anki hâli için *Copy all ▾*) bütün profili tek satırlık bir metin olarak, `DRSTOOL1:…`, panoya koyar; sohbete ya da foruma yapıştırılabilir. Ev klasörünüz `~` olarak yazılır ve içe aktarırken alanın ev klasörüne dönüşür. *Import share code…* (profil menüsü, Ctrl+K ya da doğrudan Ctrl+Shift+V / sürükle-bırak — paylaşım kodu, çevresinde başka metin olsa ya da birkaç satıra bölünmüş olsa da kendiliğinden tanınır) içeriği ve *bu* makine için linter sonucunu gösterir, bir ad sorar. Hangi kütüphanenin ya da katmanın yükleneceğini değiştiren değişkenler (`LD_PRELOAD`, Vulkan katman / ICD yolları, …) ve kabuk sözdizimi içeren değerler işaretlenir; siz onaylamadıkça alınmaz
- XDG uyumlu depolama: `$XDG_CONFIG_HOME/drstool/` (varsayılan `~/.config/drstool/`), atomik yazma
- **Geçmiş** (Profiller → History): `profiles.json` her üzerine yazıldığında veya silindiğinde `drstool/history/` altına anlık görüntü (son 60) alınır. Anlık görüntülerde gezinebilir, bir profili güncel hâliyle karşılaştırabilir, tek profili ya da dosyanın tamamını geri yükleyebilirsiniz. İkinci alt sekme Lutris YAML'larının (ve Steam / Heroic / Bottles dosyalarının) yanındaki `.bak` yedeklerinde diff ile gezinip tek tıkla geri yükler; üçüncüsü iki profili (veya mevcut editör durumunu) karşılaştırır
- **.nip içe aktarma** (Profiller → Import .nip): NVIDIA Profile Inspector dışa aktarımını `DXVK_NVAPI_DRS_SETTINGS` çiftlerine çevirir ve editöre uygular (yalnızca DWORD değerler); bilinen ayarlar adıyla tanınır ve değerleri katalogdaki yazımla saklanır, böylece editör düğmeleri doğru yanar
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
| **Add to Steam** | Steam kütüphanesine Steam dışı oyun ekler (`shortcuts.vdf`): geçerli yapılandırma başlatma seçeneklerine yazılmış bir program ya da betik (Windows `.exe` için bir Proton sürümü önceden seçilir), Shortcut sekmesinde oluşturulmuş bir DRSTool başlatıcısı ya da kendi başlatıcısıyla açılan bir Lutris / Heroic oyunu. DRSTool'un eklediği kısayollar etiketlenir; sonradan düzenlenebilir ya da kaldırılabilir. Steam kapalı olmalıdır; dosya yazılmadan önce geri okunur ve `.bak` yedeği tutulur |
| **Shortcut** | Bağımsız bir başlatıcı betiği (`~/.local/share/drstool/launchers/`) ve `.desktop` girdisi oluşturur; isteğe bağlı lutris-game-tune ile (PRE → RUN → POST). Betik, oluşturulduğu andaki yapılandırmanın anlık görüntüsüdür |
| **History / Import .nip** | Bkz. Profiller |

### Ek Araçlar
**Derleme ve ayarlama**
- **vk_flip_meter**: FLM ayarlarını düzenleme, canlı ayarlama (`FLM_CONFIG` yazar ve `SIGUSR1` gönderir)
- **lutris-game-tune**: ayarlayıcı yapılandırmasını ve oyun bazlı Lutris ayarlarını düzenleme
- **DLL Manager** (eski DLSS Updater ve Prefix Doctor'ın yerine): sistemdeki oyunları bulur — Steam, Lutris, Heroic (Epic / GOG / Amazon / elle eklenen), Bottles ve sizin eklediğiniz klasörler — ve her oyun için içindeki DLSS SR / RR / FG ve Streamline sürümlerini, Wine prefix'indeki DXVK / vkd3d-proton / dxvk-nvapi sürümlerini gösterir. *Check for updates* NVIDIA NGX sunucularındaki tüm sürümleri (staged sürümler dahil, SHA-256 doğrulamalı) ve en yeni GitHub sürümlerini listeler; her bileşen için hedef sürümü seçin, oyunları işaretleyin (ya da *select games with updates*) ve *Update selected*'e basın. Yalnızca oyunda zaten bulunan dosyalar değiştirilir (Streamline bir bütün olarak güncellenir), sembolik bağlantıların içine asla yazılmaz, her dosya atomik olarak değiştirilir ve `~/.local/share/drstool/dll-backups/` altına yedeklenir; *Restore…* orijinalleri geri koyar (oyun o dosyayı bu arada kendisi güncellediyse dokunmaz). Proton prefix'leri Proton'a bırakılır; anti-cheat içeren oyunlar kendiliğinden seçilmez. *Download only…* seçilen DLSS / Streamline sürümlerini bir klasöre indirir
- **MangoHud** ve **vkBasalt**: kaplama / son işlem yapılandırması için seçenek üreticileri
- **Tool Manager** (Extra Tools › Build & install): Proton çatallarını (GE-Proton, Proton-CachyOS, Proton-EM, dwproton, Proton-Tkg, Luxtorpeda, Boxtron, Roberta, Steam-Play-None, SteamTinkerLaunch), Wine yapılarını (Lutris-Wine, Kron4ek, Wine-Tkg), DXVK'yı (sürüm / Async / nightly) ve vkd3d'yi (proton / lutris) Steam'in `compatibilitytools.d` klasörüne, Lutris'e, Heroic'e, Bottles'a, umu klasörüne ya da seçtiğiniz herhangi bir klasöre indirir — [protondl](https://davidotek.github.io/protondl/) ile aynı katalog ve klasör düzeni; iki programdan biriyle kurulan araç diğerinde de tanınır. Proje sağlama toplamı yayımlıyorsa arşivler SHA-512 ile doğrulanır, önce bir hazırlık klasörüne açılır (başarısız ya da iptal edilen kurulum geride hiçbir şey bırakmaz); araçlar güncellenebilir (istenirse eski sürüm silinerek), kaldırılabilir veya Steam'in genel varsayılan Proton'u yapılabilir. Proton-CachyOS, CPU'nuzun desteklediği en iyi x86-64 seviyesini seçer. GitHub'ın saatte 60 istek sınırını aşmak için DRSTool'u başlatmadan önce `GITHUB_TOKEN` tanımlayın

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

### Komut satırı

Seçenek verilmezse `drstool` GUI'yi başlatır. Aşağıdaki seçenekler başsız (offscreen Qt, GUI ile aynı kod) çalışır, stdout'a yazar ve çıkar; profil adları büyük/küçük harfe bakmadan ve benzersiz alt dizgiyle eşleşir.

| Komut | Ne yapar |
|---|---|
| `drstool --list-profiles` | profil başına bir satır: DRS / env sayısı, GPU arch, gamescope |
| `drstool --print-env PROFIL [--format steam\|shell\|export\|json]` | launch line (`steam`: `env … -- %command%`, `shell`: `… "$@"`), `export` satırları veya tüm payload JSON olarak |
| `drstool --lint PROFIL [--strict]` | bu makinede tutarlılık linter'ı; hata varsa çıkış kodu 1 (`--strict` ile uyarıda da) |
| `drstool --apply PROFIL --lutris OYUN [--lutris-dir DIZIN] [--dry-run] [-y]` | profili Lutris sekmesiyle aynı şekilde bir Lutris oyun YAML'ına işler (önce yedek, elle yazılan anahtarlar korunur). `--dry-run` farkı yazdırır; `-y` yoksa sorar, terminal yoksa reddeder |
| `drstool --import-nip DOSYA [--nip-profile AD] [--into PROFIL \| --save-as AD]` | Profile Inspector `.nip` dosyasını `DXVK_NVAPI_DRS_SETTINGS`'e çevirir; isteğe bağlı olarak kayıtlı bir profile birleştirir veya yeni profil olarak saklar |
| `drstool --share PROFIL` | profilin paylaşım kodunu (`DRSTOOL1:…`) yazdırır |
| `drstool --import-share KOD\|- [--save-as AD] [--include-flagged]` | paylaşım kodunu çözer (`-` stdin'den okur), içeriğini ve bu makine için lint sonucunu yazdırır; `--save-as` kaydeder |
| `drstool --profile PROFIL` | GUI'yi o profil yüklü açar |

Çıkış kodu: 0 tamam, 1 lint hatası / yazma başarısız, 2 hatalı argüman veya bulunamadı.

### Lutris notları
Lutris de kendi yapılandırmasına yazar; bir oyunu iki yerde de düzenlerseniz yukarıdaki dış değişiklik şeridini ve *Seçili oyundan yükle* düğmesini kullanarak DRSTool'u yeniden eşitleyin.

### Ağ erişimi
DRSTool'un çoğu çevrimdışı çalışır. Şu özellikler yalnızca düğmelerine bastığınızda internete bağlanır: **Upstream drift** (GitHub raw dosyaları), **DLL Manager** › *Check for updates* / *Update* (NVIDIA NGX sunucuları ve GitHub sürümleri), **Tool Manager** (GitHub / GitLab / Codeberg sürümleri ve araç arşivleri), **Game hints** (ProtonDB özeti ve PCGamingWiki), **Community tips** (GitHub raw üzerinden ProtonDB rapor dökümü, ~70 MB, yalnızca indirme düğmesine bastığınızda).

### DRSTool dosyalarını nerede tutar
| Yol | İçerik |
|---|---|
| `~/.config/drstool/profiles.json`, `history/` | profiller ve anlık görüntüleri |
| `~/.config/drstool/dxvk/` | "config dir" modunda oluşturulan `dxvk.conf` dosyaları |
| `~/.local/share/drstool/launchers/` | Shortcut sekmesinin betikleri (`.desktop` dosyaları `~/.local/share/applications/drstool-*.desktop` altına gider) |
| `~/.cache/drstool/hints/`, `~/.cache/drstool/logs/` | Game hints önbelleği; "Enable logging vars" ile yazılan loglar |
| `~/.local/share/drstool/dll-backups/` | DLL Manager'ın değiştirdiği DLL'lerin orijinalleri (oyun başına son 10) |
| `~/.cache/drstool/dll-manager/` | indirilen DLSS / Streamline sürümleri |
| düzenlenen dosyaların yanı | zaman damgalı `.bak` kopyaları |

(`XDG_CONFIG_HOME`, `XDG_DATA_HOME` ve `XDG_CACHE_HOME` dikkate alınır.)

## Gereksinimler

- NVIDIA GPU ve tescilli sürücü (DRS ayarları için)
- Python 3.12+
- PySide6
- İsteğe bağlı, belirli özellikler için: Lutris, Gamescope, Proton/Wine, FLM'yi derlemek için Vulkan SDK + CMake

## Hızlı kurulum (her dağıtım)

```bash
./install.sh      # etkileşimli; normal kullanıcınızla çalıştırın
./uninstall.sh    # geri kaldırır (profilleriniz ve DLL yedekleriniz siz istemedikçe kalır)
```

Kurulum betiği nereye kurulacağını (yalnızca siz için `~/.local` ya da tüm kullanıcılar için `/usr/local`), PySide6 / PyYAML eksikse nasıl kurulacağını (dağıtım paketleri ya da pip ile özel bir Python ortamı), vk_flip_meter'in derlenip lutris-game-tune'un (setuid ya da polkit) kurulup kurulmayacağını sorar. Bir şey değiştirmeden önce özet gösterir, `sudo`'yu yalnızca root gereken adımlarda kullanır, onay verirseniz eksik derleme araçlarını apt / dnf / pacman / zypper ile kurar ve güncellemek için yeniden çalıştırılabilir. Soru sormadan kurulum için seçenekler: `./install.sh --help` (`--yes --user --no-flm …`). Sistem diliniz Türkçeyse mesajlar Türkçedir.

## Kaynak koddan çalıştırma

```bash
pip install PySide6 pyyaml
python3 DRSTool.py
```

`drstool_gui/` ve `drstool_core/` klasörlerini `DRSTool.py`'nin yanında tutun — arayüz paketi ve katalog verisi orada durur.

## pip / pipx ile kurulum

```bash
pipx install git+https://github.com/arabcian/DRSTool   # ya da: pip install --user .
drstool
```

vk_flip_meter ve lutris-game-tune kaynakları `<prefix>/share/drstool/` altına kurulur; arayüz onları yine derleyip kurabilir.

## Arch Linux'a kurulum

```bash
cd packaging/arch && makepkg -si
```

Arayüzü, vk_flip_meter katmanını, masaüstü girdisini ve simgeyi kurar. lutris-game-tune `/usr/local` altında durduğu için arayüzden kurulur (Extra Tools › lutris-game-tune).

## Testler

```bash
python3 -m unittest discover -s tests -p 'test_core.py' -v     # drstool_core, Qt gerekmez
QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui -v  # arayüz uçtan uca, sahte ev klasörüyle
bash lutris-game-tune-main/tests/run-tests.sh                   # lutris-game-tune (normal kullanıcı olarak)
```

## Gentoo'ya kurulum

Dahil olan ebuild (`drstool-9999.ebuild`) git'ten çeken bir live ebuild'dir.

USE bayrakları:

| Bayrak | Varsayılan | Etkisi |
|---|---|---|
| `flip-meter` | açık | vk_flip_meter Vulkan katmanını (C++) derler ve kurar |
| `lto` | kapalı | Katmanı `-flto` ile derler (`flip-meter` gerektirir) |
| `pgo` | kapalı | Katman için iki geçişli profil tabanlı optimizasyon (`flip-meter` gerektirir) |
| `lutris-tune` | açık | lutris-game-tune sarmalayıcısını, betiğini ve varsayılan yapılandırmayı kurar |
| `polkit` | kapalı | lutris-game-tune sarmalayıcısını **setuid olmadan** kurar: PRE/POST `pkexec` (polkit kuralı) ile, RUN yalnızca `cap_sys_nice` dosya yetkisiyle çalışır |

```bash
emerge --oneshot games-util/drstool
```

Not: lutris-game-tune yetkili bir sarmalayıcı gerektirir — varsayılan olarak setuid root, ya da `USE=polkit` ile pkexec + dosya yetkisi. Ebuild bunu ayarlar; `lutris-game-tune-wrapper STATUS` ile (polkit kipinde `pkexec` üzerinden) kontrol edebilirsiniz.

## Kullanım notları

- Değişiklikler seçili oyunun yapılandırmasına uygulanır. Yazmadan önce önizlemeyi kontrol edin.
- Ayarlar diske yazılır ve bir sonraki oyun başlatmada etkili olur. lutris-game-tune değişiklikleri bir sonraki PRE çalıştırmasında uygulanır.
- Lutris YAML dosyasını yedekleyin. Bir Lutris yapılandırmasına yazmadan önce DRSTool, yanına zaman damgalı bir `.bak` kopyası oluşturur ve en son 5 tanesini tutar.

## Proje yapısı

```
DRSTool.py                 giriş betiği (drstool_gui.app.main'i başlatır)
drstool_gui/               PySide6 arayüzü, her sayfa / özellik için bir modül (aşağıya bakın)
install.sh / uninstall.sh  her dağıtım için etkileşimli kurulum
drstool_core/              Qt'siz kod: katalog (modeller, yükleyici, data/*.json), paylaşım kodu, Steam kısayolları, oyun / DLL tarama, DLL değiştirme
tests/                     birim testleri (core) ve ekransız arayüz testleri
assets/drstool.png         uygulama simgesi
data/drstool.desktop       masaüstü girdisi (pip / Arch)
drstool-9999.ebuild        Gentoo ebuild
packaging/arch/PKGBUILD    Arch Linux paketi (drstool-git)
pyproject.toml             pip / pipx kurulumu
vk-flip-meter-main/        FLM Vulkan katmanı (C++, CMake)
lutris-game-tune-main/     Lutris ayarlayıcı (Bash + C sarmalayıcı: setuid ya da polkit)
DISCLAIMER.md              risk bildirimi
LICENSE                    MIT
```

### Katalog verisi

DRS ayarları, GPU mimarileri, ortam değişkenleri, Gamescope bayrakları, `dxvk.conf` anahtarları ve `VKD3D_CONFIG` bayrakları kod değil veridir: `drstool_core/data/*.json` (veri sınıfları `drstool_core/models.py`, yükleyici `drstool_core/catalog.py`). Yeni bir değişken ya da DRS ayarı eklemek bir JSON girdisi eklemektir; varsayılana eşit alanlar (boş açıklama, seçeneksiz …) yazılmayabilir. `tests/test_core.py` içindeki `CatalogTests` yinelenen girdileri ve seçeneksiz `enum`'ları yakalar.

### Arayüz paketinin yapısı

`drstool_gui/` her sayfa ya da özellik için bir modül içerir ve modüller yalnızca aşağıya doğru import eder: en altta `base`, `theme`, `widgets`; sonra uygulamanın bildikleri (`catalog_data`, `lookup`, `sysinfo`, `state`, `lint`, `launch_import`); sonra sayfalar (`settings_manager`, `setting_editor`, `env_widgets`, `profiles`, `launchers`, Extra Tools sayfaları …); en üstte `main_window`, `cli` ve `app`. Her modül, ne içerdiğini söyleyen bir docstring ile başlar. Üst düzey import'un döngü oluşturacağı birkaç fonksiyon (`lookup.env_def`, `state._gs_flag_name`, `launch_import.parse_launch_text` / `op_label`, `steam_utils.steam_compat_tools` / `steam_install_dirs`) bir sayfa sınıfını fonksiyonun içinde import eder.

## Lisans

MIT. Ayrıntılar için [LICENSE](LICENSE).

## Sorumluluk reddi

Bu proje yapay zekâ desteğiyle geliştirilmiştir ve garanti verilmeden sunulmaktadır. Tam metin için [DISCLAIMER.md](DISCLAIMER.md) dosyasına bakın.
