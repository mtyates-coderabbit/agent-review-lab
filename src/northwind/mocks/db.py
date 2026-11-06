"""In-memory SQLite database seeded at runtime. Nothing is written to disk."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from typing import Any

from northwind.mocks import fixtures

SCHEMA = """
CREATE TABLE suppliers (supplier_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE sites (site_id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL);
CREATE TABLE purchase_orders (
    po_number TEXT PRIMARY KEY, supplier_id TEXT, site_id TEXT, product TEXT,
    gallons INTEGER, price_per_gallon REAL, expected_delivery TEXT
);
CREATE TABLE delivery_tickets (
    ticket_id TEXT PRIMARY KEY, po_number TEXT, delivered_gallons INTEGER,
    delivery_date TEXT, driver_notes TEXT
);
CREATE TABLE invoices (
    invoice_id TEXT PRIMARY KEY, supplier_id TEXT, po_number TEXT, product TEXT,
    billed_gallons INTEGER, price_per_gallon REAL, delivery_date TEXT,
    total_usd REAL, supplier_notes TEXT
);
CREATE TABLE invoice_decisions (
    invoice_id TEXT PRIMARY KEY, decision TEXT NOT NULL, reason TEXT,
    decided_by TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE TABLE tanks (
    site_id TEXT, product TEXT, capacity_gal INTEGER, current_gal INTEGER,
    reorder_point_gal INTEGER, site_notes TEXT, PRIMARY KEY (site_id, product)
);
CREATE TABLE price_list (
    supplier_id TEXT, product TEXT, price_per_gallon REAL, supplier_notes TEXT,
    PRIMARY KEY (supplier_id, product)
);
CREATE TABLE quotes (
    quote_id TEXT PRIMARY KEY, site_id TEXT, product TEXT, gallons INTEGER,
    supplier_id TEXT, price_per_gallon REAL, total_usd REAL, expires_at TEXT,
    supplier_notes TEXT
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY, quote_id TEXT UNIQUE, site_id TEXT, product TEXT,
    gallons INTEGER, total_usd REAL, approved_by TEXT, placed_at TEXT
);
"""


class MockDatabase:
    """Thread-safe wrapper around a private in-memory SQLite connection.

    Services call into this from worker threads via ``asyncio.to_thread`` so the
    event loop is never blocked on database I/O.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._seed()

    def _seed(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.executemany("INSERT INTO suppliers VALUES (?, ?)", fixtures.SUPPLIERS)
            self._conn.executemany("INSERT INTO sites VALUES (?, ?, ?)", fixtures.SITES)
            self._conn.executemany(
                "INSERT INTO purchase_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                fixtures.PURCHASE_ORDERS,
            )
            self._conn.executemany(
                "INSERT INTO delivery_tickets VALUES (?, ?, ?, ?, ?)", fixtures.DELIVERY_TICKETS
            )
            self._conn.executemany(
                "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", fixtures.INVOICES
            )
            self._conn.executemany("INSERT INTO tanks VALUES (?, ?, ?, ?, ?, ?)", fixtures.TANKS)
            self._conn.executemany(
                "INSERT INTO price_list VALUES (?, ?, ?, ?)", fixtures.PRICE_LIST
            )

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(sql, tuple(params))
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
