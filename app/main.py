"""FastAPI 应用：构建登记、报告上报（含换报）、未覆盖查询。"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import repository
from .coverage_parser import CoverageParseError, parse_report_payload
from .db import get_conn, init_db
from .schemas import (
    BuildManifestRequest,
    BuildResponse,
    BuildStatus,
    ReportAccepted,
    UncoveredResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Coverage Collector",
    version="2.0.0",
    description=(
        "接收 coverage.py JSON 报告的分片覆盖收集服务。"
        "支持按尝试号换报、增量缺口维护与逐文件未覆盖直查。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(sqlite3.OperationalError)
def _sqlite_busy_handler(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    # 写锁等待超时等：提示客户端重试，不破坏既有版本。
    return JSONResponse(
        status_code=503,
        content={"detail": f"数据库暂时不可用，请重试: {exc}"},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- 构建登记

@app.post("/builds", response_model=BuildResponse, status_code=201)
def register_build(
    manifest: BuildManifestRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BuildResponse:
    try:
        n_files = repository.register_build(conn, manifest)
    except repository.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return BuildResponse(
        build_id=manifest.build_id,
        expected_shards=manifest.expected_shards,
        registered_files=n_files,
    )


@app.get("/builds/{build_id}", response_model=BuildStatus)
def get_build_status(
    build_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> BuildStatus:
    try:
        return BuildStatus(**repository.build_status(conn, build_id))
    except repository.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/builds/{build_id}/files")
def list_files(
    build_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    try:
        return {"build_id": build_id, "files": repository.list_build_files(conn, build_id)}
    except repository.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- 报告上报

@app.post(
    "/builds/{build_id}/shards/{shard}/attempts/{attempt}",
    response_model=ReportAccepted,
)
async def upload_report(
    build_id: str,
    shard: int,
    attempt: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> ReportAccepted:
    """上报一份 coverage.py JSON 报告。

    路径携带分片号与尝试号；请求体为 coverage.py ``coverage json`` 的原始产物
    （顶层含 ``files``）。换报语义见 :mod:`app.repository`。
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc

    try:
        req = parse_report_payload(build_id, shard, attempt, payload)
    except CoverageParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = repository.replace_report(conn, req)
    except repository.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except repository.ConflictError as exc:
        # 尝试号过旧：当前生效版本不动。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReportAccepted(**result)


# ---------------------------------------------------------------- 未覆盖查询

@app.get(
    "/builds/{build_id}/uncovered/{path:path}",
    response_model=UncoveredResponse,
)
def get_uncovered(
    build_id: str,
    path: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> UncoveredResponse:
    """按文件查未覆盖行/分支。

    只有构建的预期分片全部收齐后才返回结果；未收齐时 ``complete=false``
    且列表为空。换报事务进行中读到的始终是一份完整旧版（WAL 快照）。
    """
    try:
        return UncoveredResponse(
            **repository.list_uncovered(conn, build_id, path)
        )
    except repository.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
