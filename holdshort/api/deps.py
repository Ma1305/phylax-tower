"""FastAPI dependencies shared by every router."""

from __future__ import annotations

from fastapi import Request
from pymongo.database import Database


def get_db(request: Request) -> Database:
    """The database the app was started with (``app.state.db``)."""
    return request.app.state.db
