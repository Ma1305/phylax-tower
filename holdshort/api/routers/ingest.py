"""/ingest/*: how data enters the tower (docs/v2/SIMULATOR_CONTRACT.md).

Thin HTTP layer; the logic is in holdshort.ingest. ``X-Ingest-Key`` is checked only when
``INGEST_KEY`` is set. ``GET /ingest/outbound`` lives in the speaker task's router.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Annotated, Any

from bson import ObjectId
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pymongo.database import Database
from starlette.concurrency import run_in_threadpool

from holdshort import ingest as ing
from holdshort.api.deps import get_db


def require_key(x_ingest_key: Annotated[str | None, Header()] = None) -> None:
    key = os.environ.get("INGEST_KEY", "")
    if key and not hmac.compare_digest((x_ingest_key or "").encode(), key.encode()):
        raise HTTPException(status_code=401, detail="wrong or missing X-Ingest-Key")


Db = Annotated[Database, Depends(get_db)]

router = APIRouter(prefix="/ingest", tags=["ingest"], dependencies=[Depends(require_key)])


def _json(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(jsonable_encoder(content, custom_encoder={ObjectId: str}),
                        status_code=status)


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def _body(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError) as e:
        raise ing.IngestError(400, f"body is not valid JSON: {e}") from e


async def _run(fn, *args, status: int = 200) -> JSONResponse:
    try:
        return _json(await run_in_threadpool(fn, *args), status)
    except ing.IngestError as e:
        return _error(e.status, e.detail)


async def _handle(request: Request, db: Database, model, fn, *extra, status: int = 200):
    try:
        body = ing.parse(model, await _body(request))
    except ing.IngestError as e:
        return _error(e.status, e.detail)
    return await _run(fn, db, *extra, body, status=status)


@router.get("/world")
async def get_world(db: Db, tower_id: Annotated[str, Query()] = "LGA"):
    return await _run(ing.world_doc, db, tower_id)


@router.post("/positions")
async def positions(request: Request, db: Db):
    return await _handle(request, db, ing.PositionsIn, ing.ingest_positions)


@router.post("/objects/{object_id}/remove")
async def remove(object_id: str, request: Request, db: Db):
    try:
        raw = await _body(request)
        body = ing.parse(ing.RemoveIn, {} if raw is None else raw)
    except ing.IngestError as e:
        return _error(e.status, e.detail)
    return await _run(ing.remove_object, db, object_id, body)


@router.post("/audio")
async def audio(db: Db, meta: Annotated[str | None, Form()] = None,
                file: Annotated[UploadFile | None, File()] = None):
    if meta is None or file is None:
        return _error(400, "multipart parts 'meta' and 'file' are required")
    try:
        parsed = json.loads(meta)
    except ValueError:
        return _error(400, "meta: not a JSON string")
    try:
        m = ing.parse(ing.AudioMetaIn, parsed)
    except ing.IngestError as e:
        return _error(e.status, e.detail)
    data = await file.read(ing.MAX_AUDIO_BYTES + 1)
    return await _run(ing.ingest_audio, db, m, data, file.content_type, file.filename,
                      status=202)


@router.post("/transcripts")
async def transcripts(request: Request, db: Db):
    return await _handle(request, db, ing.TranscriptIn, ing.ingest_transcript, status=202)


@router.post("/weather")
async def weather(request: Request, db: Db):
    return await _handle(request, db, ing.WeatherIn, ing.ingest_weather)


@router.post("/streams")
async def streams(request: Request, db: Db):
    return await _handle(request, db, ing.StreamIn, ing.upsert_stream)


@router.post("/runs")
async def runs(request: Request, db: Db):
    return await _handle(request, db, ing.RunIn, ing.start_run)


@router.post("/runs/{run_id}/end")
async def run_end(run_id: str, db: Db):
    return await _run(ing.end_run, db, run_id)


@router.post("/reset")
async def reset(request: Request, db: Db):
    return await _handle(request, db, ing.ResetIn, ing.reset_live)
