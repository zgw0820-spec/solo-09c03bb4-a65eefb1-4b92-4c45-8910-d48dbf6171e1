"""pytest 夹具：每个用例使用独立临时 SQLite 文件。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.db import reset_for_tests
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    tmp_dir = tempfile.mkdtemp(prefix="covdb-")
    db_path = os.path.join(tmp_dir, "test.db")
    reset_for_tests(db_path)
    with TestClient(app) as c:
        yield c
