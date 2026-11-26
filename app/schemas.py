"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------- 构建登记 ----------

class FileManifest(BaseModel):
    """一个文件在构建登记时声明的可执行行与分支。

    分支用二元组 [源行号, 目标行号] 表示，与 coverage.py 的 arcs 概念一致。
    """

    path: str = Field(..., min_length=1)
    executable_lines: list[int] = Field(default_factory=list)
    branches: list[list[int]] = Field(default_factory=list)

    def normalized_branches(self) -> list[tuple[int, int]]:
        return [(int(src), int(dst)) for src, dst in self.branches]


class BuildManifestRequest(BaseModel):
    build_id: str = Field(..., min_length=1)
    expected_shards: int = Field(..., gt=0)
    files: list[FileManifest] = Field(default_factory=list)


class BuildResponse(BaseModel):
    build_id: str
    expected_shards: int
    registered_files: int


# ---------- 报告上报 ----------

class ReportUploadRequest(BaseModel):
    """上报体：分片号、尝试号 + coverage.py 风格 JSON（顶层 files）。"""

    build_id: str = Field(..., min_length=1)
    shard: int = Field(..., ge=0)
    attempt: int = Field(..., ge=1)
    files: dict[str, "CoverageFile"]


class CoverageFile(BaseModel):
    # coverage.py: executed_lines 含已执行行；executed_branches 为命中的 arc 列表
    executed_lines: list[int] = Field(default_factory=list)
    executed_branches: list[list[int]] = Field(default_factory=list)
    # 允许携带但本服务不依赖的 coverage.py 原生字段
    summary: dict | None = None
    missing_lines: list[int] | None = None
    excluded_lines: list[int] | None = None
    missing_branches: list[list[int]] | None = None


class ReportAccepted(BaseModel):
    build_id: str
    shard: int
    attempt: int
    status: str  # accepted | superseded_attempt | duplicate_unchanged
    previous_attempt: int | None = None
    files_in_report: int


# ---------- 查询 ----------

class UncoveredItem(BaseModel):
    kind: str  # line | branch
    line_no: int
    branch_no: int | None = None  # 分支的目标行号


class UncoveredResponse(BaseModel):
    build_id: str
    path: str
    complete: bool
    expected_shards: int
    received_shards: int
    coverage_version: int
    uncovered_lines: list[int]
    uncovered_branches: list[list[int]]


class BuildStatus(BaseModel):
    build_id: str
    expected_shards: int
    received_shards: int
    complete: bool
    coverage_version: int
    shards: list["ShardStatus"]


class ShardStatus(BaseModel):
    shard: int
    attempt: int
    received_at: str


ReportUploadRequest.model_rebuild()
BuildStatus.model_rebuild()
