"""SQLite persistence. Every row is keyed by venue and trader address so the
schema already fits a multi-user app."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    trader TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size TEXT NOT NULL,
    price TEXT,
    tif TEXT NOT NULL,
    status TEXT NOT NULL,
    venue_order_id TEXT,
    builder TEXT,
    builder_fee_bps TEXT,
    dry_run INTEGER NOT NULL,
    error TEXT,
    raw TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_trader ON orders (venue, trader, created_at);

CREATE TABLE IF NOT EXISTS fills (
    venue TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    trader TEXT NOT NULL,
    order_id TEXT,
    symbol TEXT,
    side TEXT,
    price TEXT,
    size TEXT,
    fee TEXT,
    builder_fee TEXT,
    ts INTEGER,
    raw TEXT,
    PRIMARY KEY (venue, fill_id, trader)
);
CREATE INDEX IF NOT EXISTS fills_trader ON fills (venue, trader, ts);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    trader TEXT NOT NULL,
    builder TEXT NOT NULL,
    max_fee_bps TEXT NOT NULL,
    raw TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS builder_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    builder TEXT NOT NULL,
    raw TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # orders -----------------------------------------------------------------
    def insert_order(self, row: dict[str, Any]) -> int:
        cur = self._execute(
            """INSERT INTO orders (venue, trader, symbol, side, size, price, tif, status,
               venue_order_id, builder, builder_fee_bps, dry_run, error, raw, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["venue"], row.get("trader"), row["symbol"], row["side"], row["size"],
                row.get("price"), row["tif"], row["status"], row.get("venue_order_id"),
                row.get("builder"), row.get("builder_fee_bps"), int(row.get("dry_run", False)),
                row.get("error"), json.dumps(row.get("raw"), default=str), now_ms(),
            ),
        )
        return int(cur.lastrowid)

    def set_order_status(self, order_id: int, status: str) -> None:
        self._execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))

    def mark_cancelled(self, venue: str, venue_order_id: str) -> None:
        self._execute(
            "UPDATE orders SET status='cancelled' WHERE venue=? AND venue_order_id=?",
            (venue, venue_order_id),
        )

    def list_orders(self, venue: str | None = None, limit: int = 100) -> list[dict]:
        if venue:
            return self._query(
                "SELECT * FROM orders WHERE venue=? ORDER BY id DESC LIMIT ?", (venue, limit)
            )
        return self._query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    # fills ------------------------------------------------------------------
    def upsert_fills(self, venue: str, trader: str, fills: list[dict[str, Any]]) -> int:
        new = 0
        for f in fills:
            cur = self._execute(
                """INSERT OR IGNORE INTO fills (venue, fill_id, trader, order_id, symbol, side,
                   price, size, fee, builder_fee, ts, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    venue, str(f["fill_id"]), trader, f.get("order_id"), f.get("symbol"),
                    f.get("side"), f.get("price"), f.get("size"), f.get("fee"),
                    f.get("builder_fee"), f.get("ts"), json.dumps(f.get("raw"), default=str),
                ),
            )
            new += cur.rowcount
        return new

    def list_fills(self, venue: str | None = None, limit: int = 100) -> list[dict]:
        if venue:
            return self._query(
                "SELECT * FROM fills WHERE venue=? ORDER BY ts DESC LIMIT ?", (venue, limit)
            )
        return self._query("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))

    # approvals / earnings ---------------------------------------------------
    def insert_approval(self, venue: str, trader: str, builder: str, max_fee_bps: str, raw: Any) -> None:
        self._execute(
            "INSERT INTO approvals (venue, trader, builder, max_fee_bps, raw, created_at) VALUES (?,?,?,?,?,?)",
            (venue, trader, builder, max_fee_bps, json.dumps(raw, default=str), now_ms()),
        )

    def list_approvals(self, venue: str | None = None) -> list[dict]:
        if venue:
            return self._query("SELECT * FROM approvals WHERE venue=? ORDER BY id DESC", (venue,))
        return self._query("SELECT * FROM approvals ORDER BY id DESC")

    def insert_earnings_snapshot(self, venue: str, builder: str, raw: Any) -> None:
        self._execute(
            "INSERT INTO builder_earnings (venue, builder, raw, created_at) VALUES (?,?,?,?)",
            (venue, builder, json.dumps(raw, default=str), now_ms()),
        )
