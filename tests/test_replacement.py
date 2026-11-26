"""换报事务性与并发语义测试。"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.config import settings
from app.db import write_transaction
from app.repository import (
    ConflictError,
    build_status,
    list_uncovered,
    register_build,
    replace_report,
)
from app.schemas import BuildManifestRequest, ReportUploadRequest
from tests.test_api import MANIFEST, SHARD0_A1, SHARD0_A2


def _manifest():
    return BuildManifestRequest(**MANIFEST)


def _report(build_id, shard, attempt, files):
    return ReportUploadRequest(
        build_id=build_id, shard=shard, attempt=attempt, files=files
    )


def _new_conn() -> sqlite3.Connection:
    """打开指向同一数据库文件的独立连接（模拟两个并发请求）。"""
    from app.db import get_conn

    db_path = get_conn().execute("PRAGMA database_list").fetchone()[2]
    conn = sqlite3.connect(
        db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_failed_replacement_keeps_old_version(client):
    """换报事务内异常 -> 回滚，旧版本完整沿用。"""
    from app.db import get_conn

    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )

    before = list_uncovered(db, "b1", "src/calc.py")

    # 手工制造一个必失败的换报：先删明细，再在同事务里违反唯一约束触发回滚。
    with pytest.raises(sqlite3.IntegrityError):
        with write_transaction(db) as tx:
            tx.execute(
                "DELETE FROM coverage_rows WHERE build_id = ? AND shard = ?",
                ("b1", 0),
            )
            tx.execute(
                "INSERT INTO reports (build_id, shard, attempt) VALUES (?, ?, ?)",
                ("b1", 0, 999),
            )
            # 同主键再插一次 -> 冲突，整个事务回滚
            tx.execute(
                "INSERT INTO reports (build_id, shard, attempt) VALUES (?, ?, ?)",
                ("b1", 0, 998),
            )

    after = list_uncovered(db, "b1", "src/calc.py")
    assert after == before
    assert build_status(db, "b1")["shards"][0]["attempt"] == 1


def test_concurrent_replacement_serialized(client):
    """两个并发换报经写锁串行化，最终只剩更高尝试号生效，无撕裂状态。

    两种合法调度都必须满足：
    - 2 先 3 后：两份都接受；
    - 3 先 2 后：2 作为更旧尝试号被 ConflictError 拒绝。
    无论哪种，最终生效的都必须是 attempt 3，且明细只属于 3 的内容。
    """
    from app.db import get_conn

    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )

    errors: list[Exception] = []

    def worker(attempt: int, payload: dict) -> None:
        conn = _new_conn()
        try:
            replace_report(conn, _report("b1", 0, attempt, payload["files"]))
        except ConflictError as exc:
            # 仅允许“更旧尝试号”这一种冲突，其他错误仍算失败。
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001 - 测试收集所有异常
            errors.append(exc)
        finally:
            conn.close()

    t2 = threading.Thread(target=worker, args=(2, SHARD0_A2))
    t3 = threading.Thread(
        target=worker,
        args=(3, {"files": {"src/calc.py": {"executed_lines": [1, 2, 3, 4]}}}),
    )
    t2.start()
    t3.start()
    t2.join()
    t3.join()

    assert all(isinstance(e, ConflictError) for e in errors)
    assert len(errors) <= 1
    # 串行化后最终生效尝试号必须是 3（更高者）
    status = build_status(db, "b1")
    assert status["shards"][0]["attempt"] == 3
    # reports 中每个分片至多一条生效记录
    rows = db.execute(
        "SELECT COUNT(*) AS c FROM reports WHERE build_id='b1' AND shard=0"
    ).fetchone()
    assert rows["c"] == 1
    # 命中明细只属于当前生效版本（attempt 3：4 行、0 分支，无 attempt 2 残留）
    hit_count = db.execute(
        "SELECT COUNT(*) AS c FROM coverage_rows WHERE build_id='b1' AND shard=0"
    ).fetchone()
    assert hit_count["c"] == 4
    branch_count = db.execute(
        "SELECT COUNT(*) AS c FROM coverage_rows "
        "WHERE build_id='b1' AND shard=0 AND kind='branch'"
    ).fetchone()
    assert branch_count["c"] == 0


def test_stale_attempt_leaves_data_untouched(client):
    from app.db import get_conn

    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 2, SHARD0_A2["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )

    with pytest.raises(ConflictError):
        replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))

    status = build_status(db, "b1")
    assert status["shards"][0]["attempt"] == 2
    body = list_uncovered(db, "b1", "src/calc.py")
    assert body["uncovered_lines"] == [7]


def test_reads_see_complete_old_version_during_replacement(client):
    """换报事务未提交期间，另一连接必须读到完整旧版，不出现撕裂状态。"""
    from app.db import get_conn

    db = get_conn()
    register_build(db, _manifest())
    replace_report(db, _report("b1", 0, 1, SHARD0_A1["files"]))
    replace_report(
        db, _report("b1", 1, 1, {"src/util.py": {"executed_lines": [1, 2, 3]}})
    )

    old = list_uncovered(db, "b1", "src/calc.py")
    assert old["complete"] is True
    assert old["uncovered_lines"] == [4]

    other = _new_conn()
    try:
        # 模拟换报进行中：拿到写锁并完成改写但先不提交。
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "DELETE FROM coverage_rows WHERE build_id = ? AND shard = ?",
            ("b1", 0),
        )
        other.executemany(
            "INSERT INTO coverage_rows "
            "(build_id, shard, path, kind, line_no, branch_no) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("b1", 0, "src/calc.py", "line", ln, None)
                for ln in SHARD0_A2["files"]["src/calc.py"]["executed_lines"]
            ]
            + [
                ("b1", 0, "src/calc.py", "branch", src, dst)
                for src, dst in SHARD0_A2["files"]["src/calc.py"][
                    "executed_branches"
                ]
            ],
        )
        other.execute(
            "UPDATE reports SET attempt = 2, received_at = datetime('now') "
            "WHERE build_id = 'b1' AND shard = 0"
        )

        # 事务挂起期间，主连接反复读到的都是完整旧版。
        for _ in range(5):
            snapshot = list_uncovered(db, "b1", "src/calc.py")
            assert snapshot == old
            assert build_status(db, "b1")["shards"][0]["attempt"] == 1

        other.execute("COMMIT")
    finally:
        other.close()

    # 提交后才切换到新版本。
    new = list_uncovered(db, "b1", "src/calc.py")
    assert new["uncovered_lines"] == [7]
    assert new["uncovered_branches"] == [[3, 5]]
    assert build_status(db, "b1")["shards"][0]["attempt"] == 2
