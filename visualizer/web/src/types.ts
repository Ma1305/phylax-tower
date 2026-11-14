// Shapes from docs/v2/CONTRACTS.md. Names and fields follow the contract exactly;
// anything marked optional here is tolerated but never required.

export type LaneKind = 'runway' | 'taxiway' | 'apron';
export type LaneState = 'clear' | 'reserved' | 'occupied' | 'conflict' | 'closed';
export type ObjectKind = 'aircraft' | 'vehicle' | 'person' | 'unknown';
export type ObjectStatus = 'airborne' | 'ground';
export type Readback = 'matched' | 'mismatch' | 'missing' | 'not_applicable';
export type PermissionAgent = 'listener' | 'safety' | 'procedures' | 'weather' | 'system';
export type AgentMode = 'reactive' | 'proactive';
export type PermissionTier = 'autonomous' | 'needs_approval' | 'never';
export type Severity = 'info' | 'caution' | 'warning' | 'critical';
export type DecisionOutcome = 'executed' | 'rejected' | 'pending_approval' | 'revoked';

export type LonLat = [number, number];
export interface GeoPoint {
  type: 'Point';
  coordinates: number[];
}

export interface Tower {
  _id: string;
  name: string;
  icao: string;
  center: GeoPoint;
}

export interface Lane {
  _id: string;
  kind: LaneKind;
  name: string;
  designators: string[];
  width_m: number | null;
  centerline: { type: 'LineString'; coordinates: number[][] } | null;
  area?: { type: 'Polygon'; coordinates: number[][][] } | null;
  crosses: string[];
}

export interface HoldPoint {
  _id: string;
  lane_id: string;
  protects: string;
  position: GeoPoint;
}

export interface Controller {
  _id: string;
  name: string;
  role: 'local' | 'ground' | 'supervisor';
  lanes: string[];
  streams: string[];
  active: boolean;
}

export interface Stream {
  _id: string;
  name: string;
  kind: string;
  frequency_mhz: number | null;
  controller_id: string;
  station: string;
}

export interface World {
  tower: Tower;
  lanes: Lane[];
  hold_points: HoldPoint[];
  controllers: Controller[];
  streams: Stream[];
}

export interface ObjectDoc {
  _id: string;
  kind: ObjectKind;
  callsign: string | null;
  label: string | null;
  status: ObjectStatus;
  position: GeoPoint;
  alt_ft: number | null;
  heading_deg: number | null;
  speed_kt: number | null;
  lane_id: string | null;
  clearance?: { instruction: string; lane_id: string | null } | null;
  hold_short?: boolean;
  updated_at?: string;
}

export interface Reservation {
  object_id: string | null;
  callsign: string;
  instruction: string;
  since?: string;
}

export interface LaneStatus {
  _id: string;
  controller_id?: string;
  state: LaneState;
  occupants: string[];
  reservations: Reservation[];
  reason: string | null;
  updated_at?: string;
}

export interface Permission {
  _id: string;
  agent: PermissionAgent;
  enabled: boolean;
  mode: AgentMode;
  tier: PermissionTier;
  by?: string | null;
  changed_at?: string;
}

/** agent_state: which database identity a tower process connected with. */
export interface AgentState {
  _id: string; // process name: "safety_agent", "listener", ...
  identity?: { kind: 'generated' | 'static' | 'shared'; username?: string | null; role?: string | null; expires_at?: string | null } | null;
  updated_at?: string;
}

/** GET /control/audit?q=...: the audit agent's answer. */
export interface AuditItem {
  t: string;
  kind: string;
  actor: string | null;
  summary: string;
  lane_id?: string | null;
  controller_id?: string | null;
  rule?: string | null;
  authorized_by?: { permission_id: string; rule_set_version: string | null } | null;
  approved_by?: string | null;
  doc_id: string;
}
export interface AuditAnswer {
  narrative: string;
  timeline: AuditItem[];
  cited: string[];
  source?: string;
}

export interface Transmission {
  _id: string;
  stream_id: string;
  controller_id?: string;
  t: string;
  t_end?: string;
  direction: 'in' | 'out';
  from: string;
  to: string;
  kind: string;
  instruction: string;
  runway?: string | null;
  at?: string | null;
  lane_id?: string | null;
  transcript: string;
  confidence: number;
  readback: Readback;
  verified: boolean;
  flag: string | null;
  clip_id: string | null;
  audio_id: string | null;
  synthetic: boolean;
  decision_id?: string | null;
}

export interface Decision {
  _id: string;
  t: string;
  intent_id: string;
  agent: string;
  intent_action: string;
  severity: Severity;
  outcome: DecisionOutcome;
  rule: string;
  lane_id: string | null;
  controller_id: string;
  stream_id: string | null;
  text: string | null;
  target: string[];
  mode: AgentMode;
  authorized_by: { permission_id: string; rule_set_version: string | null } | null;
  approved_by: string | null;
  revokes?: string | null;
  notified?: string[];
  /** The decision's intent, merged in by the API (as in /control/decisions). */
  intent?: Intent | null;
}

export interface Intent {
  action?: string;
  severity?: Severity;
  target?: string[];
  lane_id?: string | null;
  stream_id?: string | null;
  reason?: string | null;
  conflicts_with?: string | null;
  proposal_text?: string | null;
  detail?: string | null;
}

export interface Weather {
  t: string;
  visibility_sm: number | null;
  wind: string | null;
  raw?: string | null;
}

export interface Snapshot {
  t: string;
  live: boolean;
  objects: ObjectDoc[];
  lane_status: LaneStatus[];
  permissions: Permission[];
  agent_state?: AgentState[];
  weather: Weather | null;
  pending: Decision[];
  decisions: Decision[];
  transmissions: Transmission[];
}

/** [t, lon, lat, alt_ft, heading] */
export type TrackPoint = [string, number, number, number | null, number | null];
export interface Tracks {
  objects: Record<string, { kind: ObjectKind; callsign: string | null; points: TrackPoint[] }>;
}

export interface Mark {
  t: string;
  type: 'transmission' | 'decision' | 'alert' | 'run' | 'permission';
  stream_id: string | null;
  severity: Severity | null;
  label: string;
}

export interface Clip {
  clip_id: string;
  t_start: string;
  t_end: string;
  duration_s: number;
  direction: 'in' | 'out';
  has_audio: boolean;
  transmission: Transmission | null;
}

export interface LiveMsg {
  type:
    | 'snapshot'
    | 'object'
    | 'object_removed'
    | 'lane_status'
    | 'transmission'
    | 'clip'
    | 'decision'
    | 'permission'
    | 'weather';
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  doc: any;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface PermissionPatch {
  by: string;
  enabled?: boolean;
  mode?: AgentMode;
  tier?: PermissionTier;
}

export interface DataSource {
  mock: boolean;
  world(): Promise<World>;
  bounds(): Promise<{ from: string; to: string }>;
  snapshot(t?: string): Promise<Snapshot>;
  tracks(from: string, to: string, stepS: number): Promise<Tracks>;
  timeline(from: string, to: string): Promise<{ marks: Mark[] }>;
  clips(streamId: string, from: string, to: string): Promise<{ clips: Clip[] }>;
  audioUrl(clipId: string): string;
  live(onMsg: (m: LiveMsg) => void, onStatus: (s: 'open' | 'closed') => void): () => void;
  approve(decisionId: string, controllerId: string): Promise<void>;
  reject(decisionId: string, controllerId: string, reason?: string): Promise<void>;
  /** Lift an executed stop. */
  revoke(decisionId: string, controllerId: string): Promise<void>;
  setPermission(agent: PermissionAgent, patch: PermissionPatch): Promise<void>;
  /** Ask the tower's audit agent (GET /control/audit?q=). */
  audit(q: string): Promise<AuditAnswer>;
}
