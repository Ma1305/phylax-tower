"""End-to-end check of a running tower (driven by scripts/e2e_check.sh).

    uv run python scripts/e2e_check.py --api http://127.0.0.1:18xxx --db holdshort_v2_t33_e2e

Runs tools/smoke_feed.py (incident, then normal) against the tower, plays the controllers
through /control/*, and asserts on the database and the API. With --reactive it first sets
the safety agent to reactive and only checks that the incident gives an advisory and no stop.
Prints PASS / FAIL per assertion; exit code 1 if any failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from holdshort.db import get_client  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return bool(ok)


def run_feed(api: str, scenario: str, out: list[str]) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, str(REPO / "tools" / "smoke_feed.py"), "--api", api,
                          "--scenario", scenario], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, cwd=REPO)

    def pump() -> None:
        for line in p.stdout:
            out.append(line.rstrip())
            print(f"    | {line.rstrip()}", flush=True)
    threading.Thread(target=pump, daemon=True).start()
    return p


def wait_for(fn, timeout: float, every: float = 0.3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def incident_proactive(c: httpx.Client, db, api: str) -> None:
    t0 = datetime.now(UTC)
    sec_before = db.security_events.count_documents({"kind": "not_lane_owner"})
    out: list[str] = []
    feed = run_feed(api, "incident", out)

    def local_pending_stop():
        r = c.get("/control/pending", params={"controller_id": "ctl_local"})
        for d in (r.json().get("pending") if r.status_code == 200 else []) or []:
            if d.get("agent") == "safety" and d.get("intent_action") == "stop" \
                    and str(d.get("t", "")) >= t0.strftime("%Y-%m-%dT%H:%M:%S"):
                return d
        return None

    stop = wait_for(local_pending_stop, 90)
    check("pending safety stop routed to ctl_local", stop is not None,
          stop and f"decision {stop.get('_id') or stop.get('id')} rule {stop.get('rule')}")
    if stop:
        did = stop.get("_id") or stop.get("id")
        r = c.post("/control/approve", json={"decision_id": did, "controller_id": "ctl_ground"})
        sec_after = db.security_events.count_documents({"kind": "not_lane_owner"})
        check("ctl_ground approving it gets 403", r.status_code == 403, f"HTTP {r.status_code}")
        check("... and a not_lane_owner security event", sec_after == sec_before + 1,
              f"{sec_before} -> {sec_after}")
        r = c.post("/control/approve", json={"decision_id": did, "controller_id": "ctl_local"})
        check("ctl_local approves through POST /control/approve", r.status_code == 200,
              f"HTTP {r.status_code}")
        # just after the spoken stop: the crossing clearance it replaces is marked superseded
        sup = wait_for(lambda: db.context.find_one(
            {"kind": "clearance", "callsigns": "TRUCK1", "superseded_by": {"$ne": None}}), 20)
        check("the stop supersedes TRUCK1's clearance in context", sup is not None,
              sup and sup["summary"][:60])
    feed.wait(180)
    log = "\n".join(out)
    check("smoke feed finished", feed.returncode == 0 and "smoke_feed: done" in log,
          f"exit {feed.returncode}")

    # clips -> transmissions per stream
    clips = list(db.audio_clips.find({"direction": "in", "created_at": {"$gte": t0}}))
    streams = {cl["stream_id"] for cl in clips}
    check("clips arrived on at least two streams", len(streams) >= 2, ", ".join(sorted(streams)))
    bad = []
    for cl in clips:
        tx = cl.get("transmission_id") and db.transmissions.find_one({"_id": cl["transmission_id"]})
        if not tx or tx.get("stream_id") != cl["stream_id"]:
            bad.append(f"{cl['_id']}:{cl.get('status')}")
    check("each clip became a transmission with the right stream_id", clips and not bad,
          f"{len(clips)} clips, bad: {bad[:3]}")

    # lane status history
    states = [e["doc"]["state"] for e in db.state_events.find(
        {"kind": "lane_status", "ref": "RWY_04_22", "t": {"$gte": t0}}).sort("t", 1)]
    ok = "reserved" in states and "conflict" in states[states.index("reserved"):]
    check("RWY_04_22 went reserved -> conflict (state_events)", ok, " ".join(states[:6]))

    adv = db.decisions.find_one({"agent": "safety", "intent_action": "warn",
                                 "outcome": "executed", "t": {"$gte": t0}})
    check("safety advisory routed to ctl_local",
          adv is not None and adv.get("controller_id") == "ctl_local" and adv["rule"] == "advisory",
          adv and f"{adv['rule']} -> {adv.get('controller_id')}")

    # speaker
    ex = db.decisions.find_one({"agent": "safety", "intent_action": "stop", "outcome": "executed",
                                "t": {"$gte": t0}})
    syn = ex and wait_for(lambda: db.transmissions.find_one(
        {"decision_id": ex["_id"], "synthetic": True}), 20)
    oclip = syn and db.audio_clips.find_one({"_id": syn.get("clip_id"), "direction": "out"})
    check("speaker produced an outbound clip and a synthetic transmission",
          bool(syn) and bool(oclip), syn and f"'{syn.get('transcript')}' on {syn.get('stream_id')}")

    r = c.get("/ingest/outbound", params={"tower_id": "LGA",
                                          "since": (t0 - timedelta(seconds=1)).isoformat()})
    calls = r.json().get("calls", []) if r.status_code == 200 else []
    check("GET /ingest/outbound returned the stop",
          any(k.get("action") == "stop" and "TRUCK1" in (k.get("target") or []) for k in calls),
          f"{len(calls)} calls")
    on_rwy = db.positions.count_documents({"meta.object_id": "TRUCK1", "t": {"$gte": t0},
                                           "lane_id": {"$regex": "^RWY_"}})
    check("feed held TRUCK1 short (never on a runway)",
          "STOP for TRUCK1" in log and on_rwy == 0, f"{on_rwy} runway positions")

    prop = wait_for(lambda: db.decisions.find_one(
        {"agent": "procedures", "outcome": "pending_approval", "t": {"$gte": t0}}), 30)
    check("a procedures proposal is pending", prop is not None,
          prop and f"'{prop.get('text')}' -> {prop.get('controller_id')}")

    # working memory vs permanent log: the TRUCK1 crossing clearance was superseded by the
    # stop (60 s grace, then swept) while its transmission stays in the log for good.
    now = datetime.now(UTC)
    cross = db.transmissions.find_one({"to": "TRUCK1", "kind": "clearance",
                                       "instruction": "cross", "t": {"$gte": t0}})
    item = cross and db.context.find_one({"source.id": cross["_id"]})
    retired = cross is not None and (item is None or (
        item.get("superseded_by") is not None
        and item["expires_at"].replace(tzinfo=UTC) <= now + timedelta(seconds=61)))
    stale = db.context.count_documents({"expires_at": {"$lte": now - timedelta(seconds=90)}})
    check("context holds only live items (superseded ones retire)", retired and stale == 0,
          f"crossing clearance item: {'gone' if item is None else 'superseded'}, "
          f"{stale} stale, {db.context.count_documents({})} items left" if cross else
          "no crossing clearance transmission")
    ntx = db.transmissions.count_documents({"t": {"$gte": t0 - timedelta(seconds=5)}})
    check("transmissions keeps everything (incl. the superseded clearance)",
          cross is not None and ntx >= len(clips) + 1,
          f"{ntx} transmissions for {len(clips)} clips + outbound")


def normal_no_stop(c: httpx.Client, db, api: str) -> None:
    t0 = datetime.now(UTC)
    out: list[str] = []
    feed = run_feed(api, "normal", out)
    feed.wait(180)
    time.sleep(3)
    n = db.decisions.count_documents({"intent_action": "stop", "t": {"$gte": t0}})
    check("normal scenario produces no stop", feed.returncode == 0 and n == 0,
          f"{n} stop decisions")


def incident_reactive(c: httpx.Client, db, api: str) -> None:
    r = c.post("/control/permissions/safety", json={"by": "ctl_sup", "mode": "reactive"})
    check("safety set to reactive", r.status_code == 200, f"HTTP {r.status_code}")
    t0 = datetime.now(UTC)
    out: list[str] = []
    feed = run_feed(api, "incident", out)
    feed.wait(180)
    time.sleep(3)
    adv = db.decisions.count_documents({"agent": "safety", "intent_action": "warn",
                                        "outcome": "executed", "t": {"$gte": t0}})
    stops = db.decisions.count_documents({"intent_action": "stop", "t": {"$gte": t0},
                                          "outcome": {"$in": ["executed", "pending_approval"]}})
    check("reactive: an advisory", adv >= 1, f"{adv} advisories")
    check("reactive: no stop", stops == 0, f"{stops} stops")
    c.post("/control/permissions/safety", json={"by": "ctl_sup", "mode": "proactive"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    if args.db in {"holdshort", "holdshort_v2"}:
        print("refusing to run on", args.db)
        return 2
    client = get_client()
    db = client[args.db]
    with httpx.Client(base_url=args.api, timeout=30) as c:
        h = c.get("/health").json()
        check("GET /health lists ingest, control, outbound",
              {"ingest", "control", "outbound"} <= set(h.get("routers", [])), str(h["routers"]))
        print(f"voice_enabled={h.get('voice_enabled')}")
        incident_proactive(c, db, args.api)
        normal_no_stop(c, db, args.api)
        incident_reactive(c, db, args.api)
    client.close()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" +
          (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
