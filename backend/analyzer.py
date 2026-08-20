"""New-coin safety analysis.

Data sources
------------
* pump.fun bonding curve (on-chain) -> price, market cap, SOL paid into the curve
* Dexscreener (free, no key)        -> price, market cap, 5m volume, liquidity, dex id
* Helius/Solana RPC                 -> supply, holder concentration, dev holdings,
                                       bundled + sniper buys around the launch slot

Dexscreener needs 30-90s to index a new mint, so the bonding curve is the
primary source for anything younger than that and Dexscreener only fills the
gaps (5m volume, pool liquidity) once it catches up.

Dexscreener does NOT expose bundler/sniper/dev/lp-burn fields, so those are
derived on-chain here. A metric that cannot be established stays None and counts
as a FAIL, per the "missing data = reject" rule - but a metric that IS knowable
on-chain must never be left at None just because an off-chain API was slow.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

import pumpfun
import rpc
import state

log = logging.getLogger("market-fucker")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Dex ids where liquidity sits in a program-owned curve/pool that cannot be pulled.
LOCKED_LP_DEXES = {"pumpfun", "pump.fun", "pumpswap", "moonshot"}
# A hot pump.fun launch takes well over a hundred trades in its first seconds, so
# this cap has to be generous: anything below it silently turns every busy coin
# into "unverifiable" (= rejected). Lower it only if the RPC plan cannot keep up.
MAX_EARLY_TX = 150         # launch-window transactions parsed per coin
# getSignaturesForAddress returns at most 1000 per page and a busy mint collects
# thousands of (mostly failed) sniper attempts within seconds. Paging 100 at a
# time never reached the launch, which is what emptied bundler/sniper.
SIG_PAGE_SIZE = 1000
SIG_PAGE_LIMIT = 15        # up to 15k signatures walked before giving up
SNIPER_WINDOW_SEC = 15.0   # buys this soon after launch count as snipers
_early_sem = asyncio.Semaphore(8)

_dex_client: Optional[httpx.AsyncClient] = None
_sol_price: Dict[str, float] = {"price": 0.0, "ts": 0.0}


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
    supply: Optional[float] = None            # circulating (pool holdings removed)
    total_supply: Optional[float] = None      # full mint supply
    curve_sol: Optional[float] = None         # real SOL paid into the bonding curve
    curve_complete: Optional[bool] = None     # curve filled -> migrated to a DEX
    lp_burned: Optional[bool] = None
    bundler: Optional[float] = None
    sniper: Optional[float] = None
    sniper_truncated: bool = False            # launch window bigger than MAX_EARLY_TX
    dev: Optional[float] = None
    top10: Optional[float] = None
    freeze_authority: Optional[str] = None
    source: str = "?"                         # where price/mcap came from

    def feed_row(self) -> Dict[str, Any]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "mcap": self.market_cap,
            "price": self.price_usd,
            "volume_5m": self.volume_5m,
            "liquidity": self.liquidity_usd,
            "curve_sol": self.curve_sol,
            "bundler": self.bundler,
            "sniper": self.sniper,
            "dev": self.dev,
            "top10": self.top10,
            "lp_burned": self.lp_burned,
            "source": self.source,
        }


@dataclass
class AnalysisResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Optional[Metrics] = None

    @property
    def reason_text(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "tum kontroller gecti"


def _fnum(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Dexscreener
# --------------------------------------------------------------------------- #
async def fetch_dexscreener(mint: str, retries: int = 3) -> Optional[Dict[str, Any]]:
    """Best (deepest liquidity) pair for a mint. Backs off on 429."""
    url = state.DEXSCREENER_API + "/" + mint
    for attempt in range(retries):
        try:
            resp = await _client().get(url)
            if resp.status_code == 429:
                wait = 3.0 * (attempt + 1)
                log.warning("Dexscreener 429, %.0fsn bekleniyor (%d/%d)", wait, attempt + 1, retries)
                await asyncio.sleep(wait)
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


async def sol_price_usd() -> float:
    """USD price of SOL, cached for a minute (used to price the bonding curve)."""
    now = time.time()
    if _sol_price["price"] > 0 and now - _sol_price["ts"] < 60:
        return _sol_price["price"]
    pair = await fetch_dexscreener(state.WSOL_MINT, retries=2)
    price = _fnum((pair or {}).get("priceUsd")) or 0.0
    if price > 0:
        _sol_price["price"] = price
        _sol_price["ts"] = now
    return _sol_price["price"]


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
    if metrics.price_usd:
        metrics.source = "dexscreener"
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


async def _apply_curve(metrics: Metrics, curve: "pumpfun.Curve") -> None:
    """Fill price / mcap / liquidity from the bonding curve.

    Only fills what Dexscreener has not already provided, except while the curve
    is still active - there the curve is the more accurate of the two.
    """
    metrics.curve_sol = curve.sol_in_curve
    metrics.curve_complete = curve.complete
    if metrics.total_supply is None:
        metrics.total_supply = curve.supply_ui
    # Liquidity sits in a program-owned curve until it migrates: not pullable.
    if metrics.lp_burned is None:
        metrics.lp_burned = True

    if curve.complete:
        return  # migrated to a real pool - Dexscreener is authoritative from here

    price_sol = curve.price_sol
    mcap_sol = curve.market_cap_sol()
    if price_sol is None or mcap_sol is None:
        return
    sol_usd = await sol_price_usd()
    if sol_usd <= 0:
        log.debug("SOL fiyati alinamadi, curve metrikleri USD'ye cevrilemedi")
        return
    if metrics.price_usd is None or metrics.price_usd <= 0:
        metrics.price_usd = price_sol * sol_usd
        metrics.source = "bonding-curve"
    if metrics.price_native is None:
        metrics.price_native = price_sol
    if metrics.market_cap is None:
        metrics.market_cap = mcap_sol * sol_usd
    if metrics.liquidity_usd is None:
        # Both sides of the curve: SOL paid in plus the tokens backing it.
        metrics.liquidity_usd = curve.sol_in_curve * sol_usd * 2.0


# --------------------------------------------------------------------------- #
# On-chain holder metrics
# --------------------------------------------------------------------------- #
async def _real_holders(mint: str) -> Tuple[Optional[float], Optional[float],
                                            Optional[Dict[str, float]]]:
    """(total_supply, circulating_supply, {wallet: ui_amount}) minus program pools.

    The holder map is None when the chain could not be asked, and {} when it was
    asked and there genuinely are no non-pool holders yet - a brand new coin that
    nobody has bought holds 0% in top10, which is data, not a gap.
    """
    supply_info = await rpc.get_token_supply(mint)
    if not supply_info:
        return None, None, None
    supply = _fnum(supply_info.get("uiAmount")) or 0.0
    if supply <= 0:
        return None, None, None

    largest = await rpc.get_token_largest_accounts(mint)
    if not largest:
        return supply, None, None  # cagri basarisiz: holder verisi YOK

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
    circulating = max(supply - pool_held, 0.0)
    if circulating <= 0:
        circulating = None  # everything still in the curve: top10 is meaningless
    return supply, circulating, holders


async def _signatures_since_launch(mint: str, launch_signature: str,
                                   launch_tx: Optional[Dict[str, Any]] = None
                                   ) -> Tuple[List[Dict[str, Any]], bool]:
    """Signatures immediately following the known create transaction, ascending.

    Anchoring on the launch signature is far more reliable than paging back
    through history, but the RPC still answers newest-first: the launch window is
    on the LAST page, so only that page is kept. Pages are 1000 wide (the RPC
    maximum) because a hot mint collects thousands of failed sniper attempts in
    its first seconds and a 100-wide pager never got anywhere near the launch.
    """
    tail: List[Dict[str, Any]] = []
    before: Optional[str] = None
    complete = False
    for _ in range(SIG_PAGE_LIMIT):
        page = await rpc.get_signatures(mint, limit=SIG_PAGE_SIZE, before=before,
                                        until=launch_signature)
        if not page:
            complete = True
            break
        tail = page  # oldest page seen so far; the one before the launch
        if len(page) < SIG_PAGE_SIZE:
            complete = True
            break
        before = page[-1]["signature"]

    if launch_tx is None:
        launch_tx = await rpc.get_transaction(launch_signature)
    if not launch_tx:
        return [], False
    launch_entry = {"signature": launch_signature, "slot": launch_tx.get("slot"),
                    "blockTime": launch_tx.get("blockTime"),
                    "err": (launch_tx.get("meta") or {}).get("err")}

    sigs = [s for s in tail if not s.get("err")]
    sigs.append(launch_entry)
    sigs.sort(key=lambda s: (s.get("slot") or 0, s.get("blockTime") or 0))
    return sigs, complete


async def _oldest_signatures(mint: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Oldest page of signatures for a mint, ascending (no launch signature known).

    getSignaturesForAddress returns newest-first, so page backwards until the
    end of history. Returns (signatures, complete); `complete` is False when the
    launch could not be reached, which callers must treat as missing data.
    """
    page = await rpc.get_signatures(mint, limit=SIG_PAGE_SIZE)
    if not page:
        return [], False
    pages = 1
    complete = len(page) < SIG_PAGE_SIZE
    while not complete and pages < SIG_PAGE_LIMIT:
        older = await rpc.get_signatures(mint, limit=SIG_PAGE_SIZE, before=page[-1]["signature"])
        pages += 1
        if not older:
            complete = True  # nothing older left: this page holds the launch
            break
        page = older
        complete = len(page) < SIG_PAGE_SIZE
    sigs = [s for s in page if not s.get("err")]
    sigs.sort(key=lambda s: (s.get("slot") or 0, s.get("blockTime") or 0))
    return sigs, complete


def _split_launch_window(sigs: List[Dict[str, Any]]
                         ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, bool]:
    """(launch_slot_sigs, sniper_sigs, launch_time, truncated) for a sorted list.

    Signatures without a blockTime are only trusted for the slot comparison; the
    old code let `blockTime=None` fall through as 0 and swept unrelated history
    into the sniper window.
    """
    launch = sigs[0]
    launch_slot = launch.get("slot")
    launch_time = int(launch.get("blockTime") or 0)

    bundled = [s for s in sigs if s.get("slot") == launch_slot]
    sniped: List[Dict[str, Any]] = []
    if launch_time > 0:
        for s in sigs:
            if s.get("slot") == launch_slot:
                continue
            bt = s.get("blockTime")
            if bt is None:
                continue
            if 0 <= int(bt) - launch_time <= SNIPER_WINDOW_SEC:
                sniped.append(s)

    truncated = False
    budget = MAX_EARLY_TX - len(bundled)
    if budget < 0:
        # Even the launch slot alone blows the budget - keep it, it is the metric
        # that matters most, but flag the result as partial.
        bundled = bundled[:MAX_EARLY_TX]
        sniped = []
        truncated = True
    elif len(sniped) > budget:
        # Earliest snipers first: they are the aggressive ones.
        sniped = sniped[:budget]
        truncated = True
    return bundled, sniped, launch_time, truncated


async def _early_buyers(mint: str, launch_signature: Optional[str] = None,
                        launch_tx: Optional[Dict[str, Any]] = None,
                        total_supply: Optional[float] = None
                        ) -> Tuple[Optional[float], Optional[float], Optional[int], bool]:
    """(bundled_amount, sniped_amount, launch_time, truncated) for the launch window.

    bundled = tokens bought inside the launch slot itself (bundled with create)
    sniped  = tokens bought within SNIPER_WINDOW_SEC after the launch

    Returns Nones only when the launch window genuinely cannot be established.
    A window busier than MAX_EARLY_TX is NOT missing data: the amounts are
    computed from as much of it as the budget allows and `truncated` is set, so
    the caller can reject on "too busy" instead of on "no data".
    """
    if launch_signature:
        sigs, complete = await _signatures_since_launch(mint, launch_signature, launch_tx)
    else:
        sigs, complete = await _oldest_signatures(mint)
    if not sigs or not complete:
        return None, None, None, False
    if launch_tx is None:
        launch_tx = await rpc.get_transaction(sigs[0]["signature"])

    bundled_sigs, sniped_sigs, launch_time, truncated = _split_launch_window(sigs)
    launch_slot = sigs[0].get("slot")
    window = bundled_sigs + sniped_sigs
    if truncated:
        log.debug("Launch penceresi cok yogun, kismi analiz: %s", mint)

    # The create transaction credits the bonding curve with the entire supply.
    # Counting that as a "bundled buy" pinned bundler at 100% on every single
    # pump.fun launch, so the curve's own token account is excluded.
    curve_owner = pumpfun.curve_from_launch_tx(launch_tx, mint) if launch_tx else None
    # Fallback for launchpads whose curve we cannot identify: no real buyer takes
    # half the supply in one transaction - that is a pool being seeded.
    pool_cutoff = (total_supply * 0.5) if total_supply else None

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
            if curve_owner and post.get("owner") == curve_owner:
                continue
            key = (post.get("owner"), post.get("mint"))
            after = _fnum((post.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            delta = after - pre.get(key, 0.0)
            if delta > 0:
                if pool_cutoff is not None and delta >= pool_cutoff:
                    continue  # havuz besleniyor, alim degil
                gained += delta
        if gained <= 0:
            continue
        if sig.get("slot") == launch_slot:
            bundled += gained
        else:
            sniped += gained

    return bundled, sniped, launch_time, truncated


async def _creator_of(mint: str, launch_tx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Fee payer of the create transaction = launcher wallet.

    Uses the known launch transaction when there is one; walking the whole
    signature history just to find the creator is not worth the RPC budget.
    """
    tx = launch_tx
    if tx is None:
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


async def collect_metrics(mint: str, creator: Optional[str] = None, deep: bool = True,
                          launch_signature: Optional[str] = None) -> Metrics:
    metrics = Metrics(mint=mint)

    # The create transaction is needed by three different steps; fetch it once.
    launch_tx = await rpc.get_transaction(launch_signature) if launch_signature else None

    pair = await fetch_dexscreener(mint)
    if pair:
        _apply_pair(metrics, pair)

    try:
        mint_acc = await rpc.get_account_parsed(mint)
        if mint_acc:
            info = ((mint_acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            metrics.freeze_authority = info.get("freezeAuthority")
            decimals = int(info.get("decimals") or 6)
        else:
            decimals = 6
    except Exception:
        decimals = 6

    # Bonding curve: the only source that works on a 20-second-old coin.
    try:
        curve = await pumpfun.read_curve(mint, launch_tx=launch_tx, decimals=decimals)
        if curve:
            await _apply_curve(metrics, curve)
    except Exception as exc:
        log.debug("Bonding curve okunamadi (%s): %s", mint, exc)

    if metrics.name in (None, "?"):
        # Dexscreener indekslemeden once coinin adi sadece metadata'da duruyor;
        # panelde her satirin "?" gorunmesini engeller.
        meta = await rpc.get_asset_metadata(mint)
        if meta:
            metrics.name = meta.get("name") or metrics.name
            metrics.symbol = meta.get("symbol") or metrics.symbol

    if not deep:
        return metrics

    try:
        total, circulating, holders = await _real_holders(mint)
        if total:
            metrics.total_supply = total
        metrics.supply = circulating
        if metrics.total_supply and holders is not None and metrics.top10 is None:
            # Toplam arza gore: yeni bir coinde curve disi float tek bir alicidan
            # ibaret oldugu icin float'a gore olcum her zaman %100 veriyordu.
            top = sorted(holders.values(), reverse=True)[:10]
            metrics.top10 = min(sum(top) / metrics.total_supply * 100.0, 100.0)
        # Dev holdings are conventionally a share of the FULL supply, not of the
        # thin float outside the curve - measuring against the float reported
        # 80%+ for ordinary launches and rejected everything.
        if metrics.total_supply and holders is not None and metrics.dev is None:
            dev_wallet = creator or await _creator_of(mint, launch_tx=launch_tx)
            if dev_wallet:
                dev_amount = holders.get(dev_wallet)
                if dev_amount is None:
                    dev_amount = await rpc.get_token_balance_of_owner(dev_wallet, mint)
                metrics.dev = min(dev_amount / metrics.total_supply * 100.0, 100.0)
    except Exception as exc:
        log.debug("Holder analizi basarisiz (%s): %s", mint, exc)

    try:
        if metrics.bundler is None or metrics.sniper is None:
            bundled, sniped, _, truncated = await _early_buyers(
                mint, launch_signature=launch_signature, launch_tx=launch_tx,
                total_supply=metrics.total_supply)
            metrics.sniper_truncated = truncated
            # Same reasoning as dev holdings: bundle/snipe shares are quoted
            # against total supply everywhere else in this ecosystem.
            base = metrics.total_supply
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
    if metrics.sniper_truncated:
        # More buys in the launch window than the parser budget: the measured
        # share is a lower bound, so a "pass" here would be unearned.
        reasons.append("launch penceresi asiri yogun (>%d islem)" % MAX_EARLY_TX)
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

    # Demand check. A coin younger than Dexscreener's indexing lag has no 5m
    # volume at all, so the on-chain SOL paid into the curve is the primary
    # measure and the 5m volume only applies once it actually exists.
    if metrics.curve_sol is not None and not metrics.curve_complete:
        if metrics.curve_sol < cfg.min_curve_sol:
            reasons.append("curve %.2f SOL < %.2f SOL" % (metrics.curve_sol, cfg.min_curve_sol))
        if metrics.curve_sol > cfg.max_curve_sol:
            reasons.append("curve %.2f SOL > %.2f SOL" % (metrics.curve_sol, cfg.max_curve_sol))
        if metrics.volume_5m is not None and metrics.volume_5m <= cfg.min_volume_5m:
            reasons.append("5m hacim $%.0f <= $%.0f" % (metrics.volume_5m, cfg.min_volume_5m))
    elif need(metrics.volume_5m, "5m hacim") and metrics.volume_5m <= cfg.min_volume_5m:
        reasons.append("5m hacim $%.0f <= $%.0f" % (metrics.volume_5m, cfg.min_volume_5m))

    if metrics.freeze_authority:
        reasons.append("freeze authority acik")

    if metrics.price_usd is None or metrics.price_usd <= 0:
        reasons.append("fiyat verisi yok")

    return AnalysisResult(passed=not reasons, reasons=reasons, metrics=metrics)


async def analyze(mint: str, creator: Optional[str] = None, delay: Optional[float] = None,
                  deep: bool = True, launch_signature: Optional[str] = None) -> AnalysisResult:
    """Wait for launch data to populate, gather metrics, apply the rules."""
    cfg = state.settings
    wait = cfg.analyze_delay if delay is None else delay
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        metrics = await collect_metrics(mint, creator=creator, deep=deep,
                                        launch_signature=launch_signature)
    except Exception as exc:
        log.error("Analiz hatasi (%s): %s", mint, exc)
        return AnalysisResult(passed=False, reasons=["analiz hatasi: %s" % exc],
                              metrics=Metrics(mint=mint))
    return evaluate(metrics, cfg)


async def current_price(mint: str) -> Optional[float]:
    """USD price for an open position (used by the monitor loop).

    Falls back to the bonding curve while the coin is too young (or too small)
    for Dexscreener - otherwise open positions would sit with a stale price and
    neither the tiers nor the stop loss would ever fire.
    """
    pair = await fetch_dexscreener(mint, retries=2)
    price = _fnum((pair or {}).get("priceUsd"))
    if price and price > 0:
        return price
    if mint == state.WSOL_MINT:
        return None
    try:
        curve = await pumpfun.read_curve(mint)
        if curve and not curve.complete:
            price_sol = curve.price_sol
            sol_usd = await sol_price_usd()
            if price_sol and sol_usd > 0:
                return price_sol * sol_usd
    except Exception as exc:
        log.debug("Curve fiyati alinamadi (%s): %s", mint, exc)
    return None
