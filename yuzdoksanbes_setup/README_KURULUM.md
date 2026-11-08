# YUZDOKSANBES POS — Kurulum Rehberi

## 1. Kurulum (kafe bilgisayarı)
1. `YUZDOKSANBES_POS_Kurulum_<sürüm>.exe` dosyasına çift tıklayın ve Windows'un
   "Bu uygulamanın değişiklik yapmasına izin verilsin mi?" sorusuna **Evet** deyin.
2. **Şifre sayfası:** iki ayrı şifre girin:
   - **Kasa şifresi:** ödeme, iptal, yönetim paneli. Yalnızca kasaya verin.
   - **Garson şifresi:** yalnızca sipariş alma.
   - **Kurallar:** 4–16 karakter; İngilizce harf ve/veya rakam (örn. `kasa2026`).
     Türkçe harf (ı, ş, ğ…) ve boşluk kullanmayın. Büyük/küçük harf fark eder.
     İki şifre birbirinden farklı olmalı.
3. **"Windows açılışında otomatik başlat"** seçili kalsın. Bilgisayar açılınca POS
   kendiliğinden çalışır.
4. **Bitir:** POS başlar ve tarayıcıda kasa ekranı açılır.

Python veya başka bir program kurmanız gerekmez; her şey kurulumun içindedir.

## 2. Adresler
Kafe WiFi'sine bağlı her telefon, tablet ve bilgisayardan:

| Ne | Adres |
|---|---|
| Kasa (POS) | `http://siparis.local` |
| Garson | `http://siparis.local/garson` |
| Yönetim paneli | `http://siparis.local/dashboard` |
| Masa QR kodları (yazdır) | `http://siparis.local/qr-codes` |
| Müşteri (QR'ın açtığı) | `http://siparis.local/masa15` |

- Bu bilgisayarın kendisinde `http://localhost` da çalışır.
- Adresler ve bilgisayarın IP'si, açılan siyah pencerede de yazar.

**Yedek adres:** `siparis.local` açılmazsa pencerede yazan IP adresini kullanın
(örn. `http://192.168.1.20`).

## 3. Masalar ve bölgeler
Yönetim paneli → **Ayarlar** → **Masalar ve Bölgeler**:
- **Bölge:** ekleyin, adını değiştirin veya silin (örn. "Teras").
- **Masa ekleme:** bölgenin kutusuna `16` (tek masa), `16-20` (aralık) veya
  `16-20, 25` yazıp **Ekle**'ye basın.
- **Masa silme:** masanın yanındaki **✕**.

Kurallar:
- Bir masa numarası yalnızca bir bölgede olabilir.
- Açık adisyonu olan masa (kırmızı **●**) silinemez; önce hesabı kapatın.
- Geçmiş satış kayıtları ve raporlar masa silinse de korunur.
- Değişiklikler kasa ve garson ekranlarında **sayfa yenilenince** görünür.
- Yeni masaların QR kodlarını yazdırmayı unutmayın.

İlk kurulumda veritabanı **boştur**: satış, sipariş, gider, görev ve reçete
kaydı yoktur. Yalnızca başlangıç menüsü, stok listesi ve masalar hazır gelir;
hepsi panelden düzenlenebilir.

## 4. Masa QR kodları
Yönetim paneli → **Ayarlar** → **QR kodlarını aç / yazdır**.
- QR kodları `siparis.local` adresini içerir. Bilgisayarın IP'si değişse bile
  **yeniden yazdırmanız gerekmez**.

## 5. Günlük kullanım
- **Siyah pencere:** "YUZDOKSANBES POS" penceresi açık kalmalı. Kapatırsanız POS
  durur; masaüstündeki simgeyle yeniden başlatın.
- **Simgeye tekrar tıklamak:** POS zaten çalışıyorsa ikinci kez başlamaz,
  yalnızca tarayıcıda açılır.

## 6. Veriler ve yedekler
Tüm veriler `C:\ProgramData\YUZDOKSANBES` klasöründedir (Başlat menüsü →
"Veri klasörü"):
- `pos.db`: tüm satışlar, menü, stok, reçeteler…
- `backups\`: saatlik otomatik yedekler
- `logs\pos.log`: sistem kayıtları
- `credentials.json`: şifreler. En kolay değiştirme yolu kurulum dosyasını
  yeniden çalıştırmaktır (şifre sayfası her kurulumda gelir). İsterseniz bu dosyayı
  Not Defteri ile düzenleyip POS'u yeniden başlatabilirsiniz.
- `logo.png`: raporlardaki logo. Kendi logonuzu aynı adla buraya koyabilirsiniz.

**Kaldırma ve güncelleme bu klasörü silmez.** Satış verileriniz korunur.

**Google Drive yedeği:** bilgisayarda Google Drive kuruluysa her saatlik yedek
otomatik olarak `G:\Drive'ım\pos-backups\<bilgisayar adı>\` klasörüne de
kopyalanır (klasör kendiliğinden oluşturulur; İngilizce Windows'ta `My Drive`).
Google Drive başka bir sürücü harfindeyse de bulunur. Google Drive yoksa veya
henüz açılmadıysa yedekler yalnızca bu bilgisayarda tutulur ve Drive açılınca
bir sonraki yedekten itibaren oraya da kopyalanır.

## 7. Güncelleme
Yeni sürümün kurulum dosyasını çalıştırmanız yeterlidir.
- Çalışan POS otomatik kapatılır, güncellenir ve veriler aynen kalır.
- **Her kurulumda şifreler yeniden sorulur** (güncelleme, kaldırıp yeniden kurma
  dahil). Son kurulumda yazılan şifreler geçerli olur; eskileri artık çalışmaz.
  Şifreyi değiştirmek istemiyorsanız aynı şifreleri tekrar yazın.
- Şifre değişince açık olan kasa/garson/panel ekranları kendiliğinden giriş
  ekranına döner; yeni şifreyle giriş yapın.

## 8. Sorun giderme
| Sorun | Çözüm |
|---|---|
| Telefonda `siparis.local` açılmıyor | Telefon kafe WiFi'sine mi bağlı? Eski Android'ler (11 ve öncesi) `.local` adreslerini açamayabilir; pencerede yazan IP adresini kullanın. |
| Adreste `:8000` görünüyor | 80 numaralı port başka bir programda; POS otomatik olarak 8000'i kullanıyor. Pencerede yazan adresi kullanın. |
| Hiçbir cihaz bağlanamıyor | POS penceresi açık mı? Kurulum Windows Güvenlik Duvarı'na izin ekler; başka bir antivirüs varsa "YUZDOKSANBES POS"a izin verin. |
| Şifreyi unuttum | `C:\ProgramData\YUZDOKSANBES\credentials.json` dosyasına bakın. |

## 9. Kurulum dosyasını yeniden üretmek (geliştirici)
`yuzdoksanbes_setup\build.bat` dosyasını çift tıklayın.
- **Gerekenler:** Python 3.12 ve Inno Setup 6 (`winget install JRSoftware.InnoSetup --scope user`).
- **Sürümler:** `requirements.lock` dosyasında sabitlenmiştir, her derleme aynıdır.
- **Sürüm numarası:** `version.py` içinde.
- **Çıktı:** `yuzdoksanbes_setup\output\` klasörü.
