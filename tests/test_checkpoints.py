"""LangGraph + MongoDBSaver checkpoints: a restarted safety agent resumes, never replays."""

from datetime import UTC, datetime

from bson import ObjectId
from test_agents import _script_txs

from holdshort.agents._graph import configure_tracing, current_run
from holdshort.agents.safety_agent import SafetyAgent
from holdshort.contracts import ScenarioRun, to_mongo
from holdshort.gate import set_permission
from holdshort.scenario.runner import reset


def test_tracing_off_without_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert configure_tracing() is False
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test-not-a-real-key")
    monkeypatch.delenv("LANGSMITH_TRACING")
    assert configure_tracing() is True


def test_safety_resumes_mid_step_without_replay(test_db):
    db = test_db
    set_permission(db, "safety", True, "test_setup")
    reset(db)
    run_id = ObjectId.from_datetime(datetime.now(UTC))
    db.scenario_runs.insert_one({"_id": run_id, **to_mongo(ScenarioRun(
        scenario="laguardia", started_at=datetime.now(UTC), safety_enabled=True, speed=1,
        mode="text"))})
    docs = []
    for tx in _script_txs():
        doc = to_mongo(tx)
        doc["_id"] = db.transmissions.insert_one(doc).inserted_id
        docs.append(doc)
    cross = next(i for i, d in enumerate(docs)
                 if d["kind"] == "clearance" and d["instruction"] == "cross")

    a1 = SafetyAgent(db)
    assert current_run(db, docs[0]["_id"]) == run_id
    for doc in docs[:cross]:
        a1.process(doc)
    # crash after the stop intent is inserted but before flight_state is persisted
    a1.graph.invoke(a1.graph_input(docs[cross]), a1.config(run_id), interrupt_after=["emit"],
                    durability="sync")
    assert a1.snapshot(run_id).next == ("persist",)
    assert db.intents_safety.count_documents({"_id": {"$gte": run_id}}) == 1

    a2 = SafetyAgent(db)  # "restarted process": state only from Atlas
    a2.on_ready()  # finishes persist, then catches up on the rest of the script
    snap = a2.snapshot(run_id)
    assert not snap.next
    assert snap.values["processed"] == len(docs)
    assert snap.values["last_id"] == str(docs[-1]["_id"])
    assert db.intents_safety.count_documents({"_id": {"$gte": run_id}}) == 1  # no replay
    assert a2.process(docs[0]) is None  # already-processed calls are skipped
    reset(db)
