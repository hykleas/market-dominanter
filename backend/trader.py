"""Trade execution and position management.

Two execution paths, one strategy: PAPER (simulated fills against real
Dexscreener prices, balance kept in settings.json) and LIVE (Jupiter v6 swaps
signed with the wallet key). Filters, tiers, trailing stop and bookkeeping are
identical in both; only the fill differs.

Exit strategy per position:
  * stop loss  - full exit if price falls stop_loss% below entry BEFORE tier 1
  * tier 1/2/3 - sell a slice at each multiple of the entry price
  * trailing   - after tier 1, exit the rest when price drops trailing_stop%
                 below the position ATH
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

from typing import Any, Dict, Optional, Tuple

import httpx

import analyzer
import database as db
import rpc
import state

log = logging.getLogger("market-fucker")

try:  # solders is only required for live trading
    from solders.keypair import Keypair  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore
    SOLDERS_OK = True
except Exception:  # pragma: no cover - missing optional dep
    Keypair = None  # type: ignore
    VersionedTransaction = None  # type: ignore
    SOLDERS_OK = False

MONITOR_INTERVAL = 10.0
_client: Optional[httpx.AsyncClient] = None
_keypair: Any = None
_wallet_pubkey: Optional[str] = None
_decimals_cache: Dict[str, int] = {}
_sell_locks: Dict[int, asyncio.Lock] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# --------------------------------------------------------------------------- #
# Wallet
# --------------------------------------------------------------------------- #
def load_wallet() -> Optional[str]:
    """Load the signing keypair from WALLET_PRIVATE_KEY. Never logs the key."""
    global _keypair, _wallet_pubkey
    _keypair = None
    _wallet_pubkey = None
    secret = state.WALLET_PRIVATE_KEY
    if not secret:
        log.warning("WALLET_PRIVATE_KEY yok - sadece PAPER modu kullanilabilir")
        return None
    if not SOLDERS_OK:
        log.error("solders kurulu degil - sadece PAPER modu kullanilabilir")
        return None
    try:
        if secret.strip().startswith("["):
            import json
            _keypair = Keypair.from_bytes(bytes(json.loads(secret)))
        else:
            _keypair = Keypair.from_base58_string(secret)
        _wallet_pubkey = str(_keypair.pubkey())
        log.info("Cuzdan yuklendi: %s", _wallet_pubkey)
        return _wallet_pubkey
    except Exception as exc:
        # Deliberately does not include the key material in the message.
        log.error("Cuzdan yuklenemedi (private key formati hatali): %s", type(exc).__name__)
        return None


def wallet_pubkey() -> Optional[str]:
    return _wallet_pubkey


def can_go_live() -> bool:
    return _keypair is not None and SOLDERS_OK


def is_paper() -> bool:
    """Paper unless the panel switched to live AND a real wallet is available."""
    if os.getenv("PAPER_TRADING", "").lower() in ("1", "true", "yes"):
        return True
    if state.settings.paper_trading:
        return True
    return not can_go_live()


async def wallet_balance() -> float:
    if not _wallet_pubkey:
        return 0.0
    try:
        return await rpc.get_balance_sol(_wallet_pubkey)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Paper wallet
# --------------------------------------------------------------------------- #
async def ensure_paper_funded() -> None:
    """Fund the simulated wallet on first use (paper_start_usd worth of SOL)."""
    cfg = state.settings
    if cfg.paper_balance_sol > 0 or cfg.paper_funded_sol > 0:
        return
    await reset_paper_wallet(wipe_history=False)


async def reset_paper_wallet(start_usd: Optional[float] = None, wipe_history: bool = True) -> Dict[str, Any]:
    cfg = state.settings
    if start_usd is not None and start_usd > 0:
        cfg.paper_start_usd = float(start_usd)
    sol_usd = await sol_price_usd()
    if sol_usd <= 0:
        state.bus.log("SOL fiyati alinamadi, paper cuzdan fonlanamadi", "error")
        return paper_wallet()
    funded = cfg.paper_start_usd / sol_usd
    cfg.paper_balance_sol = funded
    cfg.paper_funded_sol = funded
    cfg.save()
    if wipe_history:
        db.reset_paper_history()
    state.bus.log("Paper cuzdan sifirlandi: %.4f SOL ($%.2f)" % (funded, cfg.paper_start_usd), "warn")
    await push_balance()
    return paper_wallet()


def paper_wallet() -> Dict[str, Any]:
    cfg = state.settings
    st = db.stats(is_paper=True)
    return {
        "balance_sol": cfg.paper_balance_sol,
        "funded_sol": cfg.paper_funded_sol,
        "start_usd": cfg.paper_start_usd,
        "pnl_today_sol": st["today_pnl_sol"],
        "pnl_all_sol": st["total_pnl_sol"],
        "trades": st["total_trades"],
        "win_rate": st["win_rate"],
        "fees_sol": st["total_fees_sol"],
    }


def _credit_paper(amount_sol: float) -> None:
    state.settings.paper_balance_sol = max(state.settings.paper_balance_sol + amount_sol, 0.0)
    state.settings.save()


async def push_balance() -> None:
    cfg = state.settings
    sol_usd = await sol_price_usd()
    state.bus.publish("balance_update", {
        "sol_balance": await wallet_balance(),
        "wallet": _wallet_pubkey,
        "paper": is_paper(),
        "can_go_live": can_go_live(),
        "sol_usd": sol_usd,
        "paper_wallet": paper_wallet(),
    })


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #
def _fees(amount_sol: float) -> Tuple[float, float]:
    """(platform_fee, fixed_fee) in SOL for a simulated trade of `amount_sol`."""
    cfg = state.settings
    platform = abs(amount_sol) * cfg.fee_platform_pct / 100.0
    fixed = cfg.fee_network_sol + cfg.fee_priority_sol
    return platform, fixed


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def token_decimals(mint: str) -> int:
    if mint in _decimals_cache:
        return _decimals_cache[mint]
    info = await rpc.get_token_supply(mint)
    dec = int((info or {}).get("decimals") or 6)
    _decimals_cache[mint] = dec
    return dec


async def sol_price_usd() -> float:
    return await analyzer.sol_price_usd()


def _paper_slippage(amount_usd: float, liquidity_usd: Optional[float]) -> float:
    """Fraction the simulated fill is worse than the quoted price.

    A flat base rate plus a size-vs-liquidity impact term. Without this the paper
    PnL sat on the optimistic side of reality on every single trade, which is the
    one thing a simulator must not do.
    """
    slip = max(state.settings.paper_slippage_pct, 0.0) / 100.0
    if liquidity_usd and liquidity_usd > 0 and amount_usd > 0:
        slip += min(amount_usd / liquidity_usd, 0.25)
    return min(slip, 0.5)


async def raw_token_balance(mint: str) -> int:
    """Raw (integer) token amount held by the wallet for a mint."""
    if not _wallet_pubkey:
        return 0
    res = await rpc.call(
        "getTokenAccountsByOwner",
        [_wallet_pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    total = 0
    if isinstance(res, dict):
        for acc in res.get("value", []) or []:
            try:
                total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except Exception:
                continue
    return total


# --------------------------------------------------------------------------- #
# Jupiter
# --------------------------------------------------------------------------- #
async def get_quote(input_mint: str, output_mint: str, amount: int,
                    slippage_bps: Optional[int] = None) -> Optional[Dict[str, Any]]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "slippageBps": str(slippage_bps if slippage_bps is not None else state.settings.slippage_bps),
        "onlyDirectRoutes": "false",
    }
    try:
        resp = await client().get(state.JUPITER_API + "/quote", params=params)
        if resp.status_code != 200:
            log.error("Jupiter quote basarisiz (%s): %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        if not data or not data.get("outAmount"):
            log.error("Jupiter quote bos dondu")
            return None
        return data
    except Exception as exc:
        log.error("Jupiter quote hatasi: %s", exc)
        return None


async def execute_swap(quote: Dict[str, Any]) -> Optional[str]:
    """Build, sign and send the swap. Returns the signature, or None on failure."""
    if _keypair is None or not SOLDERS_OK:
        return None
    body = {
        "quoteResponse": quote,
        "userPublicKey": _wallet_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        resp = await client().post(state.JUPITER_API + "/swap", json=body)
        if resp.status_code != 200:
            log.error("Jupiter swap basarisiz (%s): %s", resp.status_code, resp.text[:200])
            return None
        swap_tx = (resp.json() or {}).get("swapTransaction")
        if not swap_tx:
            log.error("Jupiter swap islemi dondurmedi")
            return None
    except Exception as exc:
        log.error("Jupiter swap hatasi: %s", exc)
        return None

    try:
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
        signed = VersionedTransaction(unsigned.message, [_keypair])
        payload = base64.b64encode(bytes(signed)).decode("utf-8")
    except Exception as exc:
        log.error("Islem imzalanamadi: %s", type(exc).__name__)
        return None

    signature = await rpc.send_raw_transaction(payload)
    if not signature:
        log.error("Islem gonderilemedi (RPC reddetti)")
        return None
    if not await rpc.confirm_signature(signature, timeout=60):
        log.error("Islem onaylanmadi: %s", signature)
        return None
    return signature


# --------------------------------------------------------------------------- #
# Buy
# --------------------------------------------------------------------------- #
async def buy(mint: str, name: str = "?", price_hint: Optional[float] = None,
              liquidity_hint: Optional[float] = None) -> Optional[int]:
    """Open a position worth `settings.auto_buy_sol`. Returns the position id."""
    cfg = state.settings
    if db.has_open_position(mint):
        state.bus.log("Zaten acik pozisyon var, atlandi: " + name, "warn")
        return None

    amount_sol = float(cfg.auto_buy_sol)
    entry_price = price_hint or await analyzer.current_price(mint) or 0.0
    if entry_price <= 0:
        state.bus.log("Giris fiyati alinamadi, alim atlandi: " + name, "error")
        return None

    if is_paper():
        return await _paper_buy(mint, name, amount_sol, entry_price, liquidity_hint)
    return await _live_buy(mint, name, amount_sol, entry_price)


async def _paper_buy(mint: str, name: str, amount_sol: float, entry_price: float,
                     liquidity_usd: Optional[float] = None) -> Optional[int]:
    cfg = state.settings
    await ensure_paper_funded()
    platform_fee, fixed_fee = _fees(amount_sol)
    total_cost = amount_sol + fixed_fee
    if cfg.paper_balance_sol < total_cost:
        state.bus.log("Paper bakiye yetersiz (%.4f SOL), alim atlandi" % cfg.paper_balance_sol, "warn")
        return None

    sol_usd = await sol_price_usd()
    if sol_usd <= 0:
        state.bus.log("SOL fiyati alinamadi, paper alim atlandi", "error")
        return None
    # Fill worse than quote: base slippage + price impact of our own size.
    slip = _paper_slippage(amount_sol * sol_usd, liquidity_usd)
    fill_price = entry_price * (1.0 + slip)
    tokens = (amount_sol - platform_fee) * sol_usd / fill_price

    _credit_paper(-total_cost)
    pos_id = db.add_position(mint, name, fill_price, tokens, amount_sol, is_paper=True,
                             fees_sol=platform_fee + fixed_fee, trailing_pct=cfg.trailing_stop)
    state.bot_state.coins_bought += 1
    state.bus.log("[PAPER] ALINDI %s - %.4f SOL @ $%.8f (slipaj %%%.2f, fee %.5f SOL)"
                  % (name, amount_sol, fill_price, slip * 100.0, platform_fee + fixed_fee), "success")
    state.bus.publish("position_opened", position_payload(db.get_position(pos_id)))
    await push_balance()
    return pos_id


async def _live_buy(mint: str, name: str, amount_sol: float, entry_price: float) -> Optional[int]:
    cfg = state.settings
    lamports = int(amount_sol * state.LAMPORTS_PER_SOL)
    balance = await wallet_balance()
    if balance < amount_sol + 0.003:
        state.bus.log("Yetersiz bakiye (%.4f SOL), alim atlandi" % balance, "error")
        return None

    quote = await get_quote(state.WSOL_MINT, mint, lamports)
    if not quote:
        state.bus.log("Quote alinamadi, alim atlandi: " + name, "error")
        return None

    signature = await execute_swap(quote)
    if not signature:
        state.bus.log("Alim islemi basarisiz: " + name, "error")
        return None

    decimals = await token_decimals(mint)
    try:
        tokens = int(quote["outAmount"]) / (10 ** decimals)
    except Exception:
        tokens = 0.0

    pos_id = db.add_position(mint, name, entry_price, tokens, amount_sol, is_paper=False,
                             trailing_pct=cfg.trailing_stop)
    state.bot_state.coins_bought += 1
    state.bus.log("ALINDI %s - %.4f SOL @ $%.8f (tx %s)" % (name, amount_sol, entry_price, signature[:8]),
                  "success")
    state.bus.publish("position_opened", position_payload(db.get_position(pos_id)))
    await push_balance()
    return pos_id


# --------------------------------------------------------------------------- #
# Sell
# --------------------------------------------------------------------------- #
async def sell_portion(position_id: int, percent: float, tier: str = "manual") -> bool:
    """Sell `percent` of the ORIGINAL position size. Execution follows the mode
    the position was opened in, so paper positions never touch the chain."""
    lock = _sell_locks.setdefault(position_id, asyncio.Lock())
    async with lock:
        pos = db.get_position(position_id)
        if not pos or pos.get("status") != "open":
            return False

        mint = pos["mint"]
        name = pos.get("name") or mint[:6]
        original_tokens = float(pos.get("amount_token") or 0.0)
        remaining_tokens = float(pos.get("remaining_token") or original_tokens)
        sold_so_far = float(pos.get("total_sold_percent") or 0.0)
        percent = max(0.0, min(percent, 100.0 - sold_so_far))
        if percent <= 0:
            return False

        tokens_to_sell = min(original_tokens * percent / 100.0, remaining_tokens)
        exit_price = await analyzer.current_price(mint) or float(pos.get("current_price") or 0.0)
        entry_price = float(pos.get("entry_price") or 0.0)
        cost_portion = float(pos.get("amount_sol") or 0.0) * percent / 100.0
        paper = bool(pos.get("is_paper"))

        if paper:
            sol_usd = await sol_price_usd()
            slip = _paper_slippage(0.0, None)
            fill_price = exit_price * (1.0 - slip)
            gross = (tokens_to_sell * fill_price / sol_usd) if (sol_usd > 0 and fill_price > 0) else (
                cost_portion * (fill_price / entry_price if entry_price > 0 else 1.0))
            platform_fee, fixed_fee = _fees(gross)
            net = max(gross - platform_fee - fixed_fee, 0.0)
            fees_sol = platform_fee + fixed_fee
            _credit_paper(net)
            sol_received = net
            exit_price = fill_price  # book the price actually filled at
            state.bus.log("[PAPER] SATIS %s %s %%%.0f -> %.5f SOL (fee %.5f)"
                          % (name, tier, percent, net, fees_sol), "info")
        else:
            raw_total = await raw_token_balance(mint)
            if raw_total <= 0:
                state.bus.log("Cuzdanda token yok, pozisyon kapatiliyor: " + name, "warn")
                sol_received, fees_sol = 0.0, 0.0
            else:
                fraction = percent / max(100.0 - sold_so_far, 1e-9)
                raw_to_sell = int(raw_total * min(fraction, 1.0))
                if raw_to_sell <= 0:
                    state.bus.log("Satilacak miktar cok kucuk: " + name, "warn")
                    return False
                quote = await get_quote(mint, state.WSOL_MINT, raw_to_sell)
                if not quote:
                    state.bus.log("Satis quote alinamadi: " + name, "error")
                    return False
                signature = await execute_swap(quote)
                if not signature:
                    state.bus.log("Satis islemi basarisiz: " + name, "error")
                    return False
                sol_received = int(quote["outAmount"]) / state.LAMPORTS_PER_SOL
                fees_sol = 0.0
                state.bus.log("SATIS %s %s %%%.0f -> %.5f SOL (tx %s)"
                              % (name, tier, percent, sol_received, signature[:8]), "info")

        trade = db.record_partial_sell(position_id, percent, exit_price, sol_received, tier,
                                       fees_sol=fees_sol, tokens_sold=tokens_to_sell)
        if not trade:
            return False

        trade["paper"] = paper
        state.bus.publish("trade_closed", {
            "name": name,
            "mint": mint,
            "tier": tier,
            "sold_percent": percent,
            "pnl_sol": trade["pnl_sol"],
            "pnl_percent": trade["pnl_percent"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "exit_time": trade["exit_time"],
            "is_paper": 1 if paper else 0,
            "closed": trade["closed"],
        })
        tag = "success" if trade["pnl_sol"] >= 0 else "error"
        state.bus.log("%s%s | %s %%%.0f | PnL %.2f%% (%.4f SOL)"
                      % ("[PAPER] " if paper else "", name, tier, percent,
                         trade["pnl_percent"], trade["pnl_sol"]), tag)

        if trade["closed"]:
            state.bus.publish("position_closed", {"id": position_id, "mint": mint})
        else:
            state.bus.publish("position_update", position_payload(db.get_position(position_id)))
        await push_balance()
        return True


async def sell(position_id: int, reason: str = "manual") -> bool:
    """Sell whatever is left of a position."""
    pos = db.get_position(position_id)
    if not pos:
        return False
    remaining_pct = 100.0 - float(pos.get("total_sold_percent") or 0.0)
    return await sell_portion(position_id, remaining_pct, tier=reason)


def position_payload(pos: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not pos:
        return {}
    entry = float(pos.get("entry_price") or 0.0)
    current = float(pos.get("current_price") or entry)
    out = dict(pos)
    out["pnl_percent"] = ((current - entry) / entry * 100.0) if entry > 0 else 0.0
    out["multiple"] = (current / entry) if entry > 0 else 0.0
    out["remaining_percent"] = max(100.0 - float(pos.get("total_sold_percent") or 0.0), 0.0)
    out["realized_sol"] = float(pos.get("realized_sol") or 0.0)
    return out


# --------------------------------------------------------------------------- #
# Position monitor
# --------------------------------------------------------------------------- #
async def monitor_loop() -> None:
    """Every 10s: refresh prices, lift ATHs, fire tiers / trailing stop / stop loss."""
    state.bus.log("Pozisyon takibi basladi", "info")
    while True:
        try:
            for pos in db.get_open_positions():
                try:
                    await _check_position(pos)
                except Exception as exc:
                    log.error("Pozisyon kontrol hatasi (%s): %s", pos.get("mint"), exc)
        except Exception as exc:
            log.error("Monitor dongusu hatasi: %s", exc)
        await asyncio.sleep(MONITOR_INTERVAL)


async def _check_position(pos: Dict[str, Any]) -> None:
    cfg = state.settings
    position_id = int(pos["id"])
    mint = pos["mint"]
    name = pos.get("name") or mint[:6]
    price = await analyzer.current_price(mint)
    if price is None or price <= 0:
        return
    entry = float(pos.get("entry_price") or 0.0)
    levels = db.update_position_price(position_id, price, trailing_pct=cfg.trailing_stop)
    if entry <= 0:
        return

    pnl = (price - entry) / entry * 100.0
    multiple = price / entry
    state.bus.publish("position_update", {
        "id": position_id,
        "mint": mint,
        "name": name,
        "current_price": price,
        "pnl_percent": pnl,
        "multiple": multiple,
        "ath_price": levels["ath_price"],
        "trailing_stop_price": levels["trailing_stop_price"],
        "remaining_percent": max(100.0 - float(pos.get("total_sold_percent") or 0.0), 0.0),
    })

    tier1_done = bool(pos.get("tier1_sold"))

    # Stop loss only applies while the position is untouched (before tier 1).
    if not tier1_done and pnl <= -abs(cfg.stop_loss):
        state.bus.log("Stop loss: %s (%.1f%%)" % (name, pnl), "error")
        await sell(position_id, reason="stop_loss")
        return

    tiers = (
        ("tier1", cfg.tier1_x, cfg.tier1_pct, pos.get("tier1_sold")),
        ("tier2", cfg.tier2_x, cfg.tier2_pct, pos.get("tier2_sold")),
        ("tier3", cfg.tier3_x, cfg.tier3_pct, pos.get("tier3_sold")),
    )
    for tier_name, tier_x, tier_pct, already in tiers:
        if already or multiple < tier_x or tier_pct <= 0:
            continue
        state.bus.log("%s tetiklendi: %s (%.2fx) - %%%.0f satiliyor"
                      % (tier_name.upper(), name, multiple, tier_pct), "success")
        await sell_portion(position_id, float(tier_pct), tier=tier_name)
        tier1_done = tier1_done or tier_name == "tier1"

    fresh = db.get_position(position_id)
    if not fresh or fresh.get("status") != "open":
        return

    # Trailing stop guards the remainder once tier 1 has been taken.
    if fresh.get("tier1_sold"):
        trailing = float(fresh.get("trailing_stop_price") or 0.0)
        if trailing > 0 and price <= trailing:
            ath = float(fresh.get("ath_price") or price)
            state.bus.log("Trailing stop: %s (ATH $%.8f -> $%.8f, %.2fx)"
                          % (name, ath, price, multiple), "warn")
            await sell(position_id, reason="trailing_stop")


async def balance_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await push_balance()
        except Exception as exc:
            log.debug("Bakiye guncelleme hatasi: %s", exc)
        await asyncio.sleep(interval)
