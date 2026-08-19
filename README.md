# market-fucker

Solana memecoin sniper botu. Yeni pump.fun launch'larini Helius websocket ile yakalar,
on-chain + Dexscreener verisiyle filtreler, kurallara uyanlari Jupiter uzerinden alir,
kademeli cikis + trailing stop ile otomatik satar. Paper (simulasyon) ve canli mod ayni mantikla calisir. Her sey karanlik temali web panelden
gercek zamanli izlenir.

> **Uyari:** Bu bot gercek para ile islem yapar. Memecoin alim satimi yuksek risklidir;
> yatirdiginin tamamini kaybedebilirsin. Once PAPER modunda (asagida) test et.

## Kurulum

```bash
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# .env icine HELIUS_API_KEY ve WALLET_PRIVATE_KEY yaz
python backend/main.py
```

Panel: http://127.0.0.1:8000

## .env

| Anahtar | Aciklama |
|---|---|
| `HELIUS_API_KEY` | https://helius.dev uzerinden ucretsiz alinir. Websocket + RPC icin sart. |
| `WALLET_PRIVATE_KEY` | Phantom base58 private key (veya `[1,2,...]` byte array). **Bos birakilirsa bot PAPER modunda calisir.** |
| `PORT` | Varsayilan 8000. |
| `PAPER_TRADING` | `1` yazarsan panelden LIVE moda gecilemez, bot her zaman simulasyonda kalir. |
| `JUPITER_API` | Varsayilan `https://quote-api.jup.ag/v6`. |

Private key hicbir yerde loglanmaz, frontend'e gonderilmez, `.env` `.gitignore` icindedir.

## PAPER / LIVE modu

Panelin sol ustundeki anahtar ile secilir; varsayilan **PAPER**. LIVE'a gecerken
"This will use REAL funds. Are you sure?" onayi cikar ve gecerli bir
`WALLET_PRIVATE_KEY` yoksa API 400 dondurup gecise izin vermez
(`PAPER_TRADING=1` env'i de gecisi kilitler).

Iki modda **ayni bot mantigi** calisir: ayni dinleyici, ayni filtreler, ayni kademeler,
ayni kayitlar. Tek fark islemin gerceklesmesi:

| | PAPER | LIVE |
|---|---|---|
| Alim/satim | simulasyon, zincire gitmez | Jupiter v6 swap, imzali islem |
| Fiyat | gercek Dexscreener verisi | gercek Dexscreener verisi |
| Bakiye | `settings.json` icindeki paper cuzdan | gercek cuzdan bakiyesi |
| Ucret | platform %1 + network 0.000005 SOL + priority 0.001 SOL (alim VE satimda) | zincirin gercek ucretleri |

Paper cuzdan panelden fonlanir (varsayilan **$20** karsiligi SOL, reset aninda SOL/USD
kuruna gore hesaplanir). Header'da paper bakiye (SOL + $), bugunun PnL'i, tum zaman PnL'i
ve **PAPER SIFIRLA** butonu bulunur; sifirlama paper islem gecmisini de siler, gercek
islemlere dokunmaz. Paper modunda gercek cuzdan bakiyesi soluk (gri) gosterilir.

Her pozisyon ve her satis satiri hangi modda acildigini tasir (`is_paper`), gecmis
tablosunda `PAPER`/`LIVE` etiketi gorunur. Mod degistirmek acik pozisyonlari etkilemez:
paper acilan pozisyon paper kapanir, canli acilan canli.

## Satis stratejisi: kademeli cikis + trailing stop

Duz take-profit yerine pozisyon parca parca satilir (hepsi panelden ayarlanir):

| Kademe | Varsayilan tetik | Satilan (pozisyonun %'si) |
|---|---|---|
| Tier 1 | 2x (%100 kar) | %25 |
| Tier 2 | 5x | %25 |
| Tier 3 | 10x | %25 |
| Kalan %25 | trailing stop | ATH'nin %30 altina dusunce hepsi |

- **ATH takibi:** her fiyat kontrolunde (10sn) pozisyonun zirvesi ve
  `trailing_stop_price = ATH * (1 - trailing_stop/100)` guncellenir. Ornek: coin 20x'e
  cikip %30 duserse kalan 14x'te satilir.
- **Trailing stop yalnizca Tier 1 alindiktan sonra devreye girer.**
- **Stop loss** sadece hicbir kademe tetiklenmeden once gecerlidir: giristen %30 asagi
  duserse pozisyonun tamami satilir. Tier 1'den sonra stop loss devre disidir, kalani
  trailing stop yonetir.
- Her kademe satisi ayri bir islem satiri yazar (`tier`, `sat %`, o dilimin PnL'i) ve
  panele websocket ile aninda duser. Pozisyon %100 satildiginda kapanir.

Pozisyon tablosunda giris, guncel fiyat, ATH, PnL% (ve carpan), tamamlanan kademeler
(yesil ✓), trailing stop seviyesi, kalan pozisyon yuzdesi ve manuel SAT butonu vardir.

## Akis

1. `bot.py` → `wss://mainnet.helius-rpc.com` uzerinden pump.fun program loglarina abone olur
   (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`), `Instruction: Create` iceren islemlerden
   yeni mint + kurucu cuzdani cikarir. Baglanti koparsa 5sn sonra otomatik yeniden baglanir.
2. `analyzer.py` → 10sn bekler, sonra metrikleri toplar ve **tum** kurallari uygular.
3. `trader.py` → PASS ise Jupiter v6 quote + swap ile alir, pozisyonu SQLite'a yazar.
4. Monitor dongusu her 10sn tum acik pozisyonlarin fiyatini ceker, ATH ve trailing stop
   seviyesini gunceller, kademe / trailing / stop loss tetiklenirse ilgili dilimi satar ve
   her satisi `trades` tablosuna yazar.
5. Her olay websocket ile panele push edilir; panel hicbir zaman sayfa yenilemez.

## Analiz kurallari

Hepsi gecmek zorunda; **veri bulunamayan her metrik FAIL sayilir**:

- `bundler% < max_bundler`
- `sniper% < max_sniper`
- `dev% < max_dev_holdings`
- `top10% < max_top10` (varsayilan 30)
- LP burn edilmis / kilitli
- `min_mcap <= mcap <= max_mcap`
- `5m hacim > min_volume_5m` (varsayilan 500)
- freeze authority kapali (ek guvenlik kontrolu)

### Veri kaynaklari hakkinda onemli not

Dexscreener'in ucretsiz API'si **bundler / sniper / dev holdings / LP burn / top10**
alanlarini dondurmez — sadece fiyat, mcap/fdv, hacim ve likidite verir. Bu yuzden
`analyzer.py` eksik metrikleri Helius RPC ile zincir uzerinden hesaplar:

| Metrik | Nasil hesaplanir |
|---|---|
| top10% | `getTokenLargestAccounts` + sahiplerin program hesabi mi (bonding curve / AMM vault) diye elenmesi, kalan dolasimdaki arza oran |
| dev% | Launch isleminin fee payer'inin (kurucu cuzdan) bakiyesi / dolasimdaki arz |
| bundler% | Launch **slotu icinde** alinan token miktari / dolasimdaki arz |
| sniper% | Launch'tan sonraki 15sn icinde alinan token miktari / dolasimdaki arz |
| LP burn | pump.fun / pumpswap / moonshot gibi program sahipli likiditede `true`, dogrulanamayan durumda `None` → FAIL |

`bundler%` / `sniper%` sadece launch islemine kadar geri sayfalanabildiginde hesaplanir;
launch penceresinde 40'tan fazla islem varsa metrik "dogrulanamadi" sayilir ve coin elenir.

**HELIUS_API_KEY olmadan calismaz:** anahtar yoksa RPC `api.mainnet-beta.solana.com`'a duser,
o da `getTokenLargestAccounts` gibi cagrilari reddeder → `dev` ve `top10` verisi bos kalir →
her coin FAIL alir (kural geregi). Yani anahtarsiz bot hicbir sey satin almaz.

Bunlar heuristiktir, ticari holder API'lerinin sonuclariyla birebir ayni cikmayabilir.
Odemeli bir holder API'n varsa `analyzer.collect_metrics` icine tek noktadan baglanabilir
(Dexscreener yanitinda `bundlerPercent` vb. alanlar varsa zaten oncelikli kullanilir).

Analizde coin basina ~40 RPC cagrisi yapilir. Helius ucretsiz planinda yogun saatlerde
rate limit yiyebilirsin; `analyzer.MAX_EARLY_TX` degerini dusurerek azaltabilirsin.

## Panel

- Header: PAPER/LIVE anahtari, cuzdan (kisaltilmis), gercek SOL bakiyesi, paper bakiye (SOL + $),
  paper PnL bugun / tum zaman, PAPER SIFIRLA butonu, RUNNING/STOPPED rozeti, ON/OFF butonu,
  Helius baglanti durumu
- Ayarlar formu: filtreler, satis stratejisi (tier x / tier %, trailing, stop loss), paper cuzdan
  (baslangic $, platform / network / priority fee) — kaydet → `settings.json`
- Canli coin akisi: saat, isim, mcap, bundler%, sniper%, dev%, top10%, BOUGHT/REJECTED + gerekce
- Acik pozisyonlar: giris, guncel, ATH, PnL% (+carpan), kademeler ✓, trailing seviyesi,
  kalan %, yas, manuel SAT
- Islem gecmisi: coin, PAPER/LIVE, kademe, sat %, giris, cikis, PnL%, PnL SOL, tarih
- Log akisi ve istatistikler

## API

| Endpoint | Aciklama |
|---|---|
| `GET /` | Paneli servis eder |
| `GET /api/status` | Bot durumu, bakiye, acik pozisyon sayisi, istatistik |
| `POST /api/bot/start` / `POST /api/bot/stop` | Botu baslat / durdur |
| `GET/POST /api/settings` | Ayarlari oku / kaydet (`settings.json`) |
| `GET /api/positions` | Acik pozisyonlar |
| `GET /api/trades` | Islem gecmisi |
| `POST /api/positions/{id}/sell` | Manuel satis (govde `{"percent": 50}` ile kismi satis) |
| `GET /api/paper` | Paper cuzdan durumu (bakiye, bugun / tum zaman PnL) |
| `POST /api/paper/reset` | Paper cuzdani sifirla (`{"start_usd": 20}`), paper gecmisini siler |
| `POST /api/mode` | `{"paper": false}` ile canli moda gec (cuzdan yoksa 400) |
| `WS /ws` | `init`, `new_coin`, `position_update` (fiyat + ATH + trailing + kalan %), `position_opened`, `position_closed`, `trade_closed` (kademe + sat % + PnL), `balance_update` (gercek + paper cuzdan), `log`, `status`, `settings` |

## Dosyalar

```
backend/main.py      FastAPI + websocket hub
backend/bot.py       Helius log dinleyici, launch tespiti
backend/analyzer.py  Dexscreener + on-chain metrikler, kural motoru
backend/trader.py    Jupiter alim/satim, pozisyon takibi, TP/SL
backend/database.py  SQLite (positions, trades)
backend/rpc.py       Solana JSON-RPC istemcisi
backend/state.py     Ayarlar, bot durumu, event bus
frontend/index.html  Panel (framework yok)
```

## Hata yonetimi

- Dexscreener 429 → 3sn bekle, 3 deneme
- Jupiter quote/swap hatasi → logla, islemi atla (otomatik tekrar yok)
- Helius websocket kopmasi → 5sn sonra otomatik yeniden baglanti
- Panel websocket kopmasi → 3sn'de bir yeniden baglanma
- Tum donguler try/except icinde; bot crash olmaz

## Veritabani semasi

`positions` ve `trades` tablolari acilista otomatik migrate edilir (eksik kolonlar
`ALTER TABLE` ile eklenir), eski bir `trades.db` ile de calisir.

**positions:** `id, mint, name, entry_price, current_price, amount_token, amount_sol,
timestamp, status, tier1_sold, tier2_sold, tier3_sold, ath_price, trailing_stop_price,
total_sold_percent, remaining_token, realized_sol, fees_sol, is_paper`

**trades:** `id, mint, name, entry_price, exit_price, amount_sol, pnl_sol, pnl_percent,
entry_time, exit_time, tier, sold_percent, fees_sol, is_paper`

Her kademe satisi ayri bir `trades` satiridir: `amount_sol` o dilimin maliyeti,
`pnl_sol` o dilimin net kar/zarari, `tier` degeri `tier1|tier2|tier3|trailing_stop|
stop_loss|manual`.
