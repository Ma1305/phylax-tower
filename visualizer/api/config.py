"""Settings for the visualizer API. Secrets are read from the environment and never printed."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROTECTED_DBS = {"holdshort", "holdshort_v2"}
SHARED_ENV = Path.home() / "plane-management-2" / ".env"


def load_env() -> None:
    """Load the team .env (and a local one) without overriding what is already set."""
    if SHARED_ENV.exists():
        load_dotenv(SHARED_ENV, override=False)
    load_dotenv(Path(__file__).with_name(".env"), override=False)


def mongo_uri() -> str:
    load_env()
    uri = os.environ.get("VIZ_MONGODB_URI") or os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("Set VIZ_MONGODB_URI or MONGODB_URI")
    return uri


def db_name() -> str:
    load_env()
    return os.environ.get("MONGODB_DB") or "holdshort_v2"


def cors_origins() -> list[str]:
    load_env()
    return [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
