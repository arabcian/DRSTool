# DRSTool

> ⚠️ **Riski size aittir.** DRSTool GPU sürücü ayarlarını ve isteğe bağlı olarak sistem ayarlarını değiştirir. Kullanmadan önce [DISCLAIMER.md](DISCLAIMER.md) dosyasını okuyun.

**DRSTool**, [dxvk-nvapi](https://github.com/jp7677/dxvk-nvapi) ve NVIDIA DRS (Driver Registry Settings) profilini yapılandırmak için PySide6 (Qt6) tabanlı bir arayüzdür. Ayrıca Wine, Proton, DXVK, VKD3D-Proton, Gamescope ve NVIDIA'ya özgü ayarlar için ortam değişkeni üreticileri içerir. Sonucu doğrudan bir Lutris oyun yapılandırmasına yazabilir.

English version: [README.md](README.md)

![DRSTool](assets/drstool.png)

## Bileşenler

| Bileşen | Yol | Açıklama |
|---|---|---|
| DRSTool arayüzü | `DRSTool.py` | Ana uygulama: DRS ayarları, ortam değişkenleri, Gamescope bayrakları, profiller, Lutris senkronizasyonu |
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

### Profiller
- Tam profilleri (DRS ayarları + ortam değişkenleri) kaydetme ve yükleme
- XDG uyumlu depolama: `$XDG_CONFIG_HOME/drstool/` (varsayılan `~/.config/drstool/`), atomik yazma

### Ek Araçlar
- **vk_flip_meter**: FLM ayarlarını düzenleme, canlı ayarlama (`FLM_CONFIG` yazar ve `SIGUSR1` gönderir)
- **lutris-game-tune**: ayarlayıcı yapılandırmasını ve oyun bazlı Lutris ayarlarını düzenleme

### Lutris Oyun Senkronizasyonu
- Bir Lutris oyununun YAML yapılandırmasını okur ve yazar (`system.*` anahtarları, ortam değişkenleri, Gamescope seçenekleri, Lutris Game Tune oyun öncesi/sonrası komutları)
- Mevcut bir oyun yapılandırmasından ayarları geri alır ("Seçili oyundan yükle")
- Yalnızca DRSTool'un yönettiği anahtarları değiştirir. Elle yazılmış anahtarlar korunur ve onay penceresi silinecek her şeyi listeler

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
