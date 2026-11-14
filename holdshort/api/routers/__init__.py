"""API routers. Every module here that defines a module-level ``router`` (a FastAPI
``APIRouter``) is included by holdshort/api/app.py. Get the database with
``Depends(holdshort.api.deps.get_db)``. A module that fails to import is logged and skipped,
and listed under ``routers_failed`` in ``GET /health``."""
