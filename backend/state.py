"""Shared runtime state: settings store, bot flags and the websocket event bus.

Kept separate from main.py so bot/trader/analyzer can publish events without
importing the FastAPI app (circular import).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("market-fucker")

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.json"


@dataclass
class Settings:
    auto_buy_sol: float = 0.01
    max_bundler: float = 5.0
    max_sniper: float = 10.0
    max_dev_holdings: float = 5.0
    min_mcap: float = 5000.0
    max_mcap: float = 25000.0
    take_profit: float = 100.0
    stop_loss: float = 30.0
    # Not exposed in the panel, but kept configurable here.
    max_top10: float = 30.0
    min_volume_5m: float = 500.0
    analyze_delay: float = 10.0
    slippage_bps: int = 1500

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                known = {f.name for f in fields(cls)}
                return cls(**{k: v for k, v in raw.items() if k in known})
            except Exception as exc:  # corrupt file -> fall back to defaults
                log.warning("settings.json okunamadi (%s), varsayilanlar kullaniliyor", exc)
        return cls()

    def save(self) -> None:
        try:
            SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("settings.json yazilamadi: %s", exc)

    def update(self, data: Dict[str, Any]) -> "Settings":
        known = {f.name: f.type for f in fields(self)}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            try:
                current = getattr(self, key)
                setattr(self, key, int(value) if isinstance(current, int) and not isinstance(current, bool) else float(value))
            except (TypeError, ValueError):
                continue
        self.save()
        return self

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EventBus:
    """Fan-out of bot events to every connected frontend websocket."""

    def __init__(self, history: int = 100) -> None:
        self._queues: List[asyncio.Queue] = []
        self._recent: List[Dict[str, Any]] = []
        self._history = history

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def recent(self) -> List[Dict[str, Any]]:
        return list(self._recent)

    def publish(self, msg_type: str, data: Any) -> None:
        message = {"type": msg_type, "data": data}
        if msg_type in ("new_coin", "log"):
            self._recent.append(message)
            del self._recent[:-self._history]
        for q in list(self._queues):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: drop the message rather than blocking the bot.
                pass

    def log(self, message: str, level: str = "info") -> None:
        log.info("[%s] %s", level, message)
        self.publish("log", {"message": message, "level": level})


@dataclass
class BotState:
    running: bool = False
    connected: bool = False
    coins_seen: int = 0
    coins_bought: int = 0


settings = Settings.load()
bus = EventBus()
bot_state = BotState()

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
JUPITER_API = os.getenv("JUPITER_API", "https://quote-api.jup.ag/v6").rstrip("/")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"


def reload_env() -> None:
    """Re-read secrets after dotenv has been loaded by main.py."""
    global HELIUS_API_KEY, WALLET_PRIVATE_KEY
    HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
    WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()


def rpc_url() -> str:
    if HELIUS_API_KEY:
        return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    return "https://api.mainnet-beta.solana.com"


def ws_url() -> str:
    return f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
