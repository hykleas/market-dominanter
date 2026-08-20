# market-dominanter

pump.fun / Solana memecoin botu. **Copy trading + curve-progress hibrit.**

Fresh-launch sniping stratejisi 19 Agustos 2026 calistirmasindan sonra arsive
alindi (`backend/legacy_sniper.py`): `analyze_delay=90s` + `max_mcap=$25k` +
`max_curve_sol=30` ucluisu ters secim yapiyordu - 90. saniyede hala bandin
icinde olan coin, tanimi geregi ilgi gormemis coindi. Ayrintili teshis:
[`ANALIZ-BRIEF.md`](ANALIZ-BRIEF.md).

## Uc katman

| Katman | Dosya | Ne yapar |
|---|---|---|
| 0 - Aday kesfi | `backend/wallet_discovery.py` | Kazanan tokenlarin launch penceresinde erken alim yapan cuzdanlari bulur, kesisenleri aday listesine ekler |
| 1 - Cuzdan skorlama | `backend/wallet_scorer.py` | Aday cuzdanlarin 30 gunluk gecmisini FIFO ile yeniden kurar, bot/farmer'lari eler, skorlar |
| 2 - Canli kopyalama | `backend/copy_engine.py` | Takipteki cuzdanlari websocket'ten dinler, kapilardan gecen alimlari kopyalar, cikisi lidere devreder |
| 3 - Curve momentum | `backend/pumpfun.py` | Bonding curve ilerlemesine gore pozisyon boyutunu buyutur ya da migration anini atlar |

## Hizli baslangic

```bash
pip install -r requirements.txt
cp .env.example .env          # HELIUS_API_KEY zorunlu
python backend/main.py        # PORT env ile port secilir
```

**1. Aday cuzdanlari topla.** Iki yol var, ikisi ayni dosyaya yazar.

*Otomatik:* kazanan tokenlarin erken alicilarindan aday uret.

```bash
python -m backend.wallet_discovery                      # varsayilan: 14 gun, >$200K
python -m backend.wallet_discovery --days 3 --limit 30  # daha genis kapsam
python -m backend.wallet_discovery --dry-run            # yazmadan raporla
```

*Elle:* `wallets_candidates.json` dosyasina ekle.

```json
[{"address": "...", "source": "gmgn", "note": "neden ekledigin"}]
```

Discovery elle eklenen kayitlarin `source`/`note` alanlarina dokunmaz.

### Kesif nasil calisir

1. Dexscreener'dan Solana token havuzu toplanir (arama + one cikan/boost listeleri)
2. Yas ve mcap filtresi -> "kazananlar"
3. Launch ani ZINCIRDEN dogrulanir (Metaplex metadata hesabi, tek RPC cagrisi).
   Dexscreener'in `pairCreatedAt` degeri token'in degil O HAVUZUN yasidir; canli
   olcumde BONK "10.9 gunluk" gorunuyordu (gercekte 1335).
4. Launch + 3sn .. +30dk penceresinde alim yapanlar cikarilir. Ilk 3 saniye
   MEV/sniper botlarina aittir, atilir.
5. 2+ kazananda gorunen cuzdanlar aday olur

**Tarama MINT'ten degil BONDING CURVE hesabindan yapilir.** Mint'in imza
gecmisi token yasadikca buyur (graduation sonrasi DEX hacmi de oraya birikir);
curve'un gecmisi ise graduation'da biter. Ayni token (PANTS) uzerinde olculdu:

| Hedef | Imza | Sayfa | Sure | Launch'a ulasti |
|---|---|---|---|---|
| mint | 400.000+ | 400+ | >120sn | **hayir** |
| curve | 1.980 | 2 | 0.6sn | evet |

pump.fun tokeni olmayan mint'lerde mint taramasina geri dusulur.

Launch ani, taranan adresin KENDI en eski imzasindan alinir. Disaridan bir
zaman damgasi vermek hatali olur: graduated bir token icin Dexscreener'in
`pairCreatedAt` degeri havuzun acilis - yani graduation - anidir, curve'un
gecmisi ise tam orada biter, dolayisiyla pencere hep bos cikar.

**Atlanan tokenlar.** Her calistirma kapsam ozeti basar. Iki farkli sebep var:
`launch'a ulasilamadi` butce sorunudur (`--max-pages` yukseltin), `pencerede
islem yok` ise tokenin kendi ozelligidir - olculen bir ornekte curve'un 351
imzasinin 347'si sniper botlarinin kaybettigi yaristi ve basarili 4 islem de
ilk 3 saniyedeydi, yani MEV bandinda.

**2. Skorla.** Panelden `SKORLA`, ya da:

```bash
python -m backend.wallet_scorer            # tum adaylar
python -m backend.wallet_scorer --only ADRES
python -m backend.wallet_scorer --no-ai    # AI siniflandirmayi atla
```

Batch uzun surer (cuzdan basina dakikalar) - RPC_RPS=9 ile sinirli, bir gecede
calisabilir. 429 gelirse bekleyip devam eder, cokmez.

**3. Takibe al.** Panelde skorlu listeden checkbox. Kopya motoru abonelikleri
aninda tazeler.

## Bot/farmer eleme kurallari

| Kural | Sonuc |
|---|---|
| medyan giris gecikmesi < 3sn | MEV/sniper botu -> ELE |
| medyan tutus < 30sn **ve** > 500 trade | scalper script -> ELE |
| alim boyutu varyasyon katsayisi < 0.05 | script -> ELE |
| < 15 kapanmis trade (30g) | ornekle yetersiz -> ELE |
| win rate > %85 | **SUPHELI** - elenmez, panelde kirmizi |

## Cikis stratejisi

**Kopya pozisyonlari** (`source_wallet` dolu) cikisi lidere devreder:

- Lider pozisyonunun >=%50'sini satarsa -> biz tamamen cikariz
- <%50 satarsa -> ayni oranda satariz
- **Hard stop** -%35 ve **zaman stopu** (45dk'da PnL < +%10) sadece lider
  sessiz kalirsa devreye giren guvenlik aglaridir

Tier semasi kopya modunda kapalidir - cikisi kopyalamak isin ozu.

**Legacy pozisyonlari** kademeli semayi korur (2x/5x/10x her birinde %25),
ama trailing artik tier1'e degil **`trailing_arm_x` (1.40x)** degerine
silahlanir. Eski davranista 1x-2x arasi olu bolgeydi: 1.83x'e cikan bir coin
hic kar kilitlemeden -%30 stop loss'a donebiliyordu.

## Olcum

Her kopya sinyali - kopyalanan da atlanan da - `copy_signals` tablosuna
gerekcesiyle yazilir:

```
GET /api/signals/stats     ->  {"actions": {...}, "skip_reasons": {...}}
```

Eski botun en buyuk hatasi red gerekcelerini kaydetmemesiydi; hangi kuralin
kac coini eledigi hic olculemedi. Artik her karar izlenebilir.

## Muhasebe

PnL, giris sabit ucreti dahil TOPLAM cikan SOL'e (`entry_cost_sol`) gore
olculur. Onceki surumde `amount_sol` kullaniliyordu ve giris priority+network
ucreti PnL'e hic girmiyordu - 19 Agustos'ta zarar %31 dusuk raporlandi.

`copy_size_sol` varsayilani **0.1 SOL**: 0.01'de sabit priority fee tek basina
%10, gidis-donus toplam maliyet %22 idi, yani basabas icin ~%25 fiyat artisi
gerekiyordu.

## Backtest - stratejiyi beklemeden olc

Ileriye dogru test (paper modda 100 islem biriktirmek) 1-2 hafta surer. Ayni
orneklem zaten zincirde duruyor: takip edilecek cuzdanlarin gecmis islemleri.

```bash
python -m backend.backtester --all-candidates --split-days 15
python -m backend.backtester --wallet ADRES
python -m backend.backtester --no-selection-filter    # ham potansiyel
```

**Walk-forward.** Cuzdanlari KAZANDIKLARI icin seciyoruz; ayni donemde
kopyalamayi test etmek gelecegi bilerek bahis yapmaktir. O yuzden pencere
ikiye bolunur - cuzdan eski yarinin metrikleriyle elenir, PnL yalnizca yeni
yaridan sayilir:

```
30 gun once ────────── 15 gun once ────────── bugun
  SECIM donemi            TEST donemi
```

**Modellenmeyenler** (sonucu okurken bilinmeli): gecmisteki likidite ucuza
yeniden kurulamadigi icin likidite/curve kapilari test edilmez; basarisiz
islemler, MEV ve gercek zincir slipaji yoktur. Sinyal gecikmesi
`--latency-penalty` ile kotumser bir slipaj cezasi olarak eklenir.

Yani cikan rakam **gercegin ust sinirIDIR**. Burada zarardaysa canlida kesin
zarar eder; kardaysa canlida "belki" kar eder.

## Testler

```bash
python -m pytest tests/ -q      # ya da: python tests/test_fifo.py
```

FIFO trade eslestirme ve transaction cozumleme saf fonksiyonlardir, ag
gerektirmez.

## Guvenlik

- Varsayilan **PAPER** modu. LIVE'a gecis gecerli `WALLET_PRIVATE_KEY`
  olmadan API tarafindan reddedilir.
- `ANTHROPIC_API_KEY` opsiyoneldir; yoksa AI siniflandirma sessizce atlanir ve
  skorlama tamamen metrik tabanli calisir.

---

# Onceki surum notlari

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

- `bundler% < max_bundler` (toplam arza gore, varsayilan 5)
- `sniper% < max_sniper` (toplam arza gore, varsayilan 10)
- `dev% < max_dev_holdings` (toplam arza gore, varsayilan 5)
- `top10% < max_top10` (curve disi dolasima gore, varsayilan 65)
- launch penceresi `MAX_EARLY_TX`'i asmamis olmali (asarsa olculen sniper orani
  alt sinirdir, o yuzden coin elenir)
- LP burn edilmis / kilitli
- `min_mcap <= mcap <= max_mcap`
- `min_curve_sol <= bonding curve'e giren SOL <= max_curve_sol` (varsayilan 2-30 SOL)
- `5m hacim > min_volume_5m` — sadece Dexscreener coini indeksledigi zaman uygulanir
- freeze authority kapali (ek guvenlik kontrolu)

Yuzdelerin tabani onemli: `bundler`, `sniper` ve `dev` **toplam arza** gore olculur
(ekosistemdeki tarayicilarin kullandigi taban), `top10` ise bonding curve disindaki
dolasima gore. Ilk uc metrik dolasima gore olculdugunde siradan bir launch'ta bile
%80-100 cikiyordu ve her coin eleniyordu.

### Veri kaynaklari hakkinda onemli not

Dexscreener'in ucretsiz API'si **bundler / sniper / dev holdings / LP burn / top10**
alanlarini dondurmez — sadece fiyat, mcap/fdv, hacim ve likidite verir. Bu yuzden
`analyzer.py` eksik metrikleri Helius RPC ile zincir uzerinden hesaplar:

| Metrik | Nasil hesaplanir |
|---|---|
| top10% | `getTokenLargestAccounts` + sahiplerin program hesabi mi (bonding curve / AMM vault) diye elenmesi, kalan dolasimdaki arza oran |
| dev% | Launch isleminin fee payer'inin (kurucu cuzdan) bakiyesi / **toplam arz** |
| bundler% | Launch **slotu icinde** alinan token miktari / **toplam arz** (bonding curve'un kendi hesabi haric) |
| sniper% | Launch'tan sonraki 15sn icinde alinan token miktari / **toplam arz** |
| fiyat / mcap / likidite | Once pump.fun bonding curve (`pumpfun.py`), Dexscreener indeksledikten sonra onun verisi |
| curve SOL | Bonding curve'un `real_sol_reserves` degeri = launch'tan beri icine giren gercek SOL |
| LP burn | pump.fun / pumpswap / moonshot gibi program sahipli likiditede `true`, dogrulanamayan durumda `None` → FAIL |

`bundler%` / `sniper%` launch imzasina kadar geri sayfalanabildiginde hesaplanir.
Sayfalama 1000'lik sayfalarla en fazla 15 sayfa geri gider (`SIG_PAGE_SIZE`,
`SIG_PAGE_LIMIT`); yogun bir mint saniyeler icinde binlerce *basarisiz* snipe imzasi
biriktirdigi icin 100'luk sayfalar launch'a hic ulasamiyordu.

Launch penceresinde `MAX_EARLY_TX` (150) islemden fazlasi varsa metrik "veri yok"
sayilmaz: butcenin yettigi kadari hesaplanir, sonuc alt sinir olarak isaretlenir ve
coin "launch penceresi asiri yogun" gerekcesiyle elenir.

### Bonding curve neden birincil kaynak

Dexscreener yeni bir mint'i 30-90 saniyede indeksliyor; bot ise 30 saniyelik coinlere
bakiyor. O yuzden `pumpfun.py` bonding curve hesabini dogrudan okuyup fiyat, market cap,
likidite ve curve'e giren SOL'u ilk slottan itibaren veriyor. Dexscreener yalnizca
kendisine ozel alanlari (5m hacim, havuz likiditesi) dolduruyor.

**HELIUS_API_KEY olmadan calismaz:** anahtar yoksa RPC `api.mainnet-beta.solana.com`'a duser,
o da `getTokenLargestAccounts` gibi cagrilari reddeder → `dev` ve `top10` verisi bos kalir →
her coin FAIL alir (kural geregi). Yani anahtarsiz bot hicbir sey satin almaz.

Bunlar heuristiktir, ticari holder API'lerinin sonuclariyla birebir ayni cikmayabilir.
Odemeli bir holder API'n varsa `analyzer.collect_metrics` icine tek noktadan baglanabilir
(Dexscreener yanitinda `bundlerPercent` vb. alanlar varsa zaten oncelikli kullanilir).

Analizde coin basina ~30-180 RPC cagrisi yapilir (cogu launch penceresindeki
`getTransaction`). `rpc.py` icindeki global pacer (`RPC_RPS`, varsayilan 9/sn) ve
`MAX_PARALLEL_ANALYSIS` (varsayilan 2) bunu Helius ucretsiz planinin sinirinda tutar.
Hala 429 goruyorsan once `RPC_RPS`'i, sonra `analyzer.MAX_EARLY_TX`'i dusur.

## Test

Ag ve Helius anahtari gerektirmeyen offline test:

```powershell
python tools\offline_test.py
```

Bonding curve cozumlemesi, launch penceresi bolme, kural motoru ve paper slipaj
modelini sahte RPC cevaplariyla dogrular. Canli testler icin `tools/` altindaki
`calib2.py` / `livecheck.py` / `diag.py` kullanilir (bunlar `.env` ister).

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
