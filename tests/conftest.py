"""Shared fixtures: a throwaway test database, set up once per session and dropped at the end.

Name: $HOLDSHORT_TEST_DB, else "holdshort_v2_t<N>" where N is the number in the git branch
(hq/<N>-...), else "holdshort_v2_t" + the pid. Never "holdshort" or "holdshort_v2".
"""

import os
import re
import subprocess

import pytest

FORBIDDEN = {"holdshort", "holdshort_v2"}


def test_db_name() -> str:
    name = os.environ.get("HOLDSHORT_TEST_DB", "").strip()
    if not name:
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                timeout=5, cwd=os.path.dirname(__file__),
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            branch = ""
        m = re.match(r"hq/(\d+)", branch)
        name = f"holdshort_v2_t{m.group(1) if m else os.getpid()}"
    if name in FORBIDDEN:
        raise RuntimeError(f"refusing to use {name!r} as the test database")
    return name


test_db_name.__test__ = False  # not a test


@pytest.fixture(scope="session")
def client():
    import holdshort.env  # noqa: F401  (loads .env)

    if not os.environ.get("MONGODB_URI", "").strip():
        pytest.skip("MONGODB_URI is not set (see SETUP_STATUS.md)")
    from holdshort.db import get_client

    c = get_client()
    yield c
    c.close()


@pytest.fixture(scope="session")
def test_db(client):
    import setup_atlas

    from holdshort.world import clear_cache

    name = test_db_name()
    client.drop_database(name)
    db = client[name]
    setup_atlas.setup(db, log=lambda *_: None)
    clear_cache()
    yield db
    client.drop_database(name)


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Unit tests never call a real model: holdshort.llm sees provider "none" unless a
    test stubs it."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
