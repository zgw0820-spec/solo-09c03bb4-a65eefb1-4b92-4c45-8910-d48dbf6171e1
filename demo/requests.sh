#!/usr/bin/env bash
# 演示完整换报流程的 API 请求集合（纯 curl，可直接执行）。
#
# 用法：
#   ./demo/requests.sh            # 默认 http://localhost:8000
#   BASE_URL=http://other:8000 ./demo/requests.sh
#
# 流程：
#   1. 登记构建（2 个预期分片 + 每文件可执行行/分支）
#   2. 收齐前尝试查询 -> complete=false，拒绝给出未覆盖明细
#   3. 上报 shard0 attempt1、shard1 attempt1 -> 收齐
#   4. 查询未覆盖项（旧版基线）
#   5. 同号重发 shard0 attempt1 -> duplicate_unchanged，结果不变
#   6. 更旧尝试号 shard0 attempt0 -> 409 拒绝
#   7. 换报 shard0 attempt2 -> accepted，只重算该分片命中过的文件
#   8. 再查未覆盖项 -> 行/分支随最后一份命中的得失翻转
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="demo-build-001"

hr() { printf '\n========== %s ==========\n' "$1"; }

hr "1. 登记构建（预期分片与可执行行/分支）"
curl -sS -X POST "$BASE_URL/builds" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/manifest.json"; echo

hr "2. 未收齐时查询 src/calc.py -> complete=false"
curl -sS "$BASE_URL/builds/$BUILD/uncovered/src/calc.py"; echo

hr "3a. 上报 shard 0 / attempt 1"
curl -sS -X POST \
  "$BASE_URL/builds/$BUILD/shards/0/attempts/1" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard0_attempt1.json"; echo

hr "3b. 上报 shard 1 / attempt 1"
curl -sS -X POST \
  "$BASE_URL/builds/$BUILD/shards/1/attempts/1" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard1_attempt1.json"; echo

hr "3c. 构建状态（应 complete=true）"
curl -sS "$BASE_URL/builds/$BUILD"; echo

hr "4. 收齐后查询 src/calc.py 未覆盖项（旧版基线）"
curl -sS "$BASE_URL/builds/$BUILD/uncovered/src/calc.py"; echo

hr "5. 同号重发 shard0 attempt1 -> duplicate_unchanged"
curl -sS -X POST \
  "$BASE_URL/builds/$BUILD/shards/0/attempts/1" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard0_attempt1.json"; echo

hr "6. 更旧尝试号 attempt 0 -> 期望 422（attempt 必须 >= 1，路由层拦截）"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "$BASE_URL/builds/$BUILD/shards/0/attempts/0" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard0_attempt1.json"

hr "7. 换报 shard0 attempt2 -> accepted（previous_attempt=1）"
curl -sS -X POST \
  "$BASE_URL/builds/$BUILD/shards/0/attempts/2" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard0_attempt2.json"; echo

hr "8. 换报后再查 src/calc.py 未覆盖项（行4被新报告命中；行7与分支3->5失去最后命中）"
curl -sS "$BASE_URL/builds/$BUILD/uncovered/src/calc.py"; echo

hr "9. attempt2 已生效后重发旧的有效尝试号 attempt1 -> 期望 409，结果不动"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "$BASE_URL/builds/$BUILD/shards/0/attempts/1" \
  -H 'Content-Type: application/json' \
  -d @"$HERE/coverage_reports/shard0_attempt1.json"
curl -sS "$BASE_URL/builds/$BUILD" | python3 -c 'import json,sys; print("当前生效:", json.load(sys.stdin)["shards"])'

hr "附：查询 src/util.py（全量覆盖）"
curl -sS "$BASE_URL/builds/$BUILD/uncovered/src/util.py"; echo
