# market-fucker

Solana memecoin sniper botu. Yeni pump.fun launch'larini Helius websocket ile yakalar,
on-chain + Dexscreener verisiyle filtreler, kurallara uyanlari Jupiter uzerinden alir,
take-profit / stop-loss ile otomatik satar. Her sey karanlik temali web panelden
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
| `PAPER_TRADING` | `1` yazarsan cuzdan yuklu olsa bile simulasyon modunda kalir. |
| `JUPITER_API` | Varsayilan `https://quote-api.jup.ag/v6`. |

Private key hicbir yerde loglanmaz, frontend'e gonderilmez, `.env` `.gitignore` icindedir.

## PAPER modu

`WALLET_PRIVATE_KEY` yoksa (ya da `PAPER_TRADING=1`) bot zinciri ucdan uca calistirir —
coin yakalar, analiz eder, "alir", pozisyonu gercek fiyatla takip eder ve TP/SL'de "satar" —
ama hicbir islem zincire gitmez. Panelde `PAPER` rozeti gorunur. Ayar kalibrasyonu icin kullan.

## Akis

1. `bot.py` → `wss://mainnet.helius-rpc.com` uzerinden pump.fun program loglarina abone olur
   (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`), `Instruction: Create` iceren islemlerden
   yeni mint + kurucu cuzdani cikarir. Baglanti koparsa 5sn sonra otomatik yeniden baglanir.
2. `analyzer.py` → 10sn bekler, sonra metrikleri toplar ve **tum** kurallari uygular.
3. `trader.py` → PASS ise Jupiter v6 quote + swap ile alir, pozisyonu SQLite'a yazar.
4. Monitor dongusu her 10sn tum acik pozisyonlarin fiyatini ceker, PnL hesaplar,
   take-profit / stop-loss tetiklenirse satar ve islemi `trades` tablosuna yazar.
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

Bunlar heuristiktir, ticari holder API'lerinin sonuclariyla birebir ayni cikmayabilir.
Odemeli bir holder API'n varsa `analyzer.collect_metrics` icine tek noktadan baglanabilir
(Dexscreener yanitinda `bundlerPercent` vb. alanlar varsa zaten oncelikli kullanilir).

Analizde coin basina ~40 RPC cagrisi yapilir. Helius ucretsiz planinda yogun saatlerde
rate limit yiyebilirsin; `analyzer.MAX_EARLY_TX` degerini dusurerek azaltabilirsin.

## Panel

- Header: cuzdan (kisaltilmis), SOL bakiyesi, RUNNING/STOPPED rozeti, ON/OFF butonu, Helius baglanti durumu
- Ayarlar formu (kaydet → `settings.json`)
- Canli coin akisi: saat, isim, mcap, bundler%, sniper%, dev%, top10%, BOUGHT/REJECTED + gerekce
- Acik pozisyonlar: giris, guncel fiyat, PnL%, yas, manuel SAT butonu
- Islem gecmisi: giris, cikis, PnL%, PnL SOL, tarih
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
| `POST /api/positions/{id}/sell` | Manuel satis |
| `WS /ws` | `new_coin`, `position_update`, `position_opened`, `position_closed`, `trade_closed`, `balance_update`, `log`, `status`, `settings` |

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
