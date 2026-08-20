# market-dominanter — Sistem Brief'i ve 19 Ağustos 2026 Çalıştırma Verisi

> **Bu dosyanın amacı:** botun mevcut mimarisini, kural setini ve tek gerçek
> çalıştırmasının ham sonuçlarını tek yerde toplamak; üzerine yeni bir sistem
> tasarlayabilmek için.

---

## 0. Senden istenen

Aşağıda bir Solana / pump.fun memecoin sniper botunun tam tasarımı ve ilk canlı
(paper) çalıştırmasının sonuçları var. Bot 3 işlem yaptı, 3'ü de zarar etti.

İstenen:

1. Kural setinin neden kaybettiğini teşhis et — özellikle *seçim* (hangi coini
   alıyor) ve *çıkış* (ne zaman satıyor) katmanlarını ayrı ayrı değerlendir.
2. pump.fun'ın gerçek istatistiksel yapısına göre (launch başına getiri dağılımı,
   rug oranı, runner oranı) bu stratejinin pozitif beklenen değere sahip
   olabileceği bir parametre bölgesi var mı, yoksa yaklaşımın kendisi mi yanlış?
3. Yeni bir sistem öner. Mevcut kodun neresi kalsın, neresi atılsın.

Kısıtlar bölüm 8'de.

---

## 1. Ne yapıyor — bir cümlede

pump.fun'da **yeni doğan** coinleri Helius websocket'inden yakalar, 90 saniye
bekler, on-chain güvenlik metriklerini hesaplar, tüm kurallardan geçerse sabit
miktarda (0.01 SOL) alır, kademeli kâr alma + stop loss ile çıkar.

Şu an **paper trading** modunda (gerçek fiyatlara karşı simüle dolum, sanal
bakiye). LIVE mod kodda var (Jupiter v6 swap) ama cüzdan anahtarı girilmediği
için kapalı.

---

## 2. Mimari

```
backend/
  main.py       FastAPI sunucu + websocket hub + REST API
  bot.py        Launch tespiti (Helius websocket -> pump.fun program logları)
  analyzer.py   Güvenlik/metrik analizi + kural motoru (28 KB, en büyük parça)
  pumpfun.py    Bonding curve okuyucu (on-chain, hesap verisini parse eder)
  trader.py     Alım/satım, pozisyon yönetimi, tier/trailing/stop mantığı
  rpc.py        Solana RPC istemcisi + hız sınırlayıcı (pacer) + retry
  state.py      Ayarlar (dataclass), global durum, event bus
  database.py   SQLite (trades.db): positions + trades tabloları
frontend/
  index.html    Tek dosya panel (websocket ile canlı akış)
```

Veri kaynakları:

| Kaynak | Ne veriyor |
|---|---|
| pump.fun bonding curve (on-chain) | fiyat, market cap, curve'e giren gerçek SOL |
| Dexscreener (ücretsiz, anahtarsız) | fiyat, mcap, 5m hacim, likidite, dex id |
| Helius / Solana RPC | supply, holder yoğunlaşması, dev bakiyesi, launch penceresi işlemleri |

Önemli not: Dexscreener yeni bir mint'i indekslemek için 30-90 saniye istiyor.
O yüzden 90 saniyeden genç coinlerde **birincil kaynak bonding curve**;
Dexscreener sadece boşlukları (5m hacim, havuz likiditesi) dolduruyor.

Dexscreener bundler/sniper/dev/lp-burn alanlarını **hiç vermiyor** — bunlar
`analyzer.py` içinde zincir üzerinden hesaplanıyor.

---

## 3. Keşif hattı (bot.py)

1. Helius websocket'inde pump.fun program loglarına abone ol.
2. Log satırlarında `Instruction: Create` / `CreateV2` / `Create:` marker'ı ara.
3. Create tespit edilince transaction'ı çek, `postTokenBalances`'tan mint'i
   çıkar; bulamazsa `pump` ile biten vanity adresi ara. İmzalayan hesap = dev.
4. 1 saatlik dedupe hafızası (aynı mint iki kez işlenmez).
5. **`analyze_delay = 90` saniye bekle** (semafor dışında — patlama anında
   coinler birbirinin sayacı arkasında kuyruğa girmesin diye).
6. Semafor al (`MAX_PARALLEL_ANALYSIS = 2`), analiz et.
7. Tüm kurallar geçerse `trader.buy()`.

Hız sınırları:

- `RPC_RPS = 9` (global pacer, tüm RPC çağrıları bunun içinden geçiyor)
- `MAX_PARALLEL_ANALYSIS = 2` (aynı anda 2 coin analiz edilir)
- Coin başına kabaca **~14 RPC çağrısı** (supply, largest accounts, multiple
  accounts ×2, signatures sayfalamalı, birden çok get_transaction, curve okuma,
  metadata)

---

## 4. Hesaplanan metrikler

| Metrik | Nasıl hesaplanıyor |
|---|---|
| `price_usd`, `market_cap` | Önce bonding curve (on-chain), yoksa Dexscreener |
| `curve_sol` | Bonding curve'e girmiş gerçek SOL miktarı |
| `curve_complete` | Curve dolmuş → DEX'e migrate olmuş |
| `supply` / `total_supply` | RPC `getTokenSupply`; supply = dolaşımdaki (havuz çıkarılmış) |
| `bundler` | Launch **slot'unda** alınan token miktarı / toplam arz |
| `sniper` | Launch'tan sonraki **15 saniye** içinde (`SNIPER_WINDOW_SEC`) alınan / toplam arz |
| `dev` | Dev cüzdanının bakiyesi / toplam arz |
| `top10` | En büyük 10 hesabın toplamı / toplam arz |
| `lp_burned` | pump.fun / pumpswap / moonshot gibi curve tabanlı dex'lerde otomatik `True` (likidite program sahipliğinde, çekilemez) |
| `freeze_authority` | Mint hesabından |
| `volume_5m`, `liquidity_usd` | Sadece Dexscreener |
| `sniper_truncated` | Launch penceresindeki işlem sayısı `MAX_EARLY_TX = 150`'yi aşarsa `True` |

**"Eksik veri = RED" kuralı:** bir metrik `None` kalırsa coin reddedilir.
Gerekçe: ölçülemeyen risk, yok sayılan risk değildir.

> `HELIUS_API_KEY` olmadan public RPC `getTokenLargestAccounts`'u reddettiği
> için dev/top10 hep `None` kalır ve bot **hiçbir coin almaz**. Anahtar zorunlu.

---

## 5. Kural motoru (`analyzer.evaluate`) — tam liste

Tüm eşikler 19 Ağustos 2026 canlı ölçümlerine göre kalibre edildi.

| Kural | Eşik | Red koşulu |
|---|---|---|
| `max_bundler` | 5.0 % | bundler ≥ %5 (toplam arza göre) |
| `max_sniper` | 10.0 % | sniper ≥ %10 |
| `sniper_truncated` | — | launch penceresinde >150 işlem → **koşulsuz RED** |
| `max_dev_holdings` | 5.0 % | dev ≥ %5 |
| `max_top10` | 25.0 % | top10 ≥ %25 |
| `lp_burned` | — | `True` değilse RED |
| `min_mcap` | $5,000 | mcap < $5k |
| `max_mcap` | **$25,000** | **mcap > $25k → RED** |
| `min_curve_sol` | 2.0 SOL | curve < 2 SOL (curve tamamlanmamışsa) |
| `max_curve_sol` | **30.0 SOL** | **curve > 30 SOL → RED** |
| `min_volume_5m` | 0.0 | veri varsa ve ≤ 0 ise RED |
| `freeze_authority` | — | açıksa RED |
| `price_usd` | — | yok veya ≤ 0 ise RED |

Bir coin **tüm** kuralları geçmek zorunda. Tek gerekçe bile listeye girerse RED.

Kod yorumlarındaki kalibrasyon notları (aynen):

- *"top10 toplam arza göre ölçülüyor (curve dışı float'a göre ölçüldüğünde 30
  saniyelik bir coinde tek alıcı olduğu için her zaman %100 çıkıyordu)."*
- *"Dexscreener 20 saniyelik coinde 5m hacmi boş dönüyor. Talep ölçüsü artık
  bonding curve'e giren gerçek SOL."*
- *"30sn'de coinlerin neredeyse tamamı launch tabanında duruyor (canlı ölçüm:
  mcap $2281 = curve tabanı, curve 0.00 SOL). 90sn'de ayrışıyorlar, yani filtre
  ancak orada gerçek sinyalle çalışıyor. Sniper penceresi (15sn) de kapanmış olur."*

---

## 6. Giriş ve çıkış

**Giriş:** kurallar geçince sabit `auto_buy_sol = 0.01` SOL. Pozisyon boyutlama
yok, güven skoru yok, kademeli giriş yok. Geçen her coine aynı miktar.

**Çıkış** (`trader.py`, fiyat döngüsü **10 saniyede bir**):

```
stop loss   -> tier 1'DEN ÖNCE fiyat girişin %30 altına düşerse tam çıkış
tier 1      -> 2x'te %25 sat
tier 2      -> 5x'te %25 sat
tier 3      -> 10x'te %25 sat
trailing    -> TIER 1 TETİKLENDİKTEN SONRA, ATH'nin %30 altına düşünce kalanı sat
```

Kritik davranış (`trader.py:585`):

```python
if fresh.get("tier1_sold"):        # trailing SADECE 2x görüldüyse silahlanır
    trailing = float(fresh.get("trailing_stop_price") or 0.0)
    if trailing > 0 and price <= trailing:
        await sell(position_id, reason="trailing_stop")
```

Yani **1x ile 2x arası bir ölü bölge var:** 1.99x'e çıkıp dönen bir coin hiçbir
kâr kilitlemeden -%30 stop loss'a kadar geri gelir.

---

## 7. Ücret ve slipaj modeli (paper)

```python
fee_platform_pct   = 1.0        # işlem başına %1
fee_network_sol    = 0.000005   # sabit
fee_priority_sol   = 0.001      # SABİT — pozisyon boyutundan bağımsız
paper_slippage_pct = 1.5        # taban slipaj; likidite biliniyorsa üzerine etki eklenir
slippage_bps       = 1500       # %15 max slipaj toleransı
```

`trader.py:336` — alımda cüzdandan düşen: `amount_sol + fixed_fee`
`trader.py:348` — token satın alınan miktar: `(amount_sol - platform_fee)`

0.01 SOL'lük pozisyon için gidiş-dönüş sabit maliyet:

```
%1 platform ×2  +  0.001 priority ×2  +  network  =  0.00221 SOL  =  %22.1
+ %1.5 taban slipaj ×2                            =  %3.0
--------------------------------------------------------------------
başabaş için gereken fiyat artışı                 ≈  %25
```

**Muhasebe hatası:** `pnl_sol = realized_sol - amount_sol` olarak hesaplanıyor,
giriş sabit ücretini (0.001005 SOL) içermiyor. Panel gerçek zararı olduğundan
**%31 düşük** gösteriyor.

---

## 8. Kısıtlar

- Helius API anahtarı var (ücretsiz plan, `RPC_RPS = 9` buna göre ayarlı)
- Cüzdan private key **girilmedi** → LIVE mod API tarafından reddediliyor
- Paper cüzdan varsayılanı $20 karşılığı SOL
- Python 3.12, Windows, tek makine, 7/24 çalışmıyor
- Dexscreener ücretsiz API (anahtarsız, oran limitli)
- Ödeme yapılan başka veri kaynağı yok

---

## 9. ÇALIŞTIRMA: 19 Ağustos 2026, 21:54 – 23:37 (yerel, UTC+3) — 1sa 43dk

3 alım, 2 kapalı işlem, 1 açık pozisyon. Hepsi paper.

### 9.1 Ham kayıtlar

**positions**

```json
{"id":1,"mint":"6Lh4eMqy8ZW5ZsFRh5KfsGzAx2pWQAzkPSEp2Yvupump","name":"derp",
 "entry_price":7.96126414021607e-06,"current_price":4.56252e-06,
 "amount_token":102130.3885513253,"amount_sol":0.01,
 "timestamp":"2026-08-19 21:54:11","status":"closed",
 "tier1_sold":0,"tier2_sold":0,"tier3_sold":0,
 "ath_price":1.454e-05,"trailing_stop_price":1.0178e-05,
 "total_sold_percent":100.0,"remaining_token":0.0,
 "realized_sol":0.004607070814713635,"fees_sol":0.002166687583987007,"is_paper":1}

{"id":2,"mint":"DrNTRZF7onQm6wCSadeeJTZBcPhjPPRWeCRw4MzTpump","name":"INSTAGRAM",
 "entry_price":1.3722799999999998e-05,"current_price":9.098444999999999e-06,
 "amount_token":59286.88022852481,"amount_sol":0.01,
 "timestamp":"2026-08-19 22:30:00","status":"closed",
 "tier1_sold":0,"tier2_sold":0,"tier3_sold":0,
 "ath_price":1.3722799999999998e-05,"trailing_stop_price":9.605959999999998e-06,
 "total_sold_percent":100.0,"remaining_token":0.0,
 "realized_sol":0.005462533423652806,"fees_sol":0.0021753286204409376,"is_paper":1}

{"id":3,"mint":"6xefuyAvqgSAV5bgG28p9Ud3FhMTXqqD9Bn3onw5pump","name":"ECLIPSE ",
 "entry_price":8.282925030419821e-06,"current_price":8.22e-06,
 "amount_token":98343.51959101498,"amount_sol":0.01,
 "timestamp":"2026-08-19 22:39:09","status":"open",
 "tier1_sold":0,"tier2_sold":0,"tier3_sold":0,
 "ath_price":8.282925030419821e-06,"trailing_stop_price":5.798047521293875e-06,
 "total_sold_percent":0.0,"remaining_token":98343.51959101498,
 "realized_sol":0.0,"fees_sol":0.001105,"is_paper":1}
```

**trades**

```json
{"id":1,"name":"derp","entry_price":7.96126414021607e-06,"exit_price":4.56252e-06,
 "amount_sol":0.01,"pnl_sol":-0.0053929291852863655,"pnl_percent":-42.691010879131916,
 "entry_time":"2026-08-19 21:54:11","exit_time":"2026-08-19 22:02:00",
 "tier":"stop_loss","sold_percent":100.0,"fees_sol":0.0010616875839870065,"is_paper":1}

{"id":2,"name":"INSTAGRAM","entry_price":1.3722799999999998e-05,"exit_price":9.098444999999999e-06,
 "amount_sol":0.01,"pnl_sol":-0.004537466576347194,"pnl_percent":-33.698334159209494,
 "entry_time":"2026-08-19 22:30:00","exit_time":"2026-08-19 23:37:13",
 "tier":"stop_loss","sold_percent":100.0,"fees_sol":0.0010703286204409375,"is_paper":1}
```

### 9.2 Gerçek PnL (giriş ücreti dahil, düzeltilmiş)

| Coin | Çıkan | Dönen | Net | % | Durum |
|---|---|---|---|---|---|
| derp | 0.011005 | 0.004607 | **-0.006398** | -58.1% | kapalı (stop_loss) |
| INSTAGRAM | 0.011005 | 0.005463 | **-0.005542** | -50.4% | kapalı (stop_loss) |
| ECLIPSE | 0.011005 | 0.009924 | **-0.001081** | -9.8% | açık |
| **TOPLAM** | **0.033015** | **0.019994** | **-0.013021 SOL** | **-39.4%** | |

Botun panelde gösterdiği: -0.00993 SOL. Gerçek: -0.01302 SOL.
Toplam ödenen ücret: 0.005447 SOL = konuşlandırılan sermayenin **%18.2'si**.

### 9.3 Market cap açısından (supply = 1e9)

| Coin | Giriş mcap | Zirve mcap | Çıkış mcap | Zirve çarpanı |
|---|---|---|---|---|
| derp | $7,961 | **$14,540** | $4,563 | **1.83x** |
| INSTAGRAM | $13,723 | $13,723 | $9,098 | 1.00x |
| ECLIPSE | $8,283 | $8,283 | $8,220 | 1.00x |

Filtre bandı: $5,000 – $25,000.

Bağlam için, çalıştırma anındaki **SOL fiyatı ≈ $82.2** (üç işlemin dolum
verisinden türetildi: $82.13 / $82.18 / $82.28). pump.fun bonding curve'ü
~85 SOL yatırıldığında tamamlanıp DEX'e migrate oluyor; `max_curve_sol = 30 SOL`
eşiği yani botun **curve ilerlemesinin ~%35'ini geçmiş hiçbir coini almadığı**
anlamına geliyor. Tamamlanma anındaki mcap'i bu SOL fiyatına göre yeniden
hesaplaman gerekir — yaygın olarak alıntılanan "$69k" rakamı SOL'ün $150-180
olduğu döneme ait.

### 9.4 Zaman çizelgesi

```
21:54:11  derp ALINDI      @ $7,961 mcap
   ~8dk içinde zirve       @ $14,540 mcap (1.83x)
22:02:00  derp SATILDI     @ $4,563 mcap   stop_loss  -42.7%   (tutuş: 7dk49sn)
22:30:00  INSTAGRAM ALINDI @ $13,723 mcap  (alım anı = zirve, hiç yukarı gitmedi)
22:39:09  ECLIPSE ALINDI   @ $8,283 mcap
23:37:13  INSTAGRAM SATILDI@ $9,098 mcap   stop_loss  -33.7%   (tutuş: 1sa07dk)
```

---

## 10. Şimdiye kadar tespit edilen sorunlar

Aşağıdakiler kod ve veri incelemesiyle **doğrulandı**. Bunları yeniden keşfetmene
gerek yok; asıl soru bunların ötesinde bir yapısal sorun olup olmadığı.

### A. Ters seçim: filtre "pump etmemiş coin" arıyor

`analyze_delay = 90s` + `max_mcap = $25k` + `max_curve_sol = 30 SOL` +
`sniper_truncated` (>150 launch işlemi = red) birlikte çalışınca:

90. saniyede **hâlâ** $25k altında olan coinler, tanım gereği o 90 saniyede
ilgi görmemiş coinlerdir. Runner'lar bu bandı çoktan aşmış ve reddedilmiş olur.

Veri bunu doğruluyor: aldığı en iyi coinin **zirvesi $14,540** — filtrenin kendi
üst sınırının bile altında. Bot King of the Hill'e yaklaşan bir coin hiç tutmadı.

Kural setinin tamamı rug korumasına göre kurulmuş (dev/sniper/bundler/top10
yoğunlaşmasını cezalandırıyor). Ancak pump.fun'da **erken yoğun talep hem rug'ın
hem runner'ın ortak imzası** — ikisini ayırt eden bir sinyal yok, o yüzden
ikisini birden eliyor.

`MAX_EARLY_TX` üzerindeki kod yorumu bu gerilimi zaten fark etmiş:
*"Kızışmış bir pump.fun launch'ı ilk saniyelerinde yüzün epey üzerinde işlem
görür, bu yüzden bu tavan cömert olmalı: altındaki her değer her yoğun coini
sessizce 'doğrulanamaz' (= reddedildi) haline getirir."* Tavan 150'de duruyor —
"yüzün epey üzerinde" ifadesine göre bu hâlâ düşük olabilir ve tam da en canlı
launch'ları eliyor olabilir. Ölçülmedi.

### B. Trailing stop tier 1'e kilitli → 1x-2x ölü bölgesi

`derp` 1.83x'e çıktı. Trailing stop fiyatı hesaplanıp DB'ye **yazıldı**
(`1.0178e-05` = ATH -%30 = **+%28 kâr**) ama tier 1 (2.0x) tetiklenmediği için
silahlanmadı. Fiyat düşerken bu seviyenin tam içinden geçti, bot izledi,
-%42.7'de stop loss ile çıktı. Tek işlemde ~0.0035 SOL fark.

pump.fun'da 1.8x yapıp dönen coin, 2x+ yapandan çok daha sık.

### C. Sabit ücret pozisyon boyutunu öldürüyor

`fee_priority_sol = 0.001` sabit. 0.01 SOL pozisyonda bu tek başına %10.
Gidiş-dönüş toplam maliyet %22, slipajla ~%25. Coin %20 yükselse bile zarar.
Bu tek başına stratejiyi matematiksel olarak kaybettiriyor.

### D. Stop loss %30 ayarlı, gerçekleşen çıkışlar -%42.7 ve -%33.7

Fiyat döngüsü 10 saniyede bir (`trader.py:518`) + `slippage_bps = 1500` (%15).
pump.fun hızında 10 saniye çok uzun; eşiğin geçildiği fark edildiğinde çoktan
çok altında olunuyor.

### E. Örneklem yok

1sa43dk'da 3 alım. O pencerede pump.fun'da muhtemelen 1000+ coin doğdu.
`MAX_PARALLEL_ANALYSIS = 2` ve `RPC_RPS = 9` ile coin başına ~14 çağrı →
teorik tavan ~38 coin/dk, pratikte gecikmeyle çok daha az. Bot doğan coinlerin
büyük kısmını **hiç görmüyor**; gördüklerini de semafor kuyruğunda beklettiği
için 90 saniyeden geç analiz ediyor (mcap okuması bayat).

Ayrıca: "100 işlemden 1'i 50x yapar" mantığındaki bir stratejide 3 atışla
sonuç okunmaz. Filtre mükemmel olsa bile bu örneklem anlamsız.

### F. Red gerekçeleri kaydedilmiyor — en kritik eksik

`analyzer.evaluate` her red için düzgün gerekçe metni üretiyor
(`"mcap $47000 > $25000"` gibi) ve `bot.py` bunu panele yayınlıyor, ama
**hiçbir yere yazılmıyor**. Log yok, DB'de red tablosu yok.

Sonuç: yukarıdaki A maddesi **çıkarım**, doğrudan ölçüm değil. Hangi kuralın
kaç coini elediğini kimse bilmiyor.

---

## 11. Cevaplanması istenen sorular

1. A maddesindeki ters seçim teşhisi doğru mu? Yoksa asıl darboğaz başka yerde mi?
2. `max_mcap` / `max_curve_sol` / `sniper_truncated` üçlüsü kaldırılırsa rug
   riski ne kadar artar? Rug'ı runner'dan ayıran, bu filtrelerin ıskaladığı
   ölçülebilir bir on-chain sinyal var mı?
3. `analyze_delay` kaç saniye olmalı? 90sn'de karar vermek için gereken bilgi
   ile bandın hâlâ anlamlı olması arasındaki denge nerede?
4. Kademeli çıkış (2x/5x/10x + trailing) bu varlık sınıfı için doğru şema mı?
   Alternatif: zaman tabanlı çıkış, mcap hedefli çıkış, curve ilerlemesine
   bağlı çıkış.
5. 0.01 SOL pozisyonla %22 gidiş-dönüş maliyeti varken, minimum uygulanabilir
   pozisyon boyutu ne? Toplam sermaye kaç SOL olmalı ki anlamlı bir örneklem
   (100+ işlem) çıkarılabilsin?
6. Mevcut kodda ne kalsın, ne atılsın? Sıfırdan yazmak mı daha hızlı?
