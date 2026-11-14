// Two data sources behind one interface:
//  - LiveFeed: WebSocket ws://localhost:8000/ws + REST POSTs to the API.
//  - MockFeed: replays src/mock/laguardia.json in the browser (?mock=1) and
//    answers the same controls locally, so the demo runs with no backend.
import mockScript from "./mock/laguardia.json";
import type { Decision, FeedMsg, Permission } from "./types";
import { idOf, toMs } from "./types";

// Override per page load with ?api=http://host:port&ws=ws://host:port/ws
const q = new URLSearchParams(window.location.search);
export const API_BASE: string = q.get("api") ?? import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
export const WS_URL: string = q.get("ws") ?? import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws";

export interface FeedHandlers {
  onMsg: (m: FeedMsg) => void;
  onClock: (t: number | null, running: boolean) => void;
  onStatus: (s: string) => void;
  onReset: () => void;
  onRogue: (ok: boolean, text: string) => void;
}

export interface Feed {
  setPermission(agent: string, enabled: boolean): Promise<void>;
  approve(d: Decision): Promise<void>;
  reject(d: Decision): Promise<void>;
  revoke(d: Decision): Promise<void>;
  play(): Promise<void>;
  reset(): Promise<void>;
  rogue(): Promise<void>;
  close(): void;
}

const CONTROLLER = "controller_A";

async function post(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown = text;
  try { data = JSON.parse(text); } catch { /* plain text */ }
  if (!res.ok) throw Object.assign(new Error(`${path} -> HTTP ${res.status}`), { data });
  return data;
}

// --------------------------------------------------------------- live ----
export class LiveFeed implements Feed {
  private ws: WebSocket | null = null;
  private closed = false;
  private retry: number | undefined;
  private h: FeedHandlers;

  constructor(h: FeedHandlers) {
    this.h = h;
    this.connect();
  }

  private connect() {
    if (this.closed) return;
    this.h.onStatus("connecting");
    const ws = new WebSocket(WS_URL);
    this.ws = ws;
    ws.onopen = () => this.h.onStatus("live");
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(String(ev.data)) as FeedMsg;
        if (m && typeof m === "object" && "type" in m) this.h.onMsg(m);
      } catch { /* ignore non-JSON frames */ }
    };
    ws.onclose = () => {
      this.ws = null;
      if (this.closed) return;
      this.h.onStatus("offline · retrying");
      this.retry = window.setTimeout(() => this.connect(), 1500);
    };
    ws.onerror = () => ws.close();
  }

  async setPermission(agent: string, enabled: boolean) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "set_permission", agent, enabled }));
    } else {
      await post(`/permissions/${agent}`, { enabled, by: CONTROLLER });
    }
  }
  async approve(d: Decision) {
    await post("/approve", { decision_id: idOf(d._id), approved_by: CONTROLLER, approve: true });
  }
  async reject(d: Decision) {
    await post("/approve", { decision_id: idOf(d._id), approved_by: CONTROLLER, approve: false });
  }
  async revoke(d: Decision) {
    await post("/revoke", { decision_id: idOf(d._id), by: CONTROLLER });
  }
  async play() {
    await post("/scenario/start");
  }
  async reset() {
    this.h.onReset();
    await post("/scenario/reset");
  }
  async rogue() {
    try {
      const r = await post("/rogue");
      const ok = !!(r && typeof r === "object" && (r as Record<string, unknown>).inserted === true);
      this.h.onRogue(ok, typeof r === "string" ? r : JSON.stringify(r));
    } catch (e) {
      const data = (e as { data?: unknown }).data;
      this.h.onRogue(false, data ? JSON.stringify(data) : String(e));
    }
  }
  close() {
    this.closed = true;
    window.clearTimeout(this.retry);
    this.ws?.close();
  }
}

// --------------------------------------------------------------- mock ----
interface MockEntry {
  type: FeedMsg["type"];
  doc: Record<string, unknown>;
  t?: string;
  requires?: string;
  unless?: string;
  after?: string;
  approves?: string;
  if_disabled?: { type: FeedMsg["type"]; doc: Record<string, unknown> };
}

const script = mockScript as unknown as { start: string; end: string; entries: MockEntry[] };

function entryTime(e: MockEntry): number {
  return toMs((e.t ?? e.doc.t ?? e.doc.changed_at) as string) ?? 0;
}

export class MockFeed implements Feed {
  private h: FeedHandlers;
  private speed: number;
  private t0 = toMs(script.start)!;
  private tEnd = toMs(script.end)!;
  private entries = [...script.entries].sort((a, b) => entryTime(a) - entryTime(b));
  private next = 0;
  private running = false;
  private wall0 = 0;
  private timer: number | undefined;
  private perms: Record<string, boolean> = {};
  private emitted = new Set<string>();
  private handled = new Set<string>(); // proposals the controller acted on
  private closed = false;

  constructor(h: FeedHandlers, speed = 1) {
    this.h = h;
    this.speed = speed;
    window.setTimeout(() => {
      if (this.closed) return;
      this.h.onStatus("mock replay");
      this.rewind();
    }, 0);
  }

  private now(): number {
    if (!this.running) return this.clockT;
    return Math.min(this.t0 + (Date.now() - this.wall0) * this.speed, this.tEnd);
  }
  private clockT = this.t0;

  private enabled(agent: string) {
    return this.perms[agent] !== false && this.perms.system !== false;
  }

  private emit(type: FeedMsg["type"], doc: Record<string, unknown>) {
    const id = idOf(doc._id);
    if (id) this.emitted.add(id);
    if (type === "snapshot") {
      for (const p of (doc.permissions as Permission[] | undefined) ?? []) {
        if (!(p.agent in this.perms)) this.perms[p.agent] = p.enabled;
      }
      // keep any toggles the controller made before pressing Play
      doc = {
        ...doc,
        permissions: ((doc.permissions as Permission[]) ?? []).map((p) => ({ ...p, enabled: this.perms[p.agent] })),
      };
    }
    this.h.onMsg({ type, doc } as FeedMsg);
  }

  private fire(e: MockEntry) {
    if (e.after && !this.emitted.has(e.after)) return;
    if (e.approves && this.handled.has(e.approves)) return;
    if (e.unless) {
      if (!this.enabled(e.unless)) this.emit(e.type, e.doc);
      return;
    }
    if (e.requires && !this.enabled(e.requires)) {
      if (e.if_disabled) this.emit(e.if_disabled.type, e.if_disabled.doc);
      return;
    }
    if (e.approves) this.handled.add(e.approves);
    this.emit(e.type, e.doc);
  }

  private pump = () => {
    if (this.closed) return;
    const now = this.now();
    while (this.next < this.entries.length && entryTime(this.entries[this.next]) <= now) {
      this.fire(this.entries[this.next++]);
    }
    this.h.onClock(now, this.running);
    if (now >= this.tEnd) {
      this.clockT = now;
      this.running = false;
      this.h.onClock(now, false);
      this.h.onStatus("mock replay · finished");
      return;
    }
    if (this.running) this.timer = window.setTimeout(this.pump, 100);
  };

  /** Back to the start, paused, with only the initial snapshot loaded. */
  private rewind() {
    window.clearTimeout(this.timer);
    this.running = false;
    this.next = 0;
    this.clockT = this.t0;
    this.emitted.clear();
    this.handled.clear();
    this.h.onReset();
    while (this.next < this.entries.length && entryTime(this.entries[this.next]) <= this.t0) {
      this.fire(this.entries[this.next++]);
    }
    this.h.onClock(this.t0, false);
  }

  async setPermission(agent: string, enabled: boolean) {
    this.perms[agent] = enabled;
    this.emit("permission", {
      _id: `perm-${agent}`, agent, enabled, by: CONTROLLER,
      changed_at: new Date(this.now()).toISOString(),
    });
  }
  private act(d: Decision, outcome: "executed" | "rejected") {
    const id = idOf(d._id);
    if (!id || this.handled.has(id)) return;
    this.handled.add(id);
    this.emit("decision", {
      ...d,
      _id: `${id}-${outcome}`,
      t: new Date(this.now()).toISOString(),
      outcome,
      approved_by: outcome === "executed" ? CONTROLLER : null,
      reason: outcome === "rejected" ? "rejected_by_controller" : null,
      notified: outcome === "executed" ? ["TRUCK1"] : [],
      intent_id: id,
    } as Record<string, unknown>);
  }
  async approve(d: Decision) { this.act(d, "executed"); }
  async reject(d: Decision) { this.act(d, "rejected"); }
  async revoke(d: Decision) {
    const id = idOf(d._id);
    this.emit("decision", {
      _id: `${id}-revoked-${Date.now()}`, t: new Date(this.now()).toISOString(),
      agent: d.agent, action: d.action, outcome: "revoked", decision_id: id,
      rule: d.rule, runway: d.runway, reason: "revoked_by_controller",
      authorized_by: d.authorized_by, approved_by: CONTROLLER, notified: d.notified ?? [],
    });
  }
  async play() {
    if (this.running) return;
    if (this.now() >= this.tEnd) this.rewind();
    this.wall0 = Date.now() - (this.clockT - this.t0) / this.speed;
    this.running = true;
    this.h.onStatus("mock replay · playing");
    this.pump();
  }
  async reset() {
    this.rewind();
    this.h.onStatus("mock replay");
  }
  async rogue() {
    // What Atlas answers when a misbehaving agent tries to write "clear":
    // the intents_* validator refuses the document. Nothing is inserted.
    this.h.onRogue(
      false,
      'intents_safety.insert({agent: "safety", action: "clear", target: "TRUCK1"}) -> ' +
        "DocumentValidationFailure (code 121): action must be one of stop | propose_instruction | close_runway | reopen_runway",
    );
  }
  close() {
    this.closed = true;
    window.clearTimeout(this.timer);
  }
}
