import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试用例一个独立的临时 SQLite 库。"""
    monkeypatch.setenv("LINEAGE_DB", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
