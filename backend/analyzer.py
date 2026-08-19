"""New-coin safety analysis.

Data sources
------------
* Dexscreener (free, no key)  -> price, market cap, 5m volume, liquidity, dex id
* Helius/Solana RPC           -> supply, holder concentration, dev holdings,
                                 bundled + sniper buys around the launch slot

Dexscreener does NOT expose bundler/sniper/dev/lp-burn fields, so those are
derived on-chain here. Every metric that cannot be established stays None and
counts as a FAIL, per the "missing data = reject" rule.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

import rpc
import state

log = logging.getLogger("market-fucker")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Dex ids where liquidity sits in a program-owned curve/pool that cannot be pulled.
LOCKED_LP_DEXES = {"pumpfun", "pump.fun", "pumpswap", "moonshot"}
MAX_EARLY_TX = 40          # cap on launch-window transactions parsed per coin
SIG_PAGE_LIMIT = 5         # how far back the signature pager may walk
SNIPER_WINDOW_SEC = 15.0   # buys this soon after launch count as snipers
_early_sem = asyncio.Semaphore(5)

_dex_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _dex_client
    if _dex_client is None or _dex_client.is_closed:
        _dex_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    return _dex_client


async def close() -> None:
    global _dex_client
    if _dex_client is not None and not _dex_client.is_closed:
        await _dex_client.aclose()
    _dex_client = None


@dataclass
class Metrics:
    mint: str
    name: str = "?"
    symbol: str = "?"
    price_usd: Optional[float] = None
    price_native: Optional[float] = None
    market_cap: Optional[float] = None
    volume_5m: Optional[float] = None
    liquidity_usd: Optional[float] = None
    dex_id: Optional[str] = None
    pair_address: Optional[str] = None
    supply: Optional[float] = None
    lp_burned: Optional[bool] = None
    bundler: Optional[float] = None
    sniper: Optional[float] = None
    dev: Optional[float] = None
    top10: Optional[float] = None
    freeze_authority: Optional[str] = None

    def feed_row(self) -> Dict[str, Any]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "mcap": self.market_cap,
            "price": self.price_usd,
            "volume_5m": self.volume_5m,
            "liquidity": self.liquidity_usd,
            "bundler": self.bundler,
            "sniper": self.sniper,
            "dev": self.dev,
            "top10": self.top10,
            "lp_burned": self.lp_burned,
        }


@dataclass
class AnalysisResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Optional[Metrics] = None

    @property
    def reason_text(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "tum kontroller gecti"


# --------------------------------------------------------------------------- #
# Dexscreener
# --------------------------------------------------------------------------- #
async def fetch_dexscreener(mint: str, retries: int = 3) -> Optional[Dict[str, Any]]:
    """Best (deepest liquidity) pair for a mint. Retries 429 with a 3s wait."""
    url = state.DEXSCREENER_API + "/" + mint
    for attempt in range(retries):
        try:
            resp = await _client().get(url)
            if resp.status_code == 429:
                log.warning("Dexscreener 429, 3sn bekleniyor (%d/%d)", attempt + 1, retries)
                await asyncio.sleep(3)
                continue
            if resp.status_code != 200:
                await asyncio.sleep(1)
                continue
            pairs = (resp.json() or {}).get("pairs") or []
            if not pairs:
                return None
            return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        except Exception as exc:
            log.debug("Dexscreener hata (%s): %s", mint, exc)
            await asyncio.sleep(1)
    return None


def _fnum(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_pair(metrics: Metrics, pair: Dict[str, Any]) -> None:
    base = pair.get("baseToken") or {}
    metrics.name = base.get("name") or base.get("symbol") or metrics.name
    metrics.symbol = base.get("symbol") or metrics.symbol
    metrics.price_usd = _fnum(pair.get("priceUsd"))
    metrics.price_native = _fnum(pair.get("priceNative"))
    metrics.market_cap = _fnum(pair.get("marketCap")) or _fnum(pair.get("fdv"))
    metrics.volume_5m = _fnum((pair.get("volume") or {}).get("m5"))
    metrics.liquidity_usd = _fnum((pair.get("liquidity") or {}).get("usd"))
    metrics.dex_id = pair.get("dexId")
    metrics.pair_address = pair.get("pairAddress")
    # Some enriched responses carry these directly; use them when present.
    for src_key, dst in (("bundlerPercent", "bundler"), ("sniperPercent", "sniper"),
                         ("devHoldingsPercent", "dev"), ("top10HoldersPercent", "top10")):
        val = _fnum(pair.get(src_key))
        if val is not None:
            setattr(metrics, dst, val)
    if isinstance(pair.get("lpBurned"), bool):
        metrics.lp_burned = pair["lpBurned"]
    elif (metrics.dex_id or "").lower() in LOCKED_LP_DEXES:
        # Bonding-curve / program-owned liquidity: the dev cannot pull it.
        metrics.lp_burned = True


# --------------------------------------------------------------------------- #
# On-chain holder metrics
# --------------------------------------------------------------------------- #
async def _real_holders(mint: str) -> Tuple[Optional[float], Dict[str, float]]:
    """(circulating_supply, {wallet: ui_amount}) with program-owned pools removed."""
    supply_info = await rpc.get_token_supply(mint)
    if not supply_info:
        return None, {}
    supply = _fnum(supply_info.get("uiAmount")) or 0.0
    if supply <= 0:
        return None, {}

    largest = await rpc.get_token_largest_accounts(mint)
    if not largest:
        return supply, {}

    accounts = [a.get("address") for a in largest if a.get("address")][:20]
    parsed = await rpc.get_multiple_accounts_parsed(accounts)

    owner_of: Dict[str, str] = {}
    amount_of: Dict[str, float] = {}
    for acc_addr, acc in zip(accounts, parsed):
        if not acc:
            continue
        try:
            info = acc["data"]["parsed"]["info"]
            owner = info["owner"]
            amount = _fnum(info["tokenAmount"].get("uiAmount")) or 0.0
        except Exception:
            continue
        owner_of[acc_addr] = owner
        amount_of[owner] = amount_of.get(owner, 0.0) + amount

    # An owner that is itself a program-owned account (bonding curve / AMM
    # authority) holds pool liquidity, not a real holder position.
    owners = list({o for o in owner_of.values()})
    owner_accounts = await rpc.get_multiple_accounts_parsed(owners)
    pool_owners = set()
    for owner, acc in zip(owners, owner_accounts):
        if acc is None:
            continue  # uninitialised account = plain wallet
        if acc.get("owner") and acc.get("owner") != SYSTEM_PROGRAM:
            pool_owners.add(owner)

    pool_held = sum(amt for own, amt in amount_of.items() if own in pool_owners)
    holders = {own: amt for own, amt in amount_of.items() if own not in pool_owners}
    circulating = max(supply - pool_held, 0.0) or supply
    return circulating, holders


async def _oldest_signatures(mint: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Oldest page of signatures for a mint, ascending.

    getSignaturesForAddress returns newest-first, so page backwards until the
    end of history. Returns (signatures, complete); `complete` is False when the
    launch could not be reached, which callers must treat as missing data.
    """
    page_size = 100
    page = await rpc.get_signatures(mint, limit=page_size)
    if not page:
        return [], False
    pages = 1
    complete = len(page) < page_size
    while not complete and pages < SIG_PAGE_LIMIT:
        older = await rpc.get_signatures(mint, limit=page_size, before=page[-1]["signature"])
        pages += 1
        if not older:
            complete = True  # nothing older left: this page holds the launch
            break
        page = older
        complete = len(page) < page_size
    sigs = [s for s in page if not s.get("err")]
    sigs.sort(key=lambda s: (s.get("blockTime") or 0, s.get("slot") or 0))
    return sigs, complete


async def _early_buyers(mint: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(bundled_amount, sniped_amount, launch_time) from launch-window transactions.

    bundled = tokens bought inside the launch slot itself (bundled with create)
    sniped  = tokens bought within SNIPER_WINDOW_SEC after the launch

    Returns Nones when the launch window cannot be established or is busier than
    MAX_EARLY_TX - an unverifiable metric must fail the coin, not pass it.
    """
    sigs, complete = await _oldest_signatures(mint)
    if not sigs or not complete:
        return None, None, None
    launch = sigs[0]
    launch_slot = launch.get("slot")
    launch_time = launch.get("blockTime") or 0
    window = [s for s in sigs
              if s.get("slot") == launch_slot
              or (s.get("blockTime") or 0) - launch_time <= SNIPER_WINDOW_SEC]
    if len(window) > MAX_EARLY_TX:
        log.debug("Launch penceresi cok yogun (%d islem), metrik atlandi: %s", len(window), mint)
        return None, None, None

    async def load(sig: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        async with _early_sem:
            return sig, await rpc.get_transaction(sig["signature"])

    results = await asyncio.gather(*(load(s) for s in window), return_exceptions=True)

    bundled = 0.0
    sniped = 0.0
    for item in results:
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            continue
        sig, tx = item
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre = {}
        for bal in meta.get("preTokenBalances") or []:
            pre[(bal.get("owner"), bal.get("mint"))] = _fnum(
                (bal.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
        gained = 0.0
        for post in meta.get("postTokenBalances") or []:
            if post.get("mint") != mint:
                continue
            key = (post.get("owner"), post.get("mint"))
            after = _fnum((post.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            delta = after - pre.get(key, 0.0)
            if delta > 0:
                gained += delta
        if gained <= 0:
            continue
        if sig.get("slot") == launch_slot:
            bundled += gained
        elif (sig.get("blockTime") or 0) - launch_time <= SNIPER_WINDOW_SEC:
            sniped += gained

    return bundled, sniped, launch_time


async def _creator_of(mint: str) -> Optional[str]:
    """Fee payer of the oldest transaction touching the mint = launcher wallet."""
    sigs, complete = await _oldest_signatures(mint)
    if not sigs or not complete:
        return None
    tx = await rpc.get_transaction(sigs[0]["signature"])
    if not tx:
        return None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        for key in keys:
            if key.get("signer") and key.get("writable"):
                return key.get("pubkey")
        return keys[0].get("pubkey")
    except Exception:
        return None


async def collect_metrics(mint: str, creator: Optional[str] = None, deep: bool = True) -> Metrics:
    metrics = Metrics(mint=mint)
    pair = await fetch_dexscreener(mint)
    if pair:
        _apply_pair(metrics, pair)

    try:
        mint_acc = await rpc.get_account_parsed(mint)
        if mint_acc:
            info = ((mint_acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            metrics.freeze_authority = info.get("freezeAuthority")
    except Exception:
        pass

    if not deep:
        return metrics

    try:
        circulating, holders = await _real_holders(mint)
        metrics.supply = circulating
        if circulating and holders:
            top = sorted(holders.values(), reverse=True)[:10]
            if metrics.top10 is None:
                metrics.top10 = sum(top) / circulating * 100.0
            if metrics.dev is None:
                dev_wallet = creator or await _creator_of(mint)
                if dev_wallet:
                    dev_amount = holders.get(dev_wallet)
                    if dev_amount is None:
                        dev_amount = await rpc.get_token_balance_of_owner(dev_wallet, mint)
                    metrics.dev = dev_amount / circulating * 100.0
    except Exception as exc:
        log.debug("Holder analizi basarisiz (%s): %s", mint, exc)

    try:
        if metrics.bundler is None or metrics.sniper is None:
            bundled, sniped, _ = await _early_buyers(mint)
            base = metrics.supply
            if base and bundled is not None:
                if metrics.bundler is None:
                    metrics.bundler = min(bundled / base * 100.0, 100.0)
                if metrics.sniper is None:
                    metrics.sniper = min((sniped or 0.0) / base * 100.0, 100.0)
    except Exception as exc:
        log.debug("Erken alici analizi basarisiz (%s): %s", mint, exc)

    return metrics


# --------------------------------------------------------------------------- #
# Rule evaluation
# --------------------------------------------------------------------------- #
def evaluate(metrics: Metrics, cfg: state.Settings) -> AnalysisResult:
    reasons: List[str] = []

    def need(value: Optional[float], label: str) -> bool:
        if value is None:
            reasons.append(label + " verisi yok")
            return False
        return True

    if need(metrics.bundler, "bundler") and metrics.bundler >= cfg.max_bundler:
        reasons.append("bundler %.1f%% >= %.1f%%" % (metrics.bundler, cfg.max_bundler))
    if need(metrics.sniper, "sniper") and metrics.sniper >= cfg.max_sniper:
        reasons.append("sniper %.1f%% >= %.1f%%" % (metrics.sniper, cfg.max_sniper))
    if need(metrics.dev, "dev") and metrics.dev >= cfg.max_dev_holdings:
        reasons.append("dev %.1f%% >= %.1f%%" % (metrics.dev, cfg.max_dev_holdings))
    if need(metrics.top10, "top10") and metrics.top10 >= cfg.max_top10:
        reasons.append("top10 %.1f%% >= %.1f%%" % (metrics.top10, cfg.max_top10))

    if metrics.lp_burned is not True:
        reasons.append("LP burn dogrulanamadi" if metrics.lp_burned is None else "LP burn edilmemis")

    if need(metrics.market_cap, "mcap"):
        if metrics.market_cap < cfg.min_mcap:
            reasons.append("mcap $%.0f < $%.0f" % (metrics.market_cap, cfg.min_mcap))
        elif metrics.market_cap > cfg.max_mcap:
            reasons.append("mcap $%.0f > $%.0f" % (metrics.market_cap, cfg.max_mcap))

    if need(metrics.volume_5m, "5m hacim") and metrics.volume_5m <= cfg.min_volume_5m:
        reasons.append("5m hacim $%.0f <= $%.0f" % (metrics.volume_5m, cfg.min_volume_5m))

    if metrics.freeze_authority:
        reasons.append("freeze authority acik")

    if metrics.price_usd is None or metrics.price_usd <= 0:
        reasons.append("fiyat verisi yok")

    return AnalysisResult(passed=not reasons, reasons=reasons, metrics=metrics)


async def analyze(mint: str, creator: Optional[str] = None, delay: Optional[float] = None,
                  deep: bool = True) -> AnalysisResult:
    """Wait for launch data to populate, gather metrics, apply the rules."""
    cfg = state.settings
    wait = cfg.analyze_delay if delay is None else delay
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        metrics = await collect_metrics(mint, creator=creator, deep=deep)
    except Exception as exc:
        log.error("Analiz hatasi (%s): %s", mint, exc)
        return AnalysisResult(passed=False, reasons=["analiz hatasi: %s" % exc],
                              metrics=Metrics(mint=mint))
    return evaluate(metrics, cfg)


async def current_price(mint: str) -> Optional[float]:
    """USD price for an open position (used by the monitor loop)."""
    pair = await fetch_dexscreener(mint, retries=2)
    if not pair:
        return None
    return _fnum(pair.get("priceUsd"))
