"""端到端关键流程测试。"""

from __future__ import annotations

MANIFEST = {
    "build_id": "b1",
    "expected_shards": 2,
    "files": [
        {
            "path": "src/calc.py",
            "executable_lines": [1, 2, 3, 4, 5, 6, 7],
            "branches": [[3, 4], [3, 5], [6, 7]],
        },
        {"path": "src/util.py", "executable_lines": [1, 2, 3], "branches": []},
    ],
}

SHARD0_A1 = {
    "files": {
        "src/calc.py": {
            "executed_lines": [1, 2, 3, 5, 6, 7],
            "executed_branches": [[3, 5], [6, 7]],
        }
    }
}

SHARD0_A2 = {
    "files": {
        "src/calc.py": {
            # 新命中行 4，但丢了行 7 与分支 3->5
            "executed_lines": [1, 2, 3, 4, 5, 6],
            "executed_branches": [[3, 4], [6, 7]],
        }
    }
}

SHARD1_A1 = {
    "files": {
        "src/util.py": {
            "executed_lines": [1, 2, 3],
            "executed_branches": [],
        }
    }
}


def _register(client):
    r = client.post("/builds", json=MANIFEST)
    assert r.status_code == 201, r.text
    return r


def _upload(client, build, shard, attempt, body):
    return client.post(
        f"/builds/{build}/shards/{shard}/attempts/{attempt}", json=body
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_register_twice_conflicts(client):
    _register(client)
    r = client.post("/builds", json=MANIFEST)
    assert r.status_code == 409


def test_uncovered_blocked_until_all_shards(client):
    _register(client)
    _upload(client, "b1", 0, 1, SHARD0_A1)

    status = client.get("/builds/b1").json()
    assert status["received_shards"] == 1
    assert status["complete"] is False

    r = client.get("/builds/b1/uncovered/src/calc.py")
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is False
    assert body["uncovered_lines"] == []
    assert body["uncovered_branches"] == []


def test_full_flow_and_uncovered_baseline(client):
    _register(client)
    assert _upload(client, "b1", 0, 1, SHARD0_A1).status_code == 200
    assert _upload(client, "b1", 1, 1, SHARD1_A1).status_code == 200

    body = client.get("/builds/b1/uncovered/src/calc.py").json()
    assert body["complete"] is True
    assert body["uncovered_lines"] == [4]
    assert body["uncovered_branches"] == [[3, 4]]

    util = client.get("/builds/b1/uncovered/src/util.py").json()
    assert util["uncovered_lines"] == []
    assert util["uncovered_branches"] == []


def test_same_attempt_resend_is_idempotent(client):
    _register(client)
    _upload(client, "b1", 0, 1, SHARD0_A1)
    _upload(client, "b1", 1, 1, SHARD1_A1)

    before = client.get("/builds/b1/uncovered/src/calc.py").json()
    r = _upload(client, "b1", 0, 1, SHARD0_A1)
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate_unchanged"
    after = client.get("/builds/b1/uncovered/src/calc.py").json()
    assert before == after
    assert client.get("/builds/b1").json()["shards"][0]["attempt"] == 1


def test_older_attempt_rejected(client):
    _register(client)
    _upload(client, "b1", 0, 2, SHARD0_A2)
    r = _upload(client, "b1", 0, 1, SHARD0_A1)
    assert r.status_code == 409
    # 生效的仍是 attempt 2
    shard0 = client.get("/builds/b1").json()["shards"][0]
    assert shard0["attempt"] == 2


def test_replacement_only_last_hit_loss_flips_uncovered(client):
    _register(client)
    _upload(client, "b1", 0, 1, SHARD0_A1)
    _upload(client, "b1", 1, 1, SHARD1_A1)

    baseline = client.get("/builds/b1/uncovered/src/calc.py").json()
    assert baseline["uncovered_lines"] == [4]
    assert baseline["uncovered_branches"] == [[3, 4]]
    version_before = baseline["coverage_version"]

    r = _upload(client, "b1", 0, 2, SHARD0_A2)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["previous_attempt"] == 1

    after = client.get("/builds/b1/uncovered/src/calc.py").json()
    # 行 4 获得命中 -> 从未覆盖消失；行 7 失去最后命中 -> 变回未覆盖
    assert after["uncovered_lines"] == [7]
    # 分支 3->4 获得命中；3->5 失去最后命中变回未覆盖；6->7 仍命中
    assert after["uncovered_branches"] == [[3, 5]]
    assert after["coverage_version"] > version_before

    # util.py 不在本次换报涉及的文件中，结果保持不变
    util = client.get("/builds/b1/uncovered/src/util.py").json()
    assert util["uncovered_lines"] == []


def test_shard_out_of_range_rejected(client):
    _register(client)
    r = _upload(client, "b1", 2, 1, SHARD0_A1)
    assert r.status_code == 409
    r = _upload(client, "b1", -1, 1, SHARD0_A1)
    # 路径参数 ge=0 由 FastAPI 拦截
    assert r.status_code == 422


def test_unknown_build_and_file_404(client):
    assert client.get("/builds/nope").status_code == 404
    _register(client)
    _upload(client, "b1", 0, 1, SHARD0_A1)
    _upload(client, "b1", 1, 1, SHARD1_A1)
    r = client.get("/builds/b1/uncovered/src/missing.py")
    assert r.status_code == 404


def test_malformed_report_rejected(client):
    _register(client)
    r = client.post(
        "/builds/b1/shards/0/attempts/1", json={"totals": {}}
    )
    assert r.status_code == 422
    r2 = client.post(
        "/builds/b1/shards/0/attempts/1",
        json={"files": {"x.py": {"executed_lines": ["bad"]}}},
    )
    assert r2.status_code == 422


def test_native_coveragepy_shape_accepted(client):
    """带 totals/summary/missing_* 的原生 coverage.py 结构也能解析。"""
    _register(client)
    native = {
        "files": {
            "src/calc.py": {
                "executed_lines": [1, 2, 3, 5, 6, 7],
                "summary": {"percent_covered": 75.0},
                "missing_lines": [4],
                "excluded_lines": [],
                "executed_branches": [[3, 5], [6, 7]],
                "missing_branches": [[3, 4]],
            }
        },
        "totals": {"percent_covered": 75.0},
    }
    r = _upload(client, "b1", 0, 1, native)
    assert r.status_code == 200
    assert r.json()["files_in_report"] == 1
