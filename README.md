# Coverage Collector

CI 合入前的覆盖收集服务：用 FastAPI 接收 [coverage.py](https://coverage.readthedocs.io/)
的 JSON 报告，按 **构建 / 分片 / 尝试号** 存入 SQLite，并在所有预期分片收齐后，
按文件返回未覆盖的行与分支。

## 核心语义

1. **先登记，后上报**：`POST /builds` 登记一个构建的预期分片数，以及每个文件的
   可执行行（`executable_lines`）与分支（`branches`，`[源行, 目标行]` 二元组）。
   未覆盖项始终以登记内容为准，上报数据只提供“命中”事实。
2. **收齐才可查**：已收到的分片数达到 `expected_shards` 前，未覆盖查询返回
   `complete=false` 且不给出明细，避免拿不完整数据阻塞/误导 CI。
3. **上报带分片号与尝试号**：
   `POST /builds/{build_id}/shards/{shard}/attempts/{attempt}`，请求体为
   `coverage json` 的原始产物（顶层 `files`，取其中 `executed_lines` 与
   `executed_branches`，其余字段忽略）。
4. **换报规则**：
   - 已生效的尝试只接受**更高尝试号**替代；更旧尝试号返回 `409`，当前结果不动；
   - **同号重发幂等**：返回 `duplicate_unchanged`，不删不写、结果不变。
5. **换报只维护差异涉及的缺口**：换报时先求新报告与旧报告的两个差异集合——
   `added`（新增命中）与 `removed`（撤销命中），仅对这些条目维护命中引用计数
   与缺口表；差异之外的条目（含多分片重叠命中项）完全不触碰。某条行/分支只有
   在失去**最后一份命中**（引用计数归零）时才会重新进入未覆盖集合；只要还有
   别的分片命中，就不会被重新列为未覆盖。
6. **查询不扫命中明细**：收齐后的 `GET .../uncovered/{path}` 直接读缺口表
   `uncovered_gaps`，既不扫描 `coverage_rows` 命中明细，也不扫描任何已覆盖项。
7. **事务化与同版快照**：缺口、收齐标记、覆盖版本号在同一个 `BEGIN IMMEDIATE`
   事务内提交，要么整体生效要么整体回滚；读请求通过显式快照事务
   （`BEGIN DEFERRED` + WAL）保证三项数据来自同一个已提交快照，换报失败时
   三项全部保持旧版。

## 数据模型（SQLite）

| 表 | 作用 |
| --- | --- |
| `builds` | 构建 ID、预期分片数、`complete` 收齐标记、`coverage_version` 单调版本号 |
| `build_files` | 登记的每文件可执行行/分支（未覆盖判定基准） |
| `reports` | 每个分片当前生效的尝试号（每分片至多一行） |
| `coverage_rows` | 各分片报告的命中明细留档，换报时用于求 added/removed 差异；查询路径不扫描 |
| `item_hits` | 登记项级命中引用计数：一条行/分支当前被多少个分片命中（只统计登记项） |
| `uncovered_gaps` | 缺口表：登记项中没有任何分片命中的行/分支，按文件查询只读它 |

### 旧库升级（无需重传，重启不重算）

数据库格式用 `PRAGMA user_version` 管理。旧版库（只有 `coverage_rows`，
`user_version=0`）在新进程启动时于单个写事务内一次性：

1. 给 `builds` 补 `complete` / `coverage_version` 列；
2. 按分片聚合回填 `item_hits` 引用计数；
3. 以登记项减去命中集合回填 `uncovered_gaps`；
4. 以现有 `reports` 回填收齐标记，版本号沿用旧口径（各分片尝试号之和）；
5. 推进 `user_version=1`。

升级后历史报告无需重传即可直接查询与换报；之后重启只执行幂等 DDL，
`user_version` 已推进，迁移分支整体跳过，不会全库重算。

## API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /builds` | 登记构建（预期分片 + 文件清单） |
| `GET /builds/{build_id}` | 构建收齐状态、各分片生效尝试号、覆盖版本号 |
| `GET /builds/{build_id}/files` | 登记过的文件列表 |
| `POST /builds/{build_id}/shards/{shard}/attempts/{attempt}` | 上报/换报 |
| `GET /builds/{build_id}/uncovered/{path}` | 按文件查未覆盖行与分支（直读缺口表） |

交互式文档：服务启动后访问 `http://localhost:8000/docs`。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，SQLite 数据持久化在 coverage-data 卷的 /data/coverage.db
```

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
COVERAGE_DB_PATH=./data/coverage.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COVERAGE_DB_PATH` | `/data/coverage.db` | SQLite 数据库路径 |
| `COVERAGE_BUSY_TIMEOUT_MS` | `5000` | 写锁等待毫秒数，超时返回 503 提示重试 |

## 演示换报全流程

`demo/requests.sh` 是一组可直接执行的 curl 请求。演示数据特意让两个分片在
`src/calc.py` 上大面积命中重叠，覆盖：登记 → 未收齐拦截 → 两份分片收齐 →
旧版基线 → 同号重发幂等 → 旧尝试号被拒 → 高尝试号换报 → 重叠命中受保护、
仅失去最后命中的条目翻转。

```bash
./demo/requests.sh
# 收齐基线（src/calc.py）：
#   uncovered_lines=[4, 10]，uncovered_branches=[[3, 4]]
# 换报 shard0 attempt1 -> attempt2（新增行4/分支3->4，撤销行7/分支3->5）：
#   行 4、分支 3->4 获得命中 -> 缺口消失
#   行 7、分支 3->5 被 shard0 撤销，但 shard1 仍命中 -> 保持覆盖，不回缺口
#   行 10 自始无任何分片命中 -> 始终是缺口
# 换报后：
#   uncovered_lines=[10]，uncovered_branches=[]
```

`demo/coverage_reports/` 下是三份 coverage.py 风格的示例报告。

## 性能基准

`bench/benchmark.py` 用同一组高覆盖、分片间高重叠（约 90% 行被多个分片重复
命中）的报告，对比改造前后的查询与换报开销：

```bash
SP=$(python3 -c "import site; print(site.getusersitepackages())")
PYTHONPATH="$SP" python3 -s bench/benchmark.py
```

实测（8 分片 × 20 文件 × 每文件 2004 个登记项，单文件约 1.5 万条命中明细）：

| 指标 | 旧实现 | 新实现 | 提速（多次运行区间） |
| --- | --- | --- | --- |
| 单文件未覆盖查询 | 9.4–13.0 ms（扫 15209 条命中明细后差集） | 0.03–0.04 ms（直读缺口表，仅 1 行缺口） | ~300× |
| 4 个条目变化的换报 | 824–4058 ms（涉及文件全量重算） | 1.0–1.4 ms（只维护 added/removed 差异） | ~800–2800× |

两种实现算出的未覆盖集合逐字节一致（脚本内置断言）。具体数字随机器负载
波动，量级与“查询不再随命中明细规模增长、换报只随差异规模增长”的结论稳定。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试覆盖（26 个用例）：登记幂等与冲突、收齐门禁、同号重发、旧号拒绝、换报
命中翻转、分片越界、畸形报告、事务失败回滚沿用旧版、WAL 下换报期间读到完整
旧版、并发换报串行化，以及增量模型专项——

- 多分片重叠命中时撤销一份命中不回缺口，只有最后一份命中失去才翻转；
- 查询语句经 trace 校验不触及 `coverage_rows` / `item_hits`，查询计划走
  缺口表覆盖索引而非全表扫描；
- 缺口/收齐/版本号同快照一致，换报失败三项不动；
- 旧版 SQLite 库自动升级、版本口径连续、升级后可直接换报、重启不重算；
- 24 轮随机换报差分：增量缺口始终等于独立全量重算结果，引用计数始终等于
  实际命中分片数。

## 目录结构

```
app/
  config.py           # 环境变量配置
  db.py               # SQLite 连接、WAL、建表、旧库迁移、读写事务
  schemas.py          # Pydantic 请求/响应模型
  coverage_parser.py  # coverage.py JSON 解析与校验
  repository.py       # 登记/增量换报/缺口查询核心逻辑
  main.py             # FastAPI 路由
bench/
  benchmark.py        # 新旧查询/换报开销对比与正确性断言
demo/                 # 演示请求脚本与示例报告（含分片重叠场景）
tests/                # pytest 套件
```

## 已知限制 / 待补

- 单实例 + 单 SQLite 文件，未做多副本水平扩展（WAL 已支持单实例内读写并发）。
- 构建登记不可变更、不设过期清理；长期运行需要补 TTL/归档策略。
- 上报文件路径若不在登记清单中，其命中会留档在 `coverage_rows` 但不进
  `item_hits`、不参与未覆盖判定（未覆盖以登记基准为准），目前仅静默接受，
  可考虑加告警。
- 未接入鉴权，默认假定运行在可信 CI 内网。
- `Dockerfile`/`docker-compose.yml` 已提供并通过 YAML 与构建步骤静态核对，
  但当前开发沙箱无 Docker 守护进程，未做实际镜像构建验证（本地已用等价
  uvicorn 启动方式实测全部流程，含旧库升级与重启）。
