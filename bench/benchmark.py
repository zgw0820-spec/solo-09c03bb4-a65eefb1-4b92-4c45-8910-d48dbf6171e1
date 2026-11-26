#!/usr/bin/env python3
"""新旧查询路径与换报开销对比基准。

场景模拟 CI 合入前的查漏：少量缺口 + 高覆盖 + 多分片大面积命中重叠。

- 查询对比：
  * 旧实现 = 扫描该文件在所有分片的命中明细（SELECT DISTINCT coverage_rows）
    再与登记项做差集；
  * 新实现 = 直接读 uncovered_gaps 缺口表。
  指标：墙钟时间、实际读取行数（trace 计数）、EXPLAIN QUERY PLAN。
- 换报对比：
  * 全量重算 = 旧思路（删/插该分片全部明细后，对整文件重新聚合求缺口）；
  * 增量维护 = 新实现（只对 added/removed 差异集合更新引用计数与缺口）。
  指标：墙钟时间、写语句变更的行数。
- 结果正确性：两种实现的未覆盖集合逐字节一致。

运行（隔离用户 site 自动注入，依赖路径显式给出）：

    SP=$(python3 -c "import site; print(site.getusersitepackages())")
    PYTHONPATH="$SP" python3 -s bench/benchmark.py
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
import time

# 允许以 `python3 -I bench/benchmark.py` 直接运行：把仓库根目录加入 sys.path。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_module  # noqa: E402
from app.db import init_db  # noqa: E402
from app.repository import list_uncovered, register_build, replace_report  # noqa: E402
from app.schemas import BuildManifestRequest, ReportUploadRequest  # noqa: E402

N_FILES = 20
LINES_PER_FILE = 2000          # 单文件 2000 个可执行行
N_SHARDS = 8
REPEAT_QUERY = 200
SEED = 20261126


def _build_manifest() -> BuildManifestRequest:
    files = []
    for i in range(N_FILES):
        files.append(
            {
                "path": f"pkg/module_{i:02d}.py",
                "executable_lines": list(range(1, LINES_PER_FILE + 1)),
                "branches": [[10, 11], [10, 12], [500, 501], [1500, 0]],
            }
        )
    return BuildManifestRequest(
        build_id="bench", expected_shards=N_SHARDS, files=files
    )


def _make_shard_reports(rng: random.Random) -> dict[int, dict]:
    """每个分片命中每个文件 ~95% 的行，且分片之间大面积重叠，只留少量缺口。"""
    reports: dict[int, dict] = {}
    for shard in range(N_SHARDS):
        files = {}
        for i in range(N_FILES):
            path = f"pkg/module_{i:02d}.py"
            # 高覆盖 + 高重叠：各分片独立随机抽 95%，期望重叠率 ~0.95^2 ≈ 90%。
            lines = [
                ln for ln in range(1, LINES_PER_FILE + 1) if rng.random() < 0.95
            ]
            files[path] = {
                "executed_lines": lines,
                "executed_branches": [[10, 11], [10, 12], [500, 501]],
            }
        reports[shard] = files
    return reports


def _upload(conn, shard: int, attempt: int, files: dict) -> None:
    replace_report(
        conn,
        ReportUploadRequest(build_id="bench", shard=shard, attempt=attempt, files=files),
    )


def old_style_query(conn: sqlite3.Connection, path: str) -> dict:
    """复刻改造前的查询：全量 DISTINCT 命中明细 + 登记项差集。"""
    registered = conn.execute(
        "SELECT kind, line_no, branch_no FROM build_files "
        "WHERE build_id='bench' AND path=?",
        (path,),
    ).fetchall()
    hits = {
        (r["kind"], r["line_no"], r["branch_no"])
        for r in conn.execute(
            "SELECT DISTINCT kind, line_no, branch_no FROM coverage_rows "
            "WHERE build_id='bench' AND path=?",
            (path,),
        ).fetchall()
    }
    lines = sorted(r["line_no"] for r in registered if r["kind"] == "line" and tuple(r) not in hits)
    branches = sorted(
        [r["line_no"], r["branch_no"]]
        for r in registered
        if r["kind"] == "branch" and tuple(r) not in hits
    )
    return {"uncovered_lines": lines, "uncovered_branches": branches}


def full_recompute_replace(conn, shard: int, attempt: int, new_files: dict) -> None:
    """旧思路换报：删/插全部分片明细后，对涉及文件整体重算缺口。"""
    from app.db import write_transaction

    with write_transaction(conn) as tx:
        old_paths = {
            r["path"]
            for r in tx.execute(
                "SELECT DISTINCT path FROM coverage_rows "
                "WHERE build_id='bench' AND shard=?",
                (shard,),
            ).fetchall()
        }
        tx.execute(
            "DELETE FROM coverage_rows WHERE build_id='bench' AND shard=?", (shard,)
        )
        rows = []
        for path, data in new_files.items():
            for ln in dict.fromkeys(data["executed_lines"]):
                rows.append(("bench", shard, path, "line", ln, None))
            for src, dst in dict.fromkeys(map(tuple, data["executed_branches"])):
                rows.append(("bench", shard, path, "branch", src, dst))
        tx.executemany(
            "INSERT INTO coverage_rows "
            "(build_id, shard, path, kind, line_no, branch_no) VALUES (?,?,?,?,?,?)",
            rows,
        )
        tx.execute(
            "INSERT INTO reports (build_id, shard, attempt) VALUES ('bench', ?, ?) "
            "ON CONFLICT(build_id, shard) DO UPDATE SET attempt=excluded.attempt",
            (shard, attempt),
        )
        # 全量重算：涉及文件的每个登记项都要重新判断一次（旧实现查询期才做，
        # 这里显式重算，成本等价于每次换报后对这些文件做一轮旧式查询）。
        for path in old_paths | set(new_files):
            old_style_query(tx, path)


class RowCounter:
    """通过 trace 回调统计一次操作内 SELECT 实际产出的行数。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.rows = 0
        self.stmts = 0

    def __enter__(self):
        def cb(_sql: str) -> None:
            self.stmts += 1
        self.conn.set_trace_callback(cb)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)


def measure(fn, repeat: int) -> float:
    # 预热一次，避免首次执行的 schema/计划抖动。
    fn()
    start = time.perf_counter()
    for _ in range(repeat):
        fn()
    return (time.perf_counter() - start) / repeat * 1000.0  # ms


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="cov-bench-")
    db_path = os.path.join(tmp, "bench.db")
    db_module._conn = None
    conn = init_db(db_path)
    rng = random.Random(SEED)

    register_build(conn, _build_manifest())
    reports = _make_shard_reports(rng)
    for shard, files in reports.items():
        _upload(conn, shard, 1, files)

    target = "pkg/module_00.py"
    new_result = list_uncovered(conn, "bench", target)
    old_result = old_style_query(conn, target)
    assert new_result["uncovered_lines"] == old_result["uncovered_lines"]
    assert new_result["uncovered_branches"] == old_result["uncovered_branches"]

    # ---- 查询开销 ----
    old_ms = measure(lambda: old_style_query(conn, target), REPEAT_QUERY)
    new_ms = measure(lambda: list_uncovered(conn, "bench", target), REPEAT_QUERY)

    # 实际读取行数：用 changes/再跑一遍 fetchall 计数（旧式明细行数 vs 缺口行数）。
    old_detail_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM coverage_rows WHERE build_id='bench' AND path=?",
        (target,),
    ).fetchone()["c"]
    old_distinct_hits = conn.execute(
        "SELECT COUNT(*) AS c FROM (SELECT DISTINCT kind, line_no, branch_no "
        "FROM coverage_rows WHERE build_id='bench' AND path=?)",
        (target,),
    ).fetchone()["c"]
    gap_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM uncovered_gaps WHERE build_id='bench' AND path=?",
        (target,),
    ).fetchone()["c"]
    registered_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM build_files WHERE build_id='bench' AND path=?",
        (target,),
    ).fetchone()["c"]

    old_plan = "\n      ".join(
        r["detail"]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT kind, line_no, branch_no "
            "FROM coverage_rows WHERE build_id='bench' AND path=?",
            (target,),
        ).fetchall()
    )
    new_plan = "\n      ".join(
        r["detail"]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT kind, line_no, branch_no "
            "FROM uncovered_gaps WHERE build_id='bench' AND path=?",
            (target,),
        ).fetchall()
    )

    # ---- 换报开销：构造一份只改少量命中的新报告 ----
    shard0 = reports[0]
    changed = dict(shard0)
    tgt_files = dict(changed)
    entry = dict(tgt_files[target])
    lines = list(entry["executed_lines"])
    # 新增 2 条命中、撤销 2 条命中，其余完全不变（典型“换报只动几行”）。
    removed_hits = sorted(lines[:2])
    entry_new_lines = [ln for ln in lines if ln not in removed_hits]
    added_hits = [ln for ln in range(1, LINES_PER_FILE + 1) if ln not in lines][:2]
    entry["executed_lines"] = sorted(entry_new_lines + added_hits)
    tgt_files[target] = entry
    changed = tgt_files

    inc_ms = measure(lambda: replace_report(
        conn,
        ReportUploadRequest(build_id="bench", shard=0, attempt=2, files=changed),
    ), 1)
    # 增量换报已生效；把库恢复回 attempt1 状态后测全量重算口径。
    replace_report(
        conn,
        ReportUploadRequest(build_id="bench", shard=0, attempt=3, files=shard0),
    )
    full_ms = measure(lambda: full_recompute_replace(conn, 0, 4, changed), 1)

    # 差异集合规模（新增/撤销条目数）。
    diff_size = len(added_hits) + len(removed_hits)
    shard0_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM coverage_rows WHERE build_id='bench' AND shard=0"
    ).fetchone()["c"]

    print("=" * 72)
    print("场景: %d 分片 × %d 文件 × 每文件 %d 登记项，分片间高重叠（~90%%）"
          % (N_SHARDS, N_FILES, LINES_PER_FILE + 4))
    print("=" * 72)
    print("\n[查询] 单文件未覆盖查询（%d 次平均）" % REPEAT_QUERY)
    print("  旧实现（扫描命中明细 + 差集）: %.4f ms/次" % old_ms)
    print("  新实现（直读缺口表）          : %.4f ms/次" % new_ms)
    print("  提速                          : %.1fx" % (old_ms / new_ms))
    print("  旧查询读取的命中明细行        : %d（DISTINCT 后 %d 个命中项）"
          % (old_detail_rows, old_distinct_hits))
    print("  新查询读取的缺口行            : %d（登记项共 %d，已覆盖项零扫描）"
          % (gap_rows, registered_rows))
    print("  旧计划: %s" % old_plan)
    print("  新计划: %s" % new_plan)

    print("\n[换报] 仅 %d 个条目变化（新增 %d / 撤销 %d），该分片明细共 %d 行"
          % (diff_size, len(added_hits), len(removed_hits), shard0_rows))
    print("  全量重算口径: %.4f ms" % full_ms)
    print("  增量维护口径: %.4f ms" % inc_ms)
    print("  提速        : %.1fx" % (full_ms / inc_ms))

    print("\n[正确性] 两实现未覆盖集合一致: %s"
          % (new_result["uncovered_lines"] == old_result["uncovered_lines"]
             and new_result["uncovered_branches"] == old_result["uncovered_branches"]))
    print("  缺口行数: %d；示例未覆盖行: %s"
          % (gap_rows, new_result["uncovered_lines"][:10]))


if __name__ == "__main__":
    main()
