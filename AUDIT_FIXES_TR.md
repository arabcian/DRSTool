# DRSTool — 24 Kasım 2026 denetim düzeltmeleri

Temel kaynak: DRSTool-experimental(5).zip. Önceki rapordaki 48 sayısı bir toplama hatasıydı: numaralandırılmış 51 madde var. Aşağıdaki değişiklikler bu maddelere yöneliktir; tüm maddelerin gerçek donanım üzerinde kapatıldığı anlamına gelmez.

| Maddeler | Uygulama ve kalan sınırlar |
| --- | --- |
| CM-01–04 | Directory descriptor üzerinden tarama/silme, O_NOFOLLOW ve inode kontrolleri, dosya sistemi sınırı, akış halinde tarama ve iptal kontrolü. Aynı dosya sistemi içindeki mount/rename yarışları için mutlak güvenlik garantisi yoktur. |
| DLL-01–05 | Kaynak staging ve checksum doğrulama, hedefin yeniden kontrolü, RuntimeError dahil rollback hata yönetimi, orijinal hash ile sonuç doğrulama. Dışarıdan değiştirilmiş hedefler ezilmez; gerektiğinde manuel recovery gerekir. |
| DLL-06 | Mode, sahiplik ve desteklenen extended attributes/ACL korunması. Dosya sisteminin izin vermediği durumlar hata olarak ele alınır. |
| LGT-01–02 | Eşleşmeyen POST başka oturumu silmez; eşleşen token desteklenir, eski hook'lar yalnızca tek anlamlı launcher eşleşmesinde çalışır. Steam her başlatmada token üretir. Lutris hook token'ı oyun bazlıdır: aynı oyunun eşzamanlı örnekleri ve gecikmiş hook senaryosu tam çözülmüş değildir. |
| LGT-03–05 | PRE tamamlanma durumu, snapshot ile restore, profil çatışmalarının reddi, yarım PRE kurtarma desteği. Gerçek sysfs ve root işlemleri doğrulanmamıştır. |
| CB-01–05 | Başlangıç kombinasyonu testi, boş/tekrarlanamayan/belirsiz sonuçlar, tekrar testi ve isteğe bağlı iki tutarlı deneme, tahmin aralığı; eski kaydedilmiş aramalar yeni algoritma ile sıfırlanır. |
| FT-01–03 | Ortak istatistik modülü; medyan, percentile ve low FPS tanımlarının birleştirilmesi. |
| FT-04–10 | Örnekleme uyarısı, tahmini frame ve süre etiketleri, uzun donmaları koruma, sonlu sayılar, VRAM yorumunun düzeltilmesi, karşılaştırma metadata kontrolleri, CSV parser ve 2 milyon örnek sınırı. Örnekleme ile kaybolan gerçek frame verileri geri getirilemez. |
| XID-01–07 | GPU ve zaman sınırları, belirsiz zaman/secondary-only olaylar, kök adayları, timezone, GPU bazlı örüntü. |
| XID-08 | Adaptif aramada en güçlü 10 aday içinde üçlü etkileşimler; delta debugging daha geniş grupları da deneyebilir. Olasılık modeli tüm yüksek dereceli etkileşimleri kapsamaz; bütçe biterse sonuç belirsizdir. |
| PR-01–04 | Sınırlı share code parsing, strict boolean, Gamescope seçenek kataloğu ve daha geniş hassas değişken adı filtresi. Filtre tüm olası sırları tanıyamaz; desteklenmeyen özel Gamescope flag'leri atlanabilir. |
| PR-05–08 | Bozuk veriyi koruma, yazmayı reddetme, son sağlam yedek ve açık recovery metodu, güncel okuma/oyun adı ve yapısal VDF parser. Recovery metodu çekirdektedir; yeni bir GUI kurtarma düğmesi eklenmemiştir. |
| FLM-01–05 | Queue eşleştirmesinde belirsizlik kontrolü, başarılı present ID takibi, measurement reset, büyük uygun swapchain seçimi, FPS değişikliği/gecikme durumunda limiter reset. Güvenli downstream dispatch bulunamazsa VK_ERROR_DEVICE_LOST dönüşü sürer. Swapchain seçimi sezgiseldir. Vulkan derlemesi ve gerçek GPU testi yapılamadığından açık doğrulama kalemleridir. |

## Doğrulama
- Python pytest: 275 başarılı, 114 atlanan, 7 başarılı alt test. 11 yeni regresyon testi.
- Lutris Game Tune shell: 85 başarılı, 0 başarısız.
- Wrapper: 10 başarılı, 0 başarısız; izole test scripti ile.
- Bash syntax, C wrapper -Wall -Wextra -Werror syntax, Python compileall, git diff --check: başarılı.
- PySide6 mevcut değil; atlanan GUI testleri doğrulanmamıştır.
- CMake Vulkan geliştirme kütüphanesi/başlıkları bulunamadığı için yapılandırılamadı; FLM binary derlenmedi.
- Gerçek NVIDIA GPU, Wine/Proton, Gentoo sysfs ve root tuning testleri yapılmadı.

Önceki denetimin 8. bölümündeki diğer programcıklar için listelenen maddeler doğrulanmış hatalar değil, inceleme adaylarıydı. Bu pakette o listenin tümü için ikinci bir eksiksiz denetim yapılmadı.

## Kurulum notu
Lutris Game Tune script ve wrapper birlikte güncellenmeli; mevcut Lutris hook ve Steam shortcut scriptleri yeniden oluşturulmalıdır. Eski hook'larda eşleşmeyen POST artık başka oturumu silmez. Tüm oyunlar kapalıyken STATUS ile kontrol edin; yalnızca gerekirse RESTORE kullanın. Aktif oyun varken zorla restore yapmayın. FLM değişikliklerini gerçek GPU'da doğrulamadan kararlı sürüm olarak değerlendirmeyin.
