"""InstaGrid — SQLite база данных с WAL-режимом.

Таблицы:
  niches           — группы аккаунтов
  accounts         — Instagram-аккаунты
  proxy_pools      — именованные пулы прокси (static / mobile)
  static_proxies   — отдельные статические прокси
  mobile_ip_history — история использованных мобильных IP
  logs             — события для дашборда
"""
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from backend.config import DB_PATH

_local = threading.local()
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS niches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS proxy_pools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    pool_type       TEXT    NOT NULL CHECK (pool_type IN ('static', 'mobile')),
    -- mobile-only fields:
    rotation_url    TEXT,
    proxy_host      TEXT,
    proxy_port      INTEGER,
    proxy_username  TEXT,
    proxy_password  TEXT,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at      REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS static_proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id     INTEGER NOT NULL REFERENCES proxy_pools(id) ON DELETE CASCADE,
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    username    TEXT,
    password    TEXT,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    status      TEXT    NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available', 'bound', 'burned')),
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now')),
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id        INTEGER REFERENCES niches(id) ON DELETE SET NULL,
    username        TEXT    NOT NULL,
    password        TEXT    NOT NULL,
    totp_secret     TEXT,
    status          TEXT    NOT NULL DEFAULT 'new'
                            CHECK (status IN ('new', 'logged_in', 'cooldown', 'dead')),
    static_proxy_id INTEGER UNIQUE REFERENCES static_proxies(id) ON DELETE SET NULL,
    mobile_pool_id  INTEGER REFERENCES proxy_pools(id) ON DELETE SET NULL,
    fingerprint_path TEXT,
    profile_path    TEXT,
    cooldown_until  REAL,
    last_login_at   REAL,
    last_action_at  REAL,
    notes           TEXT,
    created_at      REAL    NOT NULL DEFAULT (unixepoch('now')),
    updated_at      REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS mobile_ip_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id     INTEGER NOT NULL REFERENCES proxy_pools(id) ON DELETE CASCADE,
    ip_address  TEXT    NOT NULL,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    used_at     REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT    NOT NULL DEFAULT 'INFO',
    module      TEXT,
    account_id  INTEGER,
    message     TEXT    NOT NULL,
    details     TEXT,
    created_at  REAL    NOT NULL DEFAULT (unixepoch('now'))
);

CREATE INDEX IF NOT EXISTS idx_accounts_niche      ON accounts(niche_id);
CREATE INDEX IF NOT EXISTS idx_accounts_status      ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_static_proxies_pool  ON static_proxies(pool_id);
CREATE INDEX IF NOT EXISTS idx_static_proxies_status ON static_proxies(status);
CREATE INDEX IF NOT EXISTS idx_mobile_ip_pool       ON mobile_ip_history(pool_id);
CREATE INDEX IF NOT EXISTS idx_logs_created         ON logs(created_at);
CREATE INDEX IF NOT EXISTS idx_logs_account         ON logs(account_id);
"""


def _get_conn() -> sqlite3.Connection:
    """Один коннект на поток, WAL, retry при locked."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    _local.conn = conn
    return conn


def init_db():
    """Создать все таблицы если не существуют."""
    conn = _get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def get_db():
    """Context manager для работы с БД. Автокоммит при успехе, ролбэк при ошибке."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: tuple = ()) -> list[dict]:
    """SELECT — вернуть список словарей."""
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    """SELECT — вернуть один словарь или None."""
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE — вернуть lastrowid."""
    conn = _get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def execute_many(sql: str, params_list: list[tuple]) -> int:
    """Batch INSERT/UPDATE/DELETE."""
    conn = _get_conn()
    conn.executemany(sql, params_list)
    conn.commit()
    return len(params_list)
