# DRSTool — Kod iyileştirme teslim notları (2026-11-03)

Bu paket `/mnt/data/DRSTool-main(2).zip` içinden çıkarılan kod üzerinde
oluşturulmuştur. Orijinal kaynak dosyalarına yerinde müdahale edilmedi.

## Doğrudan kodlanan ve testlenen geliştirmeler

1. **DLL geri alma**: değiştirilecek dosyanın SHA-256 değeri değiştirme öncesinde
   işlem günlüğüne yazılır; haricen değiştirilen DLL geri alma sırasında ezilmez.
2. **Wine prefix güncellemesi**: `unlink + copy` yerine atomik değiştirme,
   işlem günlüğü ve ters sırada geri alma uygulanır.
3. **Arşiv açma**: ZIP / tar / tar.zst için yol taşması, symlink, hardlink,
   özel dosya, boyut ve aşırı sıkıştırma denetimleri bir araya getirildi.
4. **DLL doğrulama**: `MZ` yanında PE imzası, dosya konumu ve makine mimarisi
   doğrulanır; NGX ve prefix önbelleğinde içerik özeti kaydı kullanılır.
5. **Araç kurulumu**: birden fazla klasörün eski sürümü işlem tamamlanana dek
   saklanır; kurulum hata verdiğinde topluca geri alınır.
6. **Lutris yapılandırması**: sadece DRSTool'un kendi yazdığı değerler, kullanıcı
   bunları sonradan değiştirmemişse kaldırılabilir. `ruamel.yaml` kuruluysa
   yorumlar ve alıntı biçimleri korunur. Kurulu değilse PyYAML geri dönüşü
   yorumları koruyamayabilir.
7. **Oyun DLL grupları**: aynı oyun için birden fazla DLL atomik adımlarla
   değiştirilir ve arada hata olursa öncekiler geri alınır.
8. **NGX / prefix önbelleği**: dosyaların hash bilgileriyle kayıt ve doğrulama.
9. **Eşzamanlı profil / game-links güncellemeleri**: dosya kilidi ve eski sürüm
   hash'iyle çakışan yazmalar reddedilir.
10. **Destek paketi gizliliği**: belirteç/anahtar maskeleme, dosya kategorisi
    seçimi ve oluşturma öncesi gizlilik bilgilendirmesi eklendi.
12. **CI artifact seçimi**: uygun olmayan mimari/platform/test arşivleri
    elenir; belirsiz sonuçta rastgele arşiv kurulmaz.
13. **Checksum eşleştirmesi**: SHA-512 tam arşiv adına göre seçilir.
14. **Kurulum öncesi doğrulama**: Steam Proton araçlarında gerekli
    `compatibilitytool.vdf` dosyası taşıma öncesinde aranır.
15. **Prefix yedek adları**: mikrosaniye ve rastgele kimlik çakışmayı azaltır.
19. **FLM tek kaynak**: `src/flip_meter.cpp` tek kopya; CMake hem depo hem
    kurulu paket dizininden build etmeye uygun güncellendi.

## Kısmen yapılanlar / eski kodda zaten bulunanlar

11. **Tuning geri alma**: önceki PRE/POST/RESTORE sistemi korunmuştur.
    Çökme/uyku/çoklu oyun için eksiksiz sistem-düzeyi kurtarma DAHİL DEĞİL.
16. **Modül mimarisi**: GUI'den bağımsız güvenlik/işlem çekirdeği ayrıldı.
    Bütün büyük GUI sınıfları yeniden parçalara bölünmüş DEĞİL.
17. **Lazy loading**: mevcut uygulamada Extra Tools lazy loading vardı;
    bütün sekmelere yayılmış DEĞİL, performans benchmarkı yapılmadı.
18. **FLM sanitizer**: CI içinde ASAN/UBSAN/TSAN derleme işleri eklendi.
    Bu ortamda Vulkan başlıkları olmadığı için derleme ve gerçek GPU/runtime
    stres testleri çalıştırılamadı; veri yarışı bulunmadığı iddia edilmiyor.
20. **A/B frametime**: mevcut karşılaştırmaya ölçüm süresi/log kaynağı
    tutarsızlığı uyarısı eklendi. Otomatik eş-sahne tekrarlı benchmark yok.
21. **Uygulama önizlemesi**: mevcut Lutris önizlemesi korundu, Tool Manager'a
    kurulum/hedef/doğrulama onayı eklendi. Tüm değiştirme yollarının tek
    önizleme sayfası altında birleştirilmesi tamamlanmadı.
22. **Donanım-duyarlı tavsiyeler**: mevcut lint, sysinfo ve oyun tavsiyeleri
    korundu. GPU/sürücü/uzantı üzerinden bütünüyle yeni otomatik uzman
    öneri motoru dahil değil.
23. **Profil risk seviyeleri**: mevcut profil planı ve lint denetimleri
    korundu. İleri/Güvenli/Deneysel şeklinde yeni, doğrulanmış bir sınıflandırma
    motoru dahil değil.
24. **Çeviri sistemi**: bütün GUI metinleri Qt Linguist'e taşınmadı; tam
    Türkçe/İngilizce çeviri kapsamı tamamlanmadı.
25. **Teşhis**: destek paketinde kapsam ve gizlilik bilgilendirmesi geliştirildi.
    GPU + Proton + işlem günlüğünü ilişkilendiren tekil teşhis ekranı yok.

## Doğrulama

- `python -m compileall -q .` — başarılı.
- `python -m unittest discover -s tests -q` — 108 test, 66 başarılı,
  42 atlandı (**PySide6 eksik**, GUI testleri doğrulanmış sayılmaz).
- `bash lutris-game-tune-main/tests/run-tests.sh` — 78 başarılı, 0 başarısız
  (bulunulan ortamda en son çalıştırılan tam tur).
- FLM / Vulkan gerçek donanım çalışması: **yapılmadı**.
- Ağ üzerinden güncel GitHub sürümü indirme/kurma testi: **yapılmadı**.

## Dağıtım ve test önerisi

Önce geçici bir Wine prefix'i ve kopya oyun DLL'leriyle deneme yapılmalı.
Önemli oyun prefix'lerinde önce uygulamanın kendi yedeği ve ayrı bir
harici yedek saklayın. Gentoo'da PySide6, ruamel.yaml ve Vulkan başlıkları
kurulu bir makinede GUI/offscreen ve FLM sanitizer derlemeleri yeniden
çalıştırılmalı. Bu dosyadaki tamamlanmamış maddeler tamamen bitti diye
bildirilmemelidir.
