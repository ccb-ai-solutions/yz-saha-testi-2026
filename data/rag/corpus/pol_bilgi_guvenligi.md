---
doc_id: pol_bilgi_guvenligi
title: Bilgi Güvenliği Politikası
type: policy
source: synthetic
company: Pusulaköprü Teknoloji A.Ş. (kurgusal)
version: 4.2
effective_date: 2026-01-01
owner: Bilgi Güvenliği Müdürlüğü
notes: Kurgusal şirket — yalnızca değerlendirme amaçlıdır.
---

# Bilgi Güvenliği Politikası

## 1. Amaç ve Kapsam

Bu politika, Pusulaköprü Teknoloji A.Ş. ("Şirket") bilgi varlıklarının gizliliğini, bütünlüğünü ve erişilebilirliğini korumak için uyulması zorunlu kuralları belirler. Politika; tüm çalışanları, stajyerleri, Şirket sistemlerine erişen danışmanları ve tedarikçi personelini kapsar. Şirkete ait veya Şirket adına yönetilen tüm cihazlar, uygulamalar, bulut hizmetleri ve basılı belgeler bu politikaya tabidir. Politikanın sahibi Bilgi Güvenliği Müdürlüğüdür ve politika her yıl en az bir kez gözden geçirilir.

## 2. Tanımlar

- **Bilgi varlığı:** Şirket için değeri olan her türlü veri, belge, yazılım, donanım ve hizmettir.
- **Güvenlik olayı:** Bilgi varlıklarının gizliliğini, bütünlüğünü veya erişilebilirliğini tehdit eden ya da tehdit ettiğinden şüphelenilen her türlü durumdur.
- **SOC:** Güvenlik olaylarını 7/24 izleyen ve müdahaleyi koordine eden Güvenlik Operasyon Merkezidir; soc@pusulakopru.example adresinden ve 4444 numaralı dahili hattan ulaşılır.
- **Ayrıcalıklı hesap:** Sistem yöneticisi, veritabanı yöneticisi veya üretim ortamında yapılandırma değiştirme yetkisi taşıyan hesaptır.
- **İstisna:** Bu politikadaki bir kuraldan, risk değerlendirmesine dayanılarak ve süreli olarak sapılmasına verilen izindir.

## 3. Bilgi Sınıflandırma

### 3.1 Sınıflar

Şirket bilgileri dört sınıfa ayrılır. Belgeyi oluşturan kişi sınıfı belirler ve belgenin üst bilgisine veya dosya özelliklerine etiket olarak ekler. Etiketlenmemiş belgeler "Dahili" sınıfında kabul edilir.

| Sınıf | Örnekler | Temel kural |
|---|---|---|
| Kamuya Açık | Basın bültenleri, web sitesi içerikleri | Kurumsal İletişim onayıyla paylaşılır |
| Dahili | İç duyurular, süreç dokümanları | Yalnızca çalışanlar ve yetkili tedarikçilerle paylaşılır |
| Gizli | Müşteri sözleşmeleri, kaynak kodu, kişisel veriler | Bilmesi gereken prensibiyle erişilir ve dışarıya şifreli gönderilir |
| Çok Gizli | Birleşme ve satın alma çalışmaları, kriptografik anahtarlar, toplu özlük verileri | İsimli erişim listesiyle korunur ve yalnızca Şirket sistemlerinde saklanır |

### 3.2 Paylaşım ve saklama

Gizli bilgiler Şirket dışına yalnızca şifrelenmiş olarak ve onaylı dosya paylaşım platformu üzerinden gönderilir; şifre çözme parolası alıcıya ayrı bir kanaldan iletilir. Çok Gizli bilgiler e-posta eki olarak gönderilemez. Kurumsal e-postaların Şirket dışındaki adreslere otomatik olarak yönlendirilmesi yasaktır.

## 4. Erişim Yönetimi ve Kimlik Doğrulama

### 4.1 Parolalar ve çok faktörlü doğrulama

Standart kullanıcı parolaları en az 14 karakterden oluşur ve periyodik olarak değiştirilmez; ancak ele geçirildiğinden şüphelenildiğinde derhâl değiştirilir. Ayrıcalıklı hesap parolaları en az 16 karakter uzunluğunda olur ve 90 günde bir değiştirilir. Son 10 parola yeniden kullanılamaz. Şirket dışından erişilen tüm sistemlerde çok faktörlü doğrulama (MFA) zorunludur. Art arda 5 hatalı giriş denemesinde hesap 15 dakika süreyle kilitlenir.

### 4.2 Yetki tanımlama ve gözden geçirme

Erişim yetkileri, birim yöneticisinin BG-02 Erişim Talep Formu ile yaptığı talep üzerine en az yetki ilkesine göre tanımlanır. Kritik sistemlerdeki yetkiler 3 ayda bir, diğer sistemlerdeki yetkiler 6 ayda bir sistem sahipleri tarafından gözden geçirilir. Görev değişikliğinde artık gerekmeyen yetkiler 5 iş günü içinde kaldırılır. İşten ayrılan çalışanın tüm erişimleri son iş günü saat 18.00'e kadar kapatılır.

### 4.3 Tedarikçi erişimleri

Tedarikçi personeline verilen erişimler süreli olarak tanımlanır ve en fazla 6 ay geçerlidir. Sürenin uzatılması için yeni bir BG-02 formu doldurulur. Şirketin Gizli veya Çok Gizli bilgilerine erişecek tedarikçiler, erişim verilmeden önce Bilgi Güvenliği Müdürlüğünün tedarikçi güvenlik değerlendirmesinden geçer.

### 4.4 Ekran kilidi ve temiz masa

Cihazlar 5 dakika işlem yapılmadığında otomatik olarak kilitlenir. Çalışan, masasından ayrılırken ekranını elle kilitler. Ofiste ve evde temiz masa kuralı uygulanır; Gizli ve Çok Gizli belgeler mesai sonunda kilitli dolaplarda saklanır.

## 5. Cihaz ve Yazılım Güvenliği

### 5.1 Şirket cihazları

Tüm dizüstü bilgisayarlarda tam disk şifreleme ile uç nokta tespit ve müdahale (EDR) yazılımı etkin olmak zorundadır; bu yazılımların devre dışı bırakılması yasaktır. USB bellek gibi taşınabilir depolama aygıtlarının kullanımı varsayılan olarak engellidir. İş gereği zorunlu durumlarda BG-06 İstisna Talep Formu ile başvurulur. İstisnayı Bilgi Güvenliği Müdürü onaylar ve istisna en fazla 90 gün için verilir.

### 5.2 Kişisel cihazlar

Kişisel bilgisayarlarla Şirket sistemlerine erişim yasaktır. Kişisel akıllı telefonlardan yalnızca Mobil Cihaz Yönetimi (MDM) profiline kayıt yapıldıktan sonra kurumsal e-posta ve takvime erişilebilir. MDM profili, cihaz kaybı veya işten ayrılma hâlinde yalnızca Şirket verilerinin uzaktan silinmesine imkân verir; çalışanın kişisel verilerine erişmez.

### 5.3 Yazılım kullanımı

Şirket cihazlarına yalnızca Şirket Yazılım Kataloğunda yer alan yazılımlar kurulabilir. Katalog dışı yazılımlar için BG-04 Yazılım Talep Formu ile başvurulur ve talep Bilgi Güvenliği Müdürlüğü tarafından 5 iş günü içinde değerlendirilir. Lisanssız yazılım kullanımı kesinlikle yasaktır.

### 5.4 Üretken yapay zekâ araçları

Çalışanlar iş amacıyla yalnızca Bilgi Güvenliği Müdürlüğünce onaylanmış kurumsal yapay zekâ araçlarını kullanabilir. Gizli veya Çok Gizli sınıfındaki bilgiler ile kişisel veriler, herkese açık ve onaylanmamış yapay zekâ hizmetlerine girilemez. Yapay zekâ çıktıları, müşteriye sunulmadan önce bir çalışan tarafından doğruluk ve fikri mülkiyet açısından kontrol edilir.

## 6. Güvenlik Olaylarının Bildirimi ve Yönetimi

### 6.1 Bildirim yükümlülüğü

Bir güvenlik olayını fark eden veya olaydan şüphelenen her çalışan, durumu en geç 1 saat içinde soc@pusulakopru.example adresine ya da 4444 numaralı dahili hatta bildirir. Şüpheli e-postalar, e-posta istemcisindeki "Oltalama Bildir" düğmesiyle bildirilir; bu e-postalar başka kişilere iletilmez ve içindeki bağlantılara tıklanmaz. Şirket cihazının kaybolması veya çalınması da bir güvenlik olayıdır ve aynı süre içinde SOC'ye bildirilir. SOC, kaybolan veya çalınan cihazı uzaktan kilitler ve gerekirse cihazdaki verileri siler.

### 6.2 Olay seviyeleri

SOC, bildirilen olayı aşağıdaki seviyelere göre sınıflandırır ve ilk müdahaleyi belirtilen süre içinde başlatır. Seviye, olayın ilerleyişine göre SOC tarafından yükseltilebilir.

| Seviye | Örnek | İlk müdahale süresi | Bilgilendirilecekler |
|---|---|---|---|
| S1 – Kritik | Fidye yazılımı, kişisel veri sızıntısı | 15 dakika | Bilgi Güvenliği Müdürü, KVK Sorumlusu ve Genel Müdür |
| S2 – Yüksek | Ele geçirilmiş kullanıcı hesabı | 1 saat | Bilgi Güvenliği Müdürü |
| S3 – Orta | Karantinaya alınmış zararlı yazılım | 4 saat | Sistem sahibi |
| S4 – Düşük | Politika ihlali şüphesi | 1 iş günü | Birim yöneticisi |

### 6.3 Kişisel veri içeren olaylar

Kişisel verilerin yetkisiz kişilerce elde edildiğinden şüphelenilen olaylar S1 seviyesinde ele alınır. Bu olaylarda Kişisel Verilerin Korunması ve İşlenmesi Prosedüründeki ihlal bildirim süreci aynı anda başlatılır ve SOC olayı kvkk@pusulakopru.example adresine de bildirir. Kurula ve ilgili kişilere bildirim yapılıp yapılmayacağına KVK Komitesi karar verir.

## 7. Yedekleme ve İş Sürekliliği

Kritik sistemlerin yedekleri her gün artımlı, her hafta tam olarak alınır ve en az bir kopya farklı bir coğrafi bölgede saklanır. Yedekler 35 gün süreyle tutulur. Yedekten geri dönüş testleri 3 ayda bir yapılır ve sonuçları Bilgi Güvenliği Müdürlüğüne raporlanır.

## 8. Fiziksel Güvenlik

Ofislere giriş kişiye özel kartla yapılır ve kartlar başkasına devredilemez. Ziyaretçiler resepsiyonda kayıt altına alınır, ziyaretçi kartı taşır ve ziyaret süresince bir çalışan tarafından eşlik edilir. İşi biten Gizli ve Çok Gizli basılı belgeler, çapraz kesim yapan belge imha makinelerinde imha edilir.

## 9. Eğitim ve Farkındalık

Tüm çalışanlar her yıl 31 Mart'a kadar bilgi güvenliği farkındalık eğitimini tamamlar; yeni çalışanlar bu eğitimi işe başlangıçtan itibaren 15 gün içinde alır. Şirket her çeyrekte en az bir oltalama simülasyonu yapar. Son 12 ay içinde 2 simülasyonda bağlantıya tıklayan veya bilgi giren çalışan, 30 gün içinde tamamlanması gereken ek eğitime yönlendirilir.

## 10. İhlaller ve Yürürlük

Bu politikaya aykırı davranışlar, ihlalin ağırlığına göre Disiplin Yönetmeliği kapsamında değerlendirilir. Politikaya ilişkin sorular bilgiguvenligi@pusulakopru.example adresine iletilir. Bu politika 01.01.2026 tarihinde yürürlüğe girmiştir ve 4.2 sürümüdür.
