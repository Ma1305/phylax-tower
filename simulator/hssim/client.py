"""HTTP client for the tower's /ingest API (docs/v2/SIMULATOR_CONTRACT.md). Nothing else
in the simulator talks to the tower."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def iso(t: datetime) -> str:
    """ISO 8601 UTC with a Z and milliseconds."""
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TowerError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status


class TowerClient:
    def __init__(self, base_url: str, *, tower_id: str = "LGA", ingest_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        headers = {"X-Ingest-Key": ingest_key} if ingest_key else {}
        self.tower_id = tower_id
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                      transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise TowerError(method, path, r.status_code, r.text)
        return r.json() if r.content else None

    # --------------------------------------------------------------- endpoints
    async def world(self) -> dict:
        return await self._send("GET", "/ingest/world", params={"tower_id": self.tower_id})

    async def positions(self, objects: list[dict], t: datetime | None = None) -> dict:
        body: dict[str, Any] = {"tower_id": self.tower_id, "objects": objects}
        if t is not None:
            body["t"] = iso(t)
        return await self._send("POST", "/ingest/positions", json=body)

    async def remove_object(self, object_id: str, reason: str = "") -> Any:
        return await self._send("POST", f"/ingest/objects/{object_id}/remove",
                                json={"tower_id": self.tower_id, "reason": reason})

    async def audio(self, *, stream_id: str, t_start: datetime, duration_s: float, path: Path,
                    mime: str, speaker: str | None = None,
                    truth_text: str | None = None) -> str | None:
        meta = {"tower_id": self.tower_id, "stream_id": stream_id, "t_start": iso(t_start),
                "duration_s": round(duration_s, 2)}
        if speaker:
            meta["speaker"] = speaker
        if truth_text:
            meta["truth_text"] = truth_text
        files = {"file": (path.name, path.read_bytes(), mime)}
        out = await self._send("POST", "/ingest/audio", data={"meta": json.dumps(meta)},
                               files=files)
        return (out or {}).get("clip_id")

    async def transcript(self, *, stream_id: str, t_start: datetime, text: str,
                         duration_s: float | None = None, speaker: str | None = None,
                         confidence: float | None = None) -> str | None:
        body: dict[str, Any] = {"tower_id": self.tower_id, "stream_id": stream_id,
                                "t_start": iso(t_start), "text": text}
        if duration_s is not None:
            body["duration_s"] = round(duration_s, 2)
        if speaker:
            body["speaker"] = speaker
        if confidence is not None:
            body["confidence"] = confidence
        out = await self._send("POST", "/ingest/transcripts", json=body)
        return (out or {}).get("clip_id")

    async def weather(self, visibility_sm: float, wind: str, raw: str | None = None,
                      t: datetime | None = None) -> Any:
        body: dict[str, Any] = {"tower_id": self.tower_id, "visibility_sm": visibility_sm,
                                "wind": wind}
        if raw:
            body["raw"] = raw
        if t is not None:
            body["t"] = iso(t)
        return await self._send("POST", "/ingest/weather", json=body)

    async def stream_upsert(self, stream: dict) -> Any:
        return await self._send("POST", "/ingest/streams",
                                json={"tower_id": self.tower_id, **stream})

    async def run_start(self, scenario: str, kind: str, note: str = "") -> str | None:
        out = await self._send("POST", "/ingest/runs", json={
            "tower_id": self.tower_id, "scenario": scenario, "kind": kind, "note": note})
        return (out or {}).get("run_id")

    async def run_end(self, run_id: str) -> Any:
        return await self._send("POST", f"/ingest/runs/{run_id}/end")

    async def reset(self) -> Any:
        return await self._send("POST", "/ingest/reset",
                                json={"tower_id": self.tower_id, "scope": "live"})

    async def outbound(self, since: str | None) -> dict:
        params = {"tower_id": self.tower_id}
        if since:
            params["since"] = since
        return await self._send("GET", "/ingest/outbound", params=params)
