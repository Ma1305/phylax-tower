"""Platform skeleton: /health, router discovery, and the serve supervisor."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from holdshort.api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "holdshort_v2_t20"


class FakeDb:
    name = TEST_DB

    def command(self, cmd):
        assert cmd == "ping"
        return {"ok": 1}


@pytest.fixture
def routers_pkg(tmp_path, monkeypatch):
    """A temporary routers package with one good and one broken router module."""
    name = f"tmp_routers_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "dummy.py").write_text(
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "def _db(request: __import__('fastapi').Request):\n"
        "    return request.app.state.db\n"
        "@router.get('/dummy')\n"
        "def dummy(db=Depends(_db)):\n"
        "    return {'ok': True, 'db': db.name}\n"
    )
    (pkg / "broken.py").write_text("raise RuntimeError('boom on import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def test_health():
    with TestClient(create_app(FakeDb(), routers_package="holdshort.api.routers")) as c:
        body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["mongo"] is True
    assert body["db"] == TEST_DB
    assert isinstance(body["routers"], list)
    assert isinstance(body["voice_enabled"], bool)


def test_cors_localhost_any_port():
    with TestClient(create_app(FakeDb())) as c:
        r = c.get("/health", headers={"Origin": "http://localhost:5180"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5180"
        r = c.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers


def test_dummy_router_included_and_broken_skipped(routers_pkg):
    with TestClient(create_app(FakeDb(), routers_package=routers_pkg)) as c:
        assert c.get("/dummy").json() == {"ok": True, "db": TEST_DB}
        body = c.get("/health").json()
    assert body["routers"] == ["dummy"]
    assert "broken" in body["routers_failed"]
    assert "boom on import" in body["routers_failed"]["broken"]
    assert body["status"] == "degraded"


def _free_port() -> int:
    for port in range(18000, 19000):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 18000-18999")


def test_serve_api_health_and_sigterm():
    if not os.environ.get("MONGODB_URI", "").strip():
        from holdshort import env  # noqa: F401 - loads .env

        if not os.environ.get("MONGODB_URI", "").strip():
            pytest.skip("MONGODB_URI is not set")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "holdshort.serve", "--only", "api", "--db", TEST_DB,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        body = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                body = httpx.get(f"http://127.0.0.1:{port}/health", timeout=10).json()
                break
            except httpx.HTTPError:
                assert proc.poll() is None, proc.stdout.read()
                time.sleep(0.3)
        assert body is not None, "api never answered /health"
        assert body["db"] == TEST_DB
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0, out
        assert "[api]" in out
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
