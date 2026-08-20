"""SQLite storage for open positions and closed (or partially closed) trades."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("market-dominanter")

DB_PATH = Path(__file__).resolve().parent.parent / "trades.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# Columns added after the first release; applied to existing databases on boot.
POSITION_MIGRATIONS = {
    "tier1_sold": "INTEGER DEFAULT 0",
    "tier2_sold": "INTEGER DEFAULT 0",
    "tier3_sold": "INTEGER DEFAULT 0",
    "ath_price": "REAL DEFAULT 0",
    "trailing_stop_price": "REAL DEFAULT 0",
    "total_sold_percent": "REAL DEFAULT 0",
    "remaining_token": "REAL DEFAULT 0",
    "realized_sol": "REAL DEFAULT 0",
    "fees_sol": "REAL DEFAULT 0",
    "is_paper": "INTEGER DEFAULT 0",
    # Gercek giris maliyeti = amount_sol + giris sabit ucreti. amount_sol tek
    # basina kullanildigi surece PnL, giris priority+network fee'sini atliyordu
    # (19 Agustos calistirmasinda zarar %31 dusuk raporlandi).
    "entry_cost_sol": "REAL DEFAULT 0",
    "source_wallet": "TEXT",
    "curve_progress_at_entry": "REAL",
}
TRADE_MIGRATIONS = {
    "tier": "TEXT",
    "sold_percent": "REAL DEFAULT 100",
    "fees_sol": "REAL DEFAULT 0",
    "is_paper": "INTEGER DEFAULT 0",
    "source_wallet": "TEXT",
}
# `wallets` ilk surumde yoktu; sema buyudukce buradan genisletilir.
WALLET_MIGRATIONS: Dict[str, str] = {}


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def _migrate(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
            log.info("Veritabani guncellendi: %s.%s eklendi", table, name)


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                mint          TEXT NOT NULL,
                name          TEXT,
                entry_price   REAL,
                current_price REAL,
                amount_token  REAL,
                amount_sol    REAL,
                timestamp     REAL,
                status        TEXT DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                mint         TEXT NOT NULL,
                name         TEXT,
                entry_price  REAL,
                exit_price   REAL,
                amount_sol   REAL,
                pnl_sol      REAL,
                pnl_percent  REAL,
                entry_time   REAL,
                exit_time    REAL
            );
            CREATE TABLE IF NOT EXISTS wallets (
                address            TEXT PRIMARY KEY,
                source             TEXT,
                note               TEXT,
                score              REAL,
                classification     TEXT,
                ai_confidence      REAL,
                ai_reasoning       TEXT,
                win_rate           REAL,
                total_pnl_sol      REAL,
                median_hold_seconds REAL,
                median_entry_delay REAL,
                trade_count_30d    INTEGER,
                rug_hit_rate       REAL,
                is_followed        INTEGER DEFAULT 0,
                is_suspicious      INTEGER DEFAULT 0,
                last_scored_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS copy_signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet            TEXT,
                mint              TEXT,
                side              TEXT,
                wallet_amount_sol REAL,
                detected_at       TEXT,
                signal_age_ms     INTEGER,
                action            TEXT,
                skip_reason       TEXT,
                position_id       INTEGER
            );
            CREATE TABLE IF NOT EXISTS coin_decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                mint         TEXT,
                name         TEXT,
                decided_at   TEXT,
                result       TEXT,      -- BOUGHT | REJECTED
                reason_codes TEXT,      -- virgulle ayrilmis kural kodlari
                reason_text  TEXT,      -- insan okunur tam gerekce
                mcap         REAL,
                curve_sol    REAL,
                bundler      REAL,
                sniper       REAL,
                dev          REAL,
                top10        REAL,
                liquidity    REAL,
                volume_5m    REAL,
                position_id  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_coin_decisions_id ON coin_decisions(id DESC);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_time);
            CREATE INDEX IF NOT EXISTS idx_signals_id ON copy_signals(id DESC);
            CREATE INDEX IF NOT EXISTS idx_wallets_followed ON wallets(is_followed);
            """
        )
        _migrate(conn, "positions", POSITION_MIGRATIONS)
        _migrate(conn, "trades", TRADE_MIGRATIONS)
        _migrate(conn, "wallets", WALLET_MIGRATIONS)
        # Eski satirlarda entry_cost_sol=0 kalir; asagidaki _entry_cost() bunu
        # amount_sol'e dusurur, yani gecmis veri bozulmaz (sadece eski, eksik
        # muhasebesiyle kalir).
        conn.commit()
    log.info("Veritabani hazir: %s", DB_PATH)


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def add_position(mint: str, name: str, entry_price: float, amount_token: float,
                 amount_sol: float, is_paper: bool = False, fees_sol: float = 0.0,
                 trailing_pct: float = 30.0, entry_cost_sol: Optional[float] = None,
                 source_wallet: Optional[str] = None,
                 curve_progress: Optional[float] = None) -> int:
    """Open a position.

    `entry_cost_sol` is the TOTAL SOL that left the wallet (position size plus
    the fixed network/priority fee). PnL is measured against this, not against
    `amount_sol` - otherwise every trade under-reports its loss by the entry fee.
    """
    trailing = entry_price * (1.0 - trailing_pct / 100.0) if entry_price > 0 else 0.0
    cost = float(entry_cost_sol if entry_cost_sol is not None else amount_sol)
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """INSERT INTO positions (mint, name, entry_price, current_price, amount_token,
                                      amount_sol, timestamp, status, ath_price,
                                      trailing_stop_price, remaining_token, realized_sol,
                                      fees_sol, is_paper, total_sold_percent,
                                      entry_cost_sol, source_wallet, curve_progress_at_entry)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 0, ?, ?, 0, ?, ?, ?)""",
            (mint, name, entry_price, entry_price, amount_token, amount_sol, time.time(),
             entry_price, trailing, amount_token, fees_sol, 1 if is_paper else 0,
             cost, source_wallet, curve_progress),
        )
        conn.commit()
        return int(cur.lastrowid)


def _entry_cost(pos: Dict[str, Any]) -> float:
    """Total SOL outlay for a position, with a fallback for pre-migration rows."""
    cost = float(pos.get("entry_cost_sol") or 0.0)
    return cost if cost > 0 else float(pos.get("amount_sol") or 0.0)


def get_open_positions() -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_position(position_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    return dict(row) if row else None


def has_open_position(mint: str) -> bool:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT 1 FROM positions WHERE mint=? AND status='open'", (mint,)).fetchone()
    return row is not None


def update_position_price(position_id: int, current_price: float,
                          trailing_pct: float = 30.0) -> Dict[str, Any]:
    """Store the latest price, lift the ATH / trailing stop when a new high prints."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT ath_price FROM positions WHERE id=?", (position_id,)).fetchone()
        ath = float((row["ath_price"] if row else 0) or 0.0)
        new_ath = max(ath, current_price)
        trailing = new_ath * (1.0 - trailing_pct / 100.0)
        conn.execute(
            "UPDATE positions SET current_price=?, ath_price=?, trailing_stop_price=? WHERE id=?",
            (current_price, new_ath, trailing, position_id),
        )
        conn.commit()
    return {"ath_price": new_ath, "trailing_stop_price": trailing}


def record_partial_sell(position_id: int, percent: float, exit_price: float,
                        sol_received: float, tier: str, fees_sol: float = 0.0,
                        tokens_sold: float = 0.0) -> Optional[Dict[str, Any]]:
    """Book one (possibly partial) exit: writes a trade row and updates the position.

    The position is closed once ~100% of it has been sold.
    """
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            return None
        pos = dict(row)
        entry_price = float(pos.get("entry_price") or 0.0)
        percent = max(0.0, min(float(percent), 100.0 - float(pos.get("total_sold_percent") or 0.0)))
        if percent <= 0:
            return None

        # Maliyet payi giris ucretini de icerir; `sol_received` cikis ucretinden
        # arindirilmis geldigi icin PnL artik her iki yonun ucretini de tasiyor.
        cost_portion = _entry_cost(pos) * percent / 100.0
        pnl_sol = sol_received - cost_portion
        pnl_percent = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        now = time.time()

        total_sold = float(pos.get("total_sold_percent") or 0.0) + percent
        remaining = max(float(pos.get("remaining_token") or 0.0) - tokens_sold, 0.0)
        realized = float(pos.get("realized_sol") or 0.0) + sol_received
        fees_total = float(pos.get("fees_sol") or 0.0) + fees_sol
        closed = total_sold >= 99.5 or remaining <= 0

        tier_flags = {"tier1": "tier1_sold", "tier2": "tier2_sold", "tier3": "tier3_sold"}
        set_tier = tier_flags.get(tier)
        conn.execute(
            "UPDATE positions SET total_sold_percent=?, remaining_token=?, realized_sol=?,"
            " fees_sol=?, current_price=?, status=?%s WHERE id=?"
            % (", %s=1" % set_tier if set_tier else ""),
            (total_sold, remaining, realized, fees_total, exit_price,
             "closed" if closed else "open", position_id),
        )
        cur = conn.execute(
            """INSERT INTO trades (mint, name, entry_price, exit_price, amount_sol, pnl_sol,
                                   pnl_percent, entry_time, exit_time, tier, sold_percent,
                                   fees_sol, is_paper, source_wallet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["mint"], pos["name"], entry_price, exit_price, cost_portion, pnl_sol,
             pnl_percent, pos["timestamp"], now, tier, percent, fees_sol,
             int(pos.get("is_paper") or 0), pos.get("source_wallet")),
        )
        conn.commit()
        trade_id = int(cur.lastrowid)

    return {
        "id": trade_id,
        "position_id": position_id,
        "mint": pos["mint"],
        "name": pos["name"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "amount_sol": cost_portion,
        "pnl_sol": pnl_sol,
        "pnl_percent": pnl_percent,
        "entry_time": pos["timestamp"],
        "exit_time": now,
        "tier": tier,
        "sold_percent": percent,
        "fees_sol": fees_sol,
        "is_paper": int(pos.get("is_paper") or 0),
        "source_wallet": pos.get("source_wallet"),
        "closed": closed,
        "total_sold_percent": total_sold,
    }


def get_trades(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _midnight_ts() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def stats(is_paper: Optional[bool] = None) -> Dict[str, Any]:
    where = ""
    params: List[Any] = []
    if is_paper is not None:
        where = " WHERE is_paper=?"
        params.append(1 if is_paper else 0)
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_sol),0) AS pnl,"
            " COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END),0) AS wins,"
            " COALESCE(SUM(fees_sol),0) AS fees FROM trades" + where, params).fetchone()
        today = conn.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) AS pnl FROM trades WHERE exit_time >= ?"
            + (" AND is_paper=?" if is_paper is not None else ""),
            [_midnight_ts()] + params).fetchone()
    total = int(row["n"])
    return {
        "total_trades": total,
        "total_pnl_sol": float(row["pnl"]),
        "today_pnl_sol": float(today["pnl"]),
        "total_fees_sol": float(row["fees"]),
        "wins": int(row["wins"]),
        "win_rate": (int(row["wins"]) / total * 100.0) if total else 0.0,
    }


# --------------------------------------------------------------------------- #
# Wallets (KATMAN 1)
# --------------------------------------------------------------------------- #
WALLET_FIELDS = ("source", "note", "score", "classification", "ai_confidence", "ai_reasoning",
                 "win_rate", "total_pnl_sol", "median_hold_seconds", "median_entry_delay",
                 "trade_count_30d", "rug_hit_rate", "is_suspicious", "last_scored_at")


def upsert_wallet(address: str, **fields: Any) -> None:
    """Insert or update a scored wallet. `is_followed` is deliberately NOT
    touched here: re-scoring must never silently un-follow a wallet."""
    data = {k: v for k, v in fields.items() if k in WALLET_FIELDS}
    with _lock:
        conn = _connect()
        conn.execute("INSERT OR IGNORE INTO wallets (address) VALUES (?)", (address,))
        if data:
            assignments = ", ".join("%s=?" % k for k in data)
            conn.execute("UPDATE wallets SET %s WHERE address=?" % assignments,
                         list(data.values()) + [address])
        conn.commit()


def get_wallets(followed_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM wallets"
    if followed_only:
        sql += " WHERE is_followed=1"
    sql += " ORDER BY COALESCE(score, -1) DESC, address"
    with _lock:
        conn = _connect()
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_wallet(address: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
    return dict(row) if row else None


def set_wallet_followed(address: str, followed: bool) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("UPDATE wallets SET is_followed=? WHERE address=?",
                           (1 if followed else 0, address))
        conn.commit()
    return cur.rowcount > 0


def delete_wallet(address: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM wallets WHERE address=?", (address,))
        conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Copy signals (KATMAN 2)
# --------------------------------------------------------------------------- #
def add_copy_signal(wallet: str, mint: str, side: str, wallet_amount_sol: float,
                    signal_age_ms: int, action: str, skip_reason: Optional[str] = None,
                    position_id: Optional[int] = None) -> Dict[str, Any]:
    """Record one observed leader trade - copied or skipped, no exceptions.

    The old sniper published its reject reasons to the panel and persisted none
    of them, so nobody could ever measure which rule was doing the damage. Every
    decision this engine makes lands here instead.
    """
    detected_at = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """INSERT INTO copy_signals (wallet, mint, side, wallet_amount_sol, detected_at,
                                         signal_age_ms, action, skip_reason, position_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet, mint, side, wallet_amount_sol, detected_at, int(signal_age_ms),
             action, skip_reason, position_id),
        )
        conn.commit()
        signal_id = int(cur.lastrowid)
    return {
        "id": signal_id, "wallet": wallet, "mint": mint, "side": side,
        "wallet_amount_sol": wallet_amount_sol, "detected_at": detected_at,
        "signal_age_ms": int(signal_age_ms), "action": action,
        "skip_reason": skip_reason, "position_id": position_id,
    }


def get_copy_signals(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM copy_signals ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def copy_signal_stats() -> Dict[str, Any]:
    """Counts per action and per skip reason - the measurement the old bot lacked."""
    with _lock:
        conn = _connect()
        actions = conn.execute(
            "SELECT action, COUNT(*) AS n FROM copy_signals GROUP BY action").fetchall()
        reasons = conn.execute(
            "SELECT skip_reason, COUNT(*) AS n FROM copy_signals"
            " WHERE skip_reason IS NOT NULL GROUP BY skip_reason ORDER BY n DESC").fetchall()
    return {
        "actions": {r["action"]: int(r["n"]) for r in actions},
        "skip_reasons": {r["skip_reason"]: int(r["n"]) for r in reasons},
    }


# --------------------------------------------------------------------------- #
# Coin decisions (legacy sniper)
# --------------------------------------------------------------------------- #
def add_coin_decision(row: Dict[str, Any], result: str, reason_codes: List[str],
                      reason_text: str, position_id: Optional[int] = None) -> None:
    """Sniper'in bir coin hakkindaki kararini kaydet - RED DAHIL.

    19 Agustos calistirmasinin en buyuk eksigi buydu: `analyzer.evaluate` her
    red icin duzgun bir gerekce uretiyor ve panele basiyordu, ama hicbir yere
    yazmiyordu. Sonuc olarak hangi kuralin kac coini eledigi hic olculemedi ve
    filtrenin ters secim yaptigi ancak aylar sonra cikarimla anlasildi.
    """
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO coin_decisions (mint, name, decided_at, result, reason_codes,
                                           reason_text, mcap, curve_sol, bundler, sniper,
                                           dev, top10, liquidity, volume_5m, position_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row.get("mint"), row.get("name"), datetime.now().isoformat(timespec="seconds"),
             result, ",".join(reason_codes), reason_text[:500],
             row.get("mcap"), row.get("curve_sol"), row.get("bundler"), row.get("sniper"),
             row.get("dev"), row.get("top10"), row.get("liquidity"), row.get("volume_5m"),
             position_id),
        )
        conn.commit()


def get_coin_decisions(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM coin_decisions ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [dict(r) for r in rows]


def coin_decision_stats() -> Dict[str, Any]:
    """Hangi kural kac coini eledi. Sniper'in hic sahip olmadigi olcum.

    Bir coin birden fazla kurala takilabilir; `by_rule` her kuralin KAC KEZ
    devreye girdigini sayar (toplamlari coin sayisini asabilir). `sole_blocker`
    ise yalnizca O KURAL yuzunden reddedilen coinleri sayar - asil onemli olan
    budur: bir kurali gevsetmenin kac coini serbest birakacagini soyler.
    """
    with _lock:
        conn = _connect()
        totals = conn.execute(
            "SELECT result, COUNT(*) AS n FROM coin_decisions GROUP BY result").fetchall()
        rows = conn.execute(
            "SELECT reason_codes FROM coin_decisions WHERE result='REJECTED'").fetchall()

    by_rule: Dict[str, int] = {}
    sole: Dict[str, int] = {}
    for row in rows:
        codes = [c for c in (row["reason_codes"] or "").split(",") if c]
        for code in set(codes):
            by_rule[code] = by_rule.get(code, 0) + 1
        if len(set(codes)) == 1:
            only = codes[0]
            sole[only] = sole.get(only, 0) + 1
    return {
        "totals": {r["result"]: int(r["n"]) for r in totals},
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "sole_blocker": dict(sorted(sole.items(), key=lambda kv: -kv[1])),
    }


def reset_paper_history() -> None:
    """Wipe simulated positions and trades (real ones are left untouched)."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM trades WHERE is_paper=1")
        conn.execute("DELETE FROM positions WHERE is_paper=1")
        conn.commit()
    log.info("Paper gecmisi sifirlandi")
