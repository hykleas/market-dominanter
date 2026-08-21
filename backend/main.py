"""market-dominanter :: FastAPI server + websocket hub."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Allow `python backend/main.py` as well as `uvicorn backend.main:app`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import analyzer  # noqa: E402
import copy_engine  # noqa: E402
import database as db  # noqa: E402
import legacy_sniper as bot  # noqa: E402
import rpc  # noqa: E402
import state  # noqa: E402
import trader  # noqa: E402
import wallet_scorer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("market-dominanter")

# httpx her istegi INFO seviyesinde, TAM URL ile logluyor - RPC adresinde Helius
# anahtari var, yani anahtar duz metin olarak loga/konsola dusuyordu.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

FRONTEND = ROOT / "frontend" / "index.html"
_background: "list[asyncio.Task]" = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.reload_env()
    db.init_db()
    trader.load_wallet()
    if trader.is_paper():
        state.bus.log("PAPER modu aktif - gercek islem yapilmayacak", "warn")
        asyncio.create_task(trader.ensure_paper_funded())
    for coro in (bot.listen_loop(), copy_engine.listen_loop(),
                 trader.monitor_loop(), trader.balance_loop()):
        _background.append(asyncio.create_task(coro))
    state.bus.log("Strateji modu: %s" % state.settings.strategy_mode.upper(), "info")
    state.bus.log("Sunucu hazir", "success")
    try:
        yield
    finally:
        for task in _background:
            task.cancel()
        await asyncio.gather(*_background, return_exceptions=True)
        await asyncio.gather(rpc.close(), analyzer.close(), trader.close(), return_exceptions=True)


app = FastAPI(title="market-dominanter", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    if not FRONTEND.exists():
        return JSONResponse({"error": "frontend/index.html bulunamadi"}, status_code=404)
    return FileResponse(FRONTEND)


@app.get("/api/status")
async def api_status():
    payload = bot.status()
    payload["sol_balance"] = await trader.wallet_balance()
    payload["open_positions"] = len(db.get_open_positions())
    payload["stats"] = db.stats()
    payload["paper_wallet"] = trader.paper_wallet()
    payload["sol_usd"] = await trader.sol_price_usd()
    # 429'lar loga 25'te bir yaziliyor, yani logdan siddeti okunamiyor. Eksik
    # veri = coin reddi demek oldugu icin bu sayaclar filtre istatistigi kadar
    # onemli: yuksek rate_limited, kural dagilimini guvenilmez yapar.
    payload["rpc"] = dict(rpc.stats)
    return payload


@app.get("/api/paper")
async def api_paper():
    return trader.paper_wallet()


@app.post("/api/paper/reset")
async def api_paper_reset(payload: dict | None = None):
    start_usd = (payload or {}).get("start_usd")
    return await trader.reset_paper_wallet(float(start_usd) if start_usd else None)


@app.post("/api/mode")
async def api_mode(payload: dict):
    """Switch between paper and live execution. Live needs a usable wallet."""
    want_paper = bool(payload.get("paper", True))
    if not want_paper and not trader.can_go_live():
        return JSONResponse(
            {"error": "Canli mod icin .env icinde gecerli WALLET_PRIVATE_KEY gerekli",
             "paper": True}, status_code=400)
    state.settings.update({"paper_trading": want_paper})
    if want_paper:
        await trader.ensure_paper_funded()
    state.bus.log("Mod degisti: " + ("PAPER" if want_paper else "LIVE (gercek para)"),
                  "warn" if want_paper else "error")
    state.bus.publish("status", bot.status())
    await trader.push_balance()
    return {"paper": trader.is_paper()}


@app.post("/api/bot/start")
async def api_start():
    await bot.start()
    return {"running": True}


@app.post("/api/bot/stop")
async def api_stop():
    await bot.stop()
    return {"running": False}


@app.get("/api/settings")
async def api_get_settings():
    return state.settings.as_dict()


@app.post("/api/settings")
async def api_set_settings(payload: dict):
    state.settings.update(payload or {})
    state.bus.log("Ayarlar kaydedildi", "info")
    state.bus.publish("settings", state.settings.as_dict())
    return state.settings.as_dict()


@app.get("/api/positions")
async def api_positions():
    return [trader.position_payload(p) for p in db.get_open_positions()]


@app.get("/api/trades")
async def api_trades():
    return db.get_trades()


# --------------------------------------------------------------------------- #
# Wallets (KATMAN 1) + copy signals (KATMAN 2)
# --------------------------------------------------------------------------- #
_scoring_lock = asyncio.Lock()


@app.get("/api/wallets")
async def api_wallets():
    return db.get_wallets()


@app.post("/api/wallets/score")
async def api_wallets_score(payload: dict | None = None):
    """Kick off a scoring batch. It can take a long time, so it runs in the
    background and streams progress over the websocket."""
    if _scoring_lock.locked():
        return JSONResponse({"error": "Skorlama zaten calisiyor"}, status_code=409)

    body = payload or {}
    use_ai = bool(body.get("use_ai", True))
    only = body.get("address")
    candidates = [{"address": only, "source": "panel", "note": ""}] if only else None

    async def run():
        async with _scoring_lock:
            try:
                await wallet_scorer.score_wallets(candidates, use_ai=use_ai)
            except Exception as exc:
                log.error("Skorlama hatasi: %s", exc)
                state.bus.log("Skorlama hatasi: %s" % type(exc).__name__, "error")

    _background.append(asyncio.create_task(run()))
    return {"started": True}


@app.post("/api/wallets/{address}/follow")
async def api_wallet_follow(address: str, payload: dict | None = None):
    follow = bool((payload or {}).get("followed", True))
    if not db.set_wallet_followed(address, follow):
        return JSONResponse({"error": "Cuzdan bulunamadi"}, status_code=404)
    # Takip listesi degisti: kopya motoru aboneliklerini tazelesin.
    copy_engine.request_reload()
    state.bus.log("%s %s" % (address[:10], "TAKIBE ALINDI" if follow else "takipten cikarildi"),
                  "success" if follow else "warn")
    state.bus.publish("wallets", db.get_wallets())
    return {"address": address, "is_followed": 1 if follow else 0}


@app.delete("/api/wallets/{address}")
async def api_wallet_delete(address: str):
    ok = db.delete_wallet(address)
    if ok:
        copy_engine.request_reload()
        state.bus.publish("wallets", db.get_wallets())
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.get("/api/signals")
async def api_signals(limit: int = 200):
    return db.get_copy_signals(limit)


@app.get("/api/signals/stats")
async def api_signal_stats():
    """Hangi kapinin kac sinyali eledigi. Eski botta olmayan olcum."""
    return db.copy_signal_stats()


@app.get("/api/coins")
async def api_coins(limit: int = 200):
    return db.get_coin_decisions(limit)


@app.get("/api/coins/stats")
async def api_coin_stats():
    """Sniper'in hangi kurali kac coini eledigi.

    `sole_blocker` asil onemli olan: yalnizca O KURAL yuzunden reddedilen
    coinler. Bir kurali gevsetmenin kac coini serbest birakacagini soyler.
    """
    return db.coin_decision_stats()


@app.post("/api/positions/{position_id}/sell")
async def api_sell(position_id: int, payload: dict | None = None):
    """Manual exit. Body {"percent": 50} sells half; no body sells everything."""
    percent = (payload or {}).get("percent")
    if percent:
        ok = await trader.sell_portion(position_id, float(percent), tier="manual")
    else:
        ok = await trader.sell(position_id, reason="manual")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue = state.bus.subscribe()
    try:
        await ws.send_json({"type": "init", "data": {
            "status": bot.status(),
            "settings": state.settings.as_dict(),
            "positions": [trader.position_payload(p) for p in db.get_open_positions()],
            "trades": db.get_trades(50),
            "wallets": db.get_wallets(),
            "signals": db.get_copy_signals(50),
            "recent": state.bus.recent(),
            "sol_balance": await trader.wallet_balance(),
            "paper_wallet": trader.paper_wallet(),
            "sol_usd": await trader.sol_price_usd(),
            "stats": db.stats(),
        }})

        async def pump():
            while True:
                message = await queue.get()
                await ws.send_json(message)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                # Keeps the connection alive and surfaces client disconnects.
                await ws.receive_text()
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("Websocket hatasi: %s", exc)
    finally:
        state.bus.unsubscribe(queue)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    log.info("http://127.0.0.1:%d adresinde baslatiliyor", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
