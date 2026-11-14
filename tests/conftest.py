"""Shared fixtures: a throwaway "<MONGODB_DB>_test" database, set up once and dropped at the end."""

import os

import pytest

from holdshort.env import mongodb_db_name


@pytest.fixture(scope="session")
def client():
    if not os.environ.get("MONGODB_URI", "").strip():
        pytest.skip("MONGODB_URI is not set (see SETUP_STATUS.md)")
    from holdshort.db import get_client

    c = get_client()
    yield c
    c.close()


@pytest.fixture(scope="session")
def test_db(client):
    import setup_atlas

    name = f"{mongodb_db_name()}_test"
    client.drop_database(name)
    db = client[name]
    setup_atlas.setup(db, log=lambda *_: None)
    yield db
    client.drop_database(name)
