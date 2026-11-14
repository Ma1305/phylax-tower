"""Scenario playback (see holdshort/scenario/runner.py).

``start(db, *, safety_enabled, mode)`` and ``reset(db)`` are the API hooks
(``POST /scenario/start``, ``POST /scenario/reset``). ``start`` returns at once: the
run plays in a background thread of the calling process (the API). Only one run at
a time; ``reset`` cancels a run in progress, then clears the live state.

``run_marker()`` is the ObjectId of the current run's start (or of the last reset):
everything inserted for the current run has ``_id >= run_marker()``. The API uses it
so a freshly loaded page only shows the current run.

Playback speed: ``HOLDSHORT_SCENARIO_SPEED`` (default 1, real time).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.database import Database

log = logging.getLogger("holdshort.scenario")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_cancel = threading.Event()
_marker: ObjectId | None = None
_last: dict[str, Any] = {}


class Cancelled(Exception):
    """Raised inside the run thread when reset() cancels it."""


def run_marker() -> ObjectId | None:
    return _marker


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def status() -> dict[str, Any]:
    return {"running": running(), "marker": str(_marker) if _marker else None, "last": _last}


def _speed() -> float:
    try:
        return float(os.environ.get("HOLDSHORT_SCENARIO_SPEED", "1"))
    except ValueError:
        return 1.0


def _cancellable_sleep(seconds: float) -> None:
    if _cancel.wait(seconds):
        raise Cancelled


def start(db: Database, *, safety_enabled: bool, mode: str = "text",
          speed: float | None = None) -> dict[str, Any]:
    """Reset live state, set the safety switch (controller_A) and play in a thread."""
    global _thread, _marker, _last
    from holdshort.scenario import runner

    with _lock:
        if running():
            raise ValueError("a scenario run is already playing; reset first")
        runner.reset(db)
        run = runner.Run(db, speed=speed or _speed(), mode=mode,  # type: ignore[arg-type]
                         sleep=_cancellable_sleep)
        runner.set_permission(db, "safety", safety_enabled, runner.CONTROLLER)
        _cancel.clear()
        _marker = run.marker
        _last = {"marker": str(run.marker), "safety_enabled": safety_enabled, "mode": mode,
                 "state": "playing"}
        script = runner.load_script()

        def play() -> None:
            global _last
            try:
                summary = run.play(script)
                _last = {**summary, "safety_enabled": safety_enabled, "mode": mode,
                         "state": "finished"}
                log.info("scenario finished: %s", _last)
            except Cancelled:
                _last = {**_last, "state": "cancelled"}
                log.info("scenario cancelled")
            except Exception as e:  # noqa: BLE001 - log, never kill the API
                from holdshort.env import redact

                _last = {**_last, "state": "failed", "error": redact(e)}
                log.exception("scenario failed")

        _thread = threading.Thread(target=play, name="scenario", daemon=True)
        _thread.start()
        log.info("scenario started: safety=%s mode=%s speed=%g", safety_enabled, mode, run.speed)
        return dict(_last, speed=run.speed)


def reset(db: Database) -> dict[str, Any]:
    """Cancel a run in progress, then clear flight_state and runway_status (audit kept)."""
    global _marker, _last
    from holdshort.scenario import runner

    with _lock:
        if running():
            _cancel.set()
            assert _thread is not None
            _thread.join(10)
        out = runner.reset(db)
        _marker = ObjectId.from_datetime(datetime.now(UTC))
        _last = {"state": "reset", "marker": str(_marker)}
        return {**out, "marker": str(_marker)}
