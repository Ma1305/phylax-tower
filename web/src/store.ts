import type {
  AtlasEvent, Decision, FeedMsg, Permission, StateDoc, Transmission,
} from "./types";
import { idOf, normCallsign, toMs } from "./types";

export interface RogueNote {
  key: number;
  t: number | null;
  ok: boolean;
  text: string;
}

export interface Store {
  transmissions: Transmission[];
  decisions: Decision[];
  permissions: Record<string, Permission>;
  flightState: Record<string, StateDoc>;
  runwayStatus: Record<string, StateDoc>;
  events: AtlasEvent[];
  rogue: RogueNote[];
  /** Latest scenario time seen in the feed (ms) and the wall time we saw it. */
  clock: { t: number | null; wall: number; running: boolean };
}

export const initialStore = (): Store => ({
  transmissions: [],
  decisions: [],
  permissions: {},
  flightState: {},
  runwayStatus: {},
  events: [],
  rogue: [],
  clock: { t: null, wall: Date.now(), running: false },
});

export type StoreAction =
  | { kind: "msg"; msg: FeedMsg }
  | { kind: "reset" }
  | { kind: "clock"; t: number | null; running: boolean }
  | { kind: "rogue"; ok: boolean; text: string; t: number | null };

let eventKey = 0;
const MAX_EVENTS = 30;

function short(obj: Record<string, unknown>, keys: string[]): string {
  const parts: string[] = [];
  for (const k of keys) {
    const v = obj[k];
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${k}: ${JSON.stringify(v)}`);
  }
  return `{${parts.join(", ")}}`;
}

function pushEvent(s: Store, e: Omit<AtlasEvent, "key">): AtlasEvent[] {
  return [{ ...e, key: ++eventKey }, ...s.events].slice(0, MAX_EVENTS);
}

function upsertById<T extends { _id?: unknown }>(list: T[], doc: T): T[] {
  const id = idOf(doc._id);
  if (id != null) {
    const i = list.findIndex((x) => idOf(x._id) === id);
    if (i >= 0) {
      const copy = list.slice();
      copy[i] = doc;
      return copy;
    }
  }
  return [...list, doc];
}

function byTime<T extends { t: unknown }>(a: T, b: T) {
  return (toMs(a.t as never) ?? 0) - (toMs(b.t as never) ?? 0);
}

function stateKey(doc: StateDoc): { coll: "flight_state" | "runway_status"; key: string } {
  if (typeof doc.callsign === "string") return { coll: "flight_state", key: normCallsign(doc.callsign) };
  return { coll: "runway_status", key: String(doc.runway ?? doc._id ?? "?") };
}

function advanceClock(s: Store, t: number | null): Store["clock"] {
  if (t == null) return s.clock;
  if (s.clock.t != null && t < s.clock.t) return s.clock;
  return { t, wall: Date.now(), running: s.clock.running };
}

function applyMsg(s: Store, msg: FeedMsg): Store {
  switch (msg.type) {
    case "transmission": {
      const d = msg.doc;
      const t = toMs(d.t);
      return {
        ...s,
        transmissions: upsertById(s.transmissions, d).sort(byTime),
        clock: advanceClock(s, t),
        events: pushEvent(s, {
          collection: "transmissions", op: "insert", t,
          body: short(d as unknown as Record<string, unknown>, ["from", "to", "instruction", "runway", "at", "confidence"]),
        }),
      };
    }
    case "decision": {
      const d = msg.doc;
      const t = toMs(d.t);
      const tone = d.outcome === "executed" && (d.action === "stop" || d.auto_alert === "STOP")
        ? "red" : d.outcome === "executed" ? "green" : "neutral";
      return {
        ...s,
        decisions: upsertById(s.decisions, d).sort(byTime),
        clock: advanceClock(s, t),
        events: pushEvent(s, {
          collection: "decisions", op: "insert", t, tone,
          body: short(d as unknown as Record<string, unknown>, ["agent", "action", "outcome", "rule", "reason", "approved_by"]),
        }),
      };
    }
    case "permission": {
      const p = msg.doc;
      const t = toMs(p.changed_at);
      return {
        ...s,
        permissions: { ...s.permissions, [p.agent]: p },
        events: pushEvent(s, {
          collection: "permissions", op: "update", t, tone: p.enabled ? "green" : "neutral",
          body: short(p as unknown as Record<string, unknown>, ["agent", "enabled", "by"]),
        }),
      };
    }
    case "state": {
      const d = msg.doc;
      const { coll, key } = stateKey(d);
      const t = toMs((d.t ?? d.updated_at) as never);
      const blocked = String(d.state ?? d.status ?? "").includes("block");
      // the live runner moves AC8646/TRUCK1 once a scenario second: don't let
      // position-only updates flood the Atlas strip
      const prev = coll === "flight_state" ? s.flightState[key] : undefined;
      const quiet = coll === "flight_state" && prev !== undefined && prev.hold_short === d.hold_short
        && prev.current_clearance === d.current_clearance;
      return {
        ...s,
        flightState: coll === "flight_state" ? { ...s.flightState, [key]: d } : s.flightState,
        runwayStatus: coll === "runway_status" ? { ...s.runwayStatus, [key]: d } : s.runwayStatus,
        clock: advanceClock(s, t),
        events: quiet ? s.events : pushEvent(s, {
          collection: coll, op: "update", t, tone: blocked ? "red" : "neutral",
          body: short(d, coll === "flight_state"
            ? ["callsign", "status", "clearance", "runway", "hold_short"]
            : ["runway", "state", "status", "landing", "crossing", "reason"]),
        }),
      };
    }
    case "snapshot": {
      const snap = msg.doc ?? {};
      const permissions: Record<string, Permission> = {};
      for (const p of snap.permissions ?? []) permissions[p.agent] = p;
      const flightState: Record<string, StateDoc> = {};
      for (const f of snap.flight_state ?? []) flightState[stateKey(f).key] = f;
      const runwayStatus: Record<string, StateDoc> = {};
      for (const r of snap.runway_status ?? []) runwayStatus[stateKey(r).key] = r;
      const transmissions = [...(snap.transmissions ?? [])].sort(byTime);
      const decisions = [...(snap.decisions ?? [])].sort(byTime);
      const times = [...transmissions, ...decisions].map((x) => toMs(x.t)).filter((x): x is number => x != null);
      const t = toMs(snap.t) ?? (times.length ? Math.max(...times) : null);
      return {
        ...s, transmissions, decisions, permissions, flightState, runwayStatus,
        clock: t != null ? { t, wall: Date.now(), running: s.clock.running } : s.clock,
        events: pushEvent(s, {
          collection: "snapshot", op: "load", t,
          body: `{transmissions: ${transmissions.length}, decisions: ${decisions.length}, permissions: ${Object.keys(permissions).length}}`,
        }),
      };
    }
    case "rogue":
      return s;
  }
  return s;
}

export function reducer(s: Store, a: StoreAction): Store {
  switch (a.kind) {
    case "msg":
      return applyMsg(s, a.msg);
    case "reset": {
      const fresh = initialStore();
      // permissions survive a scenario reset (they live in Atlas, not the run)
      return { ...fresh, permissions: s.permissions };
    }
    case "clock":
      return { ...s, clock: { t: a.t, wall: Date.now(), running: a.running } };
    case "rogue":
      return {
        ...s,
        rogue: [{ key: ++eventKey, t: a.t, ok: a.ok, text: a.text }, ...s.rogue].slice(0, 5),
        events: pushEvent(s, {
          collection: "intents_safety", op: a.ok ? "insert" : "insert ✗ rejected",
          t: a.t, tone: a.ok ? "neutral" : "red",
          body: a.text,
        }),
      };
  }
}
