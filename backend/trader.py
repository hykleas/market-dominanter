"""Trade execution: Jupiter v6 swaps, position monitoring, take-profit / stop-loss.

Runs in PAPER mode automatically when WALLET_PRIVATE_KEY is not set, so the
panel and the whole pipeline can be exercised without risking funds.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Dict, Optional

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
_sol_price: Dict[str, float] = {"price": 0.0, "ts": 0.0}
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
        log.warning("WALLET_PRIVATE_KEY yok - PAPER (simulasyon) modunda calisiyor")
        return None
    if not SOLDERS_OK:
        log.error("solders kurulu degil - PAPER modunda calisiyor")
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


def is_paper() -> bool:
    if os.getenv("PAPER_TRADING", "").lower() in ("1", "true", "yes"):
        return True
    return _keypair is None


async def wallet_balance() -> float:
    if not _wallet_pubkey:
        return 0.0
    try:
        return await rpc.get_balance_sol(_wallet_pubkey)
    except Exception:
        return 0.0


async def push_balance() -> None:
    state.bus.publish("balance_update", {"sol_balance": await wallet_balance(),
                                         "wallet": _wallet_pubkey, "paper": is_paper()})


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
    now = time.time()
    if _sol_price["price"] > 0 and now - _sol_price["ts"] < 60:
        return _sol_price["price"]
    price = await analyzer.current_price(state.WSOL_MINT)
    if price:
        _sol_price["price"] = price
        _sol_price["ts"] = now
    return _sol_price["price"]


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
    ok = await rpc.confirm_signature(signature, timeout=60)
    if not ok:
        log.error("Islem onaylanmadi: %s", signature)
        return None
    return signature


# --------------------------------------------------------------------------- #
# Buy / sell
# --------------------------------------------------------------------------- #
async def buy(mint: str, name: str = "?", price_hint: Optional[float] = None) -> Optional[int]:
    """Buy `settings.auto_buy_sol` worth of `mint`. Returns the position id."""
    cfg = state.settings
    if db.has_open_position(mint):
        state.bus.log("Zaten acik pozisyon var, atlandi: " + name, "warn")
        return None

    amount_sol = float(cfg.auto_buy_sol)
    lamports = int(amount_sol * state.LAMPORTS_PER_SOL)
    entry_price = price_hint or await analyzer.current_price(mint) or 0.0
    decimals = await token_decimals(mint)

    if is_paper():
        sol_usd = await sol_price_usd()
        tokens = (amount_sol * sol_usd / entry_price) if (entry_price > 0 and sol_usd > 0) else 0.0
        pos_id = db.add_position(mint, name, entry_price, tokens, amount_sol)
        state.bot_state.coins_bought += 1
        state.bus.log("[PAPER] ALINDI %s - %.4f SOL @ $%.8f" % (name, amount_sol, entry_price), "success")
        state.bus.publish("position_opened", position_payload(db.get_position(pos_id)))
        return pos_id

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

    try:
        tokens = int(quote["outAmount"]) / (10 ** decimals)
    except Exception:
        tokens = 0.0
    if entry_price <= 0 and tokens > 0:
        sol_usd = await sol_price_usd()
        entry_price = (amount_sol * sol_usd / tokens) if sol_usd > 0 else 0.0

    pos_id = db.add_position(mint, name, entry_price, tokens, amount_sol)
    state.bot_state.coins_bought += 1
    state.bus.log("ALINDI %s - %.4f SOL @ $%.8f (tx %s)" % (name, amount_sol, entry_price, signature[:8]),
                  "success")
    state.bus.publish("position_opened", position_payload(db.get_position(pos_id)))
    await push_balance()
    return pos_id


async def sell(position_id: int, reason: str = "manual") -> bool:
    """Sell the whole position and close it out in the database."""
    lock = _sell_locks.setdefault(position_id, asyncio.Lock())
    if lock.locked():
        return False
    async with lock:
        pos = db.get_position(position_id)
        if not pos or pos.get("status") != "open":
            return False
        mint = pos["mint"]
        name = pos.get("name") or mint[:6]
        exit_price = await analyzer.current_price(mint) or float(pos.get("current_price") or 0.0)
        entry_price = float(pos.get("entry_price") or 0.0)
        amount_sol = float(pos.get("amount_sol") or 0.0)

        if is_paper():
            ratio = (exit_price / entry_price) if entry_price > 0 else 1.0
            sol_received = amount_sol * ratio
        else:
            raw = await raw_token_balance(mint)
            if raw <= 0:
                state.bus.log("Cuzdanda token yok, pozisyon kapatiliyor: " + name, "warn")
                sol_received = 0.0
            else:
                quote = await get_quote(mint, state.WSOL_MINT, raw)
                if not quote:
                    state.bus.log("Satis quote alinamadi: " + name, "error")
                    return False
                signature = await execute_swap(quote)
                if not signature:
                    state.bus.log("Satis islemi basarisiz: " + name, "error")
                    return False
                sol_received = int(quote["outAmount"]) / state.LAMPORTS_PER_SOL
                state.bus.log("SATILDI %s (%s) tx %s" % (name, reason, signature[:8]), "info")

        trade = db.close_position(position_id, exit_price, sol_received)
        if trade:
            state.bus.publish("trade_closed", {
                "name": name,
                "mint": mint,
                "pnl_sol": trade["pnl_sol"],
                "pnl_percent": trade["pnl_percent"],
                "reason": reason,
                "entry_price": trade["entry_price"],
                "exit_price": trade["exit_price"],
                "exit_time": trade["exit_time"],
            })
            tag = "success" if trade["pnl_sol"] >= 0 else "error"
            state.bus.log("KAPANDI %s | %s | PnL %.2f%% (%.4f SOL)"
                          % (name, reason, trade["pnl_percent"], trade["pnl_sol"]), tag)
        state.bus.publish("position_closed", {"id": position_id, "mint": mint})
        await push_balance()
        return True


def position_payload(pos: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not pos:
        return {}
    entry = float(pos.get("entry_price") or 0.0)
    current = float(pos.get("current_price") or entry)
    pnl = ((current - entry) / entry * 100.0) if entry > 0 else 0.0
    out = dict(pos)
    out["pnl_percent"] = pnl
    return out


# --------------------------------------------------------------------------- #
# Position monitor
# --------------------------------------------------------------------------- #
async def monitor_loop() -> None:
    """Every 10s: refresh prices for open positions and fire TP / SL."""
    state.bus.log("Pozisyon takibi basladi", "info")
    while True:
        try:
            positions = db.get_open_positions()
            for pos in positions:
                try:
                    await _check_position(pos)
                except Exception as exc:
                    log.error("Pozisyon kontrol hatasi (%s): %s", pos.get("mint"), exc)
        except Exception as exc:
            log.error("Monitor dongusu hatasi: %s", exc)
        await asyncio.sleep(MONITOR_INTERVAL)


async def _check_position(pos: Dict[str, Any]) -> None:
    cfg = state.settings
    mint = pos["mint"]
    price = await analyzer.current_price(mint)
    if price is None or price <= 0:
        return
    entry = float(pos.get("entry_price") or 0.0)
    if entry <= 0:
        db.update_position_price(int(pos["id"]), price)
        return
    pnl = (price - entry) / entry * 100.0
    db.update_position_price(int(pos["id"]), price)
    state.bus.publish("position_update", {
        "id": pos["id"],
        "mint": mint,
        "name": pos.get("name"),
        "current_price": price,
        "pnl_percent": pnl,
    })
    if pnl >= cfg.take_profit:
        state.bus.log("Take profit tetiklendi: %s (+%.1f%%)" % (pos.get("name"), pnl), "success")
        await sell(int(pos["id"]), reason="take_profit")
    elif pnl <= -abs(cfg.stop_loss):
        state.bus.log("Stop loss tetiklendi: %s (%.1f%%)" % (pos.get("name"), pnl), "error")
        await sell(int(pos["id"]), reason="stop_loss")


async def balance_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await push_balance()
        except Exception as exc:
            log.debug("Bakiye guncelleme hatasi: %s", exc)
        await asyncio.sleep(interval)
