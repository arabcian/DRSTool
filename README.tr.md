# DRSTool

DRSTool, Linux üzerinde DXVK_NVAPI_DRS_SETTINGS dizgileri (strings) oluşturmak için geliştirilmiş PySide6 tabanlı bir masaüstü arayüzüdür (GUI). Windows'taki NVIDIA Profile Inspector'ın DXVK-NVAPI eşdeğeridir. Bu aracın geliştirilme amacı; Linux'ta Proton/DXVK üzerinden çalışan bir oyun için NVIDIA sürücü davranışını ince ayar yapmanın, normalde hafızadan veya dağınık wiki sayfalarından uzun ve hataya açık çevre değişkeni (environment variable) dizgilerini elle yazmayı gerektirmesidir. DRSTool bu süreci aranabilir, belgelendirilmiş, tıkla-seç mantığında çalışan bir editöre dönüştürür ve sonucu oyun başına yeniden kullanılabilir bir profil olarak kaydetmenizi sağlar.

## Ne İşe Yarar?

DRSTool ile şunları yapabilirsiniz:
* NVIDIA Sürücü Ayarlarına (DRS) Göz Atın ve Yapılandırın: Windows'ta NVIDIA Profile Inspector'ın sunduğu alt seviye (low-level) ayarların aynısı, burada dxvk-nvapi'nin DXVK_NVAPI_DRS_SETTINGS çevre değişkeni için yeniden uygulandı.
* GPU Mimarisi Seçin: DRSTool'un ekran kartınız için doğru DXVK_NVAPI_GPU_ARCH değerini üretmesini sağlayın.
* Çevre Değişkenlerini Yönetin: DXVK, VKD3D-Proton ve NVIDIA __GL_* çevre değişkenlerini, değişken adlarını ve geçerli değerlerini ezberlemek zorunda kalmadan aynı aranabilir ve belgelenmiş arayüz üzerinden yapılandırın.
* Frame-Pacing Katmanını Yönetin: vk_flip_meter (FLM) frame-pacing katmanının çalışma zamanı (runtime) değişkenlerini yapılandırın ve katmanın kendisini kaynak kodundan derleyip yükleyin.
* Profilleri Kaydedin ve Yükleyin: Oyun başına tam bir profil anlık görüntüsü (DRS ayarları + GPU mimarisi + çevre değişkenleri) kaydedip daha sonra yeniden yükleyin. Oluşturulan nihai ANAHTAR=DEĞER ... kombinasyonunu kopyalayarak bir başlatma betiğine, Steam başlatma seçeneklerine veya bir Lutris konfigürasyonuna doğrudan yapıştırın.

Kısacası: Sürücü ayarlarını elle hex kodlarıyla yazmak yerine; işaret edin, tıklayın, açıklamasını görün ve kopyalayın.

## Neden Var?

Windows'ta NVIDIA Profile Inspector, NVIDIA Denetim Masası'nın sunduğu ayarların ötesine geçerek oyun başına sürücü davranışını ince ayar yapmak için standart araçtır. Linux tarafında ise Proton/Wine oyunları için bu hassas kontrolü yeniden üretmenizi sağlayacak dxvk-nvapi ayarlarına yönelik eşdeğer bir GUI bulunmuyordu; hex ayar kimliklerini (ID) ve geçerli değerleri önceden bilmeniz gerekiyordu. DRSTool; insan tarafından okunabilir adlar, açıklamalar ve her ayara özel düzenleyiciler sunarak Linux oyun yığını (DXVK, VKD3D-Proton, dxvk-nvapi, vk_flip_meter) için özel olarak bu boşluğu doldurur.

## Gereksinimler

* Python >= 3.7
* PySide6 >= 6.10
* vk_flip_meter derleme/yükleme özelliği için: Sistemde cmake, bir C++ derleyicisi ve pkexec (PolicyKit) bulunmalıdır.

## Çalıştırma

python3 DRSTool.py

## Arayüze Genel Bakış

Uygulama; sol kenar çubuğu (liste/navigasyon) ve sağ editör paneli olarak ikiye bölünmüş tek bir pencereden oluşur. Pencerenin üst kısmında ise o an üretilen çevre değişkeni dizgisini gösteren kalıcı bir çıktı çubuğu yer alır. Kenar çubuğu ve editörün ne göstereceğini beş sekme belirler:

### 1. DRS Settings (DRS Ayarları)
OpenGL, Anti-Aliasing, Texture Filtering, VSync/Flip, Frame Rate, Power, SLI, Stereo, VRR/G-Sync, DLSS/NGX, Ansel, FXAA, AO, Optimus ve Misc gibi kategoriler altında toplanmış, aranabilir 117 sürücü ayarından oluşan bir listedir. Her ayarın kısa bir açıklaması, daha detaylı uzun bir açıklaması ve arka plandaki değerin gerçekte nasıl kodlandığıyla eşleşen doğru kontrol türü (enum açılır menüsü, sayısal yukarı/aşağı sayacı veya bit alanı onay kutuları) bulunur. Bir ayar seçildiğinde sağ tarafta editörü açılır; bir değer atandığında çıktı çubuğu güncellenir ve ilgili ayar sol listede yeşil renkle vurgulanır.

### 2. GPU Arch (GPU Mimarisi)
NVIDIA GPU mimari ailelerinin (GeForce 900 serisinden RTX 50 serisine kadar, yani Maxwell'den Blackwell'e) ve her biri için örnek kartların listesidir. Bir mimari seçildiğinde çıktı dizgisine DXVK_NVAPI_GPU_ARCH eklenir; çünkü bazı DRS ayarları yalnızca sürücü hangi mimariyle çalıştığını bildiğinde doğru şekilde uygulanır.

### 3. DXVK / VKD3D / NV / FLM
Aşağıdaki bileşenleri kapsayan tek bir birleştirilmiş, kategorize edilmiş ve aranabilir listedir:
* DXVK çevre değişkenleri: (HUD bayrakları, günlük kaydı (logging), cihaz/kare ile ilgili seçenekler vb.) — 15 değişken
* VKD3D-Proton çevre değişkenleri: (VKD3D_CONFIG bayrak matrisi dahil) — 16 değişken
* NVIDIA __GL_* değişkenleri: — 31 değişken
* vk_flip_meter (FLM) çalışma zamanı değişkenleri: (FLM_MODE, FLM_TARGET_FPS, FLM_MFG_MULTIPLIER vb.) — 16 değişken

Her değişkenin türü belirlenmiştir (string, enum, bool, integer veya bayrak kümesi) ve uygun düzenleyici kontrolünü alır (metin alanı, açılır menü, onay kutusu veya DXVK_HUD ve VKD3D_CONFIG gibi çoklu bayrak değişkenleri için bir onay kutusu matrisi). Burada belirlediğiniz değerler, DRS ayarlarıyla birlikte aynı birleşik çıktı dizgisinde birleştirilir.

### 4. Profiles (Profiller)
Mevcut durumun tamamını (DRS ayarları, GPU mimarisi ve tüm çevre değişkenleri) bir isim altında kaydedin, ardından daha sonra yeniden yükleyin veya silin. Profiller, çökme veya güç kesintisi durumunda bozulmayı önlemek için atomik olarak yazılan $XDG_CONFIG_HOME/drstool/profiles.json (alternatif olarak ~/.config/drstool/profiles.json) altındaki JSON dosyasında saklanır. Eski ~/.drs_configurator_profiles.json konumunu kullanan mevcut kurulumlar, ilk çalıştırmada otomatik olarak yeni konuma taşınır.

### 5. vk_flip_meter
Bu depoda bir alt proje (subproject) olarak paketlenmiş vk_flip_meter Vulkan katmanı için bir derleme/yükleme panelidir. Katmanın kaynak kodunun yerini tespit eder veya seçmenizi sağlar, ardından yetkisiz (unprivileged) bir cmake yapılandırması ve derlemesi çalıştırır. Yalnızca gerçekten root yetkisi gerektiren iki adım için pkexec aşamasına geçer: cmake --install ve manifest kütüphane yolu düzeltmesi. Bu sayede neredeyse tüm derleme hattı normal kullanıcınız olarak çalışır ve şifre istemi (grafiksel bir polkit iletişim kutusu aracılığıyla) yalnızca mümkün olan en son anda tetiklenir.

*Katmanın çalışma zamanı ince ayarları (FLM_MODE, FLM_TARGET_FPS vb.) bu sekmeden yapılmaz.* Bu ayarlara "DXVK / VKD3D / NV / FLM" sekmesinden tek tıkla ulaşılabilir, her şeyle aynı editör kullanılır ve nihai çıktı dizgisine otomatik olarak dahil edilir.

## Çıktı Çubuğu (Output Bar)

Pencerenin üst kısmı boyunca DRSTool; mevcut DRS ayarlarınızdan, GPU mimarinizden ve çevre değişkenlerinizden oluşturulan birleşik çevre dizgisini ve bir "Kopyala" (Copy) eylemini sürekli olarak gösterir. Buradan doğrudan bir Lutris/Steam başlatma seçenekleri alanına veya bir shell betiğine yapıştırmaya hazırdır.

## Tasarım Notları

* Sinyal Odaklı Durum (Signal-driven state): Merkezi bir SettingsManager (bir QObject), DRS ayarları, GPU mimarisi ve profiller için tek doğruluk kaynağıdır (source of truth). Belirli Qt sinyalleri (settings_changed, arch_changed, profiles_changed, profile_loaded) yayar, böylece UI widget'ları yalnızca gerçekten değişen kısımları yeniden oluşturur. Örneğin; profil listesi her ayar düzenlemesinde değil, yalnızca profiles_changed sinyali geldiğinde yenilenir.
* Atomik Profil Yazımları: Profiller geçici bir dosyaya yazılır ve öncesinde bir fsync() çağrılarak os.replace() ile yerine yerleştirilir; böylece kaydetme sırasındaki bir çökme profil dosyasını bozamaz.
* Shell Uyumlu Çıktı: Birleşik çevre dizgisi shlex.quote() ile oluşturulur. Böylece boşluk veya özel karakter içeren değerler, bir shell'e yapıştırıldığında sessizce bozulmak yerine doğru şekilde tırnak içine alınır.
* Arayüz Tasarımı: Paylaşılan Qt stil sayfaları (stylesheets) aracılığıyla tüm sekmelerde (liste başlıkları, seçim vurgulamaları, kaydırma çubukları) tutarlı bir şekilde uygulanan koyu tema ve NVIDIA yeşili vurgulu kullanıcı arayüzü.
