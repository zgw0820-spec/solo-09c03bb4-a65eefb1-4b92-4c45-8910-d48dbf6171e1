"""数据访问与换报核心逻辑（v2：增量缺口模型）。

换报规则（全部在单个 SQLite 写事务内完成）：

1. 同号重发：不删除、不写入、不改任何结果，返回 ``duplicate_unchanged``。
2. 更高尝试号：
   a. 用新旧两份报告的命中集合求出 ``added``（新报告独有）与 ``removed``
      （旧报告独有）两个差异集合；
   b. 整体替换该分片自己的 coverage_rows；
   c. 仅对差异集合涉及的登记项维护 item_hits 引用计数（+1 / -1），
      并据此在 uncovered_gaps 中插入或删除缺口——未出现在差异集合里的
      条目（含被多个分片重叠命中、本次没变化的条目）完全不触碰；
   d. 更新 reports、收齐标记与 coverage_version，随缺口变更一并提交。
3. 更低尝试号：拒绝（409），当前生效报告与三项状态（缺口/收齐/版本）不动。

关键性质：一条行/分支只有在撤销命中后 item_hits 归零（失去最后一份命中）
时才会重新进入 uncovered_gaps；只要还有别的分片命中，计数 > 0，不会被
重新列为未覆盖。查询路径只读 uncovered_gaps，不扫描 coverage_rows，也
不扫描任何已覆盖项。
"""

from __future__ import annotations

import sqlite3

from .db import read_snapshot, write_transaction
from .schemas import BuildManifestRequest, CoverageFile, ReportUploadRequest


class ConflictError(Exception):
    """与既有数据冲突（构建已登记、分片号越界、尝试号过旧等）。"""


class NotFoundError(Exception):
    """构建或登记文件不存在。"""


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
        gap_rows: list[tuple] = []
        for f in manifest.files:
            for line_no in dict.fromkeys(f.executable_lines):  # 去重保序
                row = (manifest.build_id, f.path, "line", line_no, None)
                file_rows.append(row)
                # 登记时还没有任何命中：全部登记项初始即为缺口。
                gap_rows.append(row)
            for src, dst in dict.fromkeys(f.normalized_branches()):
                row = (manifest.build_id, f.path, "branch", src, dst)
                file_rows.append(row)
                gap_rows.append(row)
        tx.executemany(
            "INSERT INTO build_files (build_id, path, kind, line_no, branch_no) "
            "VALUES (?, ?, ?, ?, ?)",
            file_rows,
        )
        tx.executemany(
            "INSERT INTO uncovered_gaps (build_id, path, kind, line_no, branch_no) "
            "VALUES (?, ?, ?, ?, ?)",
            gap_rows,
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
    with read_snapshot(conn) as tx:
        build = get_build(tx, build_id)
        if build is None:
            raise NotFoundError(f"构建 {build_id} 不存在")
        shard_rows = tx.execute(
            "SELECT shard, attempt, received_at FROM reports "
            "WHERE build_id = ? ORDER BY shard",
            (build_id,),
        ).fetchall()
        return {
            "build_id": build_id,
            "expected_shards": build["expected_shards"],
            "received_shards": len(shard_rows),
            "complete": bool(build["complete"]),
            # 与缺口、收齐标记同事务提交的单调版本号
            "coverage_version": int(build["coverage_version"]),
            "shards": [dict(r) for r in shard_rows],
        }


# ---------------------------------------------------------------- 报告换报

def _file_hit_rows(
    build_id: str, shard: int, files: dict[str, CoverageFile]
) -> list[tuple]:
    """报告内去重后的命中明细（含未登记路径；后者只留档，不参与缺口判定）。"""
    rows: list[tuple] = []
    for path, data in files.items():
        for line_no in dict.fromkeys(data.executed_lines):
            rows.append((build_id, shard, path, "line", line_no, None))
        for src, dst in dict.fromkeys(
            (b[0], b[1]) for b in data.executed_branches
        ):
            rows.append((build_id, shard, path, "branch", src, dst))
    return rows


# 临时表 DDL：换报事务内承载新报告命中与两个差异集合。
_TEMP_DDL = """
CREATE TEMP TABLE IF NOT EXISTS _new_hits (
    path TEXT NOT NULL, kind TEXT NOT NULL,
    line_no INTEGER NOT NULL, branch_no INTEGER
);
CREATE TEMP TABLE IF NOT EXISTS _added (
    path TEXT NOT NULL, kind TEXT NOT NULL,
    line_no INTEGER NOT NULL, branch_no INTEGER
);
CREATE TEMP TABLE IF NOT EXISTS _removed (
    path TEXT NOT NULL, kind TEXT NOT NULL,
    line_no INTEGER NOT NULL, branch_no INTEGER
);
"""


def _load_temp_hits(tx: sqlite3.Connection, rows: list[tuple]) -> None:
    """把新报告命中（去掉 build_id/shard 列）装入临时表并去重。"""
    tx.execute("DELETE FROM _new_hits")
    tx.execute("DELETE FROM _added")
    tx.execute("DELETE FROM _removed")
    tx.executemany(
        "INSERT INTO _new_hits (path, kind, line_no, branch_no) "
        "VALUES (?, ?, ?, ?)",
        [(r[2], r[3], r[4], r[5]) for r in rows],
    )


def _maintain_gaps_for_diff(
    tx: sqlite3.Connection, build_id: str, shard: int
) -> None:
    """仅针对 added/removed 差异集合维护引用计数与缺口。

    全部为集合化 SQL，复杂度只与“变化的条目数”相关；差异集合之外的
    条目（含多分片重叠命中项）不会被读取或改写。分支条目的 branch_no
    可能为 NULL，匹配一律用 ``IS`` 做 NULL 安全比较。
    """
    # added = 新有旧无；removed = 旧有新无（仅在该分片旧明细范围内求差）。
    tx.execute(
        """
        INSERT INTO _added (path, kind, line_no, branch_no)
        SELECT n.path, n.kind, n.line_no, n.branch_no
          FROM _new_hits n
         WHERE NOT EXISTS (
                   SELECT 1 FROM coverage_rows c
                    WHERE c.build_id = ? AND c.shard = ?
                      AND c.path = n.path AND c.kind = n.kind
                      AND c.line_no = n.line_no
                      AND (c.branch_no IS n.branch_no
                           OR c.branch_no = n.branch_no)
               )
        """,
        (build_id, shard),
    )
    tx.execute(
        """
        INSERT INTO _removed (path, kind, line_no, branch_no)
        SELECT c.path, c.kind, c.line_no, c.branch_no
          FROM coverage_rows c
         WHERE c.build_id = ? AND c.shard = ?
           AND NOT EXISTS (
                   SELECT 1 FROM _new_hits n
                    WHERE n.path = c.path AND n.kind = c.kind
                      AND n.line_no = c.line_no
                      AND (n.branch_no IS c.branch_no
                           OR n.branch_no = c.branch_no)
               )
        """,
        (build_id, shard),
    )

    # ---- 撤销命中：仅对登记项的引用计数 -1 ----
    tx.execute(
        """
        UPDATE item_hits
           SET hit_shards = hit_shards - 1
         WHERE item_hits.build_id = ?
           AND EXISTS (
                   SELECT 1 FROM _removed r
                   JOIN build_files bf
                     ON bf.build_id = item_hits.build_id
                    AND bf.path = r.path AND bf.kind = r.kind
                    AND bf.line_no = r.line_no
                    AND (bf.branch_no IS r.branch_no
                         OR bf.branch_no = r.branch_no)
                    WHERE bf.path = item_hits.path
                      AND bf.kind = item_hits.kind
                      AND bf.line_no = item_hits.line_no
                      AND (bf.branch_no IS item_hits.branch_no
                           OR bf.branch_no = item_hits.branch_no)
               )
        """,
        (build_id,),
    )
    # 计数归零 = 失去最后一份命中：删除计数行，重新登记为缺口。
    tx.execute("DELETE FROM item_hits WHERE build_id = ? AND hit_shards <= 0", (build_id,))
    tx.execute(
        """
        INSERT OR IGNORE INTO uncovered_gaps
            (build_id, path, kind, line_no, branch_no)
        SELECT bf.build_id, bf.path, bf.kind, bf.line_no, bf.branch_no
          FROM _removed r
          JOIN build_files bf
            ON bf.build_id = ?
           AND bf.path = r.path AND bf.kind = r.kind
           AND bf.line_no = r.line_no
           AND (bf.branch_no IS r.branch_no OR bf.branch_no = r.branch_no)
         WHERE NOT EXISTS (
                   SELECT 1 FROM item_hits ih
                    WHERE ih.build_id = bf.build_id
                      AND ih.path = bf.path AND ih.kind = bf.kind
                      AND ih.line_no = bf.line_no
                      AND (ih.branch_no IS bf.branch_no
                           OR ih.branch_no = bf.branch_no)
               )
        """,
        (build_id,),
    )

    # ---- 新增命中：登记项的引用计数 +1（首次命中则建行，计数 1）----
    tx.execute(
        """
        UPDATE item_hits
           SET hit_shards = hit_shards + 1
         WHERE item_hits.build_id = ?
           AND EXISTS (
                   SELECT 1 FROM _added a
                   JOIN build_files bf
                     ON bf.build_id = item_hits.build_id
                    AND bf.path = a.path AND bf.kind = a.kind
                    AND bf.line_no = a.line_no
                    AND (bf.branch_no IS a.branch_no
                         OR bf.branch_no = a.branch_no)
                    WHERE bf.path = item_hits.path
                      AND bf.kind = item_hits.kind
                      AND bf.line_no = item_hits.line_no
                      AND (bf.branch_no IS item_hits.branch_no
                           OR bf.branch_no = item_hits.branch_no)
               )
        """,
        (build_id,),
    )
    tx.execute(
        """
        INSERT INTO item_hits
            (build_id, path, kind, line_no, branch_no, hit_shards)
        SELECT ?, a.path, a.kind, a.line_no, a.branch_no, 1
          FROM _added a
         WHERE EXISTS (
                   SELECT 1 FROM build_files bf
                    WHERE bf.build_id = ?
                      AND bf.path = a.path AND bf.kind = a.kind
                      AND bf.line_no = a.line_no
                      AND (bf.branch_no IS a.branch_no
                           OR bf.branch_no = a.branch_no)
               )
           AND NOT EXISTS (
                   SELECT 1 FROM item_hits ih
                    WHERE ih.build_id = ?
                      AND ih.path = a.path AND ih.kind = a.kind
                      AND ih.line_no = a.line_no
                      AND (ih.branch_no IS a.branch_no
                           OR ih.branch_no = a.branch_no)
               )
        """,
        (build_id, build_id, build_id),
    )
    # 获得命中（且只统计登记项）：从缺口表移除。仍有其他分片命中的项本来
    # 就不在缺口表里，DELETE 自然为空，绝不会被重新列为未覆盖。
    tx.execute(
        """
        DELETE FROM uncovered_gaps
         WHERE uncovered_gaps.build_id = ?
           AND EXISTS (
                   SELECT 1 FROM _added a
                   JOIN build_files bf
                     ON bf.build_id = uncovered_gaps.build_id
                    AND bf.path = a.path AND bf.kind = a.kind
                    AND bf.line_no = a.line_no
                    AND (bf.branch_no IS a.branch_no
                         OR bf.branch_no = a.branch_no)
                    WHERE bf.path = uncovered_gaps.path
                      AND bf.kind = uncovered_gaps.kind
                      AND bf.line_no = uncovered_gaps.line_no
                      AND (bf.branch_no IS uncovered_gaps.branch_no
                           OR bf.branch_no = uncovered_gaps.branch_no)
               )
        """,
        (build_id,),
    )


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
            # 同号重发：幂等，不删不写，缺口/收齐/版本三项一律不动。
            return {
                "build_id": req.build_id,
                "shard": req.shard,
                "attempt": req.attempt,
                "status": "duplicate_unchanged",
                "previous_attempt": previous_attempt,
                "files_in_report": len(req.files),
            }

        hit_rows = _file_hit_rows(req.build_id, req.shard, req.files)
        # 注意：不能用 executescript（会隐式提交当前事务），逐条执行。
        for ddl in _TEMP_DDL.split(";"):
            ddl = ddl.strip()
            if ddl:
                tx.execute(ddl)
        _load_temp_hits(tx, hit_rows)

        # 先在旧 coverage_rows 上求出 added/removed，再整体替换该分片明细。
        _maintain_gaps_for_diff(tx, req.build_id, req.shard)

        tx.execute(
            "DELETE FROM coverage_rows WHERE build_id = ? AND shard = ?",
            (req.build_id, req.shard),
        )
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
        # 收齐标记与版本号随缺口变更在同一事务提交；失败回滚则三项全不动。
        tx.execute(
            """
            UPDATE builds
               SET complete = CASE WHEN (
                        SELECT COUNT(*) FROM reports r
                         WHERE r.build_id = builds.build_id
                   ) >= expected_shards THEN 1 ELSE 0 END,
                   coverage_version = coverage_version + 1
             WHERE build_id = ?
            """,
            (req.build_id,),
        )
        tx.execute("DELETE FROM _new_hits")
        tx.execute("DELETE FROM _added")
        tx.execute("DELETE FROM _removed")
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
    # 整个请求走同一个快照事务：缺口、收齐状态、版本号必然来自同一已提交版本。
    with read_snapshot(conn) as tx:
        build = get_build(tx, build_id)
        if build is None:
            raise NotFoundError(f"构建 {build_id} 不存在")

        received = tx.execute(
            "SELECT COUNT(*) AS c FROM reports WHERE build_id = ?", (build_id,)
        ).fetchone()["c"]

        if not build["complete"]:
            # 未收齐：按要求不提供逐文件未覆盖结果。
            return {
                "build_id": build_id,
                "path": path,
                "complete": False,
                "expected_shards": build["expected_shards"],
                "received_shards": received,
                "coverage_version": int(build["coverage_version"]),
                "uncovered_lines": [],
                "uncovered_branches": [],
            }

        registered = tx.execute(
            "SELECT 1 FROM build_files WHERE build_id = ? AND path = ? LIMIT 1",
            (build_id, path),
        ).fetchone()
        if registered is None:
            raise NotFoundError(
                f"构建 {build_id} 的登记文件中没有 {path}，"
                f"未覆盖项以登记的可执行行/分支为准"
            )

        # 只读取缺口表：不扫描 coverage_rows，也不扫描任何已覆盖项。
        uncovered_lines: list[int] = []
        uncovered_branches: list[list[int]] = []
        gap_rows = tx.execute(
            "SELECT kind, line_no, branch_no FROM uncovered_gaps "
            "WHERE build_id = ? AND path = ?",
            (build_id, path),
        ).fetchall()
        for row in gap_rows:
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
            "received_shards": received,
            "coverage_version": int(build["coverage_version"]),
            "uncovered_lines": uncovered_lines,
            "uncovered_branches": uncovered_branches,
        }


def list_build_files(conn: sqlite3.Connection, build_id: str) -> list[str]:
    with read_snapshot(conn) as tx:
        build = get_build(tx, build_id)
        if build is None:
            raise NotFoundError(f"构建 {build_id} 不存在")
        return [
            r["path"]
            for r in tx.execute(
                "SELECT DISTINCT path FROM build_files WHERE build_id = ? ORDER BY path",
                (build_id,),
            ).fetchall()
        ]
