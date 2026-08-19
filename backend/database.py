"""SQLite storage for open positions and closed (or partially closed) trades."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("market-fucker")

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
}
TRADE_MIGRATIONS = {
    "tier": "TEXT",
    "sold_percent": "REAL DEFAULT 100",
    "fees_sol": "REAL DEFAULT 0",
    "is_paper": "INTEGER DEFAULT 0",
}


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
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_time);
            """
        )
        _migrate(conn, "positions", POSITION_MIGRATIONS)
        _migrate(conn, "trades", TRADE_MIGRATIONS)
        conn.commit()
    log.info("Veritabani hazir: %s", DB_PATH)


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
def add_position(mint: str, name: str, entry_price: float, amount_token: float,
                 amount_sol: float, is_paper: bool = False, fees_sol: float = 0.0,
                 trailing_pct: float = 30.0) -> int:
    trailing = entry_price * (1.0 - trailing_pct / 100.0) if entry_price > 0 else 0.0
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """INSERT INTO positions (mint, name, entry_price, current_price, amount_token,
                                      amount_sol, timestamp, status, ath_price,
                                      trailing_stop_price, remaining_token, realized_sol,
                                      fees_sol, is_paper, total_sold_percent)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 0, ?, ?, 0)""",
            (mint, name, entry_price, entry_price, amount_token, amount_sol, time.time(),
             entry_price, trailing, amount_token, fees_sol, 1 if is_paper else 0),
        )
        conn.commit()
        return int(cur.lastrowid)


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
        entry_sol = float(pos.get("amount_sol") or 0.0)
        percent = max(0.0, min(float(percent), 100.0 - float(pos.get("total_sold_percent") or 0.0)))
        if percent <= 0:
            return None

        cost_portion = entry_sol * percent / 100.0
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
                                   fees_sol, is_paper)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["mint"], pos["name"], entry_price, exit_price, cost_portion, pnl_sol,
             pnl_percent, pos["timestamp"], now, tier, percent, fees_sol,
             int(pos.get("is_paper") or 0)),
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


def reset_paper_history() -> None:
    """Wipe simulated positions and trades (real ones are left untouched)."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM trades WHERE is_paper=1")
        conn.execute("DELETE FROM positions WHERE is_paper=1")
        conn.commit()
    log.info("Paper gecmisi sifirlandi")
