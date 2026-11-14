"""Scenario runner: plays scenarios/laguardia/script.json against Atlas in (scaled) real time.

    uv run python -m holdshort.scenario.runner reset
    uv run python -m holdshort.scenario.runner start --safety on --speed 5 [--mode text|audio]

- ``reset()`` clears flight_state and runway_status only. Audit data (transmissions,
  intents, decisions, permissions, security_events) is never deleted.
- ``start(safety_enabled)`` flips the safety permission (by "controller_A", logged by the
  gate's set_permission) and then plays the script:

  * transmissions: mode "text" inserts them through the listener's text mode
    (``holdshort.listener``); if the listener package is not installed, or with
    ``text_source="script"``, it builds them from each line's ``expect`` block.
    Mode "audio" feeds the generated clips to the listener (needs the listener and
    scenarios/laguardia/audio/<level>/*.wav).
  * METAR events go to the metar collection (the weather agent watches it).
  * simple positions for AC8646 and TRUCK1 go to flight_state once per scenario
    second. TRUCK1 starts crossing after reading back its clearance, unless an
    executed stop notifying it has been decided this run: then it holds (hold_short).

All timestamps written are scenario times (start_time + offset); ``expires_at`` is
wall clock (TTL).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from bson import ObjectId
from pymongo.database import Database

from holdshort.contracts import (
    CONFIDENCE_VERIFIED_MIN,
    FlightState,
    Metar,
    Position,
    Transmission,
    to_mongo,
)
from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.gate import set_permission

log = logging.getLogger("holdshort.scenario")

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "scenarios" / "laguardia"
SCRIPT = SCENARIO_DIR / "script.json"
CONTROLLER = "controller_A"
FLIGHT_STATE_TTL = timedelta(hours=1)

TEXT_CONFIDENCE = 0.95
TEXT_CONFIDENCE_UNCLEAR = 0.55

Mode = Literal["text", "audio"]
TextSource = Literal["auto", "listener", "script"]

# ----------------------------------------------------------------- script lines

_ALIASES = {
    r"\bair canada eight six four six\b": "AC8646",
    r"\btruck one\b": "TRUCK1",
}


def load_script(path: Path = SCRIPT) -> dict[str, Any]:
    data = json.loads(path.read_text())
    data["start"] = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
    data["events"] = sorted(data["events"], key=lambda e: float(e["offset_s"]))
    return data


def addressee(speaker: str, text: str) -> str:
    """Who a call is for: tower calls name the station; everyone else calls TOWER."""
    if speaker != "TOWER":
        return "TOWER"
    low = text.lower()
    for pattern, callsign in _ALIASES.items():
        if re.search(pattern, low):
            return callsign
    return "UNKNOWN"  # e.g. "Stop, stop, stop!" with no callsign


def script_transmission(event: dict[str, Any], t: datetime) -> Transmission:
    """A Transmission straight from a script line's ``expect`` block (no listener)."""
    exp = event["expect"]
    unclear = bool(event.get("unclear"))
    confidence = TEXT_CONFIDENCE_UNCLEAR if unclear else TEXT_CONFIDENCE
    kind = exp["kind"]
    readback = {"clearance": "missing", "readback": "matched"}.get(kind, "not_applicable")
    return Transmission(
        t=t,
        **{"from": event["speaker"]},
        to=addressee(event["speaker"], event["text"]),
        kind=kind,
        instruction=exp["instruction"],
        runway=exp.get("runway"),
        at=exp.get("at"),
        transcript=event["text"],
        confidence=confidence,
        readback=readback,
        verified=not unclear and confidence >= CONFIDENCE_VERIFIED_MIN,
    )


def _listener_cls():
    try:
        from holdshort.listener.agent import Listener  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Listener


# -------------------------------------------------------------------- positions

# Approximate, for a simple map: runway 04 thresholds and the taxiway D crossing.
RWY04_THRESHOLD = (40.7692, -73.8846)
RWY22_THRESHOLD = (40.7851, -73.8689)
NM_ALONG_RWY04 = (0.01199, 0.01529)  # (dlat, dlon) per nautical mile on heading ~044
D_CROSSING = (40.7756, -73.8783)
D_HOLD_SOUTH = (40.7750, -73.8775)  # TRUCK1 holds short here
D_HOLD_NORTH = (40.7762, -73.8791)  # ... and ends up here after crossing
CROSSING_DURATION_S = 20.0
ROLLOUT_DURATION_S = 25.0


def _lerp(a: tuple[float, float], b: tuple[float, float], f: float) -> Position:
    f = max(0.0, min(1.0, f))
    return Position(lat=round(a[0] + (b[0] - a[0]) * f, 6), lon=round(a[1] + (b[1] - a[1]) * f, 6))


def jet_position(s: float, first_s: float, touchdown_s: float, final_nm: float = 10.0) -> Position:
    """AC8646: on final, linear from ``final_nm`` at ``first_s`` to the threshold, then rollout."""
    if s < touchdown_s:
        frac = (touchdown_s - max(s, first_s)) / max(touchdown_s - first_s, 1e-6)
        d = final_nm * frac
        lat0, lon0 = RWY04_THRESHOLD
        return Position(lat=round(lat0 - NM_ALONG_RWY04[0] * d, 6),
                        lon=round(lon0 - NM_ALONG_RWY04[1] * d, 6))
    # roll out over the first ~60 % of the runway
    end = (
        RWY04_THRESHOLD[0] + 0.6 * (RWY22_THRESHOLD[0] - RWY04_THRESHOLD[0]),
        RWY04_THRESHOLD[1] + 0.6 * (RWY22_THRESHOLD[1] - RWY04_THRESHOLD[1]),
    )
    return _lerp(RWY04_THRESHOLD, end, (s - touchdown_s) / ROLLOUT_DURATION_S)


@dataclass
class Truck:
    """TRUCK1's simple motion: waits at D, crosses after its readback unless held."""

    held_at_s: float | None = None
    moving_since_s: float | None = None
    progress: float = 0.0

    def position(self, s: float) -> Position:
        if self.moving_since_s is not None and self.held_at_s is None:
            self.progress = min(1.0, (s - self.moving_since_s) / CROSSING_DURATION_S)
        return _lerp(D_HOLD_SOUTH, D_HOLD_NORTH, self.progress)


# ----------------------------------------------------------------------- runner


def reset(db: Database) -> dict[str, int]:
    """Clear live state (flight_state, runway_status). Audit collections are untouched."""
    out = {
        "flight_state": db.flight_state.delete_many({}).deleted_count,
        "runway_status": db.runway_status.delete_many({}).deleted_count,
    }
    log.info("reset: %s", out)
    return out


@dataclass
class Run:
    db: Database
    speed: float = 1.0
    mode: Mode = "text"
    text_source: TextSource = "auto"
    audio_level: str = "medium"
    sleep: Callable[[float], None] = time.sleep
    marker: ObjectId = field(default_factory=lambda: ObjectId.from_datetime(datetime.now(UTC)))
    inserted: int = 0
    metars: int = 0
    truck: Truck = field(default_factory=Truck)
    _listener: Any = None

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError("speed must be > 0")
        listener_cls = _listener_cls()
        if self.mode == "audio" or self.text_source == "listener":
            if listener_cls is None:
                raise RuntimeError(
                    "holdshort.listener is not available (needed for mode audio / "
                    "text_source listener)"
                )
        if listener_cls is not None and (self.mode == "audio" or self.text_source != "script"):
            self._listener = listener_cls(db=self.db)
        self._clips = self._load_clips() if self.mode == "audio" else {}

    # -- inputs

    def _load_clips(self) -> dict[str, Path]:
        manifest = json.loads((SCENARIO_DIR / "audio" / "manifest.json").read_text())
        clips = {}
        for clip in manifest["clips"]:
            if clip.get("kind") != "transmission":
                continue
            f = clip["files"].get(self.audio_level)
            path = SCENARIO_DIR / "audio" / f["file"] if f else None
            if path is None or not path.is_file():
                raise RuntimeError(
                    f"missing audio clip {clip['id']} at level {self.audio_level!r}; run "
                    "scripts/audio/generate.py and scripts/audio/degrade.sh first"
                )
            clips[clip["id"]] = path
        return clips

    def transmission(self, event: dict[str, Any], t: datetime) -> None:
        if self.mode == "audio":
            self._listener.handle_audio(self._clips[event["id"]], t=t, speaker=event["speaker"])
        elif self._listener is not None:
            conf = TEXT_CONFIDENCE_UNCLEAR if event.get("unclear") else TEXT_CONFIDENCE
            self._listener.handle_text(event["text"], t=t, confidence=conf,
                                       speaker=event["speaker"])
        else:
            self.db.transmissions.insert_one(to_mongo(script_transmission(event, t)))
        self.inserted += 1
        log.info("%s %s: %s", t.strftime("%H:%M:%S"), event["speaker"], event["text"])

    def metar(self, event: dict[str, Any], t: datetime) -> None:
        m = Metar(t=t, visibility_sm=event["visibility_sm"], wind=event["wind"])
        self.db.metar.insert_one(to_mongo(m))
        self.metars += 1
        log.info("%s METAR vis %g sm wind %s", t.strftime("%H:%M:%S"), m.visibility_sm, m.wind)

    # -- positions

    def _stop_for(self, callsign: str) -> dict | None:
        return self.db.decisions.find_one({
            "_id": {"$gte": self.marker}, "intent_action": "stop", "outcome": "executed",
            "notified": callsign,
        })

    def _write_position(self, callsign: str, pos: Position, t: datetime,
                        hold_short: bool | None = None) -> None:
        fs = FlightState(callsign=callsign, position=pos, updated_at=t,
                         expires_at=datetime.now(UTC) + FLIGHT_STATE_TTL)
        doc = to_mongo(fs)
        fields = {k: doc[k] for k in ("position", "updated_at", "expires_at")}
        on_insert: dict[str, Any] = {"callsign": callsign}
        if hold_short is None:
            on_insert["hold_short"] = False
        else:
            fields["hold_short"] = hold_short
        self.db.flight_state.update_one(
            {"callsign": callsign}, {"$set": fields, "$setOnInsert": on_insert}, upsert=True
        )

    def tick(self, s: float, t: datetime, first_s: float, touchdown_s: float) -> None:
        self._write_position("AC8646", jet_position(s, first_s, touchdown_s), t)
        held_now = None
        if self.truck.held_at_s is None and self._stop_for("TRUCK1"):
            self.truck.held_at_s = s
            held_now = True
            log.info("TRUCK1 holding at D (executed stop)")
        self._write_position("TRUCK1", self.truck.position(s), t, hold_short=held_now)

    # -- playback

    def play(self, script: dict[str, Any]) -> dict[str, Any]:
        start: datetime = script["start"]
        events = script["events"]
        tx_events = [e for e in events if e["type"] == "transmission"]
        first_s = float(tx_events[0]["offset_s"]) if tx_events else 0.0
        touchdown_s = float(script.get("conflict_offset_s", events[-1]["offset_s"]))
        end_s = max(float(events[-1]["offset_s"]), touchdown_s) + 5
        # readback of the crossing clearance -> truck starts moving
        cross_rb = next(
            (float(e["offset_s"]) for e in tx_events
             if e["expect"]["kind"] == "readback" and e["expect"]["instruction"] == "cross"),
            None,
        )

        timeline: list[tuple[float, int, dict[str, Any] | None]] = [
            (float(e["offset_s"]), 0, e) for e in events
        ]
        timeline += [(float(s), 1, None) for s in range(int(end_s) + 1)]
        timeline.sort(key=lambda x: (x[0], x[1]))

        wall0 = time.monotonic()
        for s, _, event in timeline:
            delay = wall0 + s / self.speed - time.monotonic()
            if delay > 0:
                self.sleep(delay)
            t = start + timedelta(seconds=s)
            if event is None:
                if cross_rb is not None and s >= cross_rb and self.truck.moving_since_s is None:
                    self.truck.moving_since_s = s
                self.tick(s, t, first_s, touchdown_s)
            elif event["type"] == "transmission":
                self.transmission(event, t)
            elif event["type"] == "metar":
                self.metar(event, t)
        return {
            "marker": str(self.marker),
            "transmissions": self.inserted,
            "metars": self.metars,
            "truck_held": self.truck.held_at_s is not None,
        }


def start(
    db: Database,
    safety_enabled: bool,
    *,
    speed: float = 1.0,
    mode: Mode = "text",
    text_source: TextSource = "auto",
    audio_level: str = "medium",
    script_path: Path = SCRIPT,
) -> dict[str, Any]:
    """Set the safety switch (by controller_A), then play the scenario. Returns a summary."""
    run = Run(db, speed=speed, mode=mode, text_source=text_source, audio_level=audio_level)
    set_permission(db, "safety", safety_enabled, CONTROLLER)
    log.info("safety %s (by %s); playing at x%g, mode %s", "ON" if safety_enabled else "OFF",
             CONTROLLER, speed, mode)
    summary = run.play(load_script(script_path))
    summary["safety_enabled"] = safety_enabled
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m holdshort.scenario.runner")
    parser.add_argument("--db", default=None, help="database (default: MONGODB_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset", help="clear flight_state and runway_status")
    p = sub.add_parser("start", help="set the safety switch and play the scenario")
    p.add_argument("--safety", choices=["on", "off"], required=True)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--mode", choices=["text", "audio"], default="text")
    p.add_argument("--text-source", choices=["auto", "listener", "script"], default="auto")
    p.add_argument("--level", default="medium", help="audio level (clean|mild|medium|brutal)")
    p.add_argument("--reset", action="store_true", help="reset() before playing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    db = client[args.db or mongodb_db_name()]
    try:
        if args.cmd == "reset" or getattr(args, "reset", False):
            reset(db)
        if args.cmd == "start":
            summary = start(db, args.safety == "on", speed=args.speed, mode=args.mode,
                            text_source=args.text_source, audio_level=args.level)
            print(json.dumps(summary))
        return 0
    except Exception as e:  # never leak the connection string
        print(f"FAIL: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
