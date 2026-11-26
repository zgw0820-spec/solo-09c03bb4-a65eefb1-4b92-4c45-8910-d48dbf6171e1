"""增量缺口模型的不变量测试。

覆盖需求中的硬性语义：

- 多个分片重叠命中同一条目时，撤销其中一份命中不会把它重新列为未覆盖，
  只有失去最后一份命中（item_hits 归零）才回到缺口表；
- 收齐后的未覆盖查询只读 uncovered_gaps，不扫描 coverage_rows / item_hits；
- 缺口、收齐标记、coverage_version 来自同一已提交快照，换报失败三项不动；
- 旧版 SQLite 库（只有 coverage_rows）启动即自动升级，无需重传报告，
  再次重启不做全库重算；
- 随机化差分：增量维护的缺口始终等于“登记项 - 全量命中聚合”的重新计算结果。
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from app import db as db_module
from app.db import get_conn, init_db, read_snapshot, write_transaction
from app.repository import (
    ConflictError,
    build_status,
    list_uncovered,
    register_build,
    replace_report,
)
from app.schemas import BuildManifestRequest, CoverageFile, ReportUploadRequest
from tests.test_api import MANIFEST, SHARD0_A1, SHARD0_A2


# ------------------------------------------------------------ 构造辅助

def _manifest(build_id="b1", shards=2):
    data = {**MANIFEST, "build_id": build_id, "expected_shards": shards}
    return BuildManifestRequest(**data)


def _report(build_id, shard, attempt, files):
    return ReportUploadRequest(
        build_id=build_id,
        shard=shard,
        attempt=attempt,
        files={p: CoverageFile(**d) for p, d in files.items()},
    )


def _gaps(conn, build_id, path):
    rows = conn.execute(
        "SELECT kind, line_no, branch_no FROM uncovered_gaps "
        "WHERE build_id=? AND path=? ORDER BY kind, line_no, branch_no",
        (build_id, path),
    ).fetchall()
    return [tuple(r) for r in rows]


def _recompute_uncovered(conn, build_id, path):
    """不依赖新表的权威重算：登记项减去全部 coverage_rows 命中。"""
    registered = conn.execute(
        "SELECT kind, line_no, branch_no FROM build_files "
        "WHERE build_id=? AND path=?",
        (build_id, path),
    ).fetchall()
    hits = {
        (r["kind"], r["line_no"], r["branch_no"])
        for r in conn.execute(
            "SELECT DISTINCT kind, line_no, branch_no FROM coverage_rows "
            "WHERE build_id=? AND path=?",
            (build_id, path),
        ).fetchall()
    }
    return sorted(tuple(r) for r in registered if tuple(r) not in hits)


# ------------------------------------------------------------ 重叠保护

def test_overlapping_hit_survives_one_shard_replacement(client):
    """同一行被两个分片命中：换报撤掉一份命中，仍有另一份 -> 不回缺口。"""
    db = get_conn()
    # 两个分片都命中 src/calc.py 的行 1/2/3，形成重叠。
    overlap = {"files": {"src/calc.py": {"executed_lines": [1, 2, 3]}}}
    register_build(db, _manifest(shards=2))
    replace_report(db, _report("b1", 0, 1, overlap["files"]))
    replace_report(db, _report("b1", 1, 1, overlap["files"]))

    body = list_uncovered(db, "b1", "src/calc.py")
    assert 1 not in body["uncovered_lines"]
    assert 2 not in body["uncovered_lines"]

    # shard0 换报后不再命中行 1/2/3，但 shard1 仍命中 -> 它们不能回缺口。
    replace_report(
        db,
        _report("b1", 0, 2, {"src/calc.py": {"executed_lines": [5, 6]}}),
    )
    body = list_uncovered(db, "b1", "src/calc.py")
    assert 1 not in body["uncovered_lines"]
    assert 2 not in body["uncovered_lines"]
    assert 3 not in body["uncovered_lines"]
    # 引用计数应恰好为 1（只剩 shard1）。
    counts = {
        r["line_no"]: r["hit_shards"]
        for r in db.execute(
            "SELECT line_no, hit_shards FROM item_hits "
            "WHERE build_id='b1' AND path='src/calc.py' AND kind='line'"
        ).fetchall()
    }
    assert counts[1] == counts[2] == counts[3] == 1


def test_last_hit_loss_returns_item_to_gap(client):
    """只剩一份命中时撤掉：item_hits 归零，条目重新进入缺口表。"""
    db = get_conn()
    register_build(db, _manifest(shards=2))
    replace_report(
        db, _report("b1", 0, 1, SHARD0_A1["files"])
    )
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    # 行 7 只有 shard0 命中。
    assert _gaps(db, "b1", "src/calc.py") and (
        ("line", 7, None) not in _gaps(db, "b1", "src/calc.py")
    )
    replace_report(db, _report("b1", 0, 2, SHARD0_A2["files"]))
    assert ("line", 7, None) in _gaps(db, "b1", "src/calc.py")
    # 归零计数行必须被删除，item_hits 不允许残留 0。
    zero_rows = db.execute(
        "SELECT COUNT(*) AS c FROM item_hits WHERE hit_shards <= 0"
    ).fetchone()["c"]
    assert zero_rows == 0


def test_unregistered_hits_never_affect_gaps(client):
    """未登记路径/条目的命中只留档 coverage_rows，不进 item_hits/缺口判定。"""
    db = get_conn()
    register_build(db, _manifest())
    replace_report(
        db,
        _report(
            "b1",
            0,
            1,
            {
                "src/calc.py": {"executed_lines": [1, 2, 3]},
                "src/ghost.py": {"executed_lines": [9, 10]},  # 未登记文件
            },
        ),
    )
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    ghost = db.execute(
        "SELECT COUNT(*) AS c FROM item_hits WHERE path='src/ghost.py'"
    ).fetchone()["c"]
    assert ghost == 0
    body = list_uncovered(db, "b1", "src/calc.py")
    assert set(body["uncovered_lines"]) == {4, 5, 6, 7}


# ------------------------------------------------------------ 查询路径

def test_query_does_not_scan_hit_detail_tables(client):
    """收齐后的查询语句不得触及 coverage_rows / item_hits。"""
    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )

    executed: list[str] = []
    db.set_trace_callback(executed.append)
    body = list_uncovered(db, "b1", "src/calc.py")
    db.set_trace_callback(None)

    assert body["complete"] is True
    joined = "\n".join(executed)
    assert "coverage_rows" not in joined
    assert "item_hits" not in joined
    assert "uncovered_gaps" in joined


def test_query_plan_uses_gap_index(client):
    """查询走缺口表的 (build_id, path) 索引，而不是全表扫描。"""
    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT kind, line_no, branch_no "
        "FROM uncovered_gaps WHERE build_id='b1' AND path='src/calc.py'"
    ).fetchall()
    detail = " ".join(row["detail"] for row in plan)
    assert "SCAN uncovered_gaps" not in detail  # 不能是全表扫描
    assert "uncovered_gaps" in detail


# ------------------------------------------------------------ 版本与快照

def test_coverage_version_monotonic_and_same_snapshot(client):
    """版本号随每次接受的上报 +1；同号重发与旧号拒绝不改三项状态。"""
    db = get_conn()
    register_build(db, _manifest())
    s0 = build_status(db, "b1")
    assert s0["coverage_version"] == 0 and s0["complete"] is False

    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    s1 = build_status(db, "b1")
    assert s1["coverage_version"] == 1 and s1["complete"] is False

    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    s2 = build_status(db, "b1")
    assert s2["coverage_version"] == 2 and s2["complete"] is True

    # 同号重发：三项不动。
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    assert build_status(db, "b1") == s2

    # 换报成功：版本 +1，缺口随事务一起切换。
    replace_report(db, _report("b1", 0, 2, SHARD0_A2["files"]))
    s3 = build_status(db, "b1")
    assert s3["coverage_version"] == 3 and s3["complete"] is True
    body = list_uncovered(db, "b1", "src/calc.py")
    assert body["coverage_version"] == 3

    # 旧尝试号被拒：缺口、收齐、版本三项不动。
    with pytest.raises(ConflictError):
        replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    assert build_status(db, "b1") == s3
    assert list_uncovered(db, "b1", "src/calc.py") == body


def test_failed_replacement_leaves_three_artifacts_untouched(client):
    """换报事务失败回滚：缺口、收齐标记、版本号全部保持旧版。"""
    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    before_gaps = _gaps(db, "b1", "src/calc.py")
    before_status = build_status(db, "b1")

    with pytest.raises(sqlite3.IntegrityError):
        with write_transaction(db) as tx:
            # 故意制造失败：删掉一个计数行后违反唯一约束，整体回滚。
            tx.execute("DELETE FROM item_hits WHERE build_id='b1' LIMIT 1")
            tx.execute(
                "INSERT INTO reports (build_id, shard, attempt) VALUES (?, ?, ?)",
                ("b1", 0, 999),
            )
            tx.execute(
                "INSERT INTO reports (build_id, shard, attempt) VALUES (?, ?, ?)",
                ("b1", 0, 998),
            )

    assert _gaps(db, "b1", "src/calc.py") == before_gaps
    assert build_status(db, "b1") == before_status
    assert list_uncovered(db, "b1", "src/calc.py")["coverage_version"] == (
        before_status["coverage_version"]
    )


def test_snapshot_reads_consistent_version_inside_tx(client):
    """换报事务挂起时，读快照里的缺口与版本号必须同属旧版。"""
    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )
    old_version = build_status(db, "b1")["coverage_version"]
    old_gaps = _gaps(db, "b1", "src/calc.py")

    other = sqlite3.connect(
        db.execute("PRAGMA database_list").fetchone()[2], isolation_level=None
    )
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "UPDATE builds SET coverage_version = coverage_version + 1 "
            "WHERE build_id='b1'"
        )
        other.execute(
            "DELETE FROM uncovered_gaps "
            "WHERE build_id='b1' AND path='src/calc.py'"
        )
        # 主连接在同一快照里读到的必须是配套的旧版（旧缺口 + 旧版本号）。
        with read_snapshot(db) as tx:
            v = tx.execute(
                "SELECT coverage_version FROM builds WHERE build_id='b1'"
            ).fetchone()["coverage_version"]
            gaps = tx.execute(
                "SELECT line_no FROM uncovered_gaps "
                "WHERE build_id='b1' AND path='src/calc.py' AND kind='line'"
            ).fetchall()
            assert v == old_version
            assert sorted(r["line_no"] for r in gaps) == sorted(
                r[1] for r in old_gaps if r[0] == "line"
            )
        other.execute("ROLLBACK")
    finally:
        other.close()


# ------------------------------------------------------------ 旧库迁移

def _create_legacy_db(path: str) -> None:
    """按 v1 应用的原始 schema 造一个旧库（无 item_hits/gaps/新列）。"""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA user_version = 0")
    conn.executescript(
        """
        CREATE TABLE builds (
            build_id TEXT PRIMARY KEY,
            expected_shards INTEGER NOT NULL CHECK (expected_shards > 0),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE build_files (
            build_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
            line_no INTEGER NOT NULL, branch_no INTEGER,
            UNIQUE (build_id, path, kind, line_no, branch_no)
        );
        CREATE TABLE reports (
            build_id TEXT NOT NULL, shard INTEGER NOT NULL, attempt INTEGER NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (build_id, shard)
        );
        CREATE TABLE coverage_rows (
            build_id TEXT NOT NULL, shard INTEGER NOT NULL, path TEXT NOT NULL,
            kind TEXT NOT NULL, line_no INTEGER NOT NULL, branch_no INTEGER,
            UNIQUE (build_id, shard, path, kind, line_no, branch_no)
        );
        """
    )
    conn.execute("INSERT INTO builds (build_id, expected_shards) VALUES ('leg', 2)")
    lines = [1, 2, 3, 4, 5, 6, 7]
    branches = [(3, 4), (3, 5), (6, 7)]
    conn.executemany(
        "INSERT INTO build_files (build_id, path, kind, line_no, branch_no) "
        "VALUES ('leg', 'src/calc.py', 'line', ?, NULL)",
        [(ln,) for ln in lines],
    )
    conn.executemany(
        "INSERT INTO build_files (build_id, path, kind, line_no, branch_no) "
        "VALUES ('leg', 'src/calc.py', 'branch', ?, ?)",
        branches,
    )
    # shard0 命中 1,2,3,5,6,7 与分支 3->5、6->7；shard1 重叠命中 1,2,3。
    conn.executemany(
        "INSERT INTO coverage_rows (build_id, shard, path, kind, line_no, branch_no)"
        " VALUES ('leg', 0, 'src/calc.py', 'line', ?, NULL)",
        [(ln,) for ln in (1, 2, 3, 5, 6, 7)],
    )
    conn.executemany(
        "INSERT INTO coverage_rows (build_id, shard, path, kind, line_no, branch_no)"
        " VALUES ('leg', 1, 'src/calc.py', 'line', ?, NULL)",
        [(ln,) for ln in (1, 2, 3)],
    )
    conn.executemany(
        "INSERT INTO coverage_rows (build_id, shard, path, kind, line_no, branch_no)"
        " VALUES ('leg', 0, 'src/calc.py', 'branch', ?, ?)",
        [(3, 5), (6, 7)],
    )
    conn.execute("INSERT INTO reports (build_id, shard, attempt) VALUES ('leg', 0, 5)")
    conn.execute("INSERT INTO reports (build_id, shard, attempt) VALUES ('leg', 1, 2)")
    conn.close()


def test_legacy_db_upgrades_without_reupload(tmp_path):
    path = str(tmp_path / "legacy.db")
    _create_legacy_db(path)

    db_module._conn = None  # 强制重新初始化走迁移分支
    conn = init_db(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    status = build_status(conn, "leg")
    assert status["complete"] is True
    # 版本号沿用旧口径（尝试号之和 5+2=7）。
    assert status["coverage_version"] == 7

    body = list_uncovered(conn, "leg", "src/calc.py")
    assert body["uncovered_lines"] == [4]
    assert body["uncovered_branches"] == [[3, 4]]

    # 重叠命中项的引用计数必须是 2。
    overlaps = {
        r["line_no"]: r["hit_shards"]
        for r in conn.execute(
            "SELECT line_no, hit_shards FROM item_hits "
            "WHERE build_id='leg' AND path='src/calc.py' AND kind='line'"
        ).fetchall()
    }
    assert overlaps == {1: 2, 2: 2, 3: 2, 5: 1, 6: 1, 7: 1}

    # 升级后换报直接可用，无需重传历史报告。
    replace_report(
        conn,
        _report(
            "leg",
            1,
            3,
            {"src/calc.py": {"executed_lines": [1, 2, 3, 4]}},
        ),
    )
    body = list_uncovered(conn, "leg", "src/calc.py")
    assert body["uncovered_lines"] == []
    assert body["coverage_version"] == 8


def test_restart_does_not_recompute(tmp_path):
    """第二次启动只跑幂等 DDL：user_version 已推进，迁移分支整体跳过。"""
    path = str(tmp_path / "legacy.db")
    _create_legacy_db(path)

    db_module._conn = None
    first = init_db(path)
    gaps_after_migration = _gaps(first, "leg", "src/calc.py")
    first.close()

    # 重启：重新走 init_db；若再次执行回填，INSERT OR IGNORE 虽幂等但版本号
    # 重算会把换报后的版本冲回旧口径——这里先制造一次换报再重启验证。
    db_module._conn = None
    conn = init_db(path)
    replace_report(
        conn,
        _report("leg", 0, 6, {"src/calc.py": {"executed_lines": [1, 2]}}),
    )
    version_after_replace = build_status(conn, "leg")["coverage_version"]
    gaps_after_replace = _gaps(conn, "leg", "src/calc.py")
    conn.close()

    db_module._conn = None
    restarted = init_db(path)
    assert restarted.execute("PRAGMA user_version").fetchone()[0] == (
        db_module.SCHEMA_VERSION
    )
    assert build_status(restarted, "leg")["coverage_version"] == version_after_replace
    assert _gaps(restarted, "leg", "src/calc.py") == gaps_after_replace


# ------------------------------------------------------------ 随机差分

def test_randomized_gap_matches_full_recomputation(client):
    """随机多轮换报后，增量缺口始终与全量重算结果一致。"""
    rng = random.Random(20261126)
    db = get_conn()
    manifest = BuildManifestRequest(
        build_id="rnd",
        expected_shards=4,
        files=[
            {
                "path": "m.py",
                "executable_lines": list(range(1, 41)),
                "branches": [[10, 11], [10, 12], [20, 21], [30, 0]],
            }
        ],
    )
    register_build(db, manifest)
    all_lines = list(range(1, 41))
    all_branches = [(10, 11), (10, 12), (20, 21), (30, 0)]

    attempts = {s: 0 for s in range(4)}
    for _ in range(24):
        shard = rng.randrange(4)
        attempts[shard] += 1
        n_lines = rng.randrange(0, len(all_lines) + 1)
        lines = rng.sample(all_lines, n_lines)
        branches = rng.sample(all_branches, rng.randrange(0, len(all_branches) + 1))
        replace_report(
            db,
            _report(
                "rnd",
                shard,
                attempts[shard],
                {
                    "m.py": {
                        "executed_lines": lines,
                        "executed_branches": [list(b) for b in branches],
                    }
                },
            ),
        )

        # 每轮都对增量结果做独立全量校验（行 + 分支，含 NULL branch_no）。
        assert _gaps(db, "rnd", "m.py") == _recompute_uncovered(db, "rnd", "m.py")

        # 引用计数不变量：计数 = 命中该条目的不同分片数。
        for row in db.execute(
            "SELECT kind, line_no, branch_no, hit_shards FROM item_hits "
            "WHERE build_id='rnd' AND path='m.py'"
        ).fetchall():
            actual = db.execute(
                "SELECT COUNT(DISTINCT shard) AS c FROM coverage_rows "
                "WHERE build_id='rnd' AND path='m.py' AND kind=? AND line_no=? "
                "AND (branch_no IS ? OR branch_no=?)",
                (row["kind"], row["line_no"], row["branch_no"], row["branch_no"]),
            ).fetchone()["c"]
            assert row["hit_shards"] == actual

    # 收齐后 API 结果与缺口表、全量重算三者一致。
    body = list_uncovered(db, "rnd", "m.py")
    api_items = sorted(
        [("line", ln, None) for ln in body["uncovered_lines"]]
        + [("branch", s, d) for s, d in body["uncovered_branches"]]
    )
    assert api_items == _recompute_uncovered(db, "rnd", "m.py")
