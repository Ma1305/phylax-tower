"""Smoke feed: a small stand-in for the simulator. Talks to the tower ONLY via /ingest/* (HTTP).

    uv run python tools/smoke_feed.py --api http://localhost:8000 --scenario normal
    uv run python tools/smoke_feed.py --scenario incident --speed 2 --no-audio
    options: --scenario normal|incident|intruder|both  --speed N  --loop  --no-audio  --no-reset

Scenario lines and tracks live in tools/fixtures/scenarios.json. Geometry and ids come from
GET /ingest/world. Every timestamp is real UTC "now" (a faster --speed shortens the gaps).
Audio: ElevenLabs TTS when VOICE_ENABLED is true and ELEVENLABS_API_KEY is set (cached in
tools/fixtures/audio/<hash>.mp3), otherwise an ffmpeg tone of the right length.
This file imports nothing from holdshort/.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
AUDIO_CACHE = FIXTURES / "audio"
SCENARIOS = FIXTURES / "scenarios.json"
REPO = HERE.parent
TOWER_ID = "LGA"
NM_M = 1852.0
M_PER_DEG_LAT = 111_320.0


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Clock:
    """Real wall clock. Tests pass a fake one with the same two methods."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


# ---------------------------------------------------------------- geometry

def _coords(geom) -> list:
    if geom is None:
        return []
    if isinstance(geom, dict):
        return geom.get("coordinates") or []
    return list(geom)


def _xy(lon: float, lat: float, ref_lat: float) -> tuple[float, float]:
    return lon * M_PER_DEG_LAT * math.cos(math.radians(ref_lat)), lat * M_PER_DEG_LAT


def dist_m(a: list[float], b: list[float]) -> float:
    ref = (a[1] + b[1]) / 2
    ax, ay = _xy(a[0], a[1], ref)
    bx, by = _xy(b[0], b[1], ref)
    return math.hypot(bx - ax, by - ay)


def bearing_deg(a: list[float], b: list[float]) -> float:
    ref = (a[1] + b[1]) / 2
    ax, ay = _xy(a[0], a[1], ref)
    bx, by = _xy(b[0], b[1], ref)
    return (math.degrees(math.atan2(bx - ax, by - ay)) + 360) % 360


def along(line: list, f: float) -> list[float]:
    """Point at fraction f (0..1) of a polyline's length."""
    segs = [dist_m(line[i], line[i + 1]) for i in range(len(line) - 1)]
    target = max(0.0, min(1.0, f)) * sum(segs)
    for i, s in enumerate(segs):
        if target <= s or i == len(segs) - 1:
            k = 0 if s == 0 else min(1.0, target / s)
            a, b = line[i], line[i + 1]
            return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k]
        target -= s
    return list(line[0])


class World:
    def __init__(self, doc: dict):
        self.doc = doc
        self.points = {k: list(v) for k, v in (doc.get("points") or {}).items()}
        self.lanes = {(ln.get("_id") or ln.get("id")): ln for ln in doc.get("lanes", [])}
        self.hold_points = {(h.get("_id") or h.get("id")): h for h in doc.get("hold_points", [])}
        self.streams = {(s.get("_id") or s.get("id")) for s in doc.get("streams", [])}

    def resolve(self, at) -> list[float]:
        if isinstance(at, list):
            return [float(at[0]), float(at[1])]
        if at in self.points:
            return self.points[at]
        if at in self.hold_points:
            return [float(c) for c in _coords(self.hold_points[at]["position"])]
        name, _, arg = at.partition("@")
        if name.startswith("FINAL_"):
            return self.final_point(name[len("FINAL_"):], float(arg))
        if name in self.lanes:
            return along(_coords(self.lanes[name]["centerline"]), float(arg or 0))
        raise KeyError(f"unknown location {at!r} (not in GET /ingest/world)")

    def final_point(self, designator: str, nm: float) -> list[float]:
        for ln in self.lanes.values():
            if ln.get("kind") == "runway" and designator in (ln.get("designators") or []):
                line = _coords(ln["centerline"])
                thr, far = (line[0], line[-1]) if ln["designators"][0] == designator \
                    else (line[-1], line[0])
                d = dist_m(thr, far)
                k = -nm * NM_M / d
                return [thr[0] + (far[0] - thr[0]) * k, thr[1] + (far[1] - thr[1]) * k]
        raise KeyError(f"no runway {designator} in world")


# ---------------------------------------------------------------- scenario

def load_scenario(name: str, path: Path = SCENARIOS) -> dict:
    data = json.loads(path.read_text())
    scs = data["scenarios"]
    sc = dict(scs[name])
    if "combine" in sc:
        objects: dict[str, dict] = {}
        lines: list[dict] = []
        duration = 0.0
        for part in sc["combine"]:
            p = scs[part]
            for o in p["objects"]:
                objects.setdefault(o["object_id"], o)
            lines += [ln for ln in p["lines"] if ln not in lines]
            duration = max(duration, p["duration_s"])
        sc.update(objects=list(objects.values()), lines=lines, duration_s=duration)
    sc["lines"] = sorted(sc["lines"], key=lambda ln: ln["t"])
    sc["voices"] = data.get("voices", {})
    sc["name"] = name
    return sc


class Mover:
    def __init__(self, spec: dict, world: World):
        self.spec = spec
        self.id = spec["object_id"]
        self.keys = [(k["t"], world.resolve(k["at"]), k.get("alt_ft")) for k in spec["track"]]
        self.frozen: tuple[list[float], float | None] | None = None
        self.heading = bearing_deg(self.keys[0][1], self.keys[-1][1]) if len(self.keys) > 1 else 0
        self.obeys_stop = bool(spec.get("obeys_stop", True))

    def _at(self, t: float) -> tuple[list[float], float | None, float]:
        keys = self.keys
        if t < keys[0][0]:
            return keys[0][1], keys[0][2], 0.0
        for (t0, p0, a0), (t1, p1, a1) in zip(keys, keys[1:], strict=False):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                pos = [p0[0] + (p1[0] - p0[0]) * k, p0[1] + (p1[1] - p0[1]) * k]
                alt = None if a0 is None else a0 + ((a1 if a1 is not None else a0) - a0) * k
                d = dist_m(p0, p1)
                if d > 0.5:
                    self.heading = bearing_deg(p0, p1)
                speed = (d / (t1 - t0)) * 3600 / NM_M if t1 > t0 else 0.0
                return pos, alt, speed
        return keys[-1][1], keys[-1][2], 0.0

    def active(self, t: float) -> bool:
        return t >= self.keys[0][0]

    def freeze(self, t: float) -> None:
        if self.frozen is None:
            pos, alt, _ = self._at(t)
            self.frozen = (pos, alt)

    def report(self, t: float) -> dict:
        if self.frozen is not None:
            pos, alt = self.frozen
            speed = 0.0
        else:
            pos, alt, speed = self._at(t)
        s = self.spec
        airborne = s["kind"] == "aircraft" and alt is not None and alt > 0
        obj = {"object_id": self.id, "kind": s["kind"], "callsign": s.get("callsign"),
               "label": s.get("label"), "status": "airborne" if airborne else "ground",
               "lat": round(pos[1], 7), "lon": round(pos[0], 7),
               "heading_deg": round(self.heading, 1), "speed_kt": round(speed, 1)}
        if s["kind"] == "aircraft":
            obj["alt_ft"] = round(alt or 0)
        return obj


# ---------------------------------------------------------------- audio

def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _load_env() -> None:
    """Load .env through the scripts/audio helper (process env wins)."""
    if not os.environ.get("HOLDSHORT_ENV"):
        main_env = Path.home() / "plane-management-2" / ".env"
        if main_env.is_file() and not (REPO / ".env").is_file():
            os.environ["HOLDSHORT_ENV"] = str(main_env)
    try:
        sys.path.insert(0, str(REPO / "scripts" / "audio"))
        from _common import load_env  # type: ignore

        load_env()
    except ImportError:
        pass


def tone_wav(duration_s: float, freq: float) -> bytes:
    """Placeholder tone. ffmpeg when available, else a pure-Python sine."""
    if shutil.which("ffmpeg"):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"sine=frequency={freq:.0f}:duration={duration_s:.2f}", "-ac", "1", "-ar", "16000",
             "-f", "wav", "-"], capture_output=True, check=False)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    rate = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(duration_s * rate)
        w.writeframes(b"".join(
            int(8000 * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
            for i in range(n)))
    return buf.getvalue()


def _probe_duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "csv=p=0", str(path)], capture_output=True, text=True, check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


class Voices:
    def __init__(self, voices: dict[str, str], no_audio: bool):
        self.voices = voices
        self.no_audio = no_audio
        self.key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.tts = (not no_audio) and _truthy(os.environ.get("VOICE_ENABLED")) and bool(self.key)

    def voice_for(self, speaker: str) -> str:
        return (os.environ.get(f"ELEVENLABS_VOICE_{speaker}", "").strip()
                or self.voices.get(speaker) or self.voices.get("UNKNOWN", ""))

    def clip(self, line: dict) -> tuple[bytes, str, float]:
        """Return (audio bytes, mime, duration_s) for one utterance."""
        spk = line.get("speaker") or "UNKNOWN"
        if self.tts:
            voice = self.voice_for(spk)
            h = hashlib.sha256(f"{voice}|{line['text']}".encode()).hexdigest()[:20]
            path = AUDIO_CACHE / f"{h}.mp3"
            if not path.is_file():
                try:
                    self._generate(line["text"], voice, path)
                except Exception as exc:  # degrade to a tone, never print the key
                    msg = str(exc).replace(self.key, "<key>") if self.key else str(exc)
                    print(f"smoke_feed: TTS failed ({msg[:120]}); using a tone", flush=True)
                    path = None
            if path is not None:
                return path.read_bytes(), "audio/mpeg", _probe_duration(path) or line["dur"]
        freq = 300 + (int(hashlib.md5(spk.encode()).hexdigest(), 16) % 9) * 60
        return tone_wav(line["dur"], freq), "audio/wav", float(line["dur"])

    def _generate(self, text: str, voice: str, dst: Path) -> None:
        sys.path.insert(0, str(REPO / "scripts" / "audio"))
        from generate import eleven  # type: ignore  (scripts/audio helper: TTS -> wav)

        AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            eleven(text, voice, self.key, wav)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                            "-codec:a", "libmp3lame", "-q:a", "4", str(dst)], check=True)


# ---------------------------------------------------------------- run

class Feed:
    def __init__(self, client: httpx.Client, scenario: str, speed: float = 1.0,
                 no_audio: bool = False, no_reset: bool = False, clock: Clock | None = None,
                 log=print):
        self.c = client
        self.speed = max(0.01, float(speed))
        self.no_audio = no_audio
        self.no_reset = no_reset
        self.clock = clock or Clock()
        self.log = log
        self.sc = load_scenario(scenario)
        self.stopped: list[str] = []

    def _post(self, path: str, **kw) -> httpx.Response:
        r = self.c.post(path, **kw)
        if r.status_code >= 400:
            self.log(f"smoke_feed: POST {path} -> {r.status_code} {r.text[:200]}")
        return r

    def run(self) -> dict:
        sc = self.sc
        world = World(self.c.get("/ingest/world", params={"tower_id": TOWER_ID})
                      .raise_for_status().json())
        movers = [Mover(o, world) for o in sc["objects"]]
        voices = Voices(sc.get("voices", {}), self.no_audio)
        for ln in sc["lines"]:
            if world.streams and ln["stream"] not in world.streams:
                raise KeyError(f"stream {ln['stream']} not in world")
        if not self.no_reset:
            self._post("/ingest/reset", json={"tower_id": TOWER_ID, "scope": "live"})
        r = self._post("/ingest/runs", json={"tower_id": TOWER_ID, "scenario": sc["run_name"],
                                             "kind": sc["kind"], "note": sc.get("note", "")})
        run_id = (r.json() or {}).get("run_id") if r.status_code < 400 else None
        self.log(f"smoke_feed: {sc['name']} run {run_id} speed x{self.speed}")

        start = self.clock.now()
        since = iso(start)
        duration = float(sc["duration_s"])
        pending = list(sc["lines"])  # posted when each utterance ends
        tick = 0
        counts = {"positions": 0, "clips": 0, "stops": 0}
        while True:
            t = tick  # scenario second of this position tick
            # utterances that ended by now
            while pending and pending[0]["t"] + pending[0]["dur"] <= t:
                self._send_line(pending.pop(0), start, voices)
                counts["clips"] += 1
            # outbound: obey stops
            try:
                ob = self.c.get("/ingest/outbound", params={"tower_id": TOWER_ID, "since": since})
                if ob.status_code < 400:
                    body = ob.json()
                    # keep polling from the run start: a call's t (when it was decided) can be
                    # earlier than the "now" of the poll that ran while it was being spoken
                    for call in body.get("calls", []):
                        if call.get("action") != "stop":
                            continue
                        for m in movers:
                            if m.id in (call.get("target") or []) and m.obeys_stop \
                                    and m.frozen is None:
                                m.freeze(t)
                                self.stopped.append(m.id)
                                counts["stops"] += 1
                                self.log(f"smoke_feed: t+{t}s STOP for {m.id}: "
                                         f"{call.get('text', '')}")
            except httpx.HTTPError as exc:
                self.log(f"smoke_feed: outbound poll failed: {exc}")
            objs = [m.report(t) for m in movers if m.active(t)]
            if objs:
                self._post("/ingest/positions", json={
                    "tower_id": TOWER_ID, "t": iso(self.clock.now()), "objects": objs})
                counts["positions"] += 1
            if t >= duration and not pending:
                break
            tick += 1
            wake = start + timedelta(seconds=tick / self.speed)
            self.clock.sleep((wake - self.clock.now()).total_seconds())
        if run_id:
            self._post(f"/ingest/runs/{run_id}/end", json={"tower_id": TOWER_ID})
        self.log(f"smoke_feed: done {counts} stopped={self.stopped}")
        return {"run_id": run_id, **counts, "stopped": list(self.stopped)}

    def _send_line(self, ln: dict, start: datetime, voices: Voices) -> None:
        t_start = iso(start + timedelta(seconds=ln["t"] / self.speed))
        if self.no_audio:
            self._post("/ingest/transcripts", json={
                "tower_id": TOWER_ID, "stream_id": ln["stream"], "t_start": t_start,
                "duration_s": ln["dur"], "speaker": ln.get("speaker"), "text": ln["text"]})
            return
        audio, mime, dur = voices.clip(ln)
        meta = {"tower_id": TOWER_ID, "stream_id": ln["stream"], "t_start": t_start,
                "duration_s": round(dur, 2), "speaker": ln.get("speaker"),
                "truth_text": ln["text"]}
        ext = "mp3" if mime == "audio/mpeg" else "wav"
        self._post("/ingest/audio", data={"meta": json.dumps(meta)},
                   files={"file": (f"clip.{ext}", audio, mime)})


def make_client(api: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {}
    if os.environ.get("INGEST_KEY", "").strip():
        headers["X-Ingest-Key"] = os.environ["INGEST_KEY"].strip()
    return httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=30.0,
                        transport=transport)


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None,
         clock: Clock | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("TOWER_API", "http://localhost:8000"))
    ap.add_argument("--scenario", choices=["normal", "incident", "intruder", "both"],
                    default="normal")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--loop", action="store_true", help="repeat until interrupted")
    ap.add_argument("--no-audio", action="store_true", help="send /ingest/transcripts instead")
    ap.add_argument("--no-reset", action="store_true", help="do not call /ingest/reset first")
    args = ap.parse_args(argv)
    _load_env()
    with make_client(args.api, transport) as client:
        try:
            while True:
                Feed(client, args.scenario, args.speed, args.no_audio, args.no_reset,
                     clock).run()
                if not args.loop:
                    break
        except KeyboardInterrupt:
            print("smoke_feed: interrupted")
        except httpx.HTTPError as exc:
            print(f"smoke_feed: tower unreachable or error: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
