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

log = logging.getLogger("market-dominanter")

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.json"


@dataclass
class Settings:
    # Hangi strateji trade tetikler:
    #   "copy"   -> copy_engine (takip edilen cuzdanlari kopyala)  [varsayilan]
    #   "legacy" -> legacy_sniper (fresh-launch sniping, arsiv)
    # legacy_sniper her zaman dinler ve akisi panele basar; sadece "legacy"
    # modunda alim tetikler.
    strategy_mode: str = "copy"

    # --- KATMAN 2: canli kopyalama ---
    copy_size_sol: float = 0.1          # 0.01'de sabit fee gidis-donusu %22 yiyordu
    min_leader_buy_sol: float = 0.5     # bunun altindaki lider alimi = toz
    max_signal_age_sec: float = 8.0     # tespit -> emir arasi tavan
    min_liquidity_usd: float = 10_000.0
    min_curve_sol_for_copy: float = 10.0
    hard_stop_pct: float = 35.0         # lider satmazsa diye guvenlik agi
    time_stop_minutes: float = 45.0
    time_stop_min_pnl: float = 10.0     # bu PnL'in altindaysa zaman stopu calisir
    # Lider pozisyonunun bu yuzdesinden fazlasini satarsa biz tamamen cikariz.
    leader_sell_full_threshold: float = 50.0

    # --- KATMAN 3: curve-progress sinyal guclendirici ---
    curve_boost_min: float = 0.60
    curve_boost_max: float = 0.90
    curve_boost_mult: float = 1.5
    curve_skip_above: float = 0.95      # migration ani alimi = exit liquidity riski
    # pump.fun migration esigi degisebilir; koda gomulmuyor.
    curve_graduation_sol: float = 85.0

    # --- KATMAN 1: cuzdan skorlama ---
    scorer_lookback_days: int = 30
    scorer_min_trades: int = 15         # bunun altinda ornekle yetersiz -> ELE
    scorer_max_signatures: int = 3000   # cuzdan basina imza tavani (RPS butcesi)
    # Gunde bu kadar islemden fazlasini yapan cuzdan insan degildir. Olculen
    # ornek: kesif 5.5 gunde 20.000 islem yapan bir cuzdan buldu - saatte 150,
    # 24 saniyede bir, 7/24. On tarama bunu islemleri cozmeden yakalar.
    scorer_max_tx_per_day: float = 500.0
    # Kopyalanabilirligin tek gercek testi. Olculen ornek: medyan tutusu 0
    # dakika olan bir cuzdan, 190 islemde %7.4 basari ve -%20.3 verdi. Sinyali
    # 3-8 saniye sonra gordugun icin saniyelik pozisyonlari kopyalayamazsin.
    scorer_min_hold_seconds: float = 300.0
    scorer_style_sample: int = 150
    # Liderin bundan kisa tuttugu islemi KOPYALAMA. Gecikme vergisi
    # getiri x gecikme/tutus oldugu icin kisa tutuslarda ezici, uzunlarda
    # ihmal edilebilir. 0 = filtre kapali.
    min_copy_hold_sec: float = 0.0

    # Esikler 19 Agustos 2026 canli olcumlerine gore kalibre edildi; bundler /
    # sniper / dev artik TOPLAM arza, top10 ise curve disi dolasima gore olculuyor.
    auto_buy_sol: float = 0.01
    max_bundler: float = 5.0
    max_sniper: float = 10.0
    max_dev_holdings: float = 5.0
    min_mcap: float = 5000.0
    max_mcap: float = 25000.0
    stop_loss: float = 30.0
    # top10 toplam arza gore olculuyor (curve disi float'a gore olculdugunde
    # 30 saniyelik bir coinde tek alici oldugu icin her zaman %100 cikiyordu).
    max_top10: float = 25.0
    # Dexscreener 20 saniyelik coinde 5m hacmi bos donuyor. Talep olcusu artik
    # bonding curve'e giren gercek SOL; 5m hacim sadece veri varsa uygulaniyor.
    min_volume_5m: float = 0.0
    min_curve_sol: float = 2.0
    max_curve_sol: float = 30.0
    # 30sn'de coinlerin neredeyse tamami launch tabaninda duruyor (canli olcum:
    # mcap $2281 = curve tabani, curve 0.00 SOL). 90sn'de ayrisiyorlar, yani filtre
    # ancak orada gercek sinyalle calisiyor. Sniper penceresi (15sn) de kapanmis olur.
    analyze_delay: float = 90.0
    slippage_bps: int = 1500

    # --- kademeli satis + trailing stop ---
    tier1_x: float = 2.0
    tier2_x: float = 5.0
    tier3_x: float = 10.0
    tier1_pct: float = 25.0
    tier2_pct: float = 25.0
    tier3_pct: float = 25.0
    trailing_stop: float = 30.0
    # Trailing artik tier1'e (2x) degil buna silahlanir. 19 Agustos'ta "derp"
    # 1.83x'e cikip trailing hic devreye girmeden -%42.7 stop loss'a dondu:
    # 1x-2x arasi tam bir olu bolgeydi.
    trailing_arm_x: float = 1.40

    # Acik pozisyonlarin fiyat dongusu. pump.fun hizinda 10sn cok uzundu:
    # -%30 stop loss -%42.7'de dolduruluyordu.
    price_loop_sec: float = 3.0

    # --- paper trading ---
    paper_trading: bool = True
    paper_start_usd: float = 20.0
    paper_balance_sol: float = 0.0     # 0 = henuz fonlanmadi
    paper_funded_sol: float = 0.0      # reset aninda yatirilan miktar (PnL referansi)
    fee_platform_pct: float = 1.0
    paper_slippage_pct: float = 1.5    # taban slipaj; likidite biliniyorsa uzerine etki eklenir
    fee_network_sol: float = 0.000005
    fee_priority_sol: float = 0.001

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
        known = {f.name for f in fields(self)}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    if isinstance(value, str):
                        setattr(self, key, value.strip().lower() in ("1", "true", "yes", "on"))
                    else:
                        setattr(self, key, bool(value))
                elif isinstance(current, str):
                    setattr(self, key, str(value).strip())
                elif isinstance(current, int):
                    setattr(self, key, int(value))
                else:
                    setattr(self, key, float(value))
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
    connected: bool = False          # legacy_sniper websocket
    coins_seen: int = 0
    coins_bought: int = 0
    copy_connected: bool = False     # copy_engine websocket
    followed_wallets: int = 0
    signals_seen: int = 0
    signals_copied: int = 0


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
