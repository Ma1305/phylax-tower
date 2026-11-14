"""Tier-2 API endpoints: similar incidents, confusable callsigns, voice alert audio."""

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure

from holdshort.api import app as api
from holdshort.search import callsigns, incidents
from holdshort.search.callsigns import Confusable
from holdshort.search.incidents import Incident
from holdshort.voice_alert import core


@pytest.fixture
def api_client(test_db):
    with TestClient(api.create_app(test_db, run_gate=False)) as c:
        yield c


def test_incidents_similar(api_client, monkeypatch):
    seen = {}

    def fake(text, k=3, db=None, method=None):
        seen.update(text=text, k=k)
        return [Incident("x1", "Truck on runway", "XXX", "2020-01-01", "...", "", 0.81234, "vector")]

    monkeypatch.setattr(incidents, "similar", fake)
    r = api_client.get("/incidents/similar", params={"q": "truck crossing landing", "k": 3})
    assert r.status_code == 200
    assert r.json() == {"incidents": [{"id": "x1", "title": "Truck on runway", "airport": "XXX",
                                       "date": "2020-01-01", "summary": "...", "score": 0.8123,
                                       "method": "vector"}]}
    assert seen == {"text": "truck crossing landing", "k": 3}
    assert api_client.get("/incidents/similar", params={"q": "x"}).status_code == 422


def test_search_unavailable_is_503(api_client, monkeypatch):
    def boom(*a, **k):
        raise OperationFailure("no such index")

    monkeypatch.setattr(incidents, "similar", boom)
    monkeypatch.setattr(callsigns, "similar_on_frequency", boom)
    assert api_client.get("/incidents/similar", params={"q": "abc"}).status_code == 503
    assert api_client.get("/callsigns/confusable", params={"callsign": "AC8646"}).status_code == 503


def test_callsigns_confusable(api_client, monkeypatch):
    def fake(cs, frequency=None, db=None, limit=5):
        return [Confusable(cs, "AC6846", 0.7, "same prefix, same digits in a different order")]

    monkeypatch.setattr(callsigns, "similar_on_frequency", fake)
    r = api_client.get("/callsigns/confusable", params=[("callsign", "ac8646"), ("callsign", "x y")])
    assert r.status_code == 200
    assert r.json() == {"confusable": {"AC8646": [
        {"other": "AC6846", "reason": "same prefix, same digits in a different order", "score": 0.7}]}}


def test_voice_alert_file(api_client, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ALERT_DIR", tmp_path)
    did = str(ObjectId())
    assert api_client.get(f"/voice/alerts/{did}").status_code == 404
    assert api_client.get("/voice/alerts/not-an-id").status_code == 400
    (tmp_path / f"{did}.mp3").write_bytes(b"ID3fake")
    r = api_client.get(f"/voice/alerts/{did}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"ID3fake"
