"""SQLite 连接与建表。

表结构设计要点：

- ``build_files``   登记阶段写入：每个构建预期有哪些分片、每个文件有哪些
                    可执行行/分支（来自构建登记，不随上报变化）。
- ``reports``       每个 (build, shard) 当前生效的一份尝试（只保留最高尝试号）。
- ``coverage_rows`` 命中明细：某条行/分支被哪份报告命中。
                    换报 = 在一个事务里删除旧报告明细 + 写入新报告明细，
                    聚合视图随之整体切换，旧版本在事务期间仍可完整查询，
                    失败回滚后沿用旧版。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS builds (
    build_id        TEXT PRIMARY KEY,
    expected_shards INTEGER NOT NULL CHECK (expected_shards > 0),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 注：不用 WITHOUT ROWID —— 其主键列隐式 NOT NULL，无法在行条目里
-- 用 NULL 的 branch_no 与分支条目共存。
CREATE TABLE IF NOT EXISTS build_files (
    build_id        TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('line', 'branch')),
    -- 行：line_no 为行号，branch_no 为 NULL
    -- 分支：line_no 为源行号，branch_no 为 coverage.py 的分支编号（arcs 中目标行号）
    line_no         INTEGER NOT NULL,
    branch_no       INTEGER,
    UNIQUE (build_id, path, kind, line_no, branch_no)
);

CREATE TABLE IF NOT EXISTS reports (
    build_id    TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    shard       INTEGER NOT NULL CHECK (shard >= 0),
    attempt     INTEGER NOT NULL CHECK (attempt >= 1),
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (build_id, shard)
);

CREATE TABLE IF NOT EXISTS coverage_rows (
    build_id  TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    shard     INTEGER NOT NULL,
    path      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('line', 'branch')),
    line_no   INTEGER NOT NULL,
    branch_no INTEGER,
    UNIQUE (build_id, shard, path, kind, line_no, branch_no)
);

CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON coverage_rows (build_id, path, kind, line_no, branch_no);
CREATE INDEX IF NOT EXISTS idx_build_files_build
    ON build_files (build_id, path);
CREATE INDEX IF NOT EXISTS idx_reports_build
    ON reports (build_id, shard);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,  # 显式事务，BEGIN IMMEDIATE 控制写锁
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


_conn: sqlite3.Connection | None = None


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """初始化（或复用）全局连接并执行建表 DDL。"""
    global _conn
    if _conn is None:
        _conn = _connect(db_path or settings.db_path)
        _conn.executescript(SCHEMA)
    return _conn


def get_conn() -> sqlite3.Connection:
    """FastAPI 依赖：返回已初始化的连接。"""
    return init_db()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即取写锁的事务。

    BEGIN IMMEDIATE 让并发换报串行化；等待期间读请求仍走 WAL 快照，
    看到的是完整旧版本。事务内任何异常都会回滚，旧版数据继续生效。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reset_for_tests(db_path: str) -> sqlite3.Connection:
    """测试辅助：关闭全局连接并按给定路径重新初始化。"""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None
    _conn = _connect(db_path)
    _conn.executescript(SCHEMA)
    return _conn
