# DRSTool UI ve Windows Libraries düzeltmeleri

## Sekme seçimi
- Üst gezinme sekmeleri artık tek seçimli `QButtonGroup` kullanıyor.
- Sekme seçimi tek işlemle yapılıyor; önceki sekmenin seçili kalması engelleniyor.
- Kaydırılan seçim göstergesi sadece aktif sekmeyi izliyor.

## Extra Tools > DLL Manager > Windows libraries
- Tek bir oyunu işaretlemek yeterli; mevcut satır farklı olsa bile DLL kurulum diyaloğu
  işaretlenen oyunun Wine prefix'i için açılıyor.
- Hiç işaret yoksa tabloda tıklanan (vurgulanan) oyun kullanılabiliyor.
- Birden çok oyun işaretliyse yalnızca işaretli oyunlardan vurgulanan satır hedef seçiliyor;
  belirsiz bir durumda rastgele bir prefix'e yazılmıyor.
- Henüz DXVK/DLSS DLL'leri olmayan fakat Wine prefix'i bulunan oyunlar da varsayılan listede görünüyor.
- Yardım metni ve ipucu, tek oyun seçiminin yeterli olduğunu açıklıyor.

## Test
- Regresyon testleri `tests/test_gui.py` dosyasına eklendi.
- Python modülleri `compileall` ile doğrulandı.
- Qt dışı testler tamamlandı. Bu hazırlama ortamında `PySide6` kurulu olmadığından
  yeni GUI testleri burada çalıştırılamadı ve Gentoo/Qt ortamında son kontrol gerekiyor.

GUI testlerini Gentoo'da çalıştırmak için proje dizininde:

```sh
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -p 'test_gui.py' -v
```
