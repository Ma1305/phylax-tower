// Turns the event log (transmissions + decisions) and the scenario clock into
// positions on the map. Pure functions: same inputs, same picture, so rewinding
// is just "clear the log and play again".
import type { Decision, Transmission } from "./types";
import { normCallsign, toMs } from "./types";

// ---- Map geometry (SVG units, viewBox 0 0 1000 720, y grows downward) ----
type P = { x: number; y: number };
const add = (a: P, b: P, k = 1): P => ({ x: a.x + b.x * k, y: a.y + b.y * k });

/** Runway 04 heading ~044°: unit vector toward the 22 end. */
export const RWY04_DIR: P = { x: 0.695, y: -0.719 };
/** Perpendicular, pointing south-east (where taxiway D's hold-short line is). */
export const SE: P = { x: 0.719, y: 0.695 };
export const RWY04_THR: P = { x: 300, y: 590 };
export const RWY04_LEN = 560;
export const RWY04_END = add(RWY04_THR, RWY04_DIR, RWY04_LEN);
export const RWY_HALF_W = 14;

/** Runway 13/31, heading ~134°. */
export const RWY13_THR: P = { x: 418, y: 128 };
export const RWY13_DIR: P = { x: 0.719, y: 0.695 };
export const RWY13_LEN = 400;
export const RWY13_END = add(RWY13_THR, RWY13_DIR, RWY13_LEN);

/** Taxiway D crosses runway 04 at this distance from the 04 threshold. */
export const D_STATION = 200;
export const D_CROSS = add(RWY04_THR, RWY04_DIR, D_STATION);
export const D_FROM = add(D_CROSS, SE, -170);
export const D_TO = add(D_CROSS, SE, 190);
/** Hold-short line distance from runway centerline. */
export const HOLD_DIST = 40;
export const HOLD_LINE = add(D_CROSS, SE, HOLD_DIST);

export const pointOn04 = (s: number) => add(RWY04_THR, RWY04_DIR, s);
export const pointOnD = (d: number) => add(D_CROSS, SE, d); // d>0: SE side

// ---- Timeline ----
/** Seconds from the landing clearance until the jet passes taxiway D
 *  (23:35:30 -> 23:37:08 in the reconstruction). */
export const LANDING_TO_D_S = 98;
const APPROACH_SPEED = 14; // units / s on final
const CONVOY_SPEED = 2.6; // creeping across on the original clearance
const APPROVED_SPEED = 7; // crossing after approval, runway is clear
const TRUCK_GAP = 22;
const TRUCK_COUNT = 3;
const CLEARED_DIST = HOLD_DIST * 2 + TRUCK_GAP * TRUCK_COUNT + 10;

export const TRUCKS = ["TRUCK1", "TRUCK2", "TRUCK3"];

export interface Timeline {
  landingT: number | null;
  passDT: number | null; // jet passes D
  crossClearT: number | null;
  stopT: number | null;
  stopDecision: Decision | null;
  stopLiftedT: number | null;
  approveT: number | null;
  resumeT: number | null;
}

const mentions = (d: Decision, cs: string) =>
  (d.notified ?? []).map(normCallsign).includes(cs) ||
  JSON.stringify(d.instruction ?? d.text ?? "").toUpperCase().replace(/\s+/g, "").includes(cs);

export function isStop(d: Decision): boolean {
  return d.action === "stop" || (d.auto_alert ?? "").toUpperCase() === "STOP";
}

/** Live scenario (scenarios/laguardia/script.json): landing clearance at +10 s,
 *  conflict_offset_s 73 -> the jet reaches D 63 s after its landing clearance. */
export const LIVE_LANDING_TO_D_S = 63;

export function buildTimeline(tx: Transmission[], decisions: Decision[], landingToD = LANDING_TO_D_S): Timeline {
  const landing = tx.find(
    (x) => x.instruction === "land" && x.kind !== "readback" && normCallsign(x.to) === "AC8646",
  );
  const cross = tx.find(
    (x) => x.instruction === "cross" && x.kind === "clearance" && normCallsign(x.to).startsWith("TRUCK1"),
  );
  const stop = decisions.find((d) => d.outcome === "executed" && isStop(d) && mentions(d, "TRUCK1")) ?? null;
  const stopId = stop?._id != null ? JSON.stringify(stop._id) : null;
  const revoke = decisions.find(
    (d) => d.outcome === "revoked" && stopId != null &&
      (JSON.stringify(d.decision_id) === stopId || JSON.stringify(d.revokes) === stopId),
  );
  const approve = decisions.find(
    (d) => d.outcome === "executed" && d.action === "propose_instruction" && !!d.approved_by && mentions(d, "TRUCK1"),
  );
  const landingT = landing ? toMs(landing.t) : null;
  const passDT = landingT != null ? landingT + landingToD * 1000 : null;
  const stopT = stop ? toMs(stop.t) : null;
  const stopLiftedT = revoke ? toMs(revoke.t) : null;
  const approveT = approve ? toMs(approve.t) : null;
  const candidates = [approveT, stopLiftedT].filter((x): x is number => x != null);
  let resumeT = candidates.length ? Math.min(...candidates) : null;
  // never send the convoy across in front of the landing jet
  if (resumeT != null && passDT != null) resumeT = Math.max(resumeT, passDT + 2500);
  return {
    landingT, passDT, crossClearT: cross ? toMs(cross.t) : null,
    stopT, stopDecision: stop, stopLiftedT, approveT, resumeT,
  };
}

// ---- Positions ----
/** Jet distance along runway 04 axis (negative = on final). */
function jetStation(tl: Timeline, now: number): number | null {
  if (tl.passDT == null) return null;
  const dt = (now - tl.passDT) / 1000;
  if (dt <= 0) return D_STATION + APPROACH_SPEED * dt;
  // landing roll: decelerate to taxi speed, stop near the far end
  const decel = 0.3;
  const vEnd = 3;
  const tDecel = (APPROACH_SPEED - vEnd) / decel;
  const s = dt < tDecel
    ? APPROACH_SPEED * dt - 0.5 * decel * dt * dt
    : APPROACH_SPEED * tDecel - 0.5 * decel * tDecel * tDecel + vEnd * (dt - tDecel);
  return Math.min(D_STATION + s, RWY04_LEN - 40);
}

/** How far the lead truck has moved from the hold-short line toward the NW. */
function convoyProgress(tl: Timeline, now: number): number {
  if (tl.crossClearT == null) return 0;
  const go = tl.crossClearT + 2000; // readback, then roll
  let d = 0;
  const firstEnd = tl.stopT != null ? Math.min(tl.stopT + 600, now) : now;
  if (firstEnd > go) d += ((firstEnd - go) / 1000) * CONVOY_SPEED;
  if (tl.stopT != null && tl.resumeT != null && now > tl.resumeT) {
    d += ((now - tl.resumeT) / 1000) * APPROVED_SPEED;
  }
  return Math.min(d, CLEARED_DIST);
}

export type Phase = "idle" | "conflict" | "frozen" | "held" | "landed" | "crossed";

export interface Scene {
  t: number | null;
  jet: { p: P; s: number; label: string } | null;
  trucks: { id: string; p: P }[];
  truckLabel: string;
  truckTone: "red" | "green" | "neutral";
  lights: "red" | "green" | "off";
  frozen: boolean;
  phase: Phase;
}

const FREEZE_LEAD_MS = 3000;

export function scene(tl: Timeline, now: number | null): Scene {
  let t = now;
  let frozen = false;
  // Without a STOP the convoy is on the runway when the jet arrives: freeze
  // the picture just before contact (no crash imagery, ever).
  if (t != null && tl.passDT != null && tl.stopT == null && tl.crossClearT != null) {
    const freezeAt = tl.passDT - FREEZE_LEAD_MS;
    const onRunway = convoyProgress(tl, freezeAt) > HOLD_DIST - RWY_HALF_W - 4;
    if (onRunway && t >= freezeAt) {
      t = freezeAt;
      frozen = true;
    }
  }

  let jet: Scene["jet"] = null;
  const s = t != null ? jetStation(tl, t) : null;
  if (s != null && s > -400) {
    const label = s < 0 ? "final · runway 04" : s < D_STATION ? "landing · runway 04" : "landed · rolling past D";
    jet = { p: pointOn04(s), s, label };
  }

  const prog = t != null ? convoyProgress(tl, t) : 0;
  const trucks = TRUCKS.map((id, i) => {
    const offset = HOLD_DIST + 11 + i * TRUCK_GAP - prog;
    return { id, p: pointOnD(offset) };
  });

  const stopActive = tl.stopT != null && t != null && t >= tl.stopT && (tl.resumeT == null || t < tl.resumeT);
  const crossingApproved = tl.resumeT != null && t != null && t >= tl.resumeT;
  const crossed = prog >= CLEARED_DIST;

  let truckLabel = "waiting at D";
  let truckTone: Scene["truckTone"] = "neutral";
  let phase: Phase = "idle";
  if (frozen) {
    truckLabel = "on runway 04 · cleared to cross";
    truckTone = "red";
    phase = "frozen";
  } else if (crossed) {
    truckLabel = "crossed runway 04";
    truckTone = "green";
    phase = "crossed";
  } else if (crossingApproved) {
    truckLabel = "crossing · approved";
    truckTone = "green";
    phase = "crossed";
  } else if (stopActive) {
    truckLabel = "holding short at D";
    truckTone = "red";
    phase = jet && jet.s > D_STATION ? "landed" : "held";
  } else if (prog > 0) {
    truckLabel = "crossing runway 04";
    truckTone = "red";
    phase = "conflict";
  }

  const lights = stopActive ? "red" : crossingApproved && !crossed ? "green" : "off";
  return { t, jet, trucks, truckLabel, truckTone, lights, frozen, phase };
}
