# DRSTool Experimental — F01–F11 Denetim Düzeltmeleri

Kaynak: `DRSTool_derin_kod_denetimi(1).md` (önceki deneysel sürümün statik/izole denetimi).
Hedef: `DRSTool-experimental(4).zip`. Gerçek oyun dizinleri veya Wine prefix'leri değiştirilmedi.

| ID | Durum | Uygulanan düzeltme |
| --- | --- | --- |
| F01 | Uygulandı | Commit'ten sonraki manifest hatası artık rollback diye raporlanmıyor. `ReplaceResult.status/detail` ve `TransactionCommittedError` gerçek değişiklik sayısını bildiriyor; onarılabilir yedek/jurnal korunuyor. Prefix provenance hatasında da yanlış başarısızlık verilmez. |
| F02 | Uygulandı | Kaynak ve hedef PE makine türleri (x86/x64) kıyaslanıyor, farklı/bozuk dosyalar reddediliyor. Prefix kurulumunda da mevcut DLL mimarisi kontrol ediliyor. |
| F03 | Uygulandı | Kaynak/hedef dizinlerin her bileşeni `O_NOFOLLOW` ile açılıyor; kopyalama ve silme sabitlenmiş `dirfd` üzerinden yapılıyor. Üst dizin symlink'leri reddediliyor; eski biçim prefix restore da güvenli kopya kullanıyor. |
| F04 | Uygulandı | Manifestin mevcut bileşen DLL'lerinin tamamını kapsaması ve her checksum'un eşleşmesi şart. Kısmi/kirlenmiş kayıt `mixed` dönüyor. |
| F05 | Uygulandı | Otomatik güncelleme yalnızca sürüm açıkça daha eskiyse planlanıyor. Daha yeni, eşit, bilinmeyen veya karışık sürümlerin istemsiz değiştirilmesi engellendi. Manuel downgrade bu değişiklikle sunulmuyor. |
| F06 | Uygulandı | Kısmi prefix restore için `PrefixRestoreError` (yedek konumu, geri dönen sayı, sorunlar) eklendi; GUI hatayı yakalıyor, dosyaları/korunan yedeği gösteriyor. |
| F07 | Uygulandı | Tool Manager kalıcı kurulum jurnali, dosya sistemi kilidi, başlangıçta yeniden kurtarma, kurulumu doğrulayıp commit etme, eski sürümleri koruma ve eksik kurulumları karantinaya alma desteğine sahip. Eski sürümün jurnalsiz yedeği yalnızca hedef yoksa otomatik geri konuyor. |
| F08 | Uygulandı | Bottles ve Proton tarafından yönetilen prefix DLL bileşenleri normal DRSTool güncelleme planından çıkarıldı; arayüz `managed` olarak gösteriyor. |
| F09 | Uygulandı | Restore içeriği yeni `prefix-*.json` jurnalindeki `entries` ve `files/system32` gibi yolları; ayrıca eski yedek düzenini okuyabiliyor. |
| F10 | Uygulandı | Winetricks ayrı POSIX süreç oturumunda çalışıyor; iptal süreç grubuna önce SIGTERM, sonra SIGKILL yolluyor. Diyalog, QThread bitmeden kapanmayacak şekilde düzenlendi. |
| F11 | Uygulandı | DLL taraması işlenen/toplam oyun sayısını ve iptal bilgisini ayrı iletiyor; iptal edilen işlem tam tarama olarak gösterilmiyor. |

## Doğrulama

- `python -m compileall -q drstool_core drstool_gui tests`
- `python -m unittest discover -s tests -p test_audit_regressions.py -v`: 22 yeni regresyon testi.
- `bash scripts/verify-core.sh`: mevcut Qt-dışı çekirdek, C wrapper ve shell tuner testleri + yeni regresyonlar.
- `PYTHONPATH=tests:. python -m unittest -q test_audit_regressions test_core test_enhancements test_features_nov test_gui test_integration_plan test_perf_regressions test_safety test_v3`: Qt yokken çalışabilen tüm grup.

## Sınırlar / dikkat

- Çalıştırma ortamında `PySide6` olmadığından gerçek Qt pencere entegrasyonu ve Gentoo/Steam/Bottles üzerinde canlı kurulum/geri yükleme testleri yapılmadı. Qt'suz testlerde GUI'deki ilgili yöntemlerin orijinal AST fonksiyonları izole olarak çalıştırıldı; tam Qt davranışı için offscreen CI ve gerçek sistem testi hâlâ gereklidir.
- Bu sürümde güvenlik amacıyla kaynak veya hedef yolunun herhangi bir üst dizini symlink ise DLL değiştirme reddedilir. Symlink'li oyun kütüphanelerinde önce gerçek/kanonik klasörü kullanın.
- Tool Manager kurtarmasında `.drstool-incomplete-*` klasörleri **otomatik silinmez**. Kurtarılmış eski kurulum doğrulandıktan sonra bunları manuel inceleyebilirsiniz. `.drstool-tm-install-*.json` jurnalleri kurulum/kurtarma tamamlanmadan silinmemelidir.
- Değişiklikler temel olarak DLL Manager, Tool Manager ve Winetricks akışlarını hedefler; GPU/FLM çalışma zamanı veya online release doğrulaması yapıldığı iddia edilmez.
