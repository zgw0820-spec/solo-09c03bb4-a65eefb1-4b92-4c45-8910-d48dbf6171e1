"""数据访问与换报核心逻辑。

换报规则（全部在单个 SQLite 写事务内完成）：

1. 同号重发：不删除、不写入、不改任何结果，返回 ``duplicate_unchanged``。
2. 更高尝试号：删除该分片旧报告的命中明细，写入新报告明细并更新 attempts，
   成功后整体切换；事务期间读请求通过 WAL 看到完整旧版，异常回滚沿用旧版。
3. 更低尝试号：拒绝（``superseded_attempt``），当前生效报告不动。

“某行/分支是否覆盖”按全部已收齐（或当前已收到）分片的命中明细聚合：
只有当一条目失去最后一份命中时，它才会从未覆盖状态的视角变回未覆盖——
换报只删旧报告 + 加新报告，聚合结果天然满足这一性质。
"""

from __future__ import annotations

import sqlite3

from .db import write_transaction
from .schemas import BuildManifestRequest, CoverageFile, ReportUploadRequest


class ConflictError(Exception):
    """与既有数据冲突（构建已登记、分片号越界、尝试号过旧等）。"""


class NotFoundError(Exception):
    """构建不存在。"""


# ---------------------------------------------------------------- 构建登记

def register_build(conn: sqlite3.Connection, manifest: BuildManifestRequest) -> int:
    with write_transaction(conn) as tx:
        exists = tx.execute(
            "SELECT 1 FROM builds WHERE build_id = ?", (manifest.build_id,)
        ).fetchone()
        if exists is not None:
            raise ConflictError(f"构建 {manifest.build_id} 已登记，登记不可修改")

        tx.execute(
            "INSERT INTO builds (build_id, expected_shards) VALUES (?, ?)",
            (manifest.build_id, manifest.expected_shards),
        )

        file_rows: list[tuple] = []
        for f in manifest.files:
            for line_no in dict.fromkeys(f.executable_lines):  # 去重保序
                file_rows.append((manifest.build_id, f.path, "line", line_no, None))
            for src, dst in dict.fromkeys(f.normalized_branches()):
                file_rows.append(
                    (manifest.build_id, f.path, "branch", src, dst)
                )
        tx.executemany(
            "INSERT INTO build_files (build_id, path, kind, line_no, branch_no) "
            "VALUES (?, ?, ?, ?, ?)",
            file_rows,
        )
        return len(manifest.files)


def get_build(conn: sqlite3.Connection, build_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM builds WHERE build_id = ?", (build_id,)
    ).fetchone()


def get_shard_attempt(
    conn: sqlite3.Connection, build_id: str, shard: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reports WHERE build_id = ? AND shard = ?",
        (build_id, shard),
    ).fetchone()


def build_status(conn: sqlite3.Connection, build_id: str) -> dict:
    build = get_build(conn, build_id)
    if build is None:
        raise NotFoundError(f"构建 {build_id} 不存在")
    shard_rows = conn.execute(
        "SELECT shard, attempt, received_at FROM reports "
        "WHERE build_id = ? ORDER BY shard",
        (build_id,),
    ).fetchall()
    return {
        "build_id": build_id,
        "expected_shards": build["expected_shards"],
        "received_shards": len(shard_rows),
        "complete": len(shard_rows) >= build["expected_shards"],
        # 每次有效换报都会改写 reports，行数变化即代表聚合版本变化
        "coverage_version": _coverage_version(conn, build_id),
        "shards": [dict(r) for r in shard_rows],
    }


def _coverage_version(conn: sqlite3.Connection, build_id: str) -> int:
    """覆盖数据版本号：随每次接受的上报单调递增。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(attempt), 0) AS v FROM reports WHERE build_id = ?",
        (build_id,),
    ).fetchone()
    return int(row["v"])


# ---------------------------------------------------------------- 报告换报

def _file_hit_rows(
    build_id: str, shard: int, files: dict[str, CoverageFile]
) -> list[tuple]:
    rows: list[tuple] = []
    for path, data in files.items():
        for line_no in dict.fromkeys(data.executed_lines):
            rows.append((build_id, shard, path, "line", line_no, None))
        for src, dst in dict.fromkeys(
            (b[0], b[1]) for b in data.executed_branches
        ):
            rows.append((build_id, shard, path, "branch", src, dst))
    return rows


def replace_report(
    conn: sqlite3.Connection, req: ReportUploadRequest
) -> dict:
    """登记/替换一份分片报告，返回事务结果说明。"""
    with write_transaction(conn) as tx:
        build = get_build(tx, req.build_id)
        if build is None:
            raise NotFoundError(f"构建 {req.build_id} 不存在，请先登记")
        if not (0 <= req.shard < build["expected_shards"]):
            raise ConflictError(
                f"分片号 {req.shard} 越界：该构建预期 "
                f"0..{build['expected_shards'] - 1}"
            )

        current = get_shard_attempt(tx, req.build_id, req.shard)
        previous_attempt = current["attempt"] if current else None

        if current is not None and req.attempt < current["attempt"]:
            raise ConflictError(
                f"分片 {req.shard} 已生效尝试号 {current['attempt']}，"
                f"拒绝更旧的尝试号 {req.attempt}（只接受更高尝试号换报）"
            )
        if current is not None and req.attempt == current["attempt"]:
            # 同号重发：幂等，不改任何结果。
            return {
                "build_id": req.build_id,
                "shard": req.shard,
                "attempt": req.attempt,
                "status": "duplicate_unchanged",
                "previous_attempt": previous_attempt,
                "files_in_report": len(req.files),
            }

        # 更高尝试号（或首次上报）：只动该分片自己的明细。
        # 行/分支失去的仅是这份旧报告的命中；其他分片的命中原样保留，
        # 因此条目只有在失去最后一份命中时才会在聚合中变回未覆盖。
        tx.execute(
            "DELETE FROM coverage_rows WHERE build_id = ? AND shard = ?",
            (req.build_id, req.shard),
        )
        hit_rows = _file_hit_rows(req.build_id, req.shard, req.files)
        tx.executemany(
            "INSERT INTO coverage_rows "
            "(build_id, shard, path, kind, line_no, branch_no) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            hit_rows,
        )
        tx.execute(
            "INSERT INTO reports (build_id, shard, attempt) VALUES (?, ?, ?) "
            "ON CONFLICT(build_id, shard) DO UPDATE SET "
            "attempt = excluded.attempt, received_at = datetime('now')",
            (req.build_id, req.shard, req.attempt),
        )
        return {
            "build_id": req.build_id,
            "shard": req.shard,
            "attempt": req.attempt,
            "status": "accepted",
            "previous_attempt": previous_attempt,
            "files_in_report": len(req.files),
        }


# ---------------------------------------------------------------- 未覆盖查询

def list_uncovered(
    conn: sqlite3.Connection, build_id: str, path: str
) -> dict:
    build = get_build(conn, build_id)
    if build is None:
        raise NotFoundError(f"构建 {build_id} 不存在")

    status = build_status(conn, build_id)
    if not status["complete"]:
        # 未收齐：按要求不提供逐文件未覆盖结果。
        return {
            "build_id": build_id,
            "path": path,
            "complete": False,
            "expected_shards": build["expected_shards"],
            "received_shards": status["received_shards"],
            "coverage_version": status["coverage_version"],
            "uncovered_lines": [],
            "uncovered_branches": [],
        }

    registered = conn.execute(
        "SELECT kind, line_no, branch_no FROM build_files "
        "WHERE build_id = ? AND path = ?",
        (build_id, path),
    ).fetchall()
    if not registered:
        raise NotFoundError(
            f"构建 {build_id} 的登记文件中没有 {path}，"
            f"未覆盖项以登记的可执行行/分支为准"
        )

    # 只统计当前各分片生效报告对该文件的命中。
    hits: set[tuple[str, int, int | None]] = {
        (r["kind"], r["line_no"], r["branch_no"])
        for r in conn.execute(
            "SELECT DISTINCT kind, line_no, branch_no FROM coverage_rows "
            "WHERE build_id = ? AND path = ?",
            (build_id, path),
        ).fetchall()
    }

    uncovered_lines: list[int] = []
    uncovered_branches: list[list[int]] = []
    for row in registered:
        key = (row["kind"], row["line_no"], row["branch_no"])
        if key in hits:
            continue
        if row["kind"] == "line":
            uncovered_lines.append(row["line_no"])
        else:
            uncovered_branches.append([row["line_no"], row["branch_no"]])

    uncovered_lines.sort()
    uncovered_branches.sort()
    return {
        "build_id": build_id,
        "path": path,
        "complete": True,
        "expected_shards": build["expected_shards"],
        "received_shards": status["received_shards"],
        "coverage_version": status["coverage_version"],
        "uncovered_lines": uncovered_lines,
        "uncovered_branches": uncovered_branches,
    }


def list_build_files(conn: sqlite3.Connection, build_id: str) -> list[str]:
    build = get_build(conn, build_id)
    if build is None:
        raise NotFoundError(f"构建 {build_id} 不存在")
    return [
        r["path"]
        for r in conn.execute(
            "SELECT DISTINCT path FROM build_files WHERE build_id = ? ORDER BY path",
            (build_id,),
        ).fetchall()
    ]
