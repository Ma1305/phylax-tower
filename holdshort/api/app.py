"""Phylax Tower tower API (v2): a slim FastAPI factory that includes every router module.

    uv run uvicorn holdshort.api.app:app --port 8000

- ``GET /health`` -> ``{status, mongo, db, routers, routers_failed, voice_enabled}``.
- Every module in ``holdshort/api/routers/*.py`` with a module-level ``router`` is included.
  A module that fails to import is logged and skipped (never fatal).
- Routers reach the database through ``holdshort.api.deps.get_db`` (reads
  ``request.app.state.db``, set by the lifespan here).
- CORS: http(s)://localhost / 127.0.0.1 on any port, plus comma-separated ``CORS_ORIGINS``.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import pkgutil
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("holdshort.api")

LOCALHOST_ORIGINS = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
ROUTERS_PACKAGE = "holdshort.api.routers"


def _voice_enabled() -> bool:
    try:
        from holdshort.listener.voice import voice_enabled
    except Exception:  # noqa: BLE001 - fall back to the plain flag
        flag = os.environ.get("VOICE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        return flag and bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
    return voice_enabled()


def discover_routers(package: str = ROUTERS_PACKAGE) -> tuple[list[tuple[str, APIRouter]],
                                                           dict[str, str]]:
    """Import every module of `package`; return ([(name, router)], {name: error})."""
    found: list[tuple[str, APIRouter]] = []
    failed: dict[str, str] = {}
    try:
        pkg = importlib.import_module(package)
    except Exception as e:  # noqa: BLE001
        log.error("router package %s failed to import: %r", package, e)
        return found, {package: repr(e)}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{package}.{info.name}")
        except Exception as e:  # noqa: BLE001 - a broken router must not take the API down
            log.error("router %s skipped: import failed: %r", info.name, e)
            failed[info.name] = f"{type(e).__name__}: {e}"
            continue
        router = getattr(mod, "router", None)
        if isinstance(router, APIRouter):
            found.append((info.name, router))
        else:
            log.warning("router module %s has no `router` APIRouter; skipped", info.name)
    return found, failed


def _cors_origins() -> list[str]:
    return [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]


def create_app(db: Any = None, *, routers_package: str = ROUTERS_PACKAGE) -> FastAPI:
    """Build the app. With ``db=None`` the lifespan opens MONGODB_URI / MONGODB_DB."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = None
        if db is None:
            from holdshort.db import get_client, get_db

            client = get_client(serverSelectionTimeoutMS=5000)
            app.state.db = get_db(client=client)
        else:
            app.state.db = db
        log.info("api using database %s; routers: %s", app.state.db.name,
                 ", ".join(app.state.routers) or "(none)")
        try:
            yield
        finally:
            if client is not None:
                client.close()

    app = FastAPI(title="Phylax Tower API", lifespan=lifespan)
    app.state.db = db
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_origin_regex=LOCALHOST_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    routers, failed = discover_routers(routers_package)
    app.state.routers = [name for name, _ in routers]
    app.state.routers_failed = failed
    for _name, router in routers:
        app.include_router(router)

    @app.get("/health")
    async def health() -> dict:
        the_db = app.state.db

        def ping() -> bool:
            try:
                the_db.command("ping")
                return True
            except Exception:  # noqa: BLE001 - health must answer, never raise
                return False

        mongo = the_db is not None and await run_in_threadpool(ping)
        try:
            from holdshort import llm as llm_layer

            llm_status = await run_in_threadpool(llm_layer.status)
        except Exception:  # noqa: BLE001 - health must answer, never raise
            llm_status = {"provider": "unknown"}
        return {
            "status": "ok" if mongo and not failed else "degraded",
            "mongo": mongo,
            "db": getattr(the_db, "name", None),
            "routers": app.state.routers,
            "routers_failed": failed,
            "voice_enabled": _voice_enabled(),
            "llm": llm_status,
        }

    return app


app = create_app()
