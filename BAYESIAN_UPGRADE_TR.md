# DRSTool — Bayesian Crash Lab geliştirmesi

Temel: DRSTool Experimental Audit Stage 2. Önceki düzeltmeler korunmuştur.

## Model ve algoritmalar

- Gamma-Poisson zaman-bağımlı çökme modeli korunur. Gerçek çökme zamanları ve sağ sansürlü başarılı oyun oturumları ayrı kanıtlardır. Çok kısa başarılı çalıştırmalar daha zayıf kanıt sayılır. Alt saniye süreleri yuvarlanmaz.
- Hipotez aileleri: dış neden (`none`), tek ayar, iki veya üç bağımsız neden (`OR`), 2–8 ayar birlikte (`AND`), `(A AND B) OR C`, `(A AND B) OR (C AND D)`. Büyük profillerde tam kombinasyon da özel bir AND adayıdır. Core API'de `max_order` 5–12 arasında seçilebilir (GUI'de varsayılan 8).
- Hipotez bütçeleri sınırlıdır. Küçük uzaylar tam taranır; büyük uzaylar öncelikli kombinasyonlar, profil geneline yayılan deterministik örnekler ve sabit tohumlu örnekleme ile oluşturulur. Sonlu örnekleme geri dönüşü vardır. Bütün kombinasyonları kapsamaz. Posterior olasılıkları bu hipotez uzayına koşulludur; istatistiksel kesinlik veya gerçek sistemin güvenli olduğunun ispatı değildir.
- Log uzayında normalizasyon, sonlu girdi denetimi, tekrarlanan ayarların tekilleştirilmesi, bilinmeyen ayarların reddi, sıfır önsel aile ağırlıklarının güvenli işlenmesi. `log1p` ve `expm1` kullanılarak küçük olasılıklarda sayısal kararlılık iyileştirildi.
- Sufficient statistics ve posterior önbelleği: geçmişin her planlamada tekrar taranması yerine eklenen gözlem ile güncelleme.
- Planlayıcı, küçük profillerde bütün test alt kümelerini, büyük profillerde sınırlı aday kümesini inceler. Adaylar, önde gelen hipotezler, ters kombinasyonlar, sıralama dilimleri ve deterministik bölmeler üzerinden üretilir. Dört süre seçeneğinde beklenen ikili sonuç bilgi kazanımını, süre ve kurulum maliyeti varsayımıyla dengeler. Planlanan süre 30 saniye–4 saat aralığındadır (başlangıç doğrulama süresi triage ipucuna bağlıdır). Bilgi kazanımı çökme/başarı sonucuna dayalıdır; çökme zamanının tamamı posterior güncellemesinde kullanılır.
- Çökme bilgisi yoksa hayali başarısız gözlem eklenmez. GUI her zaman editördeki tam konfigürasyonu doğrular: triage süresi yalnızca başlangıç hız önseli olarak kullanılır. Başlangıç konfigürasyonu, önerilen başlangıç süresinin toplam üç katı kadar çalışıp çökmezse `not-reproduced` sonucu; bu güvenlik garantisi değildir.
- Son doğrulamada şüphelilerin tamamının kapalı, diğer ayarların açık olduğu aynı konfigürasyonda, son başarısızlıktan beri biriken gerçek oynama süresi kullanılır. Lider hipotezin etkin çökme hızına göre koşullu tespit olasılığı en az %80 ve son gözlem başarılı olmalıdır. Ayrıca seçilen posterior eşiği aşılmalıdır. Dış neden sonucunda boş aday konfigürasyonunda gerçek çökme aranır. Test bütçesi dolarsa `inconclusive` döner ve orijinal konfigürasyon korunur.
- Xid ilişkilendirmesi genişletilmiş hipotez ailelerini kullanır; yalnızca ikincil Xid'ler varsa kök neden bilinmiyor olarak gösterilir. Ayrı belirtiler ayrı modellerle analiz edilir; diğer çökmeler ilgili belirtinin gözlemini erken sonlandırır.

## GUI ve kayıt

- Adaptive search seçildiğinde posterior eşiği (%70–99; varsayılan %95) ve maksimum deneme sayısı (5–200; varsayılan 40) belirlenebilir.
- Gerçek oynama süresi saniye olarak girilebilir. 0 bırakılırsa geçen toplam süre yalnızca kullanıcı onayından sonra kabul edilir; sıfır veya geçersiz süre kaydedilmez.
- Hipotez sayısı, kalan bütçe, tahmini çökme olasılığı, beklenen bilgi kazanımı ve doğrulama sırasında koşullu tespit olasılığı gösterilir. Türkçe karşılıklar eklendi.
- Oturum biçimi v4, gerçek test edilen ayar kümeleri ve süreleri ile bekleyen konfigürasyonu ve planlanan süreyi kaydeder. Undo gerçek son test kümesini geri getirir. Eski adaptif v3 oturumları yeni planlayıcı ile yanlış eşleştirilmez; orijinal profil korunarak yeni arama başlatılır. Deterministik v2/v3 oturumları desteklenmeye devam eder.

## Test ve sınırlar

- 11 yeni Qt-bağımsız regresyon testi: gerçek başlangıç gözlemi, doğrulama süresi, yüksek dereceli ve karma etkileşimler, posterior normalizasyonu, girdi denetimi, saklanan gerçek gözlemler, Undo, planlayıcı tutarlılığı, incremental istatistikler ve geniş örnekleme.
- Sentetik 7 ayarlı örnekte `(A+B) OR C`, `(A+B) OR (C+D)` ve altılı AND etkileşimi sırasıyla 13, 13 ve 14 testte doğru neden gruplarıyla tamamlandı. Bunlar idealize edilmiş simülasyonlardır; gerçek oyun sonuçları garanti edilmez.
- Model sabit koşullu hazard (üstel yaşam süresi) ve monoton ayar etkileşimleri varsayar. Ortam değişiklikleri, karmaşık daha büyük DNF yapıları, zamanla değişen hazard veya devre dışı bırakmayla tetiklenen bazı etkileşimler kapsam dışında kalabilir. Bu nedenle belirsiz sonuç ve deterministik arama alternatifleri korunur.
- PySide6 bulunmadığından GUI çalışma zamanı testleri çalıştırılamadı. Gerçek oyun ve NVIDIA donanımı üzerinde doğrulama yapılmadı. Vulkan kaynaklarına bu aşamada dokunulmadı.

Son tam test sonucu: **301 başarılı, 114 atlanan, 0 başarısız; 7 alt test başarılı (22,65 s).**
