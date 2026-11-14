"""Search tests. Unit tests always run; live tests use the seeded MONGODB_DB database
(run scripts/setup_search.py first) and skip if its search indexes are not queryable."""

import pytest

from holdshort.search import callsigns, incidents
from holdshort.search.callsigns import _reason, normalize


@pytest.mark.parametrize(
    ("heard", "want"),
    [
        ("truck one", "TRUCK1"),
        ("TRUCK 1", "TRUCK1"),
        ("air canada eight six four six", "AC8646"),
        ("jazz eight four six four", "AC8464"),
        ("delta two two one four", "DL2214"),
        ("tower", "TOWER"),
    ],
)
def test_normalize(heard, want):
    assert normalize(heard) == want


def test_reason():
    assert "different order" in _reason("AC8646", "AC6846")
    assert "1 digit" in _reason("TRUCK1", "TRUCK2")


@pytest.fixture(scope="module")
def search_db(client):
    from holdshort.db import get_db

    db = get_db(client=client)
    for coll, name in [
        (callsigns.COLLECTION, callsigns.INDEX),
        (incidents.COLLECTION, incidents.TEXT_INDEX),
    ]:
        ix = next(iter(db[coll].list_search_indexes(name)), None)
        if not (ix and ix.get("queryable")):
            pytest.skip(f"{coll}.{name} not ready; run scripts/setup_search.py")
    return db


def test_resolve_truck_one(search_db):
    m = callsigns.resolve("truck one", db=search_db)
    assert m is not None and m.callsign == "TRUCK1" and m.score > 0


@pytest.mark.parametrize(
    ("heard", "want"),
    [("air canada eight six four six", "AC8646"), ("truk won", "TRUCK1"), ("AC 8464", "AC8464")],
)
def test_resolve_variants(search_db, heard, want):
    assert callsigns.resolve(heard, db=search_db).callsign == want


def test_confusable_pairs(search_db):
    others = [c.other for c in callsigns.similar_on_frequency("AC8646", db=search_db)]
    assert "AC8646" not in others
    assert {"AC8464", "AC6846"} <= set(others)


@pytest.mark.parametrize("method", [None, "text"])
def test_similar_incident_laguardia_first(search_db, method):
    hits = incidents.similar(
        "fire truck crossing active runway during landing", k=3, db=search_db, method=method
    )
    assert len(hits) == 3
    assert hits[0].id == "lga-2026-03-22"
