"""Phylax Tower visualizer API: read-only /viz/* routes over the tower's MongoDB records.

Run from this directory:  uvicorn app:app --port 8100
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from gridfs import AsyncGridFSBucket
from gridfs.errors import NoFile
from pymongo import AsyncMongoClient

import config
import store
from live import LiveHub

LOCAL_ORIGIN = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
DEFAULT_WINDOW = timedelta(minutes=15)
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncMongoClient(
        config.mongo_uri(), appname="holdshort-viz-api", tz_aware=True, maxPoolSize=20
    )
    app.state.client = client
    app.state.db = client[config.db_name()]
    app.state.hub = LiveHub(app.state.db)
    app.state.hub.start()
    try:
        yield
    finally:
        await app.state.hub.stop()
        await client.close()


app = FastAPI(title="Phylax Tower visualizer API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_origin_regex=LOCAL_ORIGIN,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)


def ok(payload) -> JSONResponse:
    return JSONResponse(store.jsonable(payload))


def when(value: str | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return store.parse_t(value)
    except ValueError:
        raise HTTPException(400, f"{name} must be an ISO 8601 time") from None


def window(t_from: str | None, t_to: str | None) -> tuple[datetime, datetime]:
    end = when(t_to, "to") or store.now_utc()
    start = when(t_from, "from") or end - DEFAULT_WINDOW
    if start > end:
        raise HTTPException(400, "from must not be after to")
    return start, end


@app.get("/viz/health")
async def health(request: Request):
    hub: LiveHub = request.app.state.hub
    try:
        await request.app.state.db.command("ping")
        mongo = True
    except Exception:
        mongo = False
    body = {
        "ok": mongo,
        "db": request.app.state.db.name,
        "live": hub.ready,
        "clients": len(hub.clients),
    }
    return JSONResponse(body, status_code=200 if mongo else 503)


@app.get("/viz/world")
async def get_world(request: Request, tower_id: str = "LGA", controller_id: str | None = None):
    doc = await store.world(request.app.state.db, tower_id, controller_id)
    if doc is None:
        raise HTTPException(404, f"unknown tower {tower_id}")
    return ok(doc)


@app.get("/viz/bounds")
async def get_bounds(request: Request, tower_id: str = "LGA"):
    return ok(await store.bounds(request.app.state.db, tower_id))


@app.get("/viz/snapshot")
async def get_snapshot(
    request: Request, tower_id: str = "LGA", t: str | None = None, controller_id: str | None = None
):
    return ok(await store.snapshot(request.app.state.db, tower_id, when(t, "t"), controller_id))


@app.get("/viz/tracks")
async def get_tracks(
    request: Request,
    tower_id: str = "LGA",
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
    step_s: float = Query(1, ge=1, le=3600),
):
    start, end = window(t_from, t_to)
    return ok(await store.tracks(request.app.state.db, tower_id, start, end, step_s))


@app.get("/viz/timeline")
async def get_timeline(
    request: Request,
    tower_id: str = "LGA",
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
):
    start, end = window(t_from, t_to)
    return ok(await store.timeline(request.app.state.db, tower_id, start, end))


@app.get("/viz/streams/{stream_id}/clips")
async def get_clips(
    request: Request,
    stream_id: str,
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
):
    start, end = window(t_from, t_to)
    return ok(await store.stream_clips(request.app.state.db, stream_id, start, end))


@app.api_route("/viz/audio/{clip_id}", methods=["GET", "HEAD"])
async def get_audio(request: Request, clip_id: str):
    db = request.app.state.db
    clip_oid = store.oid(clip_id)
    if clip_oid is None:
        raise HTTPException(404, "unknown clip")
    clip = await db.audio_clips.find_one({"_id": clip_oid}, {"audio_id": 1, "mime": 1})
    if clip is None or clip.get("audio_id") is None:
        raise HTTPException(404, "no audio for this clip")
    try:
        grid_out = await AsyncGridFSBucket(db, bucket_name="audio").open_download_stream(
            clip["audio_id"]
        )
    except NoFile:
        raise HTTPException(404, "audio file is missing") from None
    total = grid_out.length
    mime = clip.get("mime") or (grid_out.metadata or {}).get("mime") or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    start, end, status = 0, total - 1, 200
    header = request.headers.get("range")
    if header:
        m = RANGE_RE.match(header.strip())
        if not m or (m.group(1) == "" and m.group(2) == ""):
            m = None
        elif m.group(1) == "":  # suffix: the last N bytes
            start = max(total - int(m.group(2)), 0)
        else:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), total - 1)
        if m is None or start > end or start >= total:
            await grid_out.close()
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"}
            )
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    length = max(end - start + 1, 0)
    body = b""
    if request.method == "GET" and length:
        await grid_out.seek(start)
        body = await grid_out.read(length)
    await grid_out.close()
    headers["Content-Length"] = str(length)
    return Response(content=body, status_code=status, media_type=mime, headers=headers)


@app.websocket("/viz/live")
async def live(ws: WebSocket, tower_id: str = "LGA"):
    hub: LiveHub = ws.app.state.hub
    await ws.accept()
    client = hub.register(tower_id)  # before the snapshot, so no change in between is lost
    reader = asyncio.create_task(_drain(ws))
    try:
        snap = await store.snapshot(ws.app.state.db, tower_id)
        await ws.send_json({"type": "snapshot", "doc": store.jsonable(snap)})
        while not client.overflowed:
            getter = asyncio.create_task(client.queue.get())
            done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                break
            await ws.send_json(getter.result())
        if client.overflowed:
            await ws.close(code=1013)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unregister(client)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader


async def _drain(ws: WebSocket) -> None:
    """Read (and ignore) client messages so a disconnect is noticed."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await ws.receive_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")))
