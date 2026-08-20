# Açık işler / bilinen sorunlar

Son güncelleme: 19 Ağustos 2026. Önceki turda "bot hiçbir coini alamıyor" durumu vardı;
aşağıdaki maddelerin tamamı düzeltildi ve **canlı Helius anahtarıyla gerçek launch'lar
üzerinde doğrulandı** (3 tur, 18 coin).

## Canlı test sonucu (19 Ağustos 2026)

| ölçüm | önce | sonra |
|---|---|---|
| bundler/sniper eksik | 6/8 | **0/6** (üç turda da) |
| bundler değeri | her coinde %100 | 0,2-12,9 arası gerçek değerler |
| coin başına analiz | — | 1,7-24 sn (RPC pacer'a bağlı) |
| RPC `rate_limited` | — | 30sn gecikmede 0, 90sn'de 49 (retry yutuyor, `failed`=0) |
| fiyat/mcap kaynağı | Dexscreener (yeni coinde boş) | bonding curve, ilk slottan itibaren |

Elenme sebepleri artık "veri yok" değil, gerçek sinyaller: `sniper %31,9`, `bundler %9,8`,
`mcap $34.436 > $25.000`. Yani filtre çalışıyor — bkz. aşağıdaki "kalan karar".

**Eşik kararı verildi:** 30 saniyede coinlerin neredeyse tamamı launch tabanında duruyordu
(mcap $2281 = 27,96 SOL × $81,56; curve 0,00 SOL); 90 saniyede ayrışıyorlar ($2281 → $34.436,
curve 0 → 0,88+). `analyze_delay` **90** yapıldı, `RPC_RPS` 9 → **6.5** (90sn'de trafik
arttığı için). Para eşikleri (`min_mcap`, `min_curve_sol`) değiştirilmedi — artık gerçek
sinyalle çalışıyorlar. Hepsi panelden ayarlanabilir.

Ağ ve Helius anahtarı gerektirmeyen regresyon testi: `python tools\offline_test.py`
(bonding curve çözümlemesi, launch penceresi bölme, kural motoru, paper slipaj — hepsi geçiyor).

## 1. bundler / sniper boş dönüyor — DÜZELTİLDİ

Üç ayrı sebep vardı, üçü de kapatıldı:

- **İmza sayfalama 100'lükti.** `getSignaturesForAddress` sayfa başına 1000 imza
  verebiliyor; kod 100 istiyordu ve 5 sayfa sonra pes ediyordu = 500 imza. Yoğun bir
  pump.fun mint'i saniyeler içinde binlerce *başarısız* snipe imzası biriktirdiği için
  launch'a hiç ulaşılamıyordu → `complete=False` → "veri yok" → RED.
  Artık `SIG_PAGE_SIZE=1000`, `SIG_PAGE_LIMIT=15` (15.000 imza) ve yalnızca launch'a
  komşu olan **son sayfa** tutuluyor (`analyzer._signatures_since_launch`).
- **Bonding curve kendi alımı olarak sayılıyordu.** Create işlemi tüm arzı bonding
  curve'e yazıyor; bu "bundle alımı" sayılınca `bundler` her launch'ta %100 çıkıyordu.
  Curve'ün token hesabı artık hariç tutuluyor (`pumpfun.curve_from_launch_tx`).
- **`blockTime=None` olan imzalar pencereye sızıyordu** (`None or 0` → 0 → fark hep
  negatif → pencereye dahil). Artık `blockTime` yoksa yalnızca slot karşılaştırması
  yapılıyor (`analyzer._split_launch_window`).

Canlı testte çıkan iki ek hata da kapatıldı:

- **pump.fun dışı launchpad'lerde bundler yine %100 çıkıyordu** (curve adresi
  bulunamayınca havuz hesabı alım sayılıyordu). Artık tek işlemde toplam arzın
  yarısından fazlasını alan hesap "havuz besleniyor" sayılıp hariç tutuluyor.
- **`top10` iki kez yanlış tabandaydı.** Curve dışı float'a göre ölçülünce 30 saniyelik
  coinde tek alıcı olduğu için hep %100 çıkıyordu; toplam arza çevrildi, eşik 65 → 25.
  Ayrıca hiç alıcı yokken "top10 verisi yok" deniyordu — artık RPC hatası (`None`) ile
  gerçekten alıcı olmaması (`%0`) ayrılıyor.
- Coin adı Dexscreener indekslemeden önce "?" görünüyordu; Helius DAS `getAsset` ile
  metadata'dan alınıyor.

Ayrıca yoğun pencere artık "veri yok" değil: `MAX_EARLY_TX` (150) aşılırsa bütçenin
yettiği kadarı hesaplanır, sonuç alt sınır olarak işaretlenir ve coin
*"launch penceresi aşırı yoğun"* gerekçesiyle elenir — dürüst bir gerekçe, sahte bir
"veri yok" değil.

## 2. RPC hız limiti — pacer duruyor, yük düştü

`rpc.py` içindeki global pacer (`RPC_RPS`, varsayılan 9/sn) ve `rpc.stats` sayaçları
duruyor. Sayfalama düzeltmesi sayesinde imza çekme maliyeti 5 çağrıdan ~1-2 çağrıya
indi, `getTransaction` çağrıları da yalnızca gerçek launch penceresi için yapılıyor.
Canlı doğrulandı: `analyze_delay=30` ile `rate_limited=0, failed=0` (91 çağrı / 6 coin).
`analyze_delay=90` ile trafik artıyor (322 çağrı, `rate_limited=49`) ama retry yuttuğu için
`failed=0`. 90sn'de kalıcı olarak çalışacaksan `RPC_RPS`'i 6-7'ye çek.

## 3. Dexscreener gecikmesi — bonding curve ile aşıldı

Dexscreener yeni mint'i 30-90 saniyede indeksliyordu, bot ise 20-30 saniyelik coinlere
bakıyor → `mcap`, `fiyat`, `5m hacim` boş → RED.

Yeni `backend/pumpfun.py` bonding curve hesabını doğrudan okuyor ve **ilk slottan
itibaren** fiyat, market cap, likidite ve curve'e giren gerçek SOL'u veriyor.
Dexscreener artık sadece kendine özel alanları (5m hacim, havuz likiditesi) dolduruyor.
`analyzer.current_price` de aynı fallback'i kullanıyor, yani açık pozisyonlar
Dexscreener'a girmeden önce de takip ediliyor (tier/stop loss gerçekten tetikleniyor).

Hesap düzeni her okumada mantık kontrolünden geçiyor; tutmazsa `None` dönüp
Dexscreener'a düşülüyor, uydurma değer üretilmiyor.

## 4. Eşikler — kalibre edildi

Kritik olan eşik sayıları değil, **yüzdelerin tabanıydı**: `bundler`, `sniper` ve `dev`
dolaşımdaki (curve dışı) arza göre ölçülüyordu. Yeni bir coinde curve dışı dolaşım çok
ince olduğu için sıradan bir launch'ta bile %80-100 çıkıyor ve her şey eleniyordu.
Artık dördü de (**bundler, sniper, dev, top10**) toplam arza göre ölçülüyor —
ekosistemdeki tarayıcıların kullandığı taban.

| ayar | eski | yeni | gerekçe |
|---|---|---|---|
| `max_top10` | 30 | 25 | tabanı toplam arza çevrildi (float'a göre hep %100 çıkıyordu) |
| `min_volume_5m` | 500 | 0 | 30sn'lik coinde 5m hacim yok; yalnızca Dexscreener veri verdiyse uygulanır |
| `min_curve_sol` | — | 2.0 | yeni: talep ölçüsü artık curve'e giren gerçek SOL |
| `max_curve_sol` | — | 30.0 | yeni: geç girişi engeller |
| `analyze_delay` | 10 | 90 | 30sn'de coinler hâlâ launch tabanında; canlı ölçüm 90sn'de ayrıştıklarını gösterdi |
| `max_bundler` / `max_sniper` / `max_dev_holdings` | 5 / 10 / 5 | aynı | taban değiştiği için artık gerçekçi |

Notlardaki canlı ölçümlerle kontrol: ChevyNova (top10 43,6 / dev 2,1) artık geçiyor,
Newbie (dev %80,4 / mcap 7,5M) hâlâ eleniyor. İkisi de `tools/offline_test.py` içinde
regresyon testi olarak duruyor.

## 5. Paper mod slipajı — eklendi

Paper alım/satım artık kotanın kötü tarafında dolduruluyor: taban `paper_slippage_pct`
(varsayılan %1,5) + alım tarafında büyüklük/likidite etkisi. Pozisyon gerçekleşen
fiyattan kaydediliyor, dolayısıyla PnL artık gerçeğin iyimser tarafında değil.
**Kalan:** likidite derinliği hâlâ tek bir orana indirgeniyor, gerçek order book
simülasyonu yok.

## 6. Küçük notlar

- `tools/*.py` dosyalarındaki `C:\Users\Lenovo\market-fucker` sabit yolları düzeltildi
  (artık `__file__`'dan türetiliyor) — bu makinede hiçbiri çalışmıyordu.
- `getTokenLargestAccounts` BONK gibi çok holder'lı tokenlarda FAIL dönüyor (Helius
  reddediyor). Yeni coinlerde sorunsuz çalışıyor, sadece bilinsin.
- Canlı alım/satım yolu (Jupiter quote + swap + imza) **hiç gerçek parayla denenmedi** —
  cüzdan anahtarı yok. İlk canlı denemeyi 0.01 SOL ile yap.
- `.env` repoya girmiyor (`.gitignore`); Helius anahtarı yazıldı, doğrulandı
  (`getSlot` OK, solana-core 4.2.0). `WALLET_PRIVATE_KEY` boş → sadece PAPER modu.
- **pypi.org bu bağlantıdan engelli.** Bağımlılıklar `.venv` içine aynadan kuruldu:
  `pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt`.
  Python 3.14 kullanılıyor (README'nin işaret ettiği 3.12 kurulumu gitmiş).
- Çalıştırma: `.\.venv\Scripts\python.exe backend\main.py`

## Test araçları (`tools/`)

| dosya | ne yapar | ağ/anahtar |
|---|---|---|
| `offline_test.py` | curve çözümleme + launch penceresi + kural motoru + paper slipaj | gerekmez |
| `selftest.py` | Dexscreener + on-chain metrik + paper alım/satım uçtan uca | gerekir |
| `selftest2.py` | Kademeli satış + trailing stop + stop loss senaryoları (scripted fiyat) | gerekmez |
| `livecheck.py` | Helius anahtarı doğrulama + websocket + tek coin analizi | gerekir |
| `calib2.py` | Canlı launch yakala, analiz et, elenme sebeplerini özetle (kalibrasyon) | gerekir |
| `diag.py` | Belirli mint'ler için imza sayfalama / early buyer teşhisi | gerekir |

Çalıştırma: `python tools\calib2.py`
