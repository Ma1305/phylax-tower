// Mock data source (?mock=1): no backend. A scripted runway-incursion scenario built on
// docs/v2/world_lga.json (copied to ./world_lga.json), expressed as a pure function of
// wall-clock time. Three minutes of it lie in the past when the page loads; it then keeps
// running live and repeats every four minutes. All shapes follow docs/v2/CONTRACTS.md.

import worldJson from './world_lga.json';
import { toneUrl } from './tone';
import { bearing, lerp, makeProjection } from '../geo';
import {
  ApiError,
  type Clip,
  type DataSource,
  type Decision,
  type LaneState,
  type LaneStatus,
  type LiveMsg,
  type Mark,
  type ObjectDoc,
  type Permission,
  type PermissionAgent,
  type PermissionPatch,
  type Reservation,
  type Severity,
  type Snapshot,
  type Tracks,
  type Transmission,
  type World,
} from '../types';

const CYCLE_S = 240;
const HISTORY_S = 180;
const P = worldJson.points;
const iso = (ms: number) => new Date(ms).toISOString();

function buildWorld(): World {
  const { toM, toLonLat } = makeProjection(worldJson.tower.center.coordinates);
  const lanes = worldJson.lanes.map((l) => {
    let area = (l as { area?: World['lanes'][number]['area'] }).area ?? null;
    if (!area && l.centerline && l.width_m) {
      const [a, b] = l.centerline.coordinates.map((c) => toM(c));
      const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const nx = (-(b[1] - a[1]) / len) * (l.width_m / 2);
      const ny = ((b[0] - a[0]) / len) * (l.width_m / 2);
      const ring = [
        [a[0] + nx, a[1] + ny],
        [b[0] + nx, b[1] + ny],
        [b[0] - nx, b[1] - ny],
        [a[0] - nx, a[1] - ny],
        [a[0] + nx, a[1] + ny],
      ].map((p) => toLonLat([p[0], p[1]]));
      area = { type: 'Polygon', coordinates: [ring] };
    }
    return { ...l, area } as unknown as World['lanes'][number];
  });
  return {
    tower: worldJson.tower as World['tower'],
    lanes,
    hold_points: worldJson.hold_points as World['hold_points'],
    controllers: worldJson.controllers as World['controllers'],
    streams: worldJson.streams as World['streams'],
  };
}

interface LaneEvent {
  t: number;
  doc: LaneStatus;
}
interface PermEvent {
  t: number;
  doc: Permission;
}
interface Run {
  t: number;
  label: string;
}
type MockDecision = Decision & { autoFor?: string };

export function createMockSource(): DataSource {
  const world = buildWorld();
  const T0 = Date.now() - HISTORY_S * 1000;
  const owner = (laneId: string) =>
    world.controllers.find((c) => c.lanes.includes(laneId))?._id ?? 'ctl_sup';

  const transmissions: Transmission[] = [];
  const clips = new Map<string, Clip & { stream_id: string }>();
  const decisions: MockDecision[] = [];
  const laneEvents: LaneEvent[] = [];
  const permEvents: PermEvent[] = [];
  const runs: Run[] = [];
  const acted = new Set<string>(); // intents a controller has decided in this session
  let built = 0;
  let seq = 0;
  const listeners = new Set<(m: LiveMsg) => void>();

  const seedPerm = (agent: PermissionAgent, mode: Permission['mode'], tier: Permission['tier']) =>
    permEvents.push({
      t: T0,
      doc: { _id: agent, agent, enabled: true, mode, tier, by: 'seed', changed_at: iso(T0) },
    });
  seedPerm('listener', 'reactive', 'autonomous');
  seedPerm('safety', 'proactive', 'needs_approval');
  seedPerm('procedures', 'proactive', 'needs_approval');
  seedPerm('weather', 'proactive', 'autonomous');
  seedPerm('system', 'proactive', 'needs_approval');

  function addTx(
    tMs: number,
    stream_id: string,
    from: string,
    to: string,
    transcript: string,
    o: Partial<Transmission> & { dur?: number; audio?: boolean } = {},
  ) {
    const id = `tx_${++seq}`;
    const dur = o.dur ?? Math.max(1.6, transcript.length * 0.06);
    const audio = o.audio ?? true;
    const confidence = o.confidence ?? 0.95;
    const tx: Transmission = {
      _id: id,
      stream_id,
      controller_id: world.streams.find((s) => s._id === stream_id)?.controller_id,
      t: iso(tMs),
      t_end: iso(tMs + dur * 1000),
      direction: o.synthetic ? 'out' : 'in',
      from,
      to,
      kind: o.kind ?? 'other',
      instruction: o.instruction ?? 'none',
      runway: o.runway ?? null,
      at: o.at ?? null,
      lane_id: o.lane_id ?? null,
      transcript,
      confidence,
      readback: o.readback ?? 'not_applicable',
      verified: confidence >= 0.8,
      flag: o.flag ?? null,
      clip_id: `clip_${seq}`,
      audio_id: audio ? `aud_${seq}` : null,
      synthetic: o.synthetic ?? false,
      decision_id: o.decision_id ?? null,
    };
    transmissions.push(tx);
    clips.set(tx.clip_id!, {
      clip_id: tx.clip_id!,
      stream_id,
      t_start: tx.t,
      t_end: tx.t_end!,
      duration_s: dur,
      direction: tx.direction,
      has_audio: audio,
      transmission: tx,
    });
    return tx;
  }

  function addDecision(tMs: number, d: Partial<MockDecision> & { agent: string; intent_action: string }) {
    const id = `dec_${++seq}`;
    const lane_id = d.lane_id ?? null;
    const doc: MockDecision = {
      _id: id,
      t: iso(tMs),
      intent_id: d.intent_id ?? `int_${seq}`,
      agent: d.agent,
      intent_action: d.intent_action,
      severity: d.severity ?? 'info',
      outcome: d.outcome ?? 'executed',
      rule: d.rule ?? 'advisory',
      lane_id,
      controller_id: d.controller_id ?? (lane_id ? owner(lane_id) : 'ctl_sup'),
      stream_id: d.stream_id ?? null,
      text: d.text ?? null,
      target: d.target ?? [],
      mode: d.mode ?? 'proactive',
      authorized_by: { permission_id: d.agent, rule_set_version: 'standard_v1' },
      approved_by: d.approved_by ?? null,
      revokes: d.revokes ?? null,
      notified: [],
      reason: d.reason ?? null,
      detail: d.detail ?? null,
      autoFor: d.autoFor,
    };
    decisions.push(doc);
    return doc;
  }

  function addLane(
    tMs: number,
    id: string,
    state: LaneState,
    occupants: string[] = [],
    reservations: Reservation[] = [],
    reason: string | null = null,
  ) {
    laneEvents.push({
      t: tMs,
      doc: { _id: id, controller_id: owner(id), state, occupants, reservations, reason, updated_at: iso(tMs) },
    });
  }

  function buildCycle(c: number) {
    const base = T0 + c * CYCLE_S * 1000;
    const at = (s: number) => base + s * 1000;
    const land: Reservation = { object_id: 'AC8646', callsign: 'AC8646', instruction: 'land', since: iso(at(20)) };
    runs.push({ t: base, label: `Simulator run: runway_incursion_demo (incident) #${c + 1}` });

    addLane(at(0), 'RWY_04_22', 'clear');
    addLane(at(0), 'RWY_13_31', 'closed', [], [], 'Closed for surface inspection');
    addLane(at(0), 'TWY_B', 'occupied', ['AC2210']);
    addLane(at(0), 'TWY_D', 'occupied', ['TRUCK1']);
    addLane(at(0), 'TWY_G', 'clear');
    addLane(at(0), 'APRON_MAIN', 'occupied', ['OBJ-17']);

    addTx(at(8), 'LGA_GND', 'GROUND', 'AC2210', 'AC2210, taxi to runway 04 via Bravo, hold short of runway 04.', {
      kind: 'clearance', instruction: 'taxi', runway: '04', at: 'B', lane_id: 'TWY_B', confidence: 0.96,
    });
    addTx(at(14), 'LGA_GND', 'AC2210', 'GROUND', 'Taxi runway 04 via Bravo, hold short runway 04, AC2210.', {
      kind: 'readback', instruction: 'taxi', runway: '04', lane_id: 'TWY_B', confidence: 0.93, readback: 'matched',
    });
    addTx(at(20), 'LGA_TWR', 'TOWER', 'AC8646', 'AC8646, wind 040 at 8, runway 04, cleared to land.', {
      kind: 'clearance', instruction: 'land', runway: '04', lane_id: 'RWY_04_22', confidence: 0.97,
    });
    addLane(at(21), 'RWY_04_22', 'reserved', [], [land]);
    addTx(at(27), 'LGA_TWR', 'AC8646', 'TOWER', 'Cleared to land runway 04, AC8646.', {
      kind: 'readback', instruction: 'land', runway: '04', lane_id: 'RWY_04_22', confidence: 0.95, readback: 'matched',
    });
    addTx(at(35), 'LGA_OPS', 'TRUCK1', 'OPS', 'Ops, Truck 1 at Delta, request to cross runway 04.', {
      kind: 'request', instruction: 'cross', runway: '04', at: 'D', lane_id: 'RWY_04_22', confidence: 0.9,
    });
    addTx(at(42), 'LGA_OPS', 'OPS', 'TRUCK1', 'Truck 1, hold short of runway 04 at Delta, landing traffic.', {
      kind: 'clearance', instruction: 'hold_short', runway: '04', at: 'D', lane_id: 'TWY_D', confidence: 0.94,
    });
    addTx(at(48), 'LGA_OPS', 'TRUCK1', 'OPS', 'Holding ... runway 04 ... Truck 1.', {
      kind: 'readback', instruction: 'hold_short', runway: '04', lane_id: 'TWY_D', confidence: 0.62,
      readback: 'mismatch', flag: 'unclear - say again',
    });
    addDecision(at(52), {
      agent: 'safety', intent_action: 'warn', severity: 'caution', lane_id: 'TWY_D', stream_id: 'LGA_OPS',
      target: ['TRUCK1'], reason: 'readback_mismatch',
      text: 'TRUCK1 readback of "hold short runway 04" was unclear (confidence 0.62).',
      detail: 'The hold-short instruction was not read back in full.',
    });
    const p1 = addDecision(at(58), {
      agent: 'procedures', intent_action: 'propose_instruction', severity: 'caution', outcome: 'pending_approval',
      rule: 'needs_approval', lane_id: 'TWY_D', stream_id: 'LGA_OPS', target: ['TRUCK1'], reason: 'readback_mismatch',
      text: 'Truck 1, say again. Confirm holding short of runway 04 at Delta.',
      detail: 'Readback of the hold-short instruction was unclear; ask for it again before the arrival.',
    });
    const a1 = addDecision(at(72), {
      ...p1, _id: undefined, outcome: 'executed', rule: 'controller_approved', approved_by: 'ctl_ground',
    } as Partial<MockDecision> & { agent: string; intent_action: string });
    addTx(at(74), 'LGA_OPS', 'HOLDSHORT', 'TRUCK1', p1.text!, {
      kind: 'clearance', instruction: 'hold_short', runway: '04', at: 'D', lane_id: 'TWY_D', confidence: 1,
      synthetic: true, decision_id: a1._id,
    });
    addTx(at(82), 'LGA_OPS', 'TRUCK1', 'OPS', 'Holding short of runway 04 at Delta, Truck 1.', {
      kind: 'readback', instruction: 'hold_short', runway: '04', at: 'D', lane_id: 'TWY_D', confidence: 0.92, readback: 'matched',
    });
    addTx(at(100), 'LGA_TWR', 'AC5512', 'TOWER', 'Tower, AC5512, nine mile final runway 04.', {
      kind: 'report', runway: '04', confidence: 0.91,
    });
    addTx(at(106), 'LGA_TWR', 'TOWER', 'AC5512', 'AC5512, number two, continue.', { kind: 'other', confidence: 0.96 });
    addDecision(at(120), {
      agent: 'weather', intent_action: 'warn', severity: 'info', controller_id: 'ctl_sup',
      reason: 'weather_update', text: 'Visibility 6 SM, wind 040 at 8. Standard rule set stays active.',
    });
    addTx(at(160), 'LGA_OPS', 'UNKNOWN', 'OPS', '... crossing ...', {
      kind: 'other', confidence: 0.41, flag: 'no callsign', audio: false, dur: 1.5,
    });
    addLane(at(166), 'RWY_04_22', 'conflict', ['TRUCK1'], [land], 'TRUCK1 on runway 04 while AC8646 is cleared to land');
    addDecision(at(167), {
      agent: 'safety', intent_action: 'warn', severity: 'critical', lane_id: 'RWY_04_22', stream_id: 'LGA_TWR',
      target: ['AC8646', 'TRUCK1'], reason: 'runway_incursion',
      text: 'TRUCK1 is on runway 04 at Delta. AC8646 is cleared to land, 1 nm final.',
      detail: 'A vehicle with no crossing clearance is on a runway reserved for landing.',
    });
    addDecision(at(170), {
      agent: 'safety', intent_action: 'stop', severity: 'critical', outcome: 'pending_approval', rule: 'needs_approval',
      lane_id: 'RWY_04_22', stream_id: 'LGA_TWR', target: ['AC8646'], reason: 'runway_incursion',
      text: 'AC8646, go around. Vehicle on the runway.',
      detail: 'TRUCK1 entered runway 04 at Delta without a clearance while AC8646 is on short final.',
    });
    addDecision(at(174), {
      agent: 'procedures', intent_action: 'propose_instruction', severity: 'warning', outcome: 'pending_approval',
      rule: 'needs_approval', lane_id: 'TWY_D', stream_id: 'LGA_OPS', target: ['TRUCK1'], reason: 'uncleared_entry',
      text: 'Truck 1, vacate runway 04 immediately via Delta.',
      detail: 'TRUCK1 was told to hold short at Delta and has no crossing clearance.',
    });
    addTx(at(188), 'LGA_TWR', 'TOWER', 'AC8646', 'AC8646, go around, vehicle on the runway.', {
      kind: 'clearance', instruction: 'go_around', runway: '04', lane_id: 'RWY_04_22', confidence: 0.97,
    });
    addLane(at(190), 'RWY_04_22', 'occupied', ['TRUCK1']);
    addTx(at(193), 'LGA_TWR', 'AC8646', 'TOWER', 'Going around, AC8646.', {
      kind: 'readback', instruction: 'go_around', runway: '04', confidence: 0.94, readback: 'matched',
    });
    addTx(at(197), 'LGA_OPS', 'OPS', 'TRUCK1', 'Truck 1, vacate runway 04 via Delta immediately.', {
      kind: 'clearance', instruction: 'taxi', at: 'D', lane_id: 'TWY_D', confidence: 0.95,
    });
    addTx(at(202), 'LGA_OPS', 'TRUCK1', 'OPS', 'Vacating runway 04, Truck 1.', {
      kind: 'readback', instruction: 'taxi', lane_id: 'TWY_D', confidence: 0.9, readback: 'matched',
    });
    addLane(at(206), 'RWY_04_22', 'clear');
    // Requests nobody answered are closed by the controller once the runway is clear.
    for (const p of decisions.filter((d) => d.outcome === 'pending_approval' && Date.parse(d.t) >= at(160))) {
      addDecision(at(215), {
        ...p, _id: undefined, outcome: 'revoked', rule: 'controller_rejected', revokes: p._id,
        approved_by: p.controller_id, autoFor: p.intent_id,
      } as Partial<MockDecision> & { agent: string; intent_action: string });
    }
  }

  function ensure(tMs: number) {
    while (T0 + built * CYCLE_S * 1000 <= tMs) buildCycle(built++);
    transmissions.sort((a, b) => a.t.localeCompare(b.t));
    decisions.sort((a, b) => a.t.localeCompare(b.t));
    laneEvents.sort((a, b) => a.t - b.t);
  }

  const visibleDecisions = () => decisions.filter((d) => !(d.autoFor && acted.has(d.autoFor)));

  // ---- object motion: a pure function of time ----
  const final04 = (dNm: number): [number, number] => {
    const f = dNm / 10;
    return [lerp(P.RWY04_THRESHOLD[0], P.FINAL_04_10NM[0], f), lerp(P.RWY04_THRESHOLD[1], P.FINAL_04_10NM[1], f)];
  };
  const finalHdg = bearing(P.FINAL_04_10NM, P.RWY04_THRESHOLD);
  const rwyHdg = bearing(P.RWY04_THRESHOLD, P.RWY22_THRESHOLD);
  const rwyLenNm = 1.19;
  const seg = (a: number[], b: number[], f: number): [number, number] => [lerp(a[0], b[0], f), lerp(a[1], b[1], f)];
  const clamp01 = (x: number) => Math.max(0, Math.min(1, x));

  function objectsAt(tMs: number): ObjectDoc[] {
    if (tMs < T0) return [];
    const s = (((tMs - T0) / 1000) % CYCLE_S + CYCLE_S) % CYCLE_S;
    const mk = (o: Partial<ObjectDoc> & { _id: string; kind: ObjectDoc['kind']; pos: number[] }): ObjectDoc => ({
      _id: o._id,
      kind: o.kind,
      callsign: o.callsign ?? null,
      label: o.label ?? null,
      status: o.status ?? 'ground',
      position: { type: 'Point', coordinates: o.pos },
      alt_ft: o.alt_ft ?? (o.status === 'airborne' ? 0 : 20),
      heading_deg: o.heading_deg ?? null,
      speed_kt: o.speed_kt ?? 0,
      lane_id: o.lane_id ?? null,
      clearance: o.clearance ?? null,
      hold_short: o.hold_short ?? false,
      updated_at: iso(tMs),
    });
    const out: ObjectDoc[] = [];

    // AC8646: on final, goes around at s=186.
    const d1 = 7.5 - (s * 7) / 180;
    const ga = s > 186;
    out.push(
      mk({
        _id: 'AC8646', kind: 'aircraft', callsign: 'AC8646', status: 'airborne',
        pos: d1 >= 0 ? final04(d1) : seg(P.RWY04_THRESHOLD, P.RWY22_THRESHOLD, -d1 / rwyLenNm),
        alt_ft: Math.round(ga ? 85 + (s - 186) * 28 : Math.max(60, d1 * 318)),
        heading_deg: Math.round(d1 >= 0 ? finalHdg : rwyHdg),
        speed_kt: ga ? 152 : 140,
        clearance: s >= 20 && !ga ? { instruction: 'land', lane_id: 'RWY_04_22' } : null,
      }),
    );
    // AC5512: number two, further out.
    const d2 = 12.5 - (s * 6.5) / 180;
    out.push(
      mk({
        _id: 'AC5512', kind: 'aircraft', callsign: 'AC5512', status: 'airborne', pos: final04(d2),
        alt_ft: Math.round(Math.min(3000, d2 * 318)), heading_deg: Math.round(finalHdg), speed_kt: 160,
      }),
    );
    // AC2210: taxiing on Bravo.
    const b = worldJson.lanes.find((l) => l._id === 'TWY_B')!.centerline!.coordinates;
    out.push(
      mk({
        _id: 'AC2210', kind: 'aircraft', callsign: 'AC2210', pos: seg(b[1], b[0], 0.12 + (s / CYCLE_S) * 0.7),
        heading_deg: Math.round(bearing(b[1], b[0])), speed_kt: 14, lane_id: 'TWY_B',
        clearance: s >= 8 ? { instruction: 'taxi', lane_id: 'TWY_B' } : null,
      }),
    );
    // TRUCK1: holds at Delta, then enters the runway.
    const hpNW = worldJson.hold_points.find((h) => h._id === 'HP_D_NW')!.position.coordinates;
    const dSE = worldJson.lanes.find((l) => l._id === 'TWY_D')!.centerline!.coordinates[0];
    let tp: number[];
    let tspd = 0;
    if (s < 10) tp = P.FIRE_STATION;
    else if (s < 40) [tp, tspd] = [seg(P.FIRE_STATION, hpNW, (s - 10) / 30), 12];
    else if (s < 160) tp = hpNW;
    else if (s < 168) [tp, tspd] = [seg(hpNW, P.D_CROSSING, (s - 160) / 8), 10];
    else if (s < 200) tp = P.D_CROSSING;
    else [tp, tspd] = [seg(P.D_CROSSING, dSE, clamp01((s - 200) / 15)), s < 215 ? 18 : 0];
    out.push(
      mk({
        _id: 'TRUCK1', kind: 'vehicle', callsign: 'TRUCK1', label: 'Fire truck', pos: tp,
        heading_deg: Math.round(bearing(P.FIRE_STATION, dSE)), speed_kt: tspd,
        lane_id: s >= 164 && s < 204 ? 'RWY_04_22' : 'TWY_D', hold_short: s >= 42 && s < 160,
      }),
    );
    // A person on the apron, and an unidentified target near Golf.
    const pa = [-73.8742, 40.7718];
    const pb = [-73.8722, 40.7738];
    out.push(
      mk({
        _id: 'OBJ-17', kind: 'person', label: 'Person on foot', pos: seg(pa, pb, s / CYCLE_S),
        heading_deg: Math.round(bearing(pa, pb)), speed_kt: 3, lane_id: 'APRON_MAIN',
      }),
    );
    if (s >= 100) out.push(mk({ _id: 'OBJ-23', kind: 'unknown', label: 'Unidentified', pos: [-73.8688, 40.7712] }));
    return out;
  }

  function lastPer<T extends { t: number; doc: { _id: string } }>(events: T[], tMs: number): T['doc'][] {
    const m = new Map<string, T['doc']>();
    for (const e of events) if (e.t <= tMs) m.set(e.doc._id, e.doc);
    return [...m.values()];
  }

  function pendingAt(tMs: number): Decision[] {
    const upTo = visibleDecisions().filter((d) => Date.parse(d.t) <= tMs);
    return upTo.filter(
      (d) => d.outcome === 'pending_approval' && !upTo.some((o) => o.intent_id === d.intent_id && o.t > d.t),
    );
  }

  function snapshotAt(tMs: number, live: boolean): Snapshot {
    ensure(tMs);
    const upTo = <T extends { t: string }>(a: T[]) => a.filter((x) => Date.parse(x.t) <= tMs);
    return {
      t: iso(tMs),
      live,
      objects: objectsAt(tMs),
      lane_status: lastPer(laneEvents, tMs),
      permissions: lastPer(permEvents, tMs),
      weather: { t: iso(T0), visibility_sm: 6, wind: '040 at 8', raw: null },
      pending: pendingAt(tMs),
      decisions: upTo(visibleDecisions()).slice(-50),
      transmissions: upTo(transmissions).slice(-100),
    };
  }

  // ---- live emitter ----
  let lastEmit = Date.now();
  let timer: number | undefined;
  const emit = (m: LiveMsg) => listeners.forEach((l) => l(m));
  function tick() {
    const now = Date.now();
    ensure(now);
    const within = (t: number) => t > lastEmit && t <= now;
    for (const o of objectsAt(now)) emit({ type: 'object', doc: o });
    const prevIds = new Set(objectsAt(lastEmit).map((o) => o._id));
    const nowIds = new Set(objectsAt(now).map((o) => o._id));
    prevIds.forEach((id) => !nowIds.has(id) && emit({ type: 'object_removed', doc: { _id: id } }));
    for (const e of laneEvents) if (within(e.t)) emit({ type: 'lane_status', doc: e.doc });
    for (const e of permEvents) if (within(e.t)) emit({ type: 'permission', doc: e.doc });
    for (const d of visibleDecisions()) if (within(Date.parse(d.t))) emit({ type: 'decision', doc: d });
    for (const x of transmissions) if (within(Date.parse(x.t))) emit({ type: 'transmission', doc: x });
    lastEmit = now;
  }

  function decide(decisionId: string, controllerId: string, approve: boolean) {
    const now = Date.now();
    ensure(now);
    const d = decisions.find((x) => x._id === decisionId);
    if (!d) throw new ApiError(404, 'unknown decision');
    const ctl = world.controllers.find((c) => c._id === controllerId);
    if (!ctl) throw new ApiError(400, 'unknown controller');
    if (!pendingAt(now).some((p) => p._id === decisionId)) throw new ApiError(409, 'already decided');
    if (ctl.role !== 'supervisor' && d.controller_id !== controllerId) {
      throw new ApiError(
        403,
        `not_lane_owner: ${ctl.name} does not own ${d.lane_id ?? 'this lane'}; it is routed to ${d.controller_id}`,
      );
    }
    acted.add(d.intent_id);
    const res = addDecision(now + 1, {
      ...d, _id: undefined, autoFor: undefined,
      outcome: approve ? 'executed' : 'revoked',
      rule: approve ? 'controller_approved' : 'controller_rejected',
      approved_by: controllerId, revokes: approve ? null : d._id,
    } as Partial<MockDecision> & { agent: string; intent_action: string });
    if (approve && d.text && d.stream_id) {
      addTx(now + 1500, d.stream_id, 'HOLDSHORT', d.target[0] ?? 'ALL', d.text, {
        kind: d.intent_action === 'stop' ? 'stop' : 'clearance', lane_id: d.lane_id, confidence: 1,
        synthetic: true, decision_id: res._id,
      });
    }
  }

  const delay = <T,>(v: T) => new Promise<T>((r) => setTimeout(() => r(v), 30));

  return {
    mock: true,
    world: () => delay(world),
    bounds: () => delay({ from: iso(T0), to: iso(Date.now()) }),
    snapshot: (t) => delay(snapshotAt(t ? Date.parse(t) : Date.now(), !t)),
    tracks(from, to, stepS) {
      const a = Math.max(T0, Date.parse(from));
      const b = Math.min(Date.now(), Date.parse(to));
      const out: Tracks = { objects: {} };
      for (let t = a; t <= b; t += stepS * 1000) {
        for (const o of objectsAt(t)) {
          const e = (out.objects[o._id] ??= { kind: o.kind, callsign: o.callsign, points: [] });
          e.points.push([iso(t), o.position.coordinates[0], o.position.coordinates[1], o.alt_ft, o.heading_deg]);
        }
      }
      return delay(out);
    },
    timeline(from, to) {
      const a = Date.parse(from);
      const b = Math.min(Date.now(), Date.parse(to));
      ensure(b);
      const inR = (t: number) => t >= a && t <= b;
      const marks: Mark[] = [];
      for (const r of runs) if (inR(r.t)) marks.push({ t: iso(r.t), type: 'run', stream_id: null, severity: null, label: r.label });
      for (const x of transmissions)
        if (inR(Date.parse(x.t)))
          marks.push({ t: x.t, type: 'transmission', stream_id: x.stream_id, severity: null, label: `${x.from} -> ${x.to}: ${x.transcript}` });
      for (const d of visibleDecisions())
        if (inR(Date.parse(d.t)))
          marks.push({
            t: d.t, type: d.intent_action === 'warn' ? 'alert' : 'decision', stream_id: d.stream_id,
            severity: d.severity as Severity, label: `${d.agent} ${d.intent_action} (${d.outcome}): ${d.text ?? d.rule}`,
          });
      for (const e of permEvents)
        if (e.t > T0 && inR(e.t))
          marks.push({ t: iso(e.t), type: 'permission', stream_id: null, severity: null, label: `${e.doc.agent} switch changed by ${e.doc.by}` });
      return delay({ marks: marks.sort((x, y) => x.t.localeCompare(y.t)) });
    },
    clips(streamId, from, to) {
      const b = Math.min(Date.now(), Date.parse(to));
      ensure(b);
      const list = [...clips.values()].filter(
        (c) => c.stream_id === streamId && Date.parse(c.t_start) >= Date.parse(from) && Date.parse(c.t_start) <= b,
      );
      return delay({ clips: list.sort((x, y) => x.t_start.localeCompare(y.t_start)) });
    },
    audioUrl(clipId) {
      const c = clips.get(clipId);
      const hz = c?.transmission?.synthetic ? 660 : c?.stream_id === 'LGA_TWR' ? 500 : c?.stream_id === 'LGA_GND' ? 410 : 340;
      return toneUrl(clipId, c?.duration_s ?? 2, hz);
    },
    live(onMsg, onStatus) {
      listeners.add(onMsg);
      const start = setTimeout(() => {
        onStatus('open');
        lastEmit = Date.now();
        onMsg({ type: 'snapshot', doc: snapshotAt(Date.now(), true) });
        if (timer === undefined) timer = window.setInterval(tick, 500);
      }, 60);
      return () => {
        clearTimeout(start);
        listeners.delete(onMsg);
        if (listeners.size === 0 && timer !== undefined) {
          clearInterval(timer);
          timer = undefined;
        }
      };
    },
    async approve(id, by) {
      decide(id, by, true);
    },
    async reject(id, by) {
      decide(id, by, false);
    },
    async setPermission(agent: PermissionAgent, patch: PermissionPatch) {
      const now = Date.now();
      const cur = lastPer(permEvents, now).find((p) => p._id === agent);
      if (!cur) throw new ApiError(404, 'unknown agent');
      const { by, ...rest } = patch;
      permEvents.push({ t: now + 1, doc: { ...cur, ...rest, by, changed_at: iso(now) } });
    },
  };
}
