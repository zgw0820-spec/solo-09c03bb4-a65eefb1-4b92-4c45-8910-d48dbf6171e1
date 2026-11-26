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
5. **换报只影响该分片命中过的文件**：事务内仅删除/重建该分片自己的命中明细，
   其他分片数据不触碰。某条行/分支只有在失去**最后一份命中**时，才会在聚合
   结果中变回未覆盖。
6. **事务化与旧版可读**：换报在单个 `BEGIN IMMEDIATE` 事务内完成，配合 SQLite
   WAL，事务进行中读请求始终看到完整旧版；事务失败自动回滚，继续沿用旧版。

## 数据模型（SQLite）

| 表 | 作用 |
| --- | --- |
| `builds` | 构建 ID、预期分片数 |
| `build_files` | 登记的每文件可执行行/分支（未覆盖判定基准） |
| `reports` | 每个分片当前生效的尝试号（每分片至多一行） |
| `coverage_rows` | 各分片报告的命中明细（行/分支），换报时整体替换该分片部分 |

## API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /builds` | 登记构建（预期分片 + 文件清单） |
| `GET /builds/{build_id}` | 构建收齐状态、各分片生效尝试号、覆盖版本号 |
| `GET /builds/{build_id}/files` | 登记过的文件列表 |
| `POST /builds/{build_id}/shards/{shard}/attempts/{attempt}` | 上报/换报 |
| `GET /builds/{build_id}/uncovered/{path}` | 按文件查未覆盖行与分支 |

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

`demo/requests.sh` 是一组可直接执行的 curl 请求，覆盖：登记 → 未收齐拦截 →
两份分片收齐 → 旧版基线 → 同号重发幂等 → 旧尝试号被拒 → 高尝试号换报 →
未覆盖项翻转。

```bash
./demo/requests.sh
# 换报前后对比（src/calc.py）：
#   旧版：uncovered_lines=[4]，uncovered_branches=[[3,4]]
#   换报：uncovered_lines=[7]，uncovered_branches=[[3,5]]
#   （行4获得命中消失；行7与分支3->5失去最后一份命中，变回未覆盖）
```

`demo/coverage_reports/` 下是三份 coverage.py 风格的示例报告。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试覆盖：登记幂等与冲突、收齐门禁、同号重发、旧号拒绝、换报命中翻转、
分片越界、畸形报告、事务失败回滚沿用旧版、WAL 下换报期间读到完整旧版、
并发换报串行化。

## 目录结构

```
app/
  config.py           # 环境变量配置
  db.py               # SQLite 连接、WAL、建表、写事务
  schemas.py          # Pydantic 请求/响应模型
  coverage_parser.py  # coverage.py JSON 解析与校验
  repository.py       # 登记/换报/查询核心逻辑
  main.py             # FastAPI 路由
demo/                 # 演示请求脚本与示例报告
tests/                # pytest 套件
```

## 已知限制 / 待补

- 单实例 + 单 SQLite 文件，未做多副本水平扩展（WAL 已支持单实例内读写并发）。
- 构建登记不可变更、不设过期清理；长期运行需要补 TTL/归档策略。
- 上报文件路径若不在登记清单中，其命中会被保存但不参与未覆盖判定
  （未覆盖以登记基准为准），目前仅静默接受，可考虑加告警。
- 未接入鉴权，默认假定运行在可信 CI 内网。
- `Dockerfile`/`docker-compose.yml` 已提供并通过 YAML 与构建步骤静态核对，
  但当前开发沙箱无 Docker 守护进程，未做实际镜像构建验证（本地已用等价
  uvicorn 启动方式实测全部流程）。
