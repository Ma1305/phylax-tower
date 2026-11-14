"""Readback check and the insert-only write of one Transmission."""

from __future__ import annotations

from typing import Any

from holdshort.contracts import Readback, Transmission, to_mongo


def check_readback(tx: Transmission, last_clearance: Transmission | dict | None) -> Readback:
    """Readback status for a new transmission.

    - clearance -> "missing" (the readback is a later, separate transmission).
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
    if isinstance(last_clearance, dict):
        last_clearance = Transmission.model_validate(last_clearance)
    same = (
        tx.instruction == last_clearance.instruction
        and tx.runway == last_clearance.runway
        and tx.at == last_clearance.at
    )
    return "matched" if same else "mismatch"


def insert(tx: Transmission, db: Any) -> Any:
    """Insert one transmission (insert only; returns the new _id)."""
    return db.transmissions.insert_one(to_mongo(tx)).inserted_id
