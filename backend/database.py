"""SQLite storage for open positions and closed trades."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("market-fucker")

DB_PATH = Path(__file__).resolve().parent.parent / "trades.db"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


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
            """
        )
        conn.commit()
    log.info("Veritabani hazir: %s", DB_PATH)


def add_position(mint: str, name: str, entry_price: float, amount_token: float, amount_sol: float) -> int:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """INSERT INTO positions (mint, name, entry_price, current_price, amount_token,
                                      amount_sol, timestamp, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (mint, name, entry_price, entry_price, amount_token, amount_sol, time.time()),
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


def update_position_price(position_id: int, current_price: float) -> None:
    with _lock:
        conn = _connect()
        conn.execute("UPDATE positions SET current_price=? WHERE id=?", (current_price, position_id))
        conn.commit()


def close_position(position_id: int, exit_price: float, sol_received: float) -> Optional[Dict[str, Any]]:
    """Mark a position closed and write the matching trade row."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            return None
        pos = dict(row)
        entry_sol = float(pos.get("amount_sol") or 0.0)
        pnl_sol = sol_received - entry_sol
        entry_price = float(pos.get("entry_price") or 0.0)
        pnl_percent = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        now = time.time()
        conn.execute(
            "UPDATE positions SET status='closed', current_price=? WHERE id=?",
            (exit_price, position_id),
        )
        cur = conn.execute(
            """INSERT INTO trades (mint, name, entry_price, exit_price, amount_sol, pnl_sol,
                                   pnl_percent, entry_time, exit_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos["mint"], pos["name"], entry_price, exit_price, entry_sol, pnl_sol,
             pnl_percent, pos["timestamp"], now),
        )
        conn.commit()
        trade_id = int(cur.lastrowid)
    return {
        "id": trade_id,
        "mint": pos["mint"],
        "name": pos["name"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "amount_sol": entry_sol,
        "pnl_sol": pnl_sol,
        "pnl_percent": pnl_percent,
        "entry_time": pos["timestamp"],
        "exit_time": now,
    }


def get_trades(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats() -> Dict[str, Any]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_sol),0) AS pnl, "
            "COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END),0) AS wins FROM trades"
        ).fetchone()
    total = int(row["n"])
    return {
        "total_trades": total,
        "total_pnl_sol": float(row["pnl"]),
        "wins": int(row["wins"]),
        "win_rate": (int(row["wins"]) / total * 100.0) if total else 0.0,
    }
