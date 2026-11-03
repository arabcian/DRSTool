# DRSTool — v3 teslim ve kurulum raporu (3 Kasım 2026)

Bu sürüm, **DRSTool-Improved-2026-11-03-v2.zip** üzerine devam eder. Önceki dosya güvenliği, Lutris, Proton, tuning ve DLL altyapısı korunmuştur. Sürüm: **2026.11.03.3**.

## Bu turda gerçekten yapılanlar

1. **DLL Manager çalışma zamanı hatası:** `drstool_gui/dll_manager.py` içinde prefix yedeği yolunun `Path + str` yüzünden `TypeError` üretmesine neden olan ifade paranteze alındı. GUI/doğrudan GPU doğrulaması yapılamadı, fakat kaynak düzeltildi ve regresyon testi var.
2. **Güvenilir A/B analizi:** `drstool_core/benchmark.py` tekrarlı koşularda FPS değişim katsayısını (% CV), deterministik run-level bootstrap belirsizlik aralığını ve ihtiyatlı bir karar metnini hesaplıyor. Aynı sahne onaylanmadıysa veya koşular arası FPS varyasyonu yüksekse sonuç `inconclusive` oluyor. Tekrar oynatılabilen otomatik oyun sahnesi üretilmedi.
3. **İşlem günlüğü analizi:** `drstool_core/diagnostics.py` artık gerçek `transaction.json`, `manifest.json` ve `prefix-*.json` adlarını tanıyor. Dosya içeriğindeki özel dizinleri göstermeyen, zaman damgalı ve sınırlı uzunlukta bir işlem geçmişi oluşturuyor. İşlem kurtarması gerektiğinde tanılama sekmesinde uyarı var. `python scripts/diagnose.py --check-transactions` sorun saptarsa 2 çıkış kodunu kullanıyor.
4. **Büyük GUI sınıfını bölme:** Editör geçmişi, geri al/ileri al, dirty state ve profil çipi işlevleri `main_history.py` içine; oturum kaydetme/geri yükleme işlevleri `main_session.py` içine taşındı. Olay işleyicileri değiştirilmedi. Tüm GUI'nin yeniden mimarilendirilmesi tamamlanmadı.
5. **Genişletilmiş Türkçe arayüz:** Qt bağımsız Türkçe katalogda 400'ün üzerinde anahtar var. Ana kontrol metinleri, tooltips ve araç ekranları için ek çeviriler ve sonradan oluşturulan GUI bileşenlerinde sabit metinleri çevirebilen filtresi mevcut. `scripts/audit_translations.py` kaynaktan görünen statik metinleri sayıyor. Bu rapor derleme zamanı kod taramasıdır; *tüm görünür yazıların Türkçeye çevrildiği veya çalışma zamanı doğrulaması yapıldığı anlamına gelmez*. Dinamik metinler, oyun/ayar içerikleri ve bazı teknik UI satırları kapsam dışındadır.
6. **Gentoo/OpenRC özel kurulum:** `scripts/gentoo-local-install.sh` yalnızca **bu kaynak paketini** kullanıcı hesabına kurar; `--check`, `--install`, `--rollback` destekler. Var olan yönetilen uygulamayı yedekleyip atomik olarak değiştirir, hata halinde önceki sürümü geri yükler. `~/.config/drstool` içeriğine dokunmaz. **Root/setuid kullanmaz, recovery servislerini etkinleştirmez, Vulkan katmanı ya da kernel ayarlarını otomatik değiştirmez.**
7. **Yeni regresyon testleri:** `tests/test_v3.py` benchmark belirsizlik sınırlarını, işlem günlüğü gizliliğini ve mimari/dil değişikliklerini kontrol ediyor.

## Önceki 25 madde içinde hâlâ sınırlı kalanlar

- **11**: RECOVER ve isteğe bağlı OpenRC izleme servisi var. Gerçek oyun çöküşü ve uyku/uyanma testi bu sanal ortamda yapılamadı.
- **16–17**: History/session mixin çıkarıldı; Extra Tools alt sayfaları zaten lazy-loading kullanıyor. Bütün top-level sayfaların lazy init'e taşınması hem kapsamlı yeniden yazma hem Qt regresyon testleri gerektiriyor; **yapılmadı**.
- **18**: FLM için sanitizer CI işi var. Yerelde Vulkan SDK başlıkları ve NVIDIA donanımı bulunmadığından derleme ve runtime sanitizer testi **yapılamadı**.
- **20**: Tekrar koşu istatistikleri ve bootstrap aralıkları eklendi. Oyun motorlarına bağlanıp aynı sahneyi otomatik tekrar oynatma **yapılmadı**.
- **21–22**: Mevcut profil/araç/Lutris önizlemeleri, linter, risk sınıflandırması korundu. Bütün üçüncü parti dosya düzenleyicileri için tek ortak GUI plan ekranı ve *güncel, her GPU/sürücü kombinasyonunu kapsayan* öneri motoru **yapılmadı**.
- **24**: Statik çeviri kapsamı genişletildi ancak **tam Türkçeleştirme bitmedi**; `scripts/audit_translations.py` ile kalanlar izlenebilir.
- **25**: DLL işlemlerine yönelik zaman çizelgesi eklendi. Steam, Proton ve bütün harici araç olaylarının ortak tam zaman çizelgesi **yapılmadı**.

## Doğrulama

- `python -m compileall -q drstool_core drstool_gui scripts tests` — **başarılı**.
- `python -m unittest discover -s tests -q` — **134 test, 92 başarılı, 42 GUI testi atlandı**.
- `bash lutris-game-tune-main/tests/run-tests.sh` — **81 başarılı**.
- `gcc -Wall -Wextra -Werror -fsyntax-only lutris-game-tune-main/lutris-game-tune-wrapper.c` — **başarılı**.
- Shell dosyalarının `bash -n` sözdizimi kontrolü — **başarılı**.
- FLM sanitizer derlemesi — **Vulkan_LIBRARY / Vulkan_INCLUDE_DIR eksikliğinden durdu**.
- Kullanıcı sistemine kurulum — **bu ortamdan yapılmadı**. Gerçek GPU ve PySide6 GUI testi — **yapılmadı**.

## Gentoo sisteminde önerilen güvenli kurulum

### 1. Kaynak arşivini aç

```bash
unzip DRSTool-Improved-2026-11-03-v3.zip
cd DRSTool-Improved
```

### 2. Qt ve Python bağımlılıklarını Gentoo paketleriyle yükle

```bash
sudo emerge -av 'dev-python/pyside[gui,widgets]' dev-python/pyyaml dev-python/ruamel-yaml
```

Paket isimleri / USE bayrakları kurulu Gentoo deposuna göre değişebilir. **Kurulmadan önce Portage işlem planını incele.** Etkin Python sürümüyle PySide6'nın eşleştiğini doğrula:

```bash
python3 -c 'from PySide6.QtWidgets import QApplication; import yaml; import ruamel.yaml; print("Qt dependency OK")'
```

### 3. Önce sadece önkontrol

```bash
bash scripts/gentoo-local-install.sh --check
```

### 4. Kontrol başarılıysa yalnızca kullanıcı alanına kur

```bash
bash scripts/gentoo-local-install.sh --install
~/.local/bin/drstool
```

Eski kurulu sürümün sahipliği belirsizse betik üzerine yazmayı reddeder. Kurulu bir önceki sürüme dönmek için:

```bash
bash scripts/gentoo-local-install.sh --rollback
```

### 5. İsteğe bağlı tam doğrulama

```bash
QT_QPA_PLATFORM=offscreen bash scripts/verify-full.sh
python scripts/diagnose.py --check-transactions
```

**Önemli:** `verify-full.sh` Vulkan SDK ve sanitizer derlemesi gerektirir; bu kontrol başarısızsa yalnızca GUI'nin kurulu olması bütün FLM donanım testlerinin geçtiği anlamına gelmez. FLM ve sistem tuning bileşenleri otomatik kurulmaz. Bunları kullanmak istersen mevcut Gentoo ebuild/sistem kurulum yöntemleri ayrıca doğrulanmalı ve açıkça seçilmelidir.
