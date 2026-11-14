"""The listener agent: audio or script line -> parsed, readback-checked Transmission -> Atlas.

The listener writes only to `transmissions`, and only with insert_one (the
collection is insert-only). It never issues intents or touches traffic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import DESCENDING
from pymongo.database import Database

from holdshort.contracts import Readback, Transmission, to_mongo
from holdshort.listener import voice
from holdshort.listener.parse import LlmFn, parse

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scenarios" / "laguardia" / "script.json"

TEXT_CONFIDENCE = 0.95
"""Simulated speech confidence for a clear script line in text mode."""
TEXT_CONFIDENCE_UNCLEAR = 0.55
"""Simulated speech confidence for a script line marked "unclear": true."""


def check_readback(tx: Transmission, last_clearance: Transmission | None) -> Readback:
    """Readback status for a new transmission.

    - clearance -> "missing" (nothing has been read back yet; the readback is a later,
      separate insert because transmissions are never edited).
    - readback -> "matched" if instruction, runway and taxiway equal the last clearance
      to that callsign, else "mismatch" (also when there was no clearance to read back).
    - anything else -> "not_applicable".
    """
    if tx.kind == "clearance":
        return "missing"
    if tx.kind != "readback":
        return "not_applicable"
    if last_clearance is None:
        return "mismatch"
    same = (
        tx.instruction == last_clearance.instruction
        and tx.runway == last_clearance.runway
        and tx.at == last_clearance.at
    )
    return "matched" if same else "mismatch"


def insert(tx: Transmission, db: Database) -> Any:
    """Insert one transmission (insert only; returns the new _id)."""
    return db.transmissions.insert_one(to_mongo(tx)).inserted_id


@dataclass
class Listener:
    """Stateful listener: keeps recent calls for context and readback checks."""

    db: Database | None = None
    llm: LlmFn | None = None
    history: list[Transmission] = field(default_factory=list)

    def last_clearance_to(self, callsign: str) -> Transmission | None:
        for tx in reversed(self.history):
            if tx.kind == "clearance" and tx.to == callsign:
                return tx
        if self.db is not None:
            doc = self.db.transmissions.find_one(
                {"kind": "clearance", "to": callsign}, sort=[("t", DESCENDING)]
            )
            if doc:
                return Transmission.model_validate(doc)
        return None

    def handle_text(
        self,
        text: str,
        *,
        t: datetime,
        confidence: float,
        speaker: str | None = None,
        audio_ref: str | None = None,
    ) -> Transmission:
        """Parse, readback-check and (if a db is set) insert one call."""
        context = {
            "t": t,
            "speaker": speaker,
            "audio_ref": audio_ref,
            "history": [to_mongo(h) for h in self.history],
        }
        tx = parse(text, context, confidence=confidence, llm=self.llm)
        tx = tx.model_copy(
            update={"readback": check_readback(tx, self.last_clearance_to(tx.from_))}
        )
        Transmission.model_validate(to_mongo(tx))  # re-run validators after the update
        if self.db is not None:
            insert(tx, self.db)
        self.history.append(tx)
        return tx

    def handle_audio(self, path: Path, *, t: datetime, speaker: str | None = None) -> Transmission:
        """clean -> transcribe -> parse -> insert for one clip."""
        cleaned = voice.clean(path)
        text, confidence = voice.transcribe(cleaned)
        return self.handle_text(
            text, t=t, confidence=confidence, speaker=speaker, audio_ref=_rel(path)
        )


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def load_script(path: Path = SCRIPT) -> tuple[datetime, list[dict[str, Any]]]:
    """(start_time, transmission events) from a scenario script."""
    data = json.loads(path.read_text())
    start = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
    lines = [e for e in data["events"] if e.get("type") == "transmission"]
    return start, lines


def script_time(start: datetime, event: dict[str, Any]) -> datetime:
    return start + timedelta(seconds=float(event.get("offset_s", 0)))


def text_confidence(event: dict[str, Any]) -> float:
    return TEXT_CONFIDENCE_UNCLEAR if event.get("unclear") else TEXT_CONFIDENCE
