"""Voice alert service: phrase building, TTS call (mocked), change-stream watcher on Atlas."""

import threading
import time
from datetime import UTC, datetime

import pytest
from bson import ObjectId

from holdshort.voice_alert import core
from holdshort.voice_alert.core import VoiceAlert, alert_path, phrase, spoken_callsign

T = datetime(2026, 3, 22, 23, 37, 18, tzinfo=UTC)


def test_spoken_callsign():
    assert spoken_callsign("TRUCK1") == "Truck one"
    assert spoken_callsign("AC8646") == "Air Canada eight six four six"
    assert spoken_callsign("TOWER") == "Tower"


def test_phrase():
    text = phrase(["TRUCK1", "AC8646"], "04", "AC8646 cleared to land")
    assert text == ("Truck one, STOP. Hold short runway zero four. "
                    "Air Canada eight six four six, traffic Truck one holding short runway zero four.")
    assert phrase(["TRUCK1"], "04", "AC8646 cleared to land") == (
        "Truck one, STOP. Hold short runway zero four.")
    assert phrase([], None) == "All vehicles, STOP. Hold position."


def test_alert_path_rejects_non_objectid():
    with pytest.raises(ValueError):
        alert_path("../../etc/passwd")


class _Resp:
    status_code = 200
    content = b"ID3fake-mp3"
    text = ""


def test_speak_writes_mp3(tmp_path, monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return _Resp()

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    monkeypatch.setattr(core.requests, "post", fake_post)
    out = core.speak("Truck one, STOP.", tmp_path / "x.mp3")
    assert out.read_bytes() == b"ID3fake-mp3"
    assert "/text-to-speech/" in seen["url"] and seen["json"]["text"] == "Truck one, STOP."


def test_speak_http_error_is_runtime_error(tmp_path, monkeypatch):
    class Bad(_Resp):
        status_code = 401
        text = "unauthorized"

    monkeypatch.setattr(core.requests, "post", lambda *a, **k: Bad())
    with pytest.raises(RuntimeError, match="401"):
        core.speak("x", tmp_path / "x.mp3")
    assert not (tmp_path / "x.mp3").exists()


def _wait(pred, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_service_speaks_only_executed_stops(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ALERT_DIR", tmp_path)
    monkeypatch.setattr(core, "speak", lambda text, dst: (dst.write_bytes(b"mp3"), dst)[1])
    perm = test_db.permissions.find_one({"agent": "safety"})["_id"]
    intent = test_db.intents_safety.insert_one({
        "t": T, "agent": "safety", "action": "stop", "target": ["TRUCK1"], "runway": "04",
        "reason": "no_crossing_during_active_landing", "conflicts_with": "AC8646 cleared to land",
    }).inserted_id
    svc = VoiceAlert(test_db, enabled=True)
    th = threading.Thread(target=svc.run, daemon=True)
    th.start()
    assert svc.ready.wait(20)
    base = {"t": T, "intent_id": intent, "agent": "safety", "intent_action": "stop",
            "authorized_by": {"permission_id": perm, "rule_set_version": "normal_v1"},
            "approved_by": None, "revokes": None, "notified": ["TRUCK1", "AC8646"]}
    test_db.decisions.insert_one({**base, "outcome": "rejected", "rule": "agent_disabled"})
    did = test_db.decisions.insert_one(
        {**base, "outcome": "executed", "rule": "tier_autonomous"}).inserted_id
    try:
        assert _wait(lambda: svc.spoken)
        time.sleep(0.5)  # nothing else should arrive (the rejected one is filtered)
    finally:
        svc.stop()
        th.join(10)
    assert len(svc.spoken) == 1
    sid, text, path = svc.spoken[0]
    assert sid == str(did) and text.startswith("Truck one, STOP. Hold short runway zero four.")
    assert path == tmp_path / f"{did}.mp3" and path.is_file()


def test_service_voice_off_logs_only(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ALERT_DIR", tmp_path)
    svc = VoiceAlert(test_db, enabled=False)
    svc.handle({"_id": ObjectId(), "agent": "safety", "intent_id": ObjectId(),
                "notified": ["TRUCK1"]})
    assert svc.spoken[0][2] is None and list(tmp_path.iterdir()) == []
