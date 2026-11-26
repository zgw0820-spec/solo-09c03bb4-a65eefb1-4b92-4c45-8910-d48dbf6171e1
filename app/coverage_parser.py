"""解析 coverage.py 的 JSON 报告。

coverage.py ``coverage json`` 产物结构（节选）::

    {
      "files": {
        "mypkg/app.py": {
          "executed_lines": [1, 2, 4],
          "summary": {...},
          "missing_lines": [...],
          "excluded_lines": [...],
          "executed_branches": [[10, 11], [10, 12]],
          "missing_branches": [[10, -3]]
        },
        ...
      },
      "totals": {...}
    }

本服务只取 ``files.*.executed_lines`` 与 ``files.*.executed_branches``。
也兼容把上述字段直接平铺在请求体里的精简格式（见 schemas.ReportUploadRequest）。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .schemas import CoverageFile, ReportUploadRequest


class CoverageParseError(ValueError):
    """报告内容无法解析或不符合 coverage.py 结构。"""


def parse_report_payload(
    build_id: str,
    shard: int,
    attempt: int,
    payload: dict[str, Any],
) -> ReportUploadRequest:
    """把原始 JSON dict 构造成 :class:`ReportUploadRequest`。

    支持两种形态：
    1. 原生 coverage.py：``{"files": {...}, "totals": {...}}``，分片/尝试号来自参数；
    2. 已平铺：请求体自带 build_id/shard/attempt/files（以参数为准，忽略自带值）。
    """
    if not isinstance(payload, dict):
        raise CoverageParseError("请求体必须是 JSON 对象")

    files = payload.get("files")
    if not isinstance(files, dict):
        raise CoverageParseError(
            "报告缺少 files 字段（coverage.py JSON 报告应包含顶层 'files' 对象）"
        )

    clean_files: dict[str, dict[str, Any]] = {}
    for path, data in files.items():
        if not isinstance(path, str) or not path:
            raise CoverageParseError(f"非法文件路径: {path!r}")
        if not isinstance(data, dict):
            raise CoverageParseError(f"文件 {path} 的条目必须是对象")
        entry: dict[str, Any] = {}
        executed_lines = data.get("executed_lines", [])
        executed_branches = data.get("executed_branches", [])
        if not isinstance(executed_lines, list) or not all(
            isinstance(x, int) for x in executed_lines
        ):
            raise CoverageParseError(f"{path}.executed_lines 必须是整数数组")
        if not isinstance(executed_branches, list) or not all(
            isinstance(b, list) and len(b) == 2 and all(isinstance(x, int) for x in b)
            for b in executed_branches
        ):
            raise CoverageParseError(
                f"{path}.executed_branches 必须是 [[源行, 目标行], ...] 形式"
            )
        entry["executed_lines"] = executed_lines
        entry["executed_branches"] = executed_branches
        if "summary" in data:
            entry["summary"] = data["summary"]
        clean_files[path] = entry

    try:
        return ReportUploadRequest(
            build_id=build_id,
            shard=shard,
            attempt=attempt,
            files={p: CoverageFile(**d) for p, d in clean_files.items()},
        )
    except ValidationError as exc:
        raise CoverageParseError(str(exc)) from exc
