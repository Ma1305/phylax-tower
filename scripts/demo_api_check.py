"""Drive the live API like the web UI does and assert the demo beats (headless).

    scripts/demo_api_check.sh            # starts scripts/demo.sh in a throwaway db, runs this

Expects the demo stack (gate, agents, API) to be up on API (default http://127.0.0.1:8000)
and reads the same database through MONGODB_DB to check what landed in Atlas.

Beats checked:
  1. start safety OFF -> no executed stop; TRUCK1 not held
  2. reset, start safety ON -> executed stop notifying TRUCK1; TRUCK1 held at D
  3. procedures proposal pending_approval -> POST /approve -> executed, approved_by
  4. POST /rogue -> 2 security_events (code 121); no "clear" intent stored
  5. reset, start safety ON, disable procedures mid-run -> proposal rejected agent_disabled
  6. every decision has authorized_by {permission_id, rule_set_version}; WS delivers
     the snapshot and live decision events with their intent; /audit status reported
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import requests
import websockets
from bson import ObjectId

from holdshort.db import get_db

API = os.environ.get("API", "http://127.0.0.1:8000").rstrip("/")
WS = API.replace("http", "ws", 1) + "/ws"
CONTROLLER = "controller_A"
db = get_db()
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}", flush=True)
    return bool(ok)


def call(method: str, path: str, body: Any = None, expect: int = 200) -> Any:
    r = requests.request(method, API + path, json=body, timeout=30)
    if r.status_code != expect:
        raise SystemExit(f"FAIL  {method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json()


def wait(pred, timeout: float = 60, every: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def wait_finished(timeout: float = 240) -> dict:
    st = wait(lambda: (s := call("GET", "/scenario/status"))["last"].get("state") in
              ("finished", "failed", "cancelled") and s, timeout, 0.5)
    if not st:
        raise SystemExit("FAIL  scenario did not finish in time")
    return st["last"]


def play(safety: bool) -> ObjectId:
    call("POST", "/scenario/reset")
    r = call("POST", "/scenario/start", {"safety_enabled": safety, "mode": "text"})
    print(f"      started run safety={safety}: {r['result']}", flush=True)
    return ObjectId(r["result"]["marker"])


def stop_for(marker: ObjectId) -> dict | None:
    return db.decisions.find_one({"_id": {"$gte": marker}, "intent_action": "stop",
                                  "outcome": "executed", "notified": "TRUCK1"})


def proposal_for(marker: ObjectId) -> dict | None:
    return db.decisions.find_one({"_id": {"$gte": marker}, "agent": "procedures",
                                  "intent_action": "propose_instruction",
                                  "approved_by": None, "revokes": None})


async def ws_listen(events: list[dict], ready: asyncio.Event, stop: asyncio.Event) -> None:
    async with websockets.connect(WS) as ws:
        first = json.loads(await ws.recv())
        events.append(first)
        ready.set()
        while not stop.is_set():
            try:
                events.append(json.loads(await asyncio.wait_for(ws.recv(), 0.5)))
            except TimeoutError:
                pass


def main() -> int:
    print(f"== API {API}, database {db.name}", flush=True)
    health = call("GET", "/health")
    check("GET /health ok + change feed open", health["status"] == "ok" and health["change_feed"],
          json.dumps(health))

    # WebSocket listener for the whole check (what the browser sees)
    ws_events: list[dict] = []
    loop = asyncio.new_event_loop()
    ready, stop = asyncio.Event(), asyncio.Event()
    import threading

    th = threading.Thread(target=lambda: loop.run_until_complete(ws_listen(ws_events, ready,
                                                                            stop)), daemon=True)
    th.start()
    wait(lambda: ready.is_set(), 15)
    check("WS /ws: first frame is a snapshot", bool(ws_events) and ws_events[0]["type"] ==
          "snapshot", ws_events[0]["type"] if ws_events else "no frame")

    # -- beat 1: safety OFF
    print("== beat 1: safety OFF", flush=True)
    m1 = play(False)
    perm = db.permissions.find_one({"agent": "safety"})
    check("safety switch OFF, changed by controller_A (logged)",
          perm["enabled"] is False and perm["by"] == CONTROLLER)
    last1 = wait_finished()
    time.sleep(2)
    stops1 = list(db.decisions.find({"_id": {"$gte": m1}, "intent_action": "stop"}))
    check("run 1: no executed stop decision",
          not any(d["outcome"] == "executed" for d in stops1),
          f"stop decisions: {[(d['outcome'], d['rule']) for d in stops1]}")
    check("run 1: TRUCK1 not held (it crosses)", last1.get("truck_held") is False, str(last1))
    n_tx = db.transmissions.count_documents({"_id": {"$gte": m1}})
    check("run 1: transmissions stored", n_tx >= 10, f"{n_tx} transmissions")

    # -- beat 2 + 3: safety ON, approve
    print("== beat 2+3: reset, safety ON, approve the proposal", flush=True)
    m2 = play(True)
    perm = db.permissions.find_one({"agent": "safety"})
    check("safety switch ON, changed by controller_A", perm["enabled"] and perm["by"] == CONTROLLER)
    stop2 = wait(lambda: stop_for(m2), 120)
    check("run 2: executed STOP notifying TRUCK1", stop2 is not None,
          f"rule={stop2['rule']} authorized_by={stop2['authorized_by']}" if stop2 else "none")
    prop = wait(lambda: proposal_for(m2), 30)
    check("run 2: procedures proposal pending_approval",
          bool(prop) and prop["outcome"] == "pending_approval",
          prop["outcome"] if prop else "no proposal")
    if prop:
        intent = db.intents_procedures.find_one({"_id": prop["intent_id"]})
        check("proposal text", intent["proposal_text"] ==
              "TRUCK1 cross runway 04 at D after AC8646 passes", repr(intent["proposal_text"]))
        ap = call("POST", "/approve", {"intent_id": str(prop["intent_id"]),
                                       "controller": CONTROLLER})
        check("POST /approve -> executed, approved_by controller_A",
              ap["outcome"] == "executed" and ap["approved_by"] == CONTROLLER
              and ap["rule"] == "controller_approved", f"{ap['outcome']} {ap['approved_by']}")
        r = requests.post(API + "/approve", json={"intent_id": str(prop["intent_id"]),
                                                  "controller": CONTROLLER}, timeout=30)
        check("second approve is refused (409)", r.status_code == 409, str(r.status_code))
    last2 = wait_finished()
    check("run 2: TRUCK1 held at D", last2.get("truck_held") is True, str(last2))
    fs = db.flight_state.find_one({"callsign": "TRUCK1"})
    check("run 2: flight_state TRUCK1 hold_short", bool(fs and fs.get("hold_short")))

    # -- beat 4: rogue
    print("== beat 4: rogue agent", flush=True)
    before = db.security_events.count_documents({})
    rg = call("POST", "/rogue")
    evs = rg["result"]
    check("POST /rogue: 2 writes blocked by Atlas (code 121)",
          len(evs) == 2 and all(e["error_code"] == 121 for e in evs),
          "; ".join(f"{e['error_code']} {e['attempted']}" for e in evs))
    check("security_events +2 in Atlas", db.security_events.count_documents({}) == before + 2)
    check("no 'clear' intent / self-approved decision stored",
          db.intents_procedures.count_documents({"action": "clear"}) == 0
          and db.decisions.count_documents({"rule": "rogue_self_approved"}) == 0)

    # -- beat 5: mid-run kill
    print("== beat 5: disable procedures mid-run", flush=True)
    m3 = play(True)
    landing = wait(lambda: db.transmissions.find_one({"_id": {"$gte": m3}, "kind": "clearance",
                                                      "instruction": "land"}), 60, 0.1)
    early = stop_for(m3) is None
    call("POST", "/permissions/procedures", {"enabled": False, "by": CONTROLLER})
    check("procedures disabled mid-run (after landing clearance, before STOP)",
          bool(landing) and early)
    stop3 = wait(lambda: stop_for(m3), 120)
    check("run 3: STOP still fires (safety on)", stop3 is not None)
    prop3 = wait(lambda: proposal_for(m3), 30)
    check("run 3: procedures' next intent rejected agent_disabled",
          bool(prop3) and prop3["outcome"] == "rejected" and prop3["rule"] == "agent_disabled",
          f"{prop3['outcome']} {prop3['rule']}" if prop3 else "no decision")
    wait_finished()
    call("POST", "/permissions/procedures", {"enabled": True, "by": CONTROLLER})

    # -- beat 6: audit trail
    print("== beat 6: audit trail", flush=True)
    ds = list(db.decisions.find({"_id": {"$gte": m1}}))
    check("every decision has authorized_by {permission_id, rule_set_version}",
          bool(ds) and all(d["authorized_by"].get("permission_id") and
                           d["authorized_by"].get("rule_set_version") for d in ds),
          f"{len(ds)} decisions")
    if stop2:
        it = db.intents_safety.find_one({"_id": stop2["intent_id"]})
        print(f"      why was TRUCK1 held? rule={it['reason']} conflicts_with="
              f"{it['conflicts_with']!r} authorized_by={stop2['authorized_by']} "
              f"approver={stop2['approved_by'] or 'autonomous (tier_autonomous)'}", flush=True)
    au = requests.get(API + "/audit", params={"q": "Why was TRUCK1 held?"}, timeout=60)
    print(f"      GET /audit -> {au.status_code} {au.text[:200]}", flush=True)

    loop.call_soon_threadsafe(stop.set)
    th.join(5)
    types = {e["type"] for e in ws_events}
    dec = [e for e in ws_events if e["type"] == "decision"]
    check("WS: live transmission, decision, permission and state events",
          {"transmission", "decision", "permission", "state"} <= types, str(sorted(types)))
    check("WS: decision events carry their intent (proposal_text for the UI)",
          any((e["doc"].get("intent") or {}).get("proposal_text") for e in dec))

    print(f"== {sum(results)}/{len(results)} checks passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
