# DRSTool iyileştirmeleri (16 Kasım 2026)

## Yeni: `drstool --run PROFIL -- KOMUT` ("canlı" başlatma satırı)
- Komutu kayıtlı profilin ortam değişkenleriyle ve (açıksa) gamescope / MangoHud sarmalayıcısıyla çalıştırır.
- Profil her başlatmada diskten okunur. Steam launch options'a bir kez
  `drstool --run AD -- %command%` (Lutris: komut öneki `drstool --run AD --`) yazmak yeterli;
  DRSTool'da kaydedilen sonraki değişiklikler yeniden senkronlamadan — Steam'i kapatmadan — geçerli olur.
- Katmanlı (base) profiller çözülür, ad eşleşmesi diğer CLI komutlarıyla aynıdır.
- QApplication oluşturulmaz; oyuna `QT_QPA_PLATFORM` gibi bir şey sızmaz. Başlatma süresi ~0,3 sn.
- Profil yoksa ya da kullanım hatalıysa komut başlatılmaz, çıkış kodu 2.
- Arayüz: Profiles › Launch line › **Copy live line** bu satırı panoya kopyalar (kayıtlı profil gerekir).
- Kod: `drstool_gui/cli.py` (`cli_exec`, `run_argv`), `drstool_gui/app.py`, `drstool_gui/launchers.py`.

## Düzeltme: oyun kartındaki .exe listesi rastgele sıradaydı
- `game_info.game_files()` dizin girdilerini dosya sisteminin verdiği sırayla (ext4'te hash sırası)
  topluyordu; liste makineden makineye değişiyordu ve `test_perf_regressions` bu yüzden kırılıyordu.
  Artık klasörler ve dosyalar ada göre sıralı gezilir.

## Düzeltme: test paketi gerçek ayar dosyanıza yazıyordu
- `python -m unittest discover -s tests` çalıştırıldığında `test_features_nov`, sahte HOME kurulmadan
  önce `drstool_gui`'yi içe aktarıyordu. QSettings dosya yolunu ilk kullanımda sabitlediği için testler
  gerçek `~/.config/drstool/drstool.conf` dosyasına yazıyordu (onboarding, oturum, pencere geometrisi,
  drift "reviewed" işaretleri). Kalan "reviewed" işareti ikinci çalıştırmada
  `test_drift_add_to_user_catalog` testini düşürüyordu.
- `tests/_home.py`: bütün test çalıştırması için tek, geçici bir HOME; `drstool_gui`'den önce içe aktarılır.
- Not: testleri daha önce kendi kullanıcınızla çalıştırdıysanız `drstool.conf` içinde
  `[drift] reviewed=DXVK-Sarek|dxvk.conf key|fork|ddraw.guiKey` satırı kalmış olabilir; silebilirsiniz.

## CI
- `scripts/verify-core.sh` artık Qt gerektirmeyen `test_perf_regressions.py` testlerini de çalıştırıyor.

## Doğrulama
- `ruff check .` temiz.
- `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests`: 211 test, art arda iki çalıştırmada da
  başarılı (PySide6 6.12, Python 3.13); gerçek `~/.config/drstool` dosyasına dokunulmadı.
- `bash scripts/verify-core.sh` başarılı.
- `--run` gerçek bir Steam / Lutris oturumunda denenmedi (bu ortamda yok); kabuktan ve sahte bir
  `gamescope` ile doğrulandı.
