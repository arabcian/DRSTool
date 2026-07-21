# DRSTool

**DRSTool**, Linux'ta NVIDIA sürücü davranışını, frame pacing'i ve sistem düzeyinde performansı oyun için ince ayar yapmanızı sağlayan PySide6 tabanlı bir masaüstü arayüzüdür — üç aracı tek bir birleşik arayüzde toplar:

- **DRS Settings editörü** — NVIDIA Profile Inspector'ın Linux karşılığı; `dxvk-nvapi`'nin `DXVK_NVAPI_DRS_SETTINGS` değişkeni için
- **vk_flip_meter** — Vulkan frame-pacing / kadans modülasyonu implicit katmanı (kaynak kod bu repoya dahil, uygulama içinden derlenir)
- **lutris-game-tune** — Lutris/Wine oyunları için sistem genelinde performans ayarlayıcı (CPU governor, PCIe ASPM, C-state'ler, THP, CCD/CCX izolasyonu; setuid-root wrapper aracılığıyla yönetilir)

Bu araç; Proton/Wine ile çalışan bir oyun için NVIDIA sürücü davranışını ince ayar yapmanın normalde hafızadan veya dağınık wiki sayfalarından uzun hex kodlu çevre değişkeni dizgilerini elle yazmayı gerektirdiği için geliştirildi. DRSTool bunu aranabilir, belgelendirilmiş, tıkla-seç mantığında çalışan bir editöre dönüştürür — ve bu felsefeyi Linux oyun yığınındaki ilgili her araca genişletir.

## Depo Yapısı

```
DRSTool/
├── DRSTool.py                    # Ana uygulama (tek dosya, PySide6)
├── assets/
│   └── drstool.png               # Uygulama ikonu
├── vk-flip-meter-main/           # Paketlenmiş vk_flip_meter kaynağı (C++/CMake)
│   ├── CMakeLists.txt
│   ├── build.sh
│   ├── src/flip_meter.cpp
│   └── manifest/
│       └── VkLayer_cpu_flip_meter.json.in
└── lutris-game-tune-main/        # Paketlenmiş lutris-game-tune kaynağı (Bash + C)
    ├── install.sh
    ├── uninstall.sh
    ├── lutris-game-tune.sh
    ├── lutris-game-tune-wrapper.c
    └── lutris-game-tune.conf
```

Her iki alt proje de bu depoya dahildir. Kurulum sırasında harici indirme yapılmaz.

## Gereksinimler

- Python ≥ 3.12
- PySide6 ≥ 6.10 (Gentoo'da: `dev-python/pyside`)
- `pkexec` (PolicyKit) — yetkili kurulum/kaydetme işlemleri için
- vk_flip_meter derlemesi için: `cmake` ≥ 3.20 ve C++20 destekleyen bir derleyici
- lutris-game-tune kurulumu için: `gcc` ve `flock` (`util-linux` paketinin bir parçası)

İsteğe bağlı, önerilir:
- `dev-python/setproctitle` — python-exec2c aracılığıyla başlatıldığında KDE/GNOME Wayland görev çubuğu ikonunun genel bir ikona düşmesini engeller

## Çalıştırma

```bash
python3 DRSTool.py
```

### Gentoo (ebuild)

Canlı-git kurulumu için depo kökünde bir `drstool-9999.ebuild` sağlanmaktadır:

```bash
# Ebuild'i yerel bir overlay'e kopyalayın, ardından:
emerge -av games-util/drstool
```

USE bayrakları:

| Bayrak | Varsayılan | Açıklama |
|---|---|---|
| `flip-meter` | AÇIK | vk_flip_meter Vulkan katmanını derle ve kur |
| `lutris-tune` | AÇIK | lutris-game-tune setuid wrapper'ını derle ve kur |
| `lto` | KAPALI | vk_flip_meter katmanı için bağlantı-zamanı optimizasyonu |
| `pgo` | KAPALI | vk_flip_meter için iki geçişli profil güdümlü optimizasyon (iş akışı için pkg_postinst mesajına bakın) |

## Arayüze Genel Bakış

Uygulama; sol kenar çubuğu ve sağ editör paneli olarak ikiye bölünmüş tek bir pencereden oluşur. Pencerenin üst kısmında ise o an üretilen çevre değişkeni dizgisini gösteren kalıcı bir çıktı çubuğu yer alır. Beş sekme kenar çubuğunun ve editörün ne göstereceğini belirler:

### 1. DRS Settings (DRS Ayarları)

Windows'ta NVIDIA Profile Inspector'ın sunduğu alt seviye ayarların aynısını — `dxvk-nvapi`'nin `DXVK_NVAPI_DRS_SETTINGS` çevre değişkeni için — içeren aranabilir, kategorize edilmiş **117 sürücü ayarı** listesidir.

Kategoriler: OpenGL, Anti-Aliasing, Texture Filtering, VSync/Flip, Frame Rate, Power, SLI, Stereo, VRR/G-Sync, DLSS/NGX, Ansel, FXAA, AO, Optimus ve Misc.

Her ayarın kısa ve uzun açıklaması ile arka plandaki değerin kodlanma biçimiyle eşleşen kontrol türü (enum açılır menüsü, sayısal sayaç veya bit alanı onay kutusu matrisi) bulunur. Bir ayar seçildiğinde sağ tarafta editörü açılır; değer atandığında çıktı çubuğu güncellenir ve ayar sol listede yeşil renkle vurgulanır.

### 2. GPU Arch (GPU Mimarisi)

NVIDIA GPU mimari ailelerinin (GeForce 900 serisinden RTX 50 serisine — Maxwell'den Blackwell'e) ve her biri için örnek kartların listesidir. Bir mimari seçildiğinde çıktı dizgisine `DXVK_NVAPI_GPU_ARCH` eklenir; çünkü bazı DRS ayarları yalnızca sürücü hangi mimariyle çalıştığını bildiğinde doğru uygulanır.

### 3. DXVK / VKD3D / NV / FLM

On kategoriyi kapsayan tek bir birleştirilmiş, kategorize edilmiş, aranabilir çevre değişkeni listesidir:

| Kategori | Değişken Sayısı | Notlar |
|---|---|---|
| DXVK | 15 | HUD bayrakları, günlük kaydı, cihaz/kare seçenekleri |
| VKD3D-Proton | 16 | 41 bayraklı `VKD3D_CONFIG` onay kutusu matrisi dahil |
| NVIDIA `__GL_*` | 31 | Thread opt, VRR, shader önbelleği, G-Sync vb. |
| NVIDIA PRIME | 4 | `__NV_PRIME_RENDER_OFFLOAD`, `DRI_PRIME` vb. |
| Proton | ~18 | Senkronizasyon (ntsync/fsync/esync), HDR, Wayland, NVAPI, NGX, DLSS |
| Wine | ~10 | `WINEFSYNC`, `WINEESYNC`, `WINEDEBUG` vb. |
| DXVK-NVAPI | ~10 | Reflex, DRS geçersiz kılma, günlük kaydı, sürücü sürümü sahtekârlığı |
| NVIDIA Smooth Motion | 4 | `VK_LAYER_NV_present` (RTX 50 kare enterpolasyonu) |
| System / Loader | 5 | `VK_LOADER_DEBUG`, `LD_PRELOAD`, `SDL_VIDEODRIVER` vb. |
| Gamescope | 22 | Çıkış geometrisi, ölçeklendirme (FSR/NIS), HDR, VRR, Steam entegrasyonu |
| vk_flip_meter | 20 | Tüm `FLM_*` çalışma zamanı değişkenleri; FLM_CONFIG + SIGUSR1 ile sıcak yeniden yüklenebilir |

Her değişken türlenmiştir (string, enum, bool, integer veya bayrak kümesi) ve uygun editör kontrolünü alır. Burada belirlenen değerler, DRS ayarlarıyla birlikte aynı birleşik çıktı dizgisinde birleşir.

### 4. Profiles (Profiller)

Mevcut durumun tamamını — DRS ayarları, GPU mimarisi ve tüm çevre değişkenleri — bir isim altında kaydedin, daha sonra yeniden yükleyin veya silin. Profiller `$XDG_CONFIG_HOME/drstool/profiles.json` (alternatif olarak `~/.config/drstool/profiles.json`) altında JSON olarak saklanır; çökme veya güç kesintisinde bozulmayı önlemek için atomik olarak yazılır. Eski `~/.drs_configurator_profiles.json` konumunu kullanan mevcut kurulumlar ilk çalıştırmada otomatik olarak taşınır.

Profiller sekmesindeki **Lutris Sync** alt sekmesi, mevcut DRS + çevre değişkeni dizgisini doğrudan bir Lutris oyununun `system.env` bloğuna zaman damgalı yedekle birlikte yazmanızı sağlar.

### 5. Extra Tools (Ek Araçlar)

Paketlenmiş iki alt proje için sekmeli panel:

#### vk_flip_meter alt sekmesi

Paketlenmiş `vk-flip-meter-main/` kaynağından vk_flip_meter Vulkan implicit katmanını derler ve kurar.

- Kaynak yolu (salt okunur) görüntülenir — seçici gerekmez, deponun bir parçasıdır
- `cmake configure` + `cmake --build` normal kullanıcınız olarak yetkisiz (unprivileged) çalışır
- Yalnızca `cmake --install` pkexec aracılığıyla yetki yükseltir (grafiksel polkit şifre istemi)
- Manifest şablonuna doğru kütüphane yolu yazılması için `FLM_LIB_PATH` derleme zamanında enjekte edilir (kurulum sonrası sed yaması gerekmez)
- **Verify** butonu `vulkaninfo --summary` ile katmanı doğrular
- **Live Tuning** bölümü: Env Vars sekmesindeki mevcut `FLM_*` değişkenlerini bir `FLM_CONFIG` dosyasına yazar ve çalışan bir oyuna `SIGUSR1` göndererek frame-pacing parametrelerini oyunu yeniden başlatmadan sıcak olarak yeniden yükler

`FLM_*` çalışma zamanı değişkenleri **DXVK / VKD3D / NV / FLM** sekmesinde yapılandırılır (bir tık uzağında) ve Copy All çıktısına otomatik dahil edilir.

#### lutris-game-tune alt sekmesi

**Nasıl çalışır:** lutris-game-tune, oyun başlatılırken (PRE) sistem düzeyinde performans iyileştirmeleri uygular ve oyun kapandığında (POST) bunları temiz bir şekilde geri alır. Küçük bir setuid-root C sarmalayıcısı (`lutris-game-tune-wrapper`) yetki yükseltmesini passwordless sudo gerektirmeden veya terminal açık bırakmadan halleder.

**PRE tarafından uygulanan ayarlar:**
- CPU frekans governor + EPP (Ryzen'de amd-pstate üzerinden; dizüstü CPU'larda boost için kritik)
- PCIe ASPM politikası (bağlantı güç durumu geçişlerini devre dışı bırakmak ~100 µs uyandırma gecikmesi ani artışlarını ortadan kaldırır)
- Derin C-state devre dışı bırakma (isteğe bağlı — frame-time sıçramalarını azaltır, ~3–8 W boşta güç artışı karşılığında)
- `vm.swappiness` ayarı (oyun verisini RAM'de tutar, oyun sırasında swap I/O'dan kaçınır)
- Transparent HugePage politikası (`enabled`, `shmem_enabled`, `defrag`)
- PCI gecikme zamanlayıcısı
- CCD/CCX çekirdek izolasyonu (oyunu CCD 0'a sabitler, arka plan süreçlerini çok CCD'li Ryzen masaüstü sistemlerde CCD 1'e kısıtlar; 7845HX gibi tek CCD'li sistemlerde otomatik atlanır)

**Komutlar / Lutris bağlantısı:**

| Alan | Değer |
|---|---|
| Pre-game script | `/usr/local/bin/lutris-game-tune-wrapper PRE` |
| Post-game script | `/usr/local/bin/lutris-game-tune-wrapper POST` |
| Command prefix (isteğe bağlı) | `/usr/local/bin/lutris-game-tune-wrapper RUN -5` |

`RUN -5`, oyunu `nice -5` (daha yüksek CPU önceliği) ile başlatmak için `nice()` sistem çağrısında root yetkisi kullanır, ardından hemen normal kullanıcınıza geri düşer — oyunun kendisi hiçbir zaman root olarak çalışmaz.

`STATUS` (`pkexec lutris-game-tune-wrapper STATUS`) mevcut aktif durumu yazdırır.

**DRSTool entegrasyonu:**
- Kurulum durumu başlangıçta otomatik tespit edilir (wrapper ikilisini kontrol eder)
- **Install** butonu `lutris-game-tune-main/install.sh`'ı pkexec aracılığıyla çalıştırır (paketlenmiş, indirme gerekmez)
- **Uninstall** butonu kurulu lib dizinindeki `uninstall.sh`'ı pkexec aracılığıyla çalıştırır
- 19 config anahtarının tamamı etiketli, belgelendirilmiş UI kontrolleri olarak gösterilir (QCheckBox, QComboBox, QSpinBox, QLineEdit)
- **Save to /etc/** komutu `/etc/lutris-game-tune.conf`'ı pkexec aracılığıyla atomik olarak yazar (stdin-piped `cat >`, ardından `chmod 644 + chown root:root`)
- Hızlı tanı için **Run STATUS** ve **View Log** butonları

Config, `/etc/lutris-game-tune.conf` içinde sistem genelinde saklanır (root sahibi, 644) ve kasıtlı olarak DRSTool profillerinin bir parçası **değildir** — tüm oyunlara global olarak uygulanır.

## Çıktı Çubuğu

Pencerenin üst kısmı boyunca DRSTool; mevcut DRS ayarlarınızdan, GPU mimarinizden ve çevre değişkenlerinizden oluşturulan birleşik çevre dizgisini Copy All butonuyla sürekli gösterir — doğrudan bir Lutris/Steam başlatma seçenekleri alanına veya shell betiğine yapıştırmaya hazırdır.

## Tasarım Notları

- **Sinyal odaklı durum:** Merkezi bir `SettingsManager` (`QObject`), DRS ayarları, GPU mimarisi ve profiller için tek doğruluk kaynağıdır. Belirli Qt sinyalleri (`settings_changed`, `arch_changed`, `profiles_changed`, `profile_loaded`) yayarak UI widget'larının yalnızca gerçekten değişen kısımları yeniden oluşturmasını sağlar.
- **Atomik profil yazımları:** Profiller geçici bir dosyaya yazılır, `fsync()` uygulanır, ardından `os.replace()` ile yerleştirilir — kaydetme sırasındaki bir çökme profil dosyasını bozamaz.
- **Shell uyumlu çıktı:** Birleşik çevre dizgisi `shlex.quote()` ile oluşturulur; boşluk veya özel karakter içeren değerler bir shell'e yapıştırıldığında sessizce bozulmak yerine doğru şekilde tırnak içine alınır.
- **Yetki ayrımı:** DRSTool'un hiçbir parçası root olarak çalışmaz. Tüm yetkili işlemler (vk_flip_meter kurulumu, lutris-game-tune kurulumu/kaydetme/durum) pkexec'e delege edilir. lutris-game-tune wrapper ikilisi tek setuid-root ikilidir ve herhangi bir şeyi çalıştırmadan önce argümanları katı biçimde doğrular.
- **Paketlenmiş alt projeler:** vk_flip_meter ve lutris-game-tune, DRSTool deposunun alt dizinleri olarak gelir. Çalışma zamanında harici indirme yapılmaz.
