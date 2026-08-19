"""Launch detection: subscribe to pump.fun program logs over the Helius websocket,
pull the new mint out of each create transaction, analyze it, buy on PASS.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import websockets

import analyzer
import rpc
import state
import trader

log = logging.getLogger("market-fucker")

CREATE_MARKERS = ("Program log: Instruction: Create", "Program log: Instruction: CreateV2",
                  "Program log: Create:")
RECONNECT_DELAY = 5.0
MAX_PARALLEL_ANALYSIS = 4

_seen: "dict[str, float]" = {}
_analysis_sem = asyncio.Semaphore(MAX_PARALLEL_ANALYSIS)
_tasks: "set[asyncio.Task]" = set()


def _remember(mint: str) -> bool:
    """True if this mint is new (dedupe with a 1h memory)."""
    now = time.time()
    for old, ts in list(_seen.items()):
        if now - ts > 3600:
            _seen.pop(old, None)
    if mint in _seen:
        return False
    _seen[mint] = now
    return True


def _is_create(logs: list) -> bool:
    for line in logs or []:
        for marker in CREATE_MARKERS:
            if marker in line:
                return True
    return False


async def _extract_mint(signature: str) -> Tuple[Optional[str], Optional[str]]:
    """(mint, creator) from a pump.fun create transaction."""
    tx = await rpc.get_transaction(signature)
    if not tx:
        return None, None
    creator = None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        for key in keys:
            if key.get("signer"):
                creator = key.get("pubkey")
                break
    except Exception:
        keys = []

    meta = tx.get("meta") or {}
    for bal in (meta.get("postTokenBalances") or []):
        mint = bal.get("mint")
        if mint and mint != state.WSOL_MINT:
            return mint, creator

    # pump.fun mints are vanity addresses ending in "pump".
    try:
        for key in keys:
            pubkey = key.get("pubkey") or ""
            if pubkey.endswith("pump") and pubkey != state.PUMP_FUN_PROGRAM:
                return pubkey, creator
    except Exception:
        pass
    return None, creator


async def _handle_new_mint(mint: str, creator: Optional[str]) -> None:
    """Analyze one launch and buy it when every rule passes."""
    async with _analysis_sem:
        try:
            state.bus.log("Yeni coin: %s - analiz %ss sonra" % (mint[:10], int(state.settings.analyze_delay)),
                          "info")
            result = await analyzer.analyze(mint, creator=creator)
            metrics = result.metrics or analyzer.Metrics(mint=mint)
            row = metrics.feed_row()
            row["time"] = time.time()
            row["result"] = "BOUGHT" if result.passed else "REJECTED"
            row["reason"] = result.reason_text
            state.bot_state.coins_seen += 1

            if not result.passed:
                state.bus.publish("new_coin", row)
                return

            if not state.bot_state.running:
                row["result"] = "REJECTED"
                row["reason"] = "bot durduruldu"
                state.bus.publish("new_coin", row)
                return

            pos_id = await trader.buy(mint, metrics.name, price_hint=metrics.price_usd)
            if pos_id is None:
                row["result"] = "REJECTED"
                row["reason"] = "alim basarisiz"
            state.bus.publish("new_coin", row)
        except Exception as exc:
            log.error("Coin islenemedi (%s): %s", mint, exc)
            state.bus.log("Coin islenemedi (%s): %s" % (mint[:10], exc), "error")


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _process_notification(payload: Dict[str, Any]) -> None:
    try:
        value = ((payload.get("params") or {}).get("result") or {}).get("value") or {}
        if value.get("err"):
            return
        if not _is_create(value.get("logs") or []):
            return
        signature = value.get("signature")
        if not signature:
            return
        mint, creator = await _extract_mint(signature)
        if not mint or not _remember(mint):
            return
        if not state.bot_state.running:
            return
        _spawn(_handle_new_mint(mint, creator))
    except Exception as exc:
        log.debug("Bildirim islenemedi: %s", exc)


async def listen_loop() -> None:
    """Helius websocket listener with automatic reconnect. Never raises."""
    subscribe = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [{"mentions": [state.PUMP_FUN_PROGRAM]}, {"commitment": "processed"}],
    })
    while True:
        if not state.HELIUS_API_KEY:
            state.bus.log("HELIUS_API_KEY yok - coin dinleyici baslatilamiyor (.env doldurun)", "error")
            await asyncio.sleep(15)
            continue
        try:
            async with websockets.connect(state.ws_url(), ping_interval=20, ping_timeout=20,
                                          max_size=8 * 1024 * 1024) as ws:
                await ws.send(subscribe)
                state.bot_state.connected = True
                state.bus.log("Helius websocket baglandi, pump.fun dinleniyor", "success")
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if payload.get("method") != "logsNotification":
                        continue
                    _spawn(_process_notification(payload))
        except Exception as exc:
            state.bot_state.connected = False
            state.bus.log("Helius baglantisi koptu (%s), %ss sonra tekrar denenecek"
                          % (type(exc).__name__, int(RECONNECT_DELAY)), "warn")
        finally:
            state.bot_state.connected = False
        await asyncio.sleep(RECONNECT_DELAY)


async def start() -> None:
    state.bot_state.running = True
    state.bus.log("Bot BASLATILDI", "success")
    state.bus.publish("status", status())


async def stop() -> None:
    state.bot_state.running = False
    state.bus.log("Bot DURDURULDU (acik analizler tamamlanacak)", "warn")
    state.bus.publish("status", status())


def status() -> Dict[str, Any]:
    return {
        "running": state.bot_state.running,
        "connected": state.bot_state.connected,
        "coins_seen": state.bot_state.coins_seen,
        "coins_bought": state.bot_state.coins_bought,
        "paper": trader.is_paper(),
        "wallet": trader.wallet_pubkey(),
    }
