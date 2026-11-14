"""End-to-end check of a running tower (driven by scripts/e2e_check.sh).

    uv run python scripts/e2e_check.py --api http://127.0.0.1:18xxx --db holdshort_v2_t33_e2e

Three runs of the tools/smoke_feed.py incident (plus one normal run) against the tower,
playing the controllers through /control/*:
  1. default (safety proactive/autonomous): the stop executes at once and is spoken; the
     procedures resume proposal waits for ctl_local (ctl_ground gets 403); the stop is lifted.
  2. safety tier needs_approval: the stop waits for ctl_local, then is spoken.
  3. safety mode reactive: an advisory and no stop.
After each run the decision log is audited (one initial decision per intent, one more per
approval / rejection / revocation, no outbound call without an executed decision).
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


def _id(d: dict | None) -> str | None:
    return d and (d.get("_id") or d.get("id"))


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def first_conflict(db, t0: datetime) -> dict | None:
    return db.state_events.find_one({"kind": "lane_status", "ref": "RWY_04_22", "t": {"$gte": t0},
                                     "doc.state": "conflict"}, sort=[("t", 1)])


def spoken(db, decision_id, timeout: float = 20) -> dict | None:
    from bson import ObjectId
    oid = ObjectId(str(decision_id))
    return wait_for(lambda: db.transmissions.find_one({"decision_id": oid, "synthetic": True}),
                    timeout)


def feed_and_stream_checks(db, t0: datetime, feed, out: list[str], label: str) -> None:
    feed.wait(180)
    log = "\n".join(out)
    check(f"{label}: smoke feed finished", feed.returncode == 0 and "smoke_feed: done" in log,
          f"exit {feed.returncode}")
    clips = list(db.audio_clips.find({"direction": "in", "created_at": {"$gte": t0}}))
    streams = {cl["stream_id"] for cl in clips}
    bad = []
    for cl in clips:
        tx = cl.get("transmission_id") and db.transmissions.find_one({"_id": cl["transmission_id"]})
        if not tx or tx.get("stream_id") != cl["stream_id"]:
            bad.append(f"{cl['_id']}:{cl.get('status')}")
    check(f"{label}: clips on >= 2 streams, each a transmission with its stream_id",
          len(streams) >= 2 and clips and not bad,
          f"{len(clips)} clips on {', '.join(sorted(streams))}; bad: {bad[:3]}")
    on_rwy = db.positions.count_documents({"meta.object_id": "TRUCK1", "t": {"$gte": t0},
                                           "lane_id": {"$regex": "^RWY_"}})
    check(f"{label}: feed held TRUCK1 short (never on a runway)",
          "STOP for TRUCK1" in log and on_rwy == 0, f"{on_rwy} runway positions")


def audit_log(c: httpx.Client, db, t0: datetime, label: str) -> None:
    """The decision log is complete: one initial decision per intent, one more per approval,
    rejection or revocation, authorized_by on all, and no outbound call without an executed
    decision behind it."""
    from bson import ObjectId
    time.sleep(2)
    r = c.get("/control/decisions", params={"tower_id": "LGA", "since": t0.isoformat(),
                                            "limit": 1000})
    docs = r.json().get("decisions", []) if r.status_code == 200 else []
    ts = [d["t"] for d in docs]
    check(f"{label}: GET /control/decisions newest first, intent merged",
          r.status_code == 200 and docs and ts == sorted(ts, reverse=True)
          and all(d.get("intent") and d["intent"]["_id"] == d["intent_id"] for d in docs),
          f"HTTP {r.status_code}, {len(docs)} decisions")
    intents = {}
    for agent in ("safety", "procedures", "weather"):
        for i in db[f"intents_{agent}"].find({"t": {"$gte": t0}}):
            intents[str(i["_id"])] = agent
    by_intent: dict[str, list[dict]] = {}
    for d in db.decisions.find({"intent_id": {"$in": [ObjectId(i) for i in intents]}}):
        by_intent.setdefault(str(d["intent_id"]), []).append(d)
    problems = []
    for iid, agent in intents.items():
        ds = by_intent.get(iid, [])
        initial = [d for d in ds if d.get("approved_by") is None and d.get("revokes") is None]
        follow = [d for d in ds if d not in initial]
        if len(initial) != 1:
            problems.append(f"{agent} intent {iid[-6:]}: {len(initial)} initial")
        for d in follow:
            if d["rule"] not in ("controller_approved", "controller_rejected",
                                 "controller_revoked") or not d.get("approved_by"):
                problems.append(f"{iid[-6:]}: follow-up {d['rule']} without a controller")
        for d in ds:
            if not (d.get("authorized_by") or {}).get("permission_id"):
                problems.append(f"{d['_id']}: no authorized_by")
    n_follow = sum(len(v) - 1 for v in by_intent.values())
    check(f"{label}: one initial decision per intent + one per approval/rejection/revocation",
          intents and not problems,
          f"{len(intents)} intents, {n_follow} follow-ups; {problems[:3]}")
    syn = list(db.transmissions.find({"synthetic": True, "t": {"$gte": t0}}))
    orphans = []
    for tx in syn:
        d = tx.get("decision_id") and db.decisions.find_one({"_id": tx["decision_id"]})
        if not d or d["outcome"] != "executed" or d["intent_action"] not in (
                "stop", "propose_instruction") or (
                d["intent_action"] == "propose_instruction" and not d.get("approved_by")):
            orphans.append(str(tx["_id"]))
    outs = db.audio_clips.count_documents({"direction": "out", "created_at": {"$gte": t0}})
    check(f"{label}: no outbound transmission without an executed decision",
          not orphans and outs == len(syn), f"{len(syn)} outbound, {outs} out clips, "
          f"orphans {orphans[:3]}")
    denied = list(db.security_events.find({"t": {"$gte": t0},
                                           "kind": {"$in": ["read_denied", "write_denied"]}}))
    check(f"{label}: no agent blocked by its own access list", not denied,
          "; ".join(f"{e.get('agent')}: {e.get('attempted')}" for e in denied[:3]))


def run_autonomous(c: httpx.Client, db, api: str) -> None:
    label = "run 1 (autonomous)"
    perm = db.permissions.find_one({"_id": "safety"})
    check(f"{label}: seeded safety tier is autonomous",
          perm and perm["tier"] == "autonomous" and perm["mode"] == "proactive",
          perm and f"{perm['mode']}/{perm['tier']} by {perm.get('by')}")
    t0 = datetime.now(UTC)
    sec_before = db.security_events.count_documents({"kind": "not_lane_owner"})
    out: list[str] = []
    feed = run_feed(api, "incident", out)
    stop = wait_for(lambda: db.decisions.find_one(
        {"agent": "safety", "intent_action": "stop", "t": {"$gte": t0}}), 90)
    conflict = wait_for(lambda: first_conflict(db, t0), 10)
    ok = stop is not None and stop["outcome"] == "executed" and stop["rule"] == "tier_autonomous" \
        and stop.get("approved_by") is None and stop.get("controller_id") == "ctl_local"
    check(f"{label}: stop executed with no approval, routed to ctl_local", ok,
          stop and f"{stop['outcome']} [{stop['rule']}] -> {stop.get('controller_id')}")
    if stop and conflict:
        lag = (stop["t"] - conflict["t"]).total_seconds()
        check(f"{label}: stop within 3 s of the conflict in lane_status", lag <= 3.0,
              f"{lag:+.2f} s")
    else:
        check(f"{label}: stop within 3 s of the conflict in lane_status", False,
              f"stop={bool(stop)} conflict={bool(conflict)}")
    syn = stop and spoken(db, stop["_id"])
    oclip = syn and db.audio_clips.find_one({"_id": syn.get("clip_id"), "direction": "out"})
    check(f"{label}: the outbound call follows (synthetic transmission + clip)",
          bool(syn) and bool(oclip),
          syn and f"'{syn.get('transcript')}' on {syn.get('stream_id')} "
                  f"{(syn['t'] - stop['t']).total_seconds():.1f} s after")
    r = c.get("/ingest/outbound", params={"tower_id": "LGA",
                                          "since": (t0 - timedelta(seconds=1)).isoformat()})
    calls = r.json().get("calls", []) if r.status_code == 200 else []
    check(f"{label}: GET /ingest/outbound returned the stop",
          any(k.get("action") == "stop" and "TRUCK1" in (k.get("target") or []) for k in calls),
          f"{len(calls)} calls")

    def pending_prop():
        r = c.get("/control/pending", params={"controller_id": "ctl_local"})
        for d in (r.json().get("pending") if r.status_code == 200 else []) or []:
            if d.get("agent") == "procedures" and d.get("intent_action") == "propose_instruction" \
                    and str(d.get("t", "")) >= _iso(t0):
                return d
        return None
    prop = wait_for(pending_prop, 30)
    check(f"{label}: the procedures proposal to resume is pending for ctl_local",
          prop is not None, prop and f"'{prop.get('text')}'")
    if prop:
        r = c.post("/control/approve", json={"decision_id": _id(prop),
                                             "controller_id": "ctl_ground"})
        sec_after = db.security_events.count_documents({"kind": "not_lane_owner"})
        check(f"{label}: ctl_ground approving it gets 403 + a security event",
              r.status_code == 403 and sec_after == sec_before + 1,
              f"HTTP {r.status_code}, not_lane_owner {sec_before} -> {sec_after}")
        r = c.post("/control/approve", json={"decision_id": _id(prop),
                                             "controller_id": "ctl_local"})
        ok = r.status_code == 200
        syn = ok and spoken(db, r.json()["_id"])
        check(f"{label}: ctl_local approves it and it is spoken", bool(syn),
              f"HTTP {r.status_code}" + (f", '{syn.get('transcript')}'" if syn else ""))
    sup = wait_for(lambda: db.context.find_one(
        {"kind": "clearance", "callsigns": "TRUCK1", "superseded_by": {"$ne": None}}), 20)
    check(f"{label}: the stop supersedes TRUCK1's clearance in context", sup is not None,
          sup and sup["summary"][:60])
    feed_and_stream_checks(db, t0, feed, out, label)
    if stop:  # the controller lifts the stop
        r = c.post("/control/revoke", json={"decision_id": str(stop["_id"]),
                                            "controller_id": "ctl_local"})
        check(f"{label}: ctl_local lifts the stop (revoke)", r.status_code == 200 and
              r.json().get("rule") == "controller_revoked", f"HTTP {r.status_code}")
    audit_log(c, db, t0, label)


def normal_no_stop(c: httpx.Client, db, api: str) -> None:
    t0 = datetime.now(UTC)
    out: list[str] = []
    feed = run_feed(api, "normal", out)
    feed.wait(180)
    time.sleep(3)
    n = db.decisions.count_documents({"intent_action": "stop", "t": {"$gte": t0}})
    check("normal scenario produces no stop", feed.returncode == 0 and n == 0,
          f"{n} stop decisions")


def run_needs_approval(c: httpx.Client, db, api: str) -> None:
    label = "run 2 (needs_approval)"
    r = c.post("/control/permissions/safety", json={"by": "ctl_sup", "tier": "needs_approval"})
    check(f"{label}: POST /control/permissions/safety tier needs_approval",
          r.status_code == 200 and r.json().get("tier") == "needs_approval",
          f"HTTP {r.status_code}")
    t0 = datetime.now(UTC)
    out: list[str] = []
    feed = run_feed(api, "incident", out)
    stop = wait_for(lambda: db.decisions.find_one(
        {"agent": "safety", "intent_action": "stop", "t": {"$gte": t0}}), 90)
    check(f"{label}: the stop is pending for ctl_local",
          stop is not None and stop["outcome"] == "pending_approval"
          and stop["rule"] == "needs_approval" and stop.get("controller_id") == "ctl_local",
          stop and f"{stop['outcome']} [{stop['rule']}] -> {stop.get('controller_id')}")
    if stop:
        time.sleep(2)
        later = db.decisions.count_documents({"intent_id": stop["intent_id"],
                                              "_id": {"$ne": stop["_id"]}})
        said = db.transmissions.count_documents({"synthetic": True, "t": {"$gte": t0}})
        check(f"{label}: it stays pending and silent until approved", later == 0 and said == 0,
              f"{later} later decisions, {said} outbound")
        r = c.post("/control/approve", json={"decision_id": str(stop["_id"]),
                                             "controller_id": "ctl_local"})
        ok = r.status_code == 200 and r.json().get("rule") == "controller_approved"
        syn = ok and spoken(db, r.json()["_id"])
        check(f"{label}: ctl_local approves it and the stop is spoken", bool(syn),
              f"HTTP {r.status_code}" + (f", '{syn.get('transcript')}'" if syn else ""))
    prop = wait_for(lambda: db.decisions.find_one(
        {"agent": "procedures", "outcome": "pending_approval", "t": {"$gte": t0}}), 30)
    if prop:
        r = c.post("/control/reject", json={"decision_id": str(prop["_id"]),
                                            "controller_id": "ctl_local", "reason": "not yet"})
        check(f"{label}: ctl_local rejects the resume proposal",
              r.status_code == 200 and r.json().get("rule") == "controller_rejected",
              f"HTTP {r.status_code}")
    else:
        check(f"{label}: a procedures proposal is pending", False)
    feed_and_stream_checks(db, t0, feed, out, label)
    audit_log(c, db, t0, label)
    c.post("/control/permissions/safety", json={"by": "ctl_sup", "tier": "autonomous"})


def run_reactive(c: httpx.Client, db, api: str) -> None:
    label = "run 3 (reactive)"
    r = c.post("/control/permissions/safety", json={"by": "ctl_sup", "mode": "reactive"})
    check(f"{label}: safety set to reactive", r.status_code == 200, f"HTTP {r.status_code}")
    t0 = datetime.now(UTC)
    out: list[str] = []
    feed = run_feed(api, "incident", out)
    feed.wait(180)
    time.sleep(3)
    adv = db.decisions.find_one({"agent": "safety", "intent_action": "warn",
                                 "outcome": "executed", "t": {"$gte": t0}})
    stops = db.decisions.count_documents({"intent_action": "stop", "t": {"$gte": t0},
                                          "outcome": {"$in": ["executed", "pending_approval"]}})
    check(f"{label}: an advisory routed to ctl_local",
          adv is not None and adv["rule"] == "advisory" and adv.get("controller_id") == "ctl_local",
          adv and f"{adv['rule']} -> {adv.get('controller_id')}")
    check(f"{label}: no stop", stops == 0, f"{stops} stops")
    audit_log(c, db, t0, label)
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
        run_autonomous(c, db, args.api)
        normal_no_stop(c, db, args.api)
        run_needs_approval(c, db, args.api)
        run_reactive(c, db, args.api)
    client.close()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" +
          (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
