from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH, DB_BUSY_TIMEOUT_MS

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
'''


def connect() -> sqlite3.Connection:
    # autocommit：事务边界一律由 read_tx/write_tx 显式管理，
    # 避免 sqlite3 模块隐式开启的 deferred 事务在写时才取锁。
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        isolation_level=None,
        timeout=DB_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    """读路径：autocommit 下每条 SELECT 自成一读事务，无写锁。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写路径：BEGIN IMMEDIATE 立即取 RESERVED 锁，全局写事务串行化。

    同一时刻仅一个写事务进入临界区，配合条件 UPDATE 的 rowcount 守卫，
    保证重叫与新叫号抢窗、同号并发重叫不会双占窗；
    临界区内任一步抛错即 ROLLBACK，号牌状态、窗口当前号指针、
    位次物化、事件、总览桶同成败，不存在半刷。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
