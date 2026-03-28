from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import settings


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    website_url TEXT NOT NULL,
    adapter_kind TEXT NOT NULL DEFAULT 'mock',
    recharge_ratio REAL NOT NULL DEFAULT 1.0,
    login_state_path TEXT NOT NULL DEFAULT '',
    browser_profile_dir TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    developer TEXT NOT NULL DEFAULT '',
    context_length INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    model_id INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider_id, raw_name),
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
    FOREIGN KEY(model_id) REFERENCES models(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    model_id INTEGER NOT NULL,
    input_price REAL NOT NULL,
    output_price REAL NOT NULL,
    fiat_input_price REAL NOT NULL DEFAULT 0,
    fiat_output_price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    unit TEXT NOT NULL DEFAULT '1M tokens',
    source_url TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
    FOREIGN KEY(model_id) REFERENCES models(id) ON DELETE CASCADE,
    UNIQUE(provider_id, model_id)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
);
"""

DB_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.数据库路径)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db() -> None:
    with DB_LOCK:
        with db_session() as conn:
            conn.executescript(SCHEMA_SQL)
    ensure_provider_columns()
    ensure_price_columns()


def ensure_provider_columns() -> None:
    columns = {row["name"] for row in fetch_all("PRAGMA table_info(providers)")}
    additions = [
        ("recharge_ratio", "REAL NOT NULL DEFAULT 1.0"),
        ("login_state_path", "TEXT NOT NULL DEFAULT ''"),
        ("browser_profile_dir", "TEXT NOT NULL DEFAULT ''"),
    ]
    for column, ddl in additions:
        if column not in columns:
            execute(f"ALTER TABLE providers ADD COLUMN {column} {ddl}")


def ensure_price_columns() -> None:
    columns = {row["name"] for row in fetch_all("PRAGMA table_info(prices)")}
    additions = [
        ("fiat_input_price", "REAL NOT NULL DEFAULT 0"),
        ("fiat_output_price", "REAL NOT NULL DEFAULT 0"),
    ]
    for column, ddl in additions:
        if column not in columns:
            execute(f"ALTER TABLE prices ADD COLUMN {column} {ddl}")

    execute(
        """
        UPDATE prices
        SET fiat_input_price = CASE
                WHEN fiat_input_price = 0 THEN input_price * COALESCE((SELECT recharge_ratio FROM providers WHERE providers.id = prices.provider_id), 1.0)
                ELSE fiat_input_price
            END,
            fiat_output_price = CASE
                WHEN fiat_output_price = 0 THEN output_price * COALESCE((SELECT recharge_ratio FROM providers WHERE providers.id = prices.provider_id), 1.0)
                ELSE fiat_output_price
            END
        """
    )


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with db_session() as conn:
        conn.execute(sql, tuple(params))


def executemany(sql: str, params: Sequence[Sequence[Any]]) -> None:
    with db_session() as conn:
        conn.executemany(sql, params)


def fetch_all(sql: str, params: Iterable[Any] = ()):
    with db_session() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def fetch_one(sql: str, params: Iterable[Any] = ()):
    with db_session() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def json_payload(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
