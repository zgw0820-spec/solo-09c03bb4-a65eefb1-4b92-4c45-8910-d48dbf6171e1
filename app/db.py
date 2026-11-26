"""SQLite 连接、建表与旧库升级。

表结构设计要点（v2：增量缺口模型）：

- ``build_files``     登记阶段写入：每个构建预期有哪些分片、每个文件有哪些
                      可执行行/分支（来自构建登记，不随上报变化）。
- ``reports``         每个 (build, shard) 当前生效的一份尝试（只保留最高尝试号）。
- ``coverage_rows``   命中明细：某条行/分支被哪份报告命中。保留它用于换报时
                      计算“新增命中 / 撤销命中”两个差异集合；查询路径不再扫描。
- ``item_hits``       登记项级别的命中引用计数：一条行/分支当前被多少个分片命中。
                      换报时按差异集合做 ±1 的集合化更新，不重放全量明细。
- ``uncovered_gaps``  缺口表：登记项中命中分片数为 0 的条目。收齐后按文件查询
                      直接读这张表，既不扫描 coverage_rows，也不扫描已覆盖项。
- ``builds``          新增 ``complete``（收齐标记）与 ``coverage_version``
                      （每次接受的上报/换报 +1，单调递增）。二者与缺口表在同一个
                      写事务内提交，读侧通过快照事务拿到一致的同版数据。

旧版数据库（``PRAGMA user_version == 0``）在启动时一次性回填 item_hits /
uncovered_gaps / complete / coverage_version，之后重启只跑幂等 DDL，不再重算。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .config import settings

# 当前应用期望的数据库格式版本。0 = 仅含 coverage_rows 的旧库；1 = 增量缺口模型。
SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS builds (
    build_id         TEXT PRIMARY KEY,
    expected_shards  INTEGER NOT NULL CHECK (expected_shards > 0),
    -- 收齐标记与覆盖版本随每次接受的上报/换报在同一事务内更新
    complete         INTEGER NOT NULL DEFAULT 0,
    coverage_version INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
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

-- 登记项级命中引用计数：(build, item) 当前被多少个分片命中。
-- 只统计登记清单内的条目；未登记路径的命中不进此表，也不参与缺口判定。
CREATE TABLE IF NOT EXISTS item_hits (
    build_id    TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('line', 'branch')),
    line_no     INTEGER NOT NULL,
    branch_no   INTEGER,
    hit_shards  INTEGER NOT NULL CHECK (hit_shards >= 0),
    UNIQUE (build_id, path, kind, line_no, branch_no)
);

-- 缺口表：登记项里当前没有任何分片命中的行/分支。查询只读这张表。
CREATE TABLE IF NOT EXISTS uncovered_gaps (
    build_id        TEXT NOT NULL REFERENCES builds(build_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('line', 'branch')),
    line_no         INTEGER NOT NULL,
    branch_no       INTEGER,
    UNIQUE (build_id, path, kind, line_no, branch_no)
);

CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON coverage_rows (build_id, path, kind, line_no, branch_no);
CREATE INDEX IF NOT EXISTS idx_coverage_shard
    ON coverage_rows (build_id, shard);
CREATE INDEX IF NOT EXISTS idx_build_files_build
    ON build_files (build_id, path);
CREATE INDEX IF NOT EXISTS idx_reports_build
    ON reports (build_id, shard);
CREATE INDEX IF NOT EXISTS idx_item_hits_build
    ON item_hits (build_id, path);
CREATE INDEX IF NOT EXISTS idx_gaps_build_path
    ON uncovered_gaps (build_id, path);
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
# 全局单连接被所有请求线程共用：串行化显式事务的开启/提交，避免两个线程
# 在同一连接上交错 BEGIN（SQLite 单连接不支持嵌套事务）。锁内只做 SQLite
# 调用，WAL 仍允许其他连接的读与写互斥并发。
_tx_lock = threading.RLock()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把仅含 coverage_rows 的旧库一次性升级到增量缺口模型。

    全部在单个写事务内完成；升级后无需重传任何报告，且后续重启只执行幂等
    DDL（user_version 已推进），不会再做全库重算。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    # builds 是首个版本就有的表；其新增列需先补齐（无论 user_version）。
    build_cols = _table_columns(conn, "builds")
    if build_cols and "complete" not in build_cols:
        conn.execute("ALTER TABLE builds ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
    if build_cols and "coverage_version" not in build_cols:
        conn.execute("ALTER TABLE builds ADD COLUMN coverage_version INTEGER NOT NULL DEFAULT 0")

    if version >= SCHEMA_VERSION:
        return

    with write_transaction(conn):
        # 引用计数：按登记项聚合当前命中分片数（未登记命中不参与缺口判定）。
        conn.execute(
            """
            INSERT OR IGNORE INTO item_hits
                (build_id, path, kind, line_no, branch_no, hit_shards)
            SELECT cr.build_id, cr.path, cr.kind, cr.line_no, cr.branch_no,
                   COUNT(DISTINCT cr.shard)
              FROM coverage_rows cr
              JOIN build_files bf
                ON bf.build_id = cr.build_id
               AND bf.path = cr.path
               AND bf.kind = cr.kind
               AND bf.line_no = cr.line_no
               AND (bf.branch_no IS cr.branch_no OR bf.branch_no = cr.branch_no)
             GROUP BY cr.build_id, cr.path, cr.kind, cr.line_no, cr.branch_no
            """
        )
        # 缺口：登记项中没有任何分片命中的条目。
        conn.execute(
            """
            INSERT OR IGNORE INTO uncovered_gaps
                (build_id, path, kind, line_no, branch_no)
            SELECT bf.build_id, bf.path, bf.kind, bf.line_no, bf.branch_no
              FROM build_files bf
             WHERE NOT EXISTS (
                       SELECT 1 FROM item_hits ih
                        WHERE ih.build_id = bf.build_id
                          AND ih.path = bf.path
                          AND ih.kind = bf.kind
                          AND ih.line_no = bf.line_no
                          AND (ih.branch_no IS bf.branch_no
                               OR ih.branch_no = bf.branch_no)
                   )
            """
        )
        # 收齐标记与版本号：以现有 reports 为准，版本号取各分片尝试号之和，
        # 与旧接口 _coverage_version 的口径保持连续。
        conn.execute(
            """
            UPDATE builds
               SET complete = CASE WHEN (
                        SELECT COUNT(*) FROM reports r
                         WHERE r.build_id = builds.build_id
                   ) >= expected_shards THEN 1 ELSE 0 END,
                   coverage_version = COALESCE((
                        SELECT SUM(attempt) FROM reports r
                         WHERE r.build_id = builds.build_id
                   ), 0)
            """
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """初始化（或复用）全局连接，执行建表 DDL 并按需升级旧库。"""
    global _conn
    if _conn is None:
        _conn = _connect(db_path or settings.db_path)
        with _tx_lock:
            _conn.executescript(SCHEMA)
            _migrate_legacy(_conn)
    return _conn


def get_conn() -> sqlite3.Connection:
    """FastAPI 依赖：返回已初始化的连接。"""
    return init_db()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即取写锁的事务。

    BEGIN IMMEDIATE 让并发换报串行化；等待期间读请求仍走 WAL 快照，
    看到的是完整旧版本。事务内任何异常都会回滚，旧版数据继续生效。
    缺口、收齐标记与覆盖版本在同一事务内提交，要么整体生效要么整体不动。
    """
    with _tx_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：保证一次请求读到的缺口、收齐状态与版本号来自同一已提交快照。

    WAL 下普通自动提交语句各自取最新快照；显式 DEFERRED 事务把它们绑到
    同一个快照上，换报在期间提交也不会让本请求读到半新半旧的数据。
    """
    with _tx_lock:
        conn.execute("BEGIN DEFERRED")
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
    with _tx_lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn = _connect(db_path)
        _conn.executescript(SCHEMA)
        # 全新库：DDL 即最新结构，直接登记格式版本，跳过迁移路径。
        _conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return _conn
