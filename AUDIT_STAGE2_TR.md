# DRSTool — İkinci denetim ve düzeltme paketi

Temel: DRSTool-experimental-audit-fixes.zip (önceki 51 maddeye yönelik düzeltmeler korunmuştur). Bu ek çalışma, kalan doğrulanabilir eksikliklere ve ek incelemede görülen somut hatalara yöneliktir.

| Bileşen | Uygulanan değişiklik |
| --- | --- |
| Lutris Game Tune / Standalone Shortcut | Her başlatmada rastgele benzersiz token üreten, root olmayan Python supervisor. PRE başarılı olmadan RUN yok; oyun komutu asla pkexec ile çalıştırılmaz. SIGINT/SIGTERM/SIGHUP aktarılır, aynı süreç grubunda kalan alt süreçler beklenir, POST kendi token'ıyla yapılır. Eşzamanlı örnekler ve gecikmiş/tekrarlanan POST için testler. |
| Lutris YAML | Eski tam eşleşen DRSTool hook'larının yeni supervisor'a geçişinde kaldırılması; özel Game Tune hook'larında sessizce üzerine yazma yerine hata. Geçersiz system/env yapısı üzerine yazılmaz. Nice aralığı wrapper ile uyumlu -20..-1. |
| Game Tune Restore | Generic restore_param için hedef kaybolduğunda, yazma başarısızlığında veya readback uyuşmazlığında snapshot korunur. Kalan generic snapshot'lar varsa efektif yapılandırma/marker silinmez; retry mümkündür. |
| Crash Lab | Sınırlı modelde en yüksek öncelikli 8 adaydan 4/5 elemanlı AND kombinasyonları, normalleştirilmiş prior, sonlu pozitif test süresi ve boolean sonuç doğrulaması. Eski adaptif oturumlar yeni algoritmaya yanlış cevap eşleştirmesini önlemek için sıfırlanır; eski deterministik oturumlar korunur. Olasılığın model koşullu olduğu GUI'de gösterilir. |
| Steam Shortcuts | Var olan AppID korunur, CRC32 çakışması ve birden fazla eşleşme reddedilir; seyrek tag indekslerinde yeni tag için kullanılmayan indeks seçilir. NUL içeren alanlar reddedilir. |
| Winetricks | Steam prefix tespiti tam steamapps/compatdata/<appid>/pfx yapısı ve 32-bit AppID kontrolü ile yapılır. Prefix eylemleri geçerli Wine prefix'i gerektirir. |
| PE/DLL sürüm okuma | MZ ile başlayıp PE header içermeyen dosyalar reddedilir; geçerli .rsrc varsa başka bölümdeki imza kullanılmaz. Tam dosya fallback okuması sınırlandırılır. Tam kaynak ağacı ayrıştırıcısı değildir. |
| Performance History | En güncel hedef grubu için karşılaştırılabilir, en yakın önceki baseline tercih edilir. Eksik fingerprint kesin karşılaştırmayı engeller, geçersiz zaman damgası sıralamayı bozmaz. |
| Session Recorder | Boot ID ile reboot/PID reuse ayrımı, write öncesi UUID doğrulaması, boş limit desteği, çalışırken ara metadata kaydı. |
| Launch Preflight | STEAM_COMPAT_DATA_PATH/pfx tanınır, LD_PRELOAD glibc'nin boşluk/iki nokta ayraçlarına göre incelenir; dinamik linker token'ları yanlış kesin hata olarak raporlanmaz. |
| Launch Import | Bozuk shell quoting sessizce space-split edilmez. Gamescope değerinin yerinde sonraki bilinen flag gelirse bu flag tüketilmez. |
| Game Links | Profil bağlantısı kaldırılınca performans geçmişi korunur. Recovery Center'da açık onaylı yedek kurtarma; lock altında bozukluk yeniden doğrulanır, orijinal karantinaya kopyalanır. |
| Support Bundle | Tırnak içindeki çok kelimeli secret'lar maskelenir. Log symlink'leri ve özel dosyalar okunmaz; 32 dosya/2 MiB dosya başına sınır. ZIP atomik oluşturulur ve 0600 izinli olur. Genel amaçlı kusursuz anonimleştirme değildir. |
| Kurulum | Supervisor standalone installer, uninstall, Gentoo ebuild ve pip kaynak dağıtımına dahil edildi. GUI test beklentileri yeni hook düzenine güncellendi. |

## Testler

- `python -m pytest -q`: 290 passed, 114 skipped, 7 subtests passed; önceki 275 başarılı teste 15 yeni test eklendi.
- `bash lutris-game-tune-main/tests/run-tests.sh`: 92 passed, 0 failed; 7 yeni shell doğrulaması.
- `bash lutris-game-tune-main/tests/wrapper-tests.sh`: 10 passed, 0 failed. İzole fake script, gerçek sistem parametresi değiştirilmedi.
- Python compileall, bash syntax, C wrapper -Wall -Wextra -Werror, git diff --check: başarılı.

## Kurulum ve kalan sınırlar

- Lutris Game Tune bileşenini yeniden kurun (script + wrapper + yeni lgtune-session.py). Oyunlar kapalıyken yeni Lutris yapılandırmasını uygulayın ve standalone shortcut'ları yeniden üretin. Yeni supervisor, ayrı prelaunch/postexit hook'larının yerine prefix_command olarak çalışır. Eski özel hook'lar manuel kontrol gerektirebilir.
- Supervisor ayrı session/process group oluşturur. Grup dışında daemonize olan süreçlerin yaşam süresi kesin takip edilemez; grupta kalan uzun ömürlü süreçler POST'u geciktirebilir. SIGKILL/power loss sonrası token kaydı bilinçli olarak unanchored bırakılır; otomatik recovery başka aktif oyunun ayarını yanlışlıkla değiştirmesin diye bunları silmez. Tüm oyunların kapalı olduğu doğrulanınca STATUS/RESTORE kullanılmalıdır. Gerçek Lutris/Wine/Proton ve polkit davranışı bu ortamda doğrulanmadı.
- Restore değişikliği generic snapshot yolunu kapsar; PCI/GPU/Wi-Fi gibi özel restore yollarının tüm donanım durumları doğrulanmadı. Cihaz kalıcı olarak kalkarsa kalan snapshot manuel değerlendirme gerektirebilir.
- PySide6 yok: 114 GUI testi çalıştırılamadı. Vulkan geliştirme başlıkları ve kütüphanesi yok: FLM derleme doğrulaması halen yapılamadı; bu turda FLM C++ kaynak kodu değiştirilmedi. Gerçek GPU/sysfs testleri yapılmadı.
- Bayes modeli sınırlı aday uzayıdır; tüm yüksek dereceli kombinasyonları kapsamaz. Daha geniş kombinasyonlar için deterministik delta debugging kullanılmalıdır.
- Önceki raporun diğer araçlar listesi inceleme adaylarıydı; her birinin eksiksiz satır satır denetiminin tamamlandığı veya tüm olası hataların giderildiği iddia edilmemektedir.
