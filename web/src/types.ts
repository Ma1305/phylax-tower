// Shapes of the documents the API forwards from MongoDB Atlas.
// Field names follow HOLD_SHORT_PLAN.md (holdshort/contracts.py is the source
// of truth on the backend). Everything is optional-tolerant on purpose: the UI
// must never crash because one field is missing.

export type AgentName = "listener" | "weather" | "safety" | "procedures" | "system";
export const AGENTS: AgentName[] = ["listener", "weather", "safety", "procedures"];

export type AnyDate = string | { $date: string | number } | number | null | undefined;

export interface Transmission {
  _id?: unknown;
  t: AnyDate;
  from: string;
  to?: string | null;
  kind?: "clearance" | "request" | "readback" | "stop" | "other";
  instruction?:
    | "land" | "takeoff" | "cross" | "hold_short" | "line_up_wait"
    | "taxi" | "go_around" | "none";
  runway?: string | null;
  at?: string | null;
  transcript?: string; // cleaned text
  raw_transcript?: string | null; // raw STT before cleanup (if present)
  confidence?: number;
  readback?: string | null; // matched | mismatch | missing | ...
  verified?: boolean;
  flags?: string[];
  audio_ref?: string | null;
}

export interface AuthorizedBy {
  permission_id?: unknown;
  rule_set_version?: string | number;
}

export interface Decision {
  _id?: unknown;
  t: AnyDate;
  agent: string;
  action?: string; // stop | propose_instruction | close_runway | reopen_runway | set_rule_set
  outcome?: "executed" | "rejected" | "pending_approval" | "revoked";
  rule?: string | null;
  reason?: string | null;
  runway?: string | null;
  conflicts_with?: string | null;
  auto_alert?: string | null;
  notified?: string[];
  text?: string | null;
  instruction?: Record<string, unknown> | string | null;
  authorized_by?: AuthorizedBy | null;
  approved_by?: string | null;
  intent_id?: unknown;
  decision_id?: unknown; // for revoke: the decision it points at
  revokes?: unknown;
}

export interface Permission {
  _id?: unknown;
  agent: string;
  enabled: boolean;
  tier?: "autonomous" | "needs_approval" | "never";
  by?: string;
  changed_at?: AnyDate;
}

/** flight_state or runway_status document. */
export type StateDoc = Record<string, unknown>;

export type FeedMsg =
  | { type: "transmission"; doc: Transmission }
  | { type: "decision"; doc: Decision }
  | { type: "permission"; doc: Permission }
  | { type: "state"; doc: StateDoc }
  | { type: "snapshot"; doc: Snapshot }
  | { type: "rogue"; doc: Record<string, unknown> };

export interface Snapshot {
  transmissions?: Transmission[];
  decisions?: Decision[];
  permissions?: Permission[];
  flight_state?: StateDoc[];
  runway_status?: StateDoc[];
  t?: AnyDate;
}

export interface AtlasEvent {
  key: number;
  collection: string;
  op: string;
  t: number | null;
  body: string;
  tone?: "red" | "green" | "neutral";
}

export function toMs(d: AnyDate): number | null {
  if (d == null) return null;
  if (typeof d === "number") return d;
  if (typeof d === "string") {
    const ms = Date.parse(d);
    return Number.isNaN(ms) ? null : ms;
  }
  if (typeof d === "object" && "$date" in d) return toMs(d.$date as string | number);
  return null;
}

export function idOf(x: unknown): string | null {
  if (x == null) return null;
  if (typeof x === "string") return x;
  if (typeof x === "object" && x && "$oid" in x) return String((x as { $oid: string }).$oid);
  return String(x);
}

export function fmtTime(ms: number | null): string {
  if (ms == null) return "--:--:--";
  return new Date(ms).toISOString().slice(11, 19);
}

/** "TRUCK 1" -> "TRUCK1" (contracts: uppercase, no spaces). */
export function normCallsign(s: string | null | undefined): string {
  return (s ?? "").toUpperCase().replace(/\s+/g, "");
}
