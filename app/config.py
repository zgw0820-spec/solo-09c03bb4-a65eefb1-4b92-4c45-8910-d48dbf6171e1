"""运行期配置，全部可通过环境变量覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("COVERAGE_DB_PATH", "/data/coverage.db")
    # SQLite 写锁等待（毫秒），配合 WAL 支持换报期间并发查询。
    busy_timeout_ms: int = int(os.getenv("COVERAGE_BUSY_TIMEOUT_MS", "5000"))


settings = Settings()
