"""Hold Short HTTP + WebSocket API for the web UI.

    uv run uvicorn holdshort.api.app:app --port 8000

- ``WS /ws``: first message ``{"type": "snapshot", "doc": {...}}`` (permissions, rules,
  recent transmissions/decisions, flight_state, runway_status), then one
  ``{"type", "doc", "collection", "op"}`` message per change (see holdshort/api/feed.py).
- ``POST /permissions/{agent}``, ``POST /approve``, ``POST /revoke``: the controller's
  actions, done through holdshort.gate (every change is logged / insert-only).
- ``POST /scenario/start``, ``POST /scenario/reset``, ``POST /rogue``, ``GET /audit``: call
  optional hooks provided by other tasks (see HOOKS); 501 until they exist.
- ``GET /incidents/similar?q=&k=3`` (holdshort.search.incidents.similar),
  ``GET /callsigns/confusable?callsign=AC8646&callsign=TRUCK1``
  (holdshort.search.callsigns.similar_on_frequency): tier-2 search, 503 if Atlas Search
  is not set up (scripts/setup_search.py).
- ``GET /voice/alerts/{decision_id}``: the mp3 holdshort.voice_alert spoke for an
  executed STOP (404 until it exists, e.g. VOICE_ENABLED false).

The action gate normally runs as its own process (``python -m holdshort.gate``). Set
``HOLDSHORT_API_RUN_GATE=1`` to run it inside the API process instead (one gate only).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import re
import threading
from collections.abc import Callable
from typing import Any, Literal

from bson import ObjectId
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from pymongo.database import Database
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from holdshort import gate
from holdshort.api.feed import ChangeFeed, jsonable, snapshot
from holdshort.listener.voice import voice_enabled

log = logging.getLogger("holdshort.api")

LOCALHOST_ORIGINS = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

HOOKS: dict[str, tuple[str, str, str]] = {
    # name: (module, function, called as)
    "scenario_start": ("holdshort.scenario", "start", "start(db, *, safety_enabled, mode)"),
    "scenario_reset": ("holdshort.scenario", "reset", "reset(db)"),
    "rogue": ("holdshort.agents", "rogue", "rogue(db)"),
    "audit": ("holdshort.audit", "answer", "answer(db, q)"),
}
"""Functions other tasks provide. Each returns a JSON-able dict (or None) quickly;
long-running work (the scenario runner) should start its own thread/process."""


# ----------------------------------------------------------------------- bodies


def _object_id(v: Any) -> str:
    if not isinstance(v, str) or not ObjectId.is_valid(v):
        raise ValueError("must be a 24-character hex ObjectId")
    return v


class PermissionBody(BaseModel):
    enabled: bool
    by: str = Field(min_length=1, description="Controller making the change.")


class ApproveBody(BaseModel):
    intent_id: str
    controller: str = Field(min_length=1)

    _check_id = field_validator("intent_id")(_object_id)


class RevokeBody(BaseModel):
    decision_id: str
    controller: str = Field(min_length=1)

    _check_id = field_validator("decision_id")(_object_id)


class ScenarioStartBody(BaseModel):
    safety_enabled: bool
    mode: Literal["text", "audio"] = "text"


# ------------------------------------------------------------------------ hooks


def load_hook(name: str) -> Callable[..., Any]:
    """Import an optional hook, or raise 501 saying which task must provide it."""
    module, attr, signature = HOOKS[name]
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        # only "the module isn't there yet"; a broken import inside it should surface
        if e.name is None or not module.startswith(e.name):
            raise
        mod = None
    fn = getattr(mod, attr, None) if mod is not None else None
    if not callable(fn):
        raise HTTPException(
            status_code=501,
            detail=f"not implemented yet: {module}.{signature} is not available "
            f"(provided by a later task)",
        )
    return fn


def _call_hook(db: Database, name: str, *args: Any, **kwargs: Any) -> dict:
    fn = load_hook(name)
    try:
        result = fn(db, *args, **kwargs)
    except (gate.GateError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "result": jsonable(result)}


# -------------------------------------------------------------------------- app


def create_app(db: Database | None = None, *, run_gate: bool | None = None) -> FastAPI:
    """Build the app. With ``db=None`` it connects to MONGODB_URI/MONGODB_DB on startup."""
    if run_gate is None:
        run_gate = os.environ.get("HOLDSHORT_API_RUN_GATE", "").lower() in ("1", "true", "yes")
    state: dict[str, Any] = {"db": db}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        client = None
        if state["db"] is None:
            from holdshort.db import get_client, get_db

            client = get_client()
            state["db"] = get_db(client=client)
        the_db: Database = state["db"]
        feed = ChangeFeed(the_db)
        feed.start()
        if not await run_in_threadpool(feed.ready.wait, 20):
            log.warning("change feed did not open within 20s; events may be delayed")
        state["feed"] = feed
        g = th = None
        if run_gate:
            g = gate.Gate(the_db)
            th = threading.Thread(target=g.run, name="gate", daemon=True)
            th.start()
            state["gate"] = g
        try:
            yield
        finally:
            if g is not None:
                g.stop()
                await run_in_threadpool(th.join, 10)
            await run_in_threadpool(feed.stop)
            if client is not None:
                client.close()

    app = FastAPI(title="Hold Short API", lifespan=lifespan)
    app.state.hs = state
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCALHOST_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _db() -> Database:
        return state["db"]

    def _run_marker() -> ObjectId | None:
        """Start of the current run when the scenario hooks are loaded (else None)."""
        try:
            from holdshort.scenario import run_marker
        except ImportError:
            return None
        return run_marker()

    # ------------------------------------------------------------------ routes

    @app.get("/health")
    def health() -> dict:
        db = _db()
        try:
            db.command("ping")
            mongo = True
        except Exception:  # noqa: BLE001 - health must answer, never raise
            mongo = False
        feed: ChangeFeed | None = state.get("feed")
        return {
            "status": "ok" if mongo else "degraded",
            "mongo": mongo,
            "db": db.name,
            "change_feed": bool(feed and feed.ready.is_set()),
            "gate_in_process": "gate" in state,
            "voice_enabled": voice_enabled(),  # voice alert mp3s exist only when true
        }

    @app.post("/permissions/{agent}")
    def set_permission(agent: str, body: PermissionBody) -> dict:
        try:
            doc = gate.set_permission(_db(), agent, body.enabled, body.by)
        except gate.GateError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return jsonable(doc)

    @app.post("/approve")
    def approve(body: ApproveBody) -> dict:
        try:
            doc = gate.approve(_db(), body.intent_id, body.controller)
        except gate.GateError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return jsonable(doc)

    @app.post("/revoke")
    def revoke(body: RevokeBody) -> dict:
        try:
            doc = gate.revoke(_db(), body.decision_id, body.controller)
        except gate.GateError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return jsonable(doc)

    @app.post("/scenario/start")
    def scenario_start(body: ScenarioStartBody) -> dict:
        return _call_hook(
            _db(), "scenario_start", safety_enabled=body.safety_enabled, mode=body.mode
        )

    @app.get("/scenario/status")
    def scenario_status() -> dict:
        try:
            from holdshort.scenario import status
        except ImportError as e:
            raise HTTPException(status_code=501, detail="scenario hooks not available") from e
        return jsonable(status())

    @app.post("/scenario/reset")
    def scenario_reset() -> dict:
        return _call_hook(_db(), "scenario_reset")

    @app.post("/rogue")
    def rogue() -> dict:
        return _call_hook(_db(), "rogue")

    @app.get("/audit")
    def audit(q: str = Query(min_length=1)) -> dict:
        return _call_hook(_db(), "audit", q)

    @app.get("/incidents/similar")
    def incidents_similar(q: str = Query(min_length=3), k: int = Query(3, ge=1, le=10)) -> dict:
        from holdshort.search import incidents

        try:
            found = incidents.similar(q, k=k, db=_db())
        except PyMongoError as e:
            raise HTTPException(status_code=503, detail=f"incident search unavailable: "
                                f"{type(e).__name__} (run scripts/setup_search.py)") from e
        return {"incidents": [
            {"id": i.id, "title": i.title, "airport": i.airport, "date": i.date,
             "summary": i.summary, "score": round(i.score, 4), "method": i.method}
            for i in found
        ]}

    @app.get("/callsigns/confusable")
    def callsigns_confusable(callsign: list[str] = Query(min_length=1, max_length=20)) -> dict:
        from holdshort.contracts import CALLSIGN_PATTERN
        from holdshort.search import callsigns

        out: dict[str, list[dict]] = {}
        for cs in {c.strip().upper() for c in callsign if c.strip()}:
            if not re.fullmatch(CALLSIGN_PATTERN, cs):
                continue  # UNKNOWN, ATIS, ... are skipped, not errors
            try:
                found = callsigns.similar_on_frequency(cs, db=_db(), limit=3)
            except PyMongoError as e:
                raise HTTPException(status_code=503, detail=f"callsign search unavailable: "
                                    f"{type(e).__name__} (run scripts/setup_search.py)") from e
            out[cs] = [{"other": c.other, "reason": c.reason, "score": round(c.score, 4)}
                       for c in found]
        return {"confusable": out}

    @app.get("/voice/alerts/{decision_id}")
    def voice_alert(decision_id: str) -> FileResponse:
        from holdshort.voice_alert import alert_path

        try:
            path = alert_path(decision_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no voice alert for this decision")
        return FileResponse(path, media_type="audio/mpeg")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        feed: ChangeFeed = state["feed"]
        queue = feed.subscribe()  # subscribe before the snapshot so nothing is missed
        try:
            snap = await run_in_threadpool(snapshot, _db(), _run_marker())
            await websocket.send_json({"type": "snapshot", "doc": snap})

            async def pump() -> None:
                while True:
                    await websocket.send_json(await queue.get())

            async def drain() -> None:  # notice disconnects; client messages are ignored
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    exc = t.exception()
                    if not isinstance(exc, WebSocketDisconnect | RuntimeError):
                        raise exc
        except WebSocketDisconnect:
            pass
        finally:
            feed.unsubscribe(queue)

    return app


app = create_app()
