"""KATMAN 2 - takip edilen cuzdanlari canli kopyala.

Helius websocket'inde her takipteki cuzdan icin `logsSubscribe` acilir. Bir
islem gorulunce `getTransaction` ile cozulur, alim mi satim mi belirlenir ve
kapilardan gecerse kopyalanir.

Cikis karari lidere devredilir: tier semasi burada YOK. Lider satarsa biz de
satariz; lider satmazsa hard stop ve zaman stopu devreye girer (trader.py).

KRITIK: her sinyal - kopyalanan da atlanan da - `copy_signals` tablosuna yazilir.
Eski sniper'in en buyuk hatasi red gerekcelerini kaydetmemesiydi; hangi kuralin
kac coini eledigi hic olculemedi. Burada her karar izlenebilir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import websockets

import analyzer
import database as db
import pumpfun
import rpc
import state
import trader
from wallet_scorer import parse_wallet_tx

log = logging.getLogger("market-dominanter")

RECONNECT_DELAY = 5.0
RESUBSCRIBE_POLL = 10.0     # takip listesi degisikligini bu araliklarla yakala

_tasks: "set[asyncio.Task]" = set()
_seen_signatures: "dict[str, float]" = {}
# (cuzdan, mint) -> liderin elindeki token miktari. Lider satisinin ORANINI
# hesaplamak icin sart; oran bilinmeden "yarisini satti" ile "hepsini satti"
# ayirt edilemez.
_leader_holdings: "dict[tuple, float]" = {}
_reload_event: Optional[asyncio.Event] = None


def request_reload() -> None:
    """Takip listesi degisti - dinleyici aboneliklerini tazelesin."""
    if _reload_event is not None:
        _reload_event.set()


def _dedupe(signature: str) -> bool:
    """True if this signature has not been handled yet (1h memory)."""
    now = time.time()
    for old, ts in list(_seen_signatures.items()):
        if now - ts > 3600:
            _seen_signatures.pop(old, None)
    if signature in _seen_signatures:
        return False
    _seen_signatures[signature] = now
    return True


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------- #
# Kapilar
# --------------------------------------------------------------------------- #
async def _buy_gate(mint: str, leader_sol: float, age_sec: float) -> Dict[str, Any]:
    """Alim kapilari. `skip` None ise alim serbest.

    Sira onemli: bedava kontroller once, boylece atilacak bir sinyal icin
    pahali RPC/Dexscreener cagrisi yapilmaz - RPS butcesi asil burada korunuyor.
    """
    cfg = state.settings
    out: Dict[str, Any] = {"skip": None, "progress": None, "multiplier": 1.0,
                           "name": mint[:6], "price": None, "liquidity": None}

    if leader_sol < cfg.min_leader_buy_sol:
        out["skip"] = "dust"
        return out
    if age_sec > cfg.max_signal_age_sec:
        out["skip"] = "stale_signal"
        return out
    if db.has_open_position(mint):
        out["skip"] = "already_open"
        return out

    # KATMAN 3: curve durumu hem likidite kapisini hem boyut carpanini belirler.
    curve = await pumpfun.read_curve(mint)

    if curve is not None and not curve.complete:
        out["progress"] = curve.progress
        if curve.progress > cfg.curve_skip_above:
            out["skip"] = "near_migration_skip"      # migration ani = exit liquidity riski
            return out
        if curve.sol_in_curve < cfg.min_curve_sol_for_copy:
            out["skip"] = "low_liquidity"
            return out
        if cfg.curve_boost_min <= curve.progress <= cfg.curve_boost_max:
            out["multiplier"] = float(cfg.curve_boost_mult)
        sol_usd = await analyzer.sol_price_usd()
        if curve.price_sol and sol_usd > 0:
            out["price"] = curve.price_sol * sol_usd
        return out

    # Curve yok ya da tamamlanmis -> DEX likiditesine bak.
    if curve is not None and curve.complete:
        out["progress"] = 1.0
    pair = await analyzer.fetch_dexscreener(mint, retries=2)
    if pair:
        base = pair.get("baseToken") or {}
        out["name"] = base.get("symbol") or base.get("name") or out["name"]
        try:
            out["price"] = float(pair.get("priceUsd") or 0) or None
            out["liquidity"] = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            pass
    if out["liquidity"] is None or out["liquidity"] < cfg.min_liquidity_usd:
        out["skip"] = "low_liquidity"
    return out


# --------------------------------------------------------------------------- #
# Sinyal isleme
# --------------------------------------------------------------------------- #
async def _handle_signal(wallet: str, signature: str, detected_at: float) -> None:
    tx = await rpc.get_transaction(signature)
    if not tx:
        return
    event = parse_wallet_tx(tx, wallet)
    if not event:
        return   # transfer / LP / airdrop - trade degil

    state.bot_state.signals_seen += 1
    age_ms = int((time.time() - detected_at) * 1000)
    key = (wallet, event.mint)
    name = event.mint[:6]

    if event.side == "buy":
        _leader_holdings[key] = _leader_holdings.get(key, 0.0) + event.tokens
        await _handle_leader_buy(wallet, event, age_ms, detected_at)
        return

    # --- SATIS ---
    held = _leader_holdings.get(key)
    if held and held > 0:
        fraction = min(event.tokens / held, 1.0) * 100.0
        _leader_holdings[key] = max(held - event.tokens, 0.0)
    else:
        # Lideri bu pozisyonun ortasinda yakalamisiz: elindekini bilmiyoruz.
        # Gozlenen satisi tam cikis saymak, yarim bilgiyle pozisyonda kalmaktan
        # daha guvenli.
        fraction = 100.0

    # Sinyal, pozisyonumuz olsa da olmasa da kaydedilir - ama "kopyalandi"
    # sadece gercekten sattigimizda yazilir.
    position = _open_position_for(event.mint, wallet)
    signal = db.add_copy_signal(
        wallet, event.mint, "sell", event.sol, age_ms,
        action="copied" if position else "skipped",
        skip_reason=None if position else "no_position",
        position_id=int(position["id"]) if position else None)
    state.bus.publish("copy_signal", signal)
    if not position:
        return

    if fraction >= state.settings.leader_sell_full_threshold:
        state.bus.log("Lider %s cikti (%%%.0f) -> %s tamamen satiliyor"
                      % (wallet[:6], fraction, name), "warn")
        await trader.sell(int(position["id"]), reason="leader_exit")
    else:
        state.bus.log("Lider %s %%%.0f satti -> %s ayni oranda satiliyor"
                      % (wallet[:6], fraction, name), "info")
        await trader.sell_portion(int(position["id"]), fraction, tier="leader_partial")


async def _handle_leader_buy(wallet: str, event, age_ms: int, detected_at: float) -> None:
    cfg = state.settings
    gate = await _buy_gate(event.mint, event.sol, time.time() - detected_at)

    if gate["skip"]:
        signal = db.add_copy_signal(wallet, event.mint, "buy", event.sol, age_ms,
                                    action="skipped", skip_reason=gate["skip"])
        state.bus.publish("copy_signal", signal)
        state.bus.log("ATLANDI %s (%s) - lider %s %.2f SOL aldi"
                      % (event.mint[:10], gate["skip"], wallet[:6], event.sol), "warn")
        return

    multiplier = float(gate["multiplier"] or 1.0)
    size = float(cfg.copy_size_sol) * multiplier
    if multiplier > 1.0:
        state.bus.log("Curve momentum %%%.0f -> boyut x%.1f (%.3f SOL)"
                      % ((gate["progress"] or 0) * 100, multiplier, size), "success")

    position_id = await trader.buy(event.mint, gate["name"], amount_sol=size,
                                   price_hint=gate["price"], liquidity_hint=gate["liquidity"],
                                   source_wallet=wallet, curve_progress=gate["progress"])
    action = "copied" if position_id else "skipped"
    reason = None if position_id else "buy_failed"
    if position_id:
        state.bot_state.signals_copied += 1

    signal = db.add_copy_signal(wallet, event.mint, "buy", event.sol, age_ms,
                                action=action, skip_reason=reason, position_id=position_id)
    state.bus.publish("copy_signal", signal)
    state.bus.publish("status", _status())


def _open_position_for(mint: str, wallet: str) -> Optional[Dict[str, Any]]:
    for pos in db.get_open_positions():
        if pos.get("mint") == mint and pos.get("source_wallet") == wallet:
            return pos
    return None


def _status() -> Dict[str, Any]:
    import legacy_sniper
    return legacy_sniper.status()


# --------------------------------------------------------------------------- #
# Websocket dinleyici
# --------------------------------------------------------------------------- #
async def _process_notification(payload: Dict[str, Any], sub_map: Dict[int, str]) -> None:
    try:
        params = payload.get("params") or {}
        wallet = sub_map.get(params.get("subscription"))
        value = ((params.get("result") or {}).get("value") or {})
        if not wallet or value.get("err"):
            return
        signature = value.get("signature")
        if not signature or not _dedupe(signature):
            return
        if not state.bot_state.running or state.settings.strategy_mode != "copy":
            return
        _spawn(_handle_signal(wallet, signature, time.time()))
    except Exception as exc:
        log.debug("Kopya bildirimi islenemedi: %s", exc)


async def listen_loop() -> None:
    """Takipteki cuzdanlari dinler; takip listesi degisince yeniden abone olur."""
    global _reload_event
    _reload_event = asyncio.Event()
    no_wallet_ticks = 0

    while True:
        wallets = [w["address"] for w in db.get_wallets(followed_only=True)]
        state.bot_state.followed_wallets = len(wallets)

        if not state.HELIUS_API_KEY:
            if no_wallet_ticks % 8 == 0:
                state.bus.log("HELIUS_API_KEY yok - kopya motoru baslatilamiyor", "error")
            no_wallet_ticks += 1
            await asyncio.sleep(15)
            continue
        if not wallets:
            if no_wallet_ticks % 12 == 0:
                state.bus.log("Takip edilen cuzdan yok - once skorlayip takibe alin", "warn")
            no_wallet_ticks += 1
            await _wait_for_reload(RESUBSCRIBE_POLL)
            continue
        no_wallet_ticks = 0

        try:
            await _run_subscription(wallets)
        except Exception as exc:
            state.bot_state.copy_connected = False
            state.bus.log("Kopya motoru baglantisi koptu (%s), %ss sonra tekrar"
                          % (type(exc).__name__, int(RECONNECT_DELAY)), "warn")
            await asyncio.sleep(RECONNECT_DELAY)
        finally:
            state.bot_state.copy_connected = False


async def _wait_for_reload(timeout: float) -> None:
    if _reload_event is None:
        await asyncio.sleep(timeout)
        return
    try:
        await asyncio.wait_for(_reload_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    _reload_event.clear()


async def _run_subscription(wallets: List[str]) -> None:
    """Tek websocket, cuzdan basina bir logsSubscribe."""
    async with websockets.connect(state.ws_url(), ping_interval=20, ping_timeout=20,
                                  max_size=8 * 1024 * 1024) as ws:
        pending: Dict[int, str] = {}
        sub_map: Dict[int, str] = {}
        for i, wallet in enumerate(wallets, start=1):
            pending[i] = wallet
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": "processed"}],
            }))

        state.bot_state.copy_connected = True
        state.bus.log("Kopya motoru bagli - %d cuzdan dinleniyor" % len(wallets), "success")
        state.bus.publish("status", _status())

        watcher = asyncio.create_task(_reload_watcher(ws))
        try:
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                # Abonelik onaylari: request id -> subscription id eslesmesi.
                if "result" in payload and isinstance(payload.get("result"), int):
                    wallet = pending.pop(payload.get("id"), None)
                    if wallet:
                        sub_map[payload["result"]] = wallet
                    continue
                if payload.get("method") == "logsNotification":
                    _spawn(_process_notification(payload, sub_map))
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


async def _reload_watcher(ws) -> None:
    """Takip listesi degisince baglantiyi kapatir; dis dongu yeniden abone olur."""
    await _wait_for_reload(3600.0)
    state.bus.log("Takip listesi degisti - abonelikler yenileniyor", "info")
    await ws.close()
