# Açık işler / bilinen sorunlar

Canlı Helius anahtarıyla gerçek pump.fun launch'ları üzerinde test edildi (19 Ağustos 2026).
Durum: **bot hiçbir coini alamıyor** — aşağıdaki 4 numaralı madde kritik.

## 1. bundler / sniper metriği boş dönüyor — KISMEN DÜZELTİLDİ, DOĞRULANMADI

- **Sorun:** pump.fun mint'lerinde yüzlerce *başarısız* snipe işlemi oluyor. `_oldest_signatures`
  imzaları geriye sayfalayıp launch'a ulaşmaya çalışıyordu, 400+ imza geriye gidip yine
  ulaşamıyordu → `complete=False` → metrik "veri yok" → coin RED.
- **Yapılan:** bot zaten launch işleminin imzasını biliyor; artık `analyzer.analyze(...)`'a
  `launch_signature` geçiliyor ve `_signatures_since_launch()` `until=<launch_sig>` ile
  sadece launch sonrası imzaları çekiyor (`analyzer.py`, `bot.py`, `rpc.py`).
- **Kalan:** 8 coinlik canlı testte hâlâ 6'sında boştu, ama o test 8 analizi aynı anda
  çalıştırdığı için RPC 429 yemiş olabilir (bkz. madde 2). **Yeniden test edilmeli:**
  `tools/calib2.py` (WANT=4, sem=2) çalıştır, çıktıdaki `RPC istatistik` satırında
  `rate_limited` 0'a yakınsa ve bundler/sniper doluysa sorun kapanmıştır.

## 2. RPC hız limiti — pacer eklendi, DOĞRULANMADI

- Ücretsiz Helius planı ~10 istek/sn. Yoğun bir launch'ın analizi 150+ `getTransaction`
  çağrısı yapıyor. Paralel analizde 429 geliyor, kod da 429'u sessizce "veri yok" → RED'e
  çeviriyordu.
- **Yapılan:** `rpc.py` içine global pacer (`RPC_RPS`, varsayılan 9/sn) + `rpc.stats`
  sayaçları (`calls`, `rate_limited`, `failed`) eklendi. `MAX_PARALLEL_ANALYSIS` varsayılanı
  4 → 2 yapıldı, ikisi de `.env`'den ayarlanabiliyor.
- **Kalan:** gerçek akışta doğrulanmadı. 429 devam ederse `RPC_RPS`'i düşür veya
  `analyzer.MAX_EARLY_TX`'i (şu an 150) azalt — ama çok düşürürsen yoğun coinler yine
  "doğrulanamadı" diye elenir.

## 3. Dexscreener yeni coinleri geç indeksliyor

- 20 saniyelik coinlerde `mcap`, `5m hacim`, `fiyat` çoğunlukla boş geliyor → bunlar da
  "veri yok" → RED. Bir coinde 13. saniyede veri geldi, 8 coinlik testte hiçbirinde gelmedi.
- **Denenecek:** `analyze_delay` 10 → 45/60 sn. Geç girmek pahalı ama veri yoksa zaten alım yok.
- Ayrıca Dexscreener da 429 veriyor; şu an 3 deneme × 3 sn bekleme var, yetmiyor olabilir.

## 4. Eşikler gerçek veriyle uyumsuz — KALİBRASYON ŞART

Canlı ölçümler (gerçek launch'lar):

| coin | mcap | 5m hacim | top10% | dev% | bundler% | sniper% |
|---|---|---|---|---|---|---|
| ChevyNova | $7.030 | $383 | 43,6 | 2,1 | - | - |
| The Jumping | $9.852 | $16.184 | 38,5 | 1,6 | - | - |
| Newbie | $7.5M | $95k | 100 | 80,4 | 100 | 0 |

- `min_volume_5m = 500`: 10-20 saniyelik coinde 5 dakikalık hacim doğal olarak düşük →
  neredeyse her coini eliyor. Düşür ya da `analyze_delay`'i artır.
- `max_top10 = 30`: yeni coinlerde bonding curve dışı dolaşım küçük olduğu için top10
  %38-100 arası çıkıyor. %30 eşiği pratikte her şeyi eliyor. Gerçekçi aralık ölçülmeli.
- `max_mcap = 25000`: makul, örneklerin çoğu bu bandın altında/üstünde dağılıyor.

## 5. Küçük notlar

- `getTokenLargestAccounts` BONK gibi çok holder'lı tokenlarda FAIL dönüyor (Helius
  reddediyor). Yeni coinlerde sorunsuz çalışıyor, sadece bilinsin.
- Paper mod **slipaj ve likidite derinliğini simüle etmiyor**; PnL gerçeğin iyimser tarafında.
- Canlı alım/satım yolu (Jupiter quote + swap + imza) **hiç gerçek parayla denenmedi** —
  cüzdan anahtarı yok. İlk canlı denemeyi 0.01 SOL ile yap.
- `.env` içinde Helius anahtarı var, `.gitignore`'da — repoya girmiyor.

## Test araçları (`tools/`)

| dosya | ne yapar |
|---|---|
| `selftest.py` | Dexscreener + on-chain metrik + paper alım/satım uçtan uca |
| `selftest2.py` | Kademeli satış + trailing stop + stop loss senaryoları (scripted fiyat) |
| `livecheck.py` | Helius anahtarı doğrulama + websocket + tek coin analizi |
| `calib2.py` | Canlı launch yakala, analiz et, elenme sebeplerini özetle (kalibrasyon) |
| `diag.py` | Belirli mint'ler için imza sayfalama / early buyer teşhisi |

Çalıştırma: `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" tools\calib2.py`
