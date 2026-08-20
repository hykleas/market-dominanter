"""KATMAN 0 - aday cuzdanlari on-chain veriden otomatik uret.

Fikir: son gunlerde GERCEKTEN kazanmis tokenlari bul, her birinin launch
penceresine bak, birden fazla kazananda erken alim yapmis cuzdanlari cikar.
Bir kazananda erken olmak sans; ucunde erken olmak sinyaldir.

Akis:
  1. Dexscreener'dan Solana token havuzu topla, mcap ve yasa gore filtrele
     -> "kazananlar" (varsayilan: son 14 gun, mcap > $200K)
  2. Launch anini ZINCIRDEN dogrula (Metaplex metadata hesabi, tek RPC cagrisi)
  3. Launch + 3sn .. launch + 30dk arasinda ALIM yapan cuzdanlari cikar
     (ilk 3 saniye MEV/sniper botlarindir, atilir)
  4. 2+ kazananda gorunen cuzdanlari aday say
  5. wallets_candidates.json'a ekle (mevcut adresler korunur)

Sonra normal akis: `wallet_scorer` bu adaylari skorlar ve botlari eler. Burada
eleme YAPILMAZ - burasi sadece "kim bakmaya deger" listesi uretir.

CLI:
    python -m backend.wallet_discovery
    python -m backend.wallet_discovery --days 14 --min-mcap 200000
    python -m backend.wallet_discovery --limit 30 --min-hits 3 --dry-run

Batch yavastir ve maliyeti token'in populerligiyle dogru orantilidir: launch
penceresine varmak icin araya giren TUM islemlerin uzerinden gecmek gerekir
(getSignaturesForAddress yalnizca yeniden eskiye gider). Canli olcumde $324K'lik
bir token 400.000+ imzaya sahipti. Genc tokenlar cok daha ucuzdur - `--days 2-3`
ile calistirmak kapsami ciddi sekilde artirir. Gece birakilmak uzere tasarlandi.

DEXSCREENER KISITI: ucretsiz API'de "son 14 gunun en cok kazananlari" diye bir
uc nokta YOK. Havuz; one cikarilan/boost'lanan listeler ve arama terimlerinden
kuruluyor, yani her calistirma piyasanin bir kesitini gorur. Bu yuzden komut
adaylari BIRIKTIRIR: her gece calistirildikca kapsam genisler.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyzer  # noqa: E402
import database as db  # noqa: E402
import rpc  # noqa: E402
import state  # noqa: E402
from wallet_scorer import CANDIDATES_FILE, parse_wallet_tx  # noqa: E402

log = logging.getLogger("market-dominanter")

DEX_BASE = "https://api.dexscreener.com"
TOKENS_BATCH = 30           # /latest/dex/tokens tek istekte bu kadar adres alir
SIG_PAGE = 1000

# Havuzu genisletmek icin kullanilan arama terimleri. Ucretsiz API'de
# "kazananlar" uc noktasi olmadigi icin bu bir agdir, kesin bir liste degil.
DEFAULT_SEARCH_TERMS = ("pump", "SOL", "bonk", "wif", "cat", "dog", "ai",
                        "moon", "meme", "coin")

# Hedef bant: sifirdan yuz binlere/birkac milyona cikmis memecoin launch'lari.
# Bunun ustu major listing'dir; ne kopyalanabilir ne de launch penceresine
# makul bir imza butcesiyle ulasilabilir.
DEFAULT_MAX_MCAP = 50_000_000.0

# Launch anini bulmak icin kullanilan Metaplex metadata programi.
MPL_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

try:
    from solders.pubkey import Pubkey  # type: ignore
    SOLDERS_OK = True
except Exception:  # pragma: no cover - opsiyonel bagimlilik
    Pubkey = None  # type: ignore
    SOLDERS_OK = False


@dataclass
class Winner:
    mint: str
    symbol: str
    market_cap: float
    created_at: float      # unix saniye
    liquidity: float
    launch_signature: Optional[str] = None

    @property
    def age_days(self) -> float:
        return (time.time() - self.created_at) / 86400.0


# Token sembolleri saldirgan kontrolundedir. Bir canli ornekte sembolde U+202E
# (sagdan-sola override) vardi ve Windows konsolunda yazdirmak crash ediyordu;
# ayni metin log'a, aday notuna ve panele de gidiyor.
_SAFE_SYMBOL_MAX = 16


def safe_symbol(value: Optional[str], fallback: str = "?") -> str:
    """Sembolu goruntulenebilir hale getir: kontrol/bidi karakterleri at."""
    if not value:
        return fallback
    cleaned = "".join(ch for ch in str(value)
                      if ch.isprintable() and unicodedata.category(ch) != "Cf")
    cleaned = cleaned.strip()
    return cleaned[:_SAFE_SYMBOL_MAX] if cleaned else fallback


# --------------------------------------------------------------------------- #
# Dexscreener havuzu
# --------------------------------------------------------------------------- #
async def _get_json(url: str, retries: int = 3) -> Any:
    """Dexscreener GET; 429'da geri cekilir. analyzer'in istemcisini paylasir."""
    client = analyzer._client()
    for attempt in range(retries):
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                wait = 3.0 * (attempt + 1)
                log.warning("Dexscreener 429, %.0fsn bekleniyor (%d/%d)", wait, attempt + 1, retries)
                await asyncio.sleep(wait)
                continue
            if resp.status_code != 200:
                await asyncio.sleep(1.0)
                continue
            return resp.json()
        except Exception as exc:
            log.debug("Dexscreener istek hatasi (%s): %s", url, exc)
            await asyncio.sleep(1.0)
    return None


async def collect_pairs(search_terms: Sequence[str]) -> List[Dict[str, Any]]:
    """Havuzu uc kaynaktan topla: arama, one cikan profiller, boost listeleri.

    Arama sonuclari TAM pair objesi dondurur (mcap + pairCreatedAt dahil), o
    yuzden zenginlestirmeye gerek kalmaz. Profil/boost listeleri sadece adres
    verir; onlar /latest/dex/tokens ile zenginlestirilir.
    """
    pairs: List[Dict[str, Any]] = []
    addresses: Set[str] = set()

    for term in search_terms:
        data = await _get_json("%s/latest/dex/search?q=%s" % (DEX_BASE, term))
        found = (data or {}).get("pairs") or []
        pairs.extend(p for p in found if p.get("chainId") == "solana")
        log.info("arama '%s': %d solana pair", term, sum(1 for p in found
                                                         if p.get("chainId") == "solana"))

    for path in ("/token-profiles/latest/v1", "/token-boosts/top/v1", "/token-boosts/latest/v1"):
        data = await _get_json(DEX_BASE + path)
        for item in data if isinstance(data, list) else []:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                addresses.add(item["tokenAddress"])
    log.info("profil/boost listelerinden %d benzersiz adres", len(addresses))

    ordered = sorted(addresses)
    for i in range(0, len(ordered), TOKENS_BATCH):
        batch = ordered[i:i + TOKENS_BATCH]
        data = await _get_json("%s/latest/dex/tokens/%s" % (DEX_BASE, ",".join(batch)))
        pairs.extend(p for p in ((data or {}).get("pairs") or [])
                     if p.get("chainId") == "solana")
    return pairs


def select_winners(pairs: Iterable[Dict[str, Any]], days: float, min_mcap: float,
                   limit: int, now: Optional[float] = None,
                   max_mcap: Optional[float] = None) -> List[Winner]:
    """Havuzdan kazananlari sec. Saf fonksiyon (testi tests/test_discovery.py).

    Ayni mint birden fazla pair ile gelebilir (farkli dex'ler); mint basina EN
    ESKI pairCreatedAt tutulur - token'in gercek dogum ani odur, migrate
    oldugu havuzunki degil.
    """
    now = now if now is not None else time.time()
    best: Dict[str, Winner] = {}

    for pair in pairs:
        base = pair.get("baseToken") or {}
        mint = base.get("address")
        created_ms = pair.get("pairCreatedAt")
        if not mint or not created_ms:
            continue
        try:
            created = float(created_ms) / 1000.0
            mcap = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        except (TypeError, ValueError):
            continue

        age_days = (now - created) / 86400.0
        if age_days > days or age_days < 0 or mcap < min_mcap:
            continue
        # Ust sinir: milyar dolarlik bir listing "kopyalanabilir memecoin
        # launch'i" degil. Ayrica launch penceresine ulasmak icin yuz binlerce
        # imza gerekir - butce doldugu icin zaten atlanirdi, bosuna RPC yakardi.
        if max_mcap is not None and mcap > max_mcap:
            continue

        current = best.get(mint)
        if current is None or created < current.created_at:
            best[mint] = Winner(mint=mint, symbol=safe_symbol(base.get("symbol"), mint[:6]),
                                market_cap=mcap, created_at=created, liquidity=liquidity)
        elif mcap > current.market_cap:
            current.market_cap = mcap

    winners = sorted(best.values(), key=lambda w: w.market_cap, reverse=True)
    return winners[:limit]


def apply_true_ages(winners: Sequence[Winner], oldest_by_mint: Dict[str, float],
                    days: float, now: Optional[float] = None
                    ) -> Tuple[List[Winner], List[Tuple[Winner, float]]]:
    """(gercekten yeni olanlar, [(eski token, gercek yasi)]). Saf fonksiyon.

    NEDEN GEREKLI: havuzdaki bir pair, token'in dogumunu degil O HAVUZUN
    acilisini gosterir. Canli olcumde BONK "10.9 gunluk" cikti - 2022 tokeni,
    sadece yeni bir havuzu vardi. Boyle bir token'in launch penceresini taramak
    hem anlamsiz binlerce RPC cagrisi hem de tamamen alakasiz "erken alici"
    listesi uretirdi.
    """
    now = now if now is not None else time.time()
    fresh: List[Winner] = []
    stale: List[Tuple[Winner, float]] = []
    for winner in winners:
        oldest = oldest_by_mint.get(winner.mint)
        if oldest is None:
            fresh.append(winner)          # dogrulanamadi: onceki yasla devam
            continue
        age_days = (now - oldest) / 86400.0
        if age_days > days:
            stale.append((winner, age_days))
        else:
            winner.created_at = oldest
            fresh.append(winner)
    return fresh, stale


def metadata_pda(mint: str) -> Optional[str]:
    """Metaplex metadata PDA: ["metadata", program, mint]."""
    if not SOLDERS_OK:
        return None
    try:
        program = Pubkey.from_string(MPL_METADATA_PROGRAM)
        pda, _ = Pubkey.find_program_address(
            [b"metadata", bytes(program), bytes(Pubkey.from_string(mint))], program)
        return str(pda)
    except Exception as exc:
        log.debug("Metadata PDA hesaplanamadi (%s): %s", mint, exc)
        return None


async def launch_time_onchain(mint: str) -> Tuple[Optional[float], Optional[str]]:
    """(launch_ts, launch_signature) - TEK RPC cagrisiyla.

    Mint'in imza gecmisini geriye sayfalamak bu is icin kullanilamaz: canli
    olcumde $324K'lik bir pump.fun tokeninin 400.000+ imzasi vardi ve 400 sayfa
    (120sn) bile launch'a ulasmadi.

    Metadata hesabi ise launch isleminde yazilir ve sonrasinda neredeyse hic
    dokunulmaz - olculen dort tokende 0-2 imza, 0.2-0.4 saniye. Ayni bilgi,
    bin kat ucuza.

    Metadata'si olmayan token (Token-2022 uzantisi kullananlar) icin None
    doner; cagiran Dexscreener'in pairCreatedAt degerine geri duser.
    """
    pda = metadata_pda(mint)
    if not pda:
        return None, None
    sigs = await rpc.get_signatures(pda, limit=SIG_PAGE)
    if not sigs or len(sigs) >= SIG_PAGE:
        return None, None      # bos ya da beklenmedik sekilde yogun: guvenme
    oldest = min(sigs, key=lambda s: (s.get("slot") or 0, s.get("blockTime") or 0))
    ts = float(oldest.get("blockTime") or 0)
    return (ts if ts > 0 else None), oldest.get("signature")


async def verify_ages(winners: Sequence[Winner], days: float) -> List[Winner]:
    """Gercek launch anini zincirden dogrula, eski tokenlari at.

    Dexscreener'in pairCreatedAt'i token'in degil O HAVUZUN yasidir; canli
    olcumde BONK "10.9 gunluk" (gercekte 1335) ve PUMP "6.9 gunluk" (gercekte
    402) gorunuyordu. Zincirdeki metadata hesabi bu yanilgiyi tek cagrida
    kesiyor.
    """
    oldest: Dict[str, float] = {}
    for winner in winners:
        ts, signature = await launch_time_onchain(winner.mint)
        if ts:
            oldest[winner.mint] = ts
            winner.launch_signature = signature

    fresh, stale = apply_true_ages(winners, oldest, days)
    for winner, age in stale:
        log.info("  ELENDI %-12s gercek yasi %.0f gun (yeni havuz acmis eski token)",
                 winner.symbol, age)
    if stale:
        state.bus.log("%d token elendi: yeni havuzu var ama kendisi eski" % len(stale), "warn")
    unverified = sum(1 for w in fresh if w.mint not in oldest)
    if unverified:
        log.info("  %d token zincirden dogrulanamadi, Dexscreener yasiyla devam", unverified)
    return fresh


# --------------------------------------------------------------------------- #
# Launch penceresi
# --------------------------------------------------------------------------- #
def tx_signer(tx: Dict[str, Any]) -> Optional[str]:
    """Islemin ilk imzalayani = alici. Token hesabi/curve PDA'si degil."""
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for key in keys:
        if isinstance(key, dict) and key.get("signer"):
            return key.get("pubkey")
    if keys and isinstance(keys[0], dict):
        return keys[0].get("pubkey")
    return None


async def signatures_in_window(mint: str, launch_ts: float, start: float, end: float,
                               max_pages: int) -> Tuple[List[Dict[str, Any]], bool]:
    """(penceredeki imzalar artan sirada, pencereye_ulasildi).

    getSignaturesForAddress her zaman yeniden eskiye gider, o yuzden launch
    penceresine varmak icin araya giren tum islemlerin uzerinden gecmek
    gerekiyor - maliyet token'in populerligiyle dogru orantili ve kacinilmaz.
    Butce dolarsa pencereye ulasilamadi demektir ve cagiran token'i ATLAMALI:
    yarim bir pencere, "erken alici" listesini sessizce carpitir.
    """
    collected: List[Dict[str, Any]] = []
    before: Optional[str] = None
    reached = False

    for _ in range(max_pages):
        page = await rpc.get_signatures(mint, limit=SIG_PAGE, before=before)
        if not page:
            reached = True       # daha eskisi yok
            break
        for sig in page:
            block_time = float(sig.get("blockTime") or 0)
            if not sig.get("err") and start <= block_time <= end:
                collected.append(sig)
        oldest_ts = float(page[-1].get("blockTime") or 0)
        if oldest_ts and oldest_ts <= launch_ts:
            reached = True       # launch anini gectik: pencere tam
            break
        if len(page) < SIG_PAGE:
            reached = True
            break
        before = page[-1]["signature"]

    collected.sort(key=lambda s: (s.get("slot") or 0, s.get("blockTime") or 0))
    return collected, reached


async def early_buyers(winner: Winner, window_min: float, skip_first_sec: float,
                       max_tx: int, max_pages: int) -> Tuple[Set[str], Optional[str]]:
    """(erken alici cuzdanlar, atlama_sebebi)."""
    launch_ts = winner.created_at
    if launch_ts <= 0:
        return set(), "launch zamani yok"

    start = launch_ts + skip_first_sec
    end = launch_ts + window_min * 60.0
    sigs, reached = await signatures_in_window(winner.mint, launch_ts, start, end, max_pages)
    if not reached:
        return set(), "launch penceresine ulasilamadi (%d sayfa yetmedi)" % max_pages
    window = sigs[:max_tx]
    if not window:
        return set(), "pencerede islem yok"

    buyers: Set[str] = set()
    for sig in window:
        tx = await rpc.get_transaction(sig["signature"])
        if not tx:
            continue
        signer = tx_signer(tx)
        if not signer:
            continue
        event = parse_wallet_tx(tx, signer)
        if event and event.side == "buy" and event.mint == winner.mint:
            buyers.add(signer)
    return buyers, None


# --------------------------------------------------------------------------- #
# Aday dosyasi
# --------------------------------------------------------------------------- #
def load_existing() -> List[Dict[str, Any]]:
    if not CANDIDATES_FILE.exists():
        return []
    try:
        raw = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("%s bozuk (%s) - dokunulmuyor", CANDIDATES_FILE.name, exc)
        raise
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out.append({"address": item, "source": "?", "note": ""})
        elif isinstance(item, dict) and item.get("address"):
            out.append(item)
    return out


def merge_candidates(existing: List[Dict[str, Any]], found: Dict[str, int]
                     ) -> Tuple[List[Dict[str, Any]], int, int]:
    """(birlesmis liste, eklenen, guncellenen). Saf fonksiyon.

    Elle eklenmis bir kaydin `source`/`note` alanlari EZILMEZ; sadece
    auto-discovery'nin kendi notu tazelenir. Aksi halde gece calisan batch,
    kullanicinin kendi notlarini silerdi.
    """
    merged = [dict(item) for item in existing]
    by_address = {item["address"]: item for item in merged}
    added = updated = 0

    for address, hits in sorted(found.items(), key=lambda kv: (-kv[1], kv[0])):
        note = "%d kazanan tokenda erken alici" % hits
        current = by_address.get(address)
        if current is None:
            entry = {"address": address, "source": "auto-discovery", "note": note}
            merged.append(entry)
            by_address[address] = entry
            added += 1
        elif current.get("source") == "auto-discovery" and current.get("note") != note:
            current["note"] = note
            updated += 1
    return merged, added, updated


def save_candidates(entries: List[Dict[str, Any]]) -> None:
    CANDIDATES_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")


# --------------------------------------------------------------------------- #
# Orkestrasyon
# --------------------------------------------------------------------------- #
async def discover(days: float = 14.0, min_mcap: float = 200_000.0, limit: int = 25,
                   window_min: float = 30.0, skip_first_sec: float = 3.0,
                   min_hits: int = 2, max_tx: int = 200, max_pages: int = 150,
                   search_terms: Sequence[str] = DEFAULT_SEARCH_TERMS,
                   max_mcap: Optional[float] = DEFAULT_MAX_MCAP,
                   dry_run: bool = False) -> Dict[str, int]:
    state.bus.log("Aday kesfi basladi: son %.0f gun, mcap $%s - $%s"
                  % (days, format(int(min_mcap), ","),
                     format(int(max_mcap), ",") if max_mcap else "sinirsiz"), "info")

    pairs = await collect_pairs(search_terms)
    # Havuz genis tutulur, sonra gercek yasa gore budanir: bir pair'in yasi
    # token'in degil o havuzun yasidir.
    shortlist = select_winners(pairs, days, min_mcap, limit * 2, max_mcap=max_mcap)
    winners = (await verify_ages(shortlist, days))[:limit]
    if not winners:
        state.bus.log("Filtreye uyan kazanan token bulunamadi - esikleri gevsetin", "warn")
        return {}

    state.bus.log("%d kazanan token secildi (havuz: %d pair)" % (len(winners), len(pairs)),
                  "success")
    for w in winners:
        log.info("  %-12s mcap $%-12s yas %.1f gun  %s",
                 w.symbol, format(int(w.market_cap), ","), w.age_days, w.mint)

    hits: Counter = Counter()
    per_token: Dict[str, int] = {}
    skipped_tokens: List[Tuple[str, str]] = []
    for i, winner in enumerate(winners, 1):
        state.bus.log("[%d/%d] %s launch penceresi taraniyor..."
                      % (i, len(winners), winner.symbol), "info")
        try:
            buyers, skipped = await early_buyers(winner, window_min, skip_first_sec,
                                                 max_tx, max_pages)
        except Exception as exc:
            log.error("%s taranamadi: %s", winner.symbol, exc)
            state.bus.log("%s taranamadi (%s)" % (winner.symbol, type(exc).__name__), "error")
            continue
        if skipped:
            skipped_tokens.append((winner.symbol, skipped))
            state.bus.log("%s ATLANDI: %s" % (winner.symbol, skipped), "warn")
            continue
        per_token[winner.symbol] = len(buyers)
        hits.update(buyers)
        state.bus.log("%s -> %d erken alici" % (winner.symbol, len(buyers)), "success")

    scanned = len(per_token)
    state.bus.log("Kapsam: %d/%d token tarandi, %d atlandi"
                  % (scanned, len(winners), len(skipped_tokens)),
                  "success" if scanned >= len(winners) / 2 else "warn")
    if skipped_tokens and scanned < len(winners):
        # Kesisim yontemi taranan token sayisina cok duyarli: 2 tokende
        # kesisim aramak, 20 tokendekinden cok daha zayif bir sinyaldir.
        state.bus.log("Kapsami artirmak icin: --max-pages yukseltin ya da --days dusurun "
                      "(genc tokenlarin launch penceresi cok daha ucuz)", "info")

    found = {address: count for address, count in hits.items() if count >= min_hits}
    state.bus.log("%d cuzdan %d+ kazananda kesisti (toplam %d erken alici goruldu)"
                  % (len(found), min_hits, len(hits)), "success" if found else "warn")

    if found and not dry_run:
        existing = load_existing()
        merged, added, updated = merge_candidates(existing, found)
        save_candidates(merged)
        state.bus.log("wallets_candidates.json: %d yeni, %d guncellendi, toplam %d"
                      % (added, updated, len(merged)), "success")
    return found


def _print_summary(found: Dict[str, int], dry_run: bool) -> None:
    if not found:
        print("\nEsiklere uyan aday cikmadi. --min-hits 1 ya da --days 21 deneyin.\n")
        return
    print("\n%-46s %s" % ("ADAY CUZDAN", "KAZANAN TOKEN SAYISI"))
    print("-" * 70)
    for address, hits in sorted(found.items(), key=lambda kv: (-kv[1], kv[0])):
        print("%-46s %d" % (address, hits))
    print()
    if dry_run:
        print("--dry-run: dosyaya yazilmadi.\n")
    else:
        print("Sirada: python -m backend.wallet_scorer\n")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m backend.wallet_discovery",
        description="Kazanan tokenlarin erken alicilarindan aday cuzdan uret.")
    p.add_argument("--days", type=float, default=14.0, help="token yasi tavani (gun)")
    p.add_argument("--min-mcap", type=float, default=200_000.0, help="minimum market cap ($)")
    p.add_argument("--max-mcap", type=float, default=DEFAULT_MAX_MCAP,
                   help="maksimum market cap ($); 0 = sinirsiz")
    p.add_argument("--limit", type=int, default=25, help="taranacak kazanan token sayisi")
    p.add_argument("--window-min", type=float, default=30.0, help="launch sonrasi pencere (dk)")
    p.add_argument("--skip-first-sec", type=float, default=3.0,
                   help="ilk N saniyedeki alicilar atlanir (MEV/sniper)")
    p.add_argument("--min-hits", type=int, default=2,
                   help="aday olmak icin kac kazananda gorunmeli")
    p.add_argument("--max-tx", type=int, default=200, help="token basina cozulecek islem tavani")
    p.add_argument("--max-pages", type=int, default=150,
                   help="launch'a ulasmak icin imza sayfasi butcesi (sayfa=1000)")
    p.add_argument("--search", action="append", default=None,
                   help="ek arama terimi (tekrarlanabilir)")
    p.add_argument("--dry-run", action="store_true", help="dosyaya yazma, sadece raporla")
    return p.parse_args(argv)


async def _main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        state.reload_env()
    except ImportError:
        pass

    args = _parse_args(argv)
    if not state.HELIUS_API_KEY:
        print("HELIUS_API_KEY yok - launch pencereleri public RPC ile taranamaz.")
        return 1

    terms = list(DEFAULT_SEARCH_TERMS) + list(args.search or [])
    db.init_db()
    try:
        found = await discover(
            days=args.days, min_mcap=args.min_mcap, limit=args.limit,
            window_min=args.window_min, skip_first_sec=args.skip_first_sec,
            min_hits=args.min_hits, max_tx=args.max_tx, max_pages=args.max_pages,
            search_terms=terms, max_mcap=args.max_mcap or None, dry_run=args.dry_run)
        _print_summary(found, args.dry_run)
    finally:
        await asyncio.gather(rpc.close(), analyzer.close(), return_exceptions=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
