"""SQLite 连接与 schema。

设计要点：
- 连接使用 isolation_level=None（自动提交），事务全部用显式
  BEGIN / BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免 Python
  sqlite3 默认的隐式事务行为干扰一致性语义。
- WAL 模式：读事务不阻塞写事务，读事务在开始时拿到稳定的已提交快照，
  保证"一次查询的四季来自同一已提交视图"，且另一窗口上传期间查询不被卡住。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "./data/app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company       TEXT NOT NULL,
    fiscal_year   INTEGER NOT NULL,
    report_no     TEXT NOT NULL,
    disclosed_at  TEXT NOT NULL,          -- 规范化 UTC ISO 时刻
    content_hash  TEXT NOT NULL,          -- 规范化内容哈希，用于幂等与冲突检测
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (company, report_no)
);

CREATE TABLE IF NOT EXISTS facts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           INTEGER NOT NULL REFERENCES reports(id),
    company             TEXT NOT NULL,
    fiscal_year         INTEGER NOT NULL,
    quarter             INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    disclosed_at        TEXT NOT NULL,    -- 该累计值被披露的时刻（规范化 UTC ISO）
    cumulative_revenue  INTEGER NOT NULL, -- 累计营收，整数元
    UNIQUE (company, fiscal_year, quarter, disclosed_at)
);

CREATE INDEX IF NOT EXISTS idx_facts_visibility
    ON facts (company, fiscal_year, quarter, disclosed_at);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：FastAPI 依赖注入与同步端点可能位于不同线程；
    # 每个请求独占一条连接且事务显式串行，跨线程使用是安全的。
    conn = sqlite3.connect(path, isolation_level=None, timeout=15,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def path_from_env() -> str:
    return os.environ.get("APP_DB_PATH", DEFAULT_DB_PATH)
