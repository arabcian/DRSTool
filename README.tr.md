# DRSTool

**Linux'ta Proton/DXVK ile oynadığın oyunlar için NVIDIA sürücü ayarlarını ve oyun ortam değişkenlerini tıklayarak ayarlamanı sağlayan masaüstü aracı.**

Windows'taki **NVIDIA Profile Inspector**'ı düşün: DRSTool onun Linux karşılığıdır. Ek olarak DXVK, VKD3D-Proton, Proton, Wine, gamescope ve daha birçok bileşenin ayarlarını da tek pencerede toplar.

> ⚠️ **Kendi riskinde kullan.** Sürücü ayarlarıyla oynamak çökme, donma veya görüntü sorunlarına yol açabilir. Bir şey ters giderse eklediğin değişkenleri kaldırıp oyunu yeniden başlatman yeterlidir. Ayrıntılar: [DISCLAIMER.md](DISCLAIMER.md)

<img width="1970" height="1467" alt="DRSTool ekran görüntüsü" src="https://github.com/user-attachments/assets/8e3e741d-17a7-4ce1-818b-3bedc1da8c81" />

---

## Neden var?

Linux'ta bir oyunun NVIDIA davranışını değiştirmek normalde şöyle bir satırı elle yazmak demektir:

```
DXVK_NVAPI_DRS_SETTINGS=0x10E41DF3=0xffffff,0x10E41DF7=0xffffff DXVK_NVAPI_GPU_ARCH=GB200 VKD3D_CONFIG=dxr ... %command%
```

Hex kodlarını ezberlemek, hangi değişkenin ne işe yaradığını wiki'lerde aramak, bir harf hatası yüzünden ayarın hiç çalışmaması... DRSTool bunun yerine sana:

- **Okunabilir isimler** ("DLSS-SR Preset" gibi) ve her ayar için **açıklama** gösterir,
- Değeri **butonla / listeden seçtirir**, yanlış değer girmeni zorlaştırır,
- Sonucu **tek tıkla kopyalanabilir** hazır bir satıra çevirir,
- Her oyun için **profil** olarak kaydeder, istersen doğrudan **Lutris** ayarına yazar.

---

## Neler yapabilirsin?

| | |
|---|---|
| 🎛️ **NVIDIA sürücü ayarları (DRS)** | DLSS preset/mod, Frame Generation, Ray Reconstruction, V-Sync, G-Sync, anti-aliasing, doku filtreleme ve daha fazlası — 118 ayar, açıklamalarıyla. |
| 🖥️ **GPU mimarisi** | Kartının mimarisini (Maxwell → Blackwell) seç; bazı ayarlar ancak bununla doğru çalışır. |
| 🌿 **Ortam değişkenleri** | 250'den fazla değişken: DXVK, VKD3D-Proton, DXVK-NVAPI, NVIDIA `__GL_*`, NVIDIA PRIME, Proton, Wine, gamescope, vk_flip_meter ve bazı fork'lar (GE, CachyOS, EM vb. — hangi fork'a ait olduğu yazılı). |
| 🎮 **gamescope komut oluşturucu** | Çözünürlük, HDR, VRR, FSR/NIS/SGSR, FPS limiti, MangoApp gibi gamescope bayraklarını seçerek hazır komut üretir. |
| 💾 **Profiller** | Oyun başına tüm ayarları kaydet, sonra tek tıkla geri yükle. |
| 🔄 **Lutris senkronizasyonu** | Ayarları seçtiğin oyunun Lutris yapılandırmasına yaz ya da oradan içeri aktar. |
| ⏱️ **vk_flip_meter** | Frame-pacing (kare zamanlaması) katmanını kaynaktan derleyip kur, çalışırken ayarla. |
| ⚙️ **lutris-game-tune** | Oyun açılırken sistemi oyun moduna alan, kapanınca eski haline döndüren yardımcıyı yönet. |

---

## Kurulum

### Gereksinimler

- Python 3.12 veya üstü
- PySide6 (Qt 6)
- PyYAML (Lutris özelliği için)
- *(İsteğe bağlı)* `setproctitle` — görev çubuğunda doğru simge/isim için
- *(İsteğe bağlı)* vk_flip_meter derlemek için: `cmake`, C++ derleyici, Vulkan başlık dosyaları ve `pkexec`

### Hızlı başlangıç

```bash
git clone https://github.com/arabcian/DRSTool.git
cd DRSTool
pip install --user PySide6 pyyaml
python3 DRSTool.py
```

### Gentoo

Depodaki `drstool-9999.ebuild` ile kurabilirsin. USE bayrakları:

| USE | Ne yapar |
|---|---|
| `flip-meter` *(varsayılan açık)* | vk_flip_meter Vulkan katmanını derler ve kurar |
| `lutris-tune` *(varsayılan açık)* | lutris-game-tune yardımcısını kurar |
| `lto` | Katmanı LTO ile derler |
| `pgo` | Katmanı iki aşamalı PGO ile derler (talimatlar kurulum sonunda gösterilir) |

---

## Nasıl kullanılır? (5 adımda)

1. **DRS Settings** sekmesinde değiştirmek istediğin ayarı bul (üstteki arama kutusu işini kolaylaştırır), sağda değerini seç. Ayarlanan satırlar solda **yeşil** görünür.
2. **GPU Arch** sekmesinden ekran kartının mimarisini seç.
3. **Environment** sekmesinden istediğin ortam değişkenlerini ayarla. gamescope kullanacaksan listedeki **"Gamescope launch flags"** satırına tıkla.
4. Pencerenin üstündeki **çıktı çubuğunda** oluşan satırı **Copy all** ile kopyala:
   - Terminal / betik için düz biçim,
   - veya Steam için sonu `%command%` ile biten **Steam launch options** biçimi.
5. Beğendiğin ayarları **Profiles** sekmesinde bir isimle kaydet.

Steam'de: Oyuna sağ tık → **Özellikler** → **Başlatma Seçenekleri** kutusuna yapıştır.

---

## Sekmeler

### 1. DRS Settings
NVIDIA sürücü ayarları kategorilere ayrılmış halde listelenir (DLSS/NGX, V-Sync, G-Sync/VRR, Anti-Aliasing, doku filtreleme, güç, OpenGL...). Her ayarın kısa ve uzun açıklaması vardır. Değer tipine göre uygun kontrol gelir: seçenek butonları, sayı alanı veya bit kutucukları.

### 2. GPU Arch
Kartının mimarisini seçersin; çıktıya `DXVK_NVAPI_GPU_ARCH` eklenir. Her mimari için örnek kart modelleri gösterilir.

### 3. Environment
Tüm ortam değişkenleri tek listede, kategorilere ayrılmış olarak durur. Öne çıkan kolaylıklar:

- **DXVK_HUD** ve **VKD3D_CONFIG**: bayrakları tek tek butonla aç/kapat, her birinin açıklaması yanında.
- **DXVK_CONFIG**: dxvk.conf ayarlarını tablodan seç. Örneğin:
  - `dxvk.latencySleep` — DXVK'nın düşük gecikme / Reflex modu
  - `dxvk.maxFrameRate` — FPS sınırı
  - `dxgi.syncInterval` — V-Sync'i zorla aç/kapat
  - `dxgi.hideNvidiaGpu` — DLSS/Reflex için NVIDIA'yı gizleme
  - Tabloda olmayan anahtarları "Other entries" alanına yazabilirsin, silinmez.
- **gamescope launch flags**: gamescope komutunu bayrakları işaretleyerek oluşturur.

> 💡 Tanımadığın bir değer bir profilde kaldıysa (ör. vkd3d-proton'dan kaldırılmış eski bir bayrak), DRSTool onu **silmez**, sarı bir notla gösterir.

### 4. Extra Tools
- **vk_flip_meter**: Katmanı kaynaktan derleyip kurar. Derleme normal kullanıcınla yapılır; şifre sadece sisteme kopyalama adımında (`pkexec`) istenir. Oyun çalışırken ayarları **Live Tuning** bölümünden anında değiştirebilirsin.
- **lutris-game-tune**: Oyun başlarken CPU/sistem ayarlarını oyun moduna alan, oyun kapanınca geri yükleyen aracı kurar, durumunu ve günlüğünü gösterir.

### 5. Profiles + Lutris Game Sync
- **Solda** profillerin: kaydet, yükle, sil.
- **Sağda** Lutris senkronizasyonu:
  1. Listeden oyununu seç.
  2. **Load from selected game** ile o oyunun mevcut ayarlarını DRSTool'a aktar.
  3. İstediğin değişiklikleri yap.
  4. **Write to Lutris config** ile yaz. Yazmadan önce neyin ekleneceğini ve **neyin silineceğini** gösteren bir onay penceresi çıkar. Her yazmada otomatik yedek alınır.

> ⚠️ **Önemli:** DRSTool, katalogundaki değişkenlerin "sahibi" gibi davranır — ekranda boş olan bir değişkeni Lutris dosyasından siler. Bu yüzden bir oyuna yazmadan önce **mutlaka önce "Load from selected game"** yap. Katalogda olmayan, kendi elinle yazdığın değişkenlere dokunulmaz.

---

## Kısayollar

| Tuş | İşlev |
|---|---|
| `Ctrl+F` | Aramaya odaklan |
| `Esc` | Aramayı temizle |
| `Ctrl+1` … `Ctrl+5` | Sekmeler arasında geç |
| `Ctrl+S` | Yüklü profili kaydet |
| `Ctrl+Shift+C` | Oluşan satırın tamamını kopyala |

Profil yüklüyken değişiklik yaparsan pencere başlığında **•** işareti belirir; kaydetmeyi unutmazsın.

---

## Ayarlar nerede saklanıyor?

- Profiller: `~/.config/drstool/profiles.json` (veya `$XDG_CONFIG_HOME/drstool/profiles.json`)
- Dosya güvenli şekilde yazılır; kayıt sırasında elektrik gitse bile bozulmaz.
- Eski `~/.drs_configurator_profiles.json` dosyası ilk açılışta otomatik taşınır.

---

## Sık sorulan sorular

**Ayarım etki etmiyor gibi, ne yapmalıyım?**
DRS ayarlarının çoğu yalnızca **dxvk-nvapi etkinken** çalışır. Proton'da `PROTON_ENABLE_NVAPI=1` gerekebilir. DLSS ile ilgili ayarlar için oyunun DLSS'i gerçekten kullanıyor olması gerekir.

**DLSS preset olarak ne seçmeliyim?**
Emin değilsen **"Latest"** seç; sürücü her mod için önerilen preset'i kullanır.

**Bir şey bozuldu, nasıl geri alırım?**
Çıktı çubuğundaki **Reset** ile hepsini sıfırla, ya da Steam/Lutris'teki başlatma seçeneklerini sil. Lutris için DRSTool'un aldığı yedek dosyası oyunun `.yml` dosyasının yanında durur.

**Hangi değişkenin hangi Proton sürümünde çalıştığını nasıl bilirim?**
Fork'a özgü değişkenlerin açıklamasında hangi fork'a (GE-Proton, Proton-CachyOS, Proton-EM...) ait oldukları yazar. Upstream'den kaldırılmış olanlar da açıklamada belirtilir.

---

## Kapsanan projeler

DRSTool'daki ayar ve açıklamalar şu projelerin güncel kaynak kodlarından derlenmiştir (son eşitleme: Eylül 2026):

- [DXVK](https://github.com/doitsujin/dxvk)
- [VKD3D-Proton](https://github.com/HansKristian-Work/vkd3d-proton)
- [DXVK-NVAPI](https://github.com/jp7677/dxvk-nvapi)
- [NVIDIA NVAPI başlıkları](https://github.com/NVIDIA/nvapi)
- [gamescope](https://github.com/ValveSoftware/gamescope)
- [Proton](https://github.com/ValveSoftware/Proton)

---

## Lisans

MIT — ayrıntılar için [LICENSE](LICENSE). Bu proje yapay zeka yardımıyla geliştirilmiştir; bkz. [DISCLAIMER.md](DISCLAIMER.md).
