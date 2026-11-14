import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { AirfieldMap } from "./AirfieldMap";
import type { Feed } from "./feed";
import { LiveFeed, MockFeed } from "./feed";
import { buildTimeline, isStop, LIVE_LANDING_TO_D_S, scene as buildScene } from "./sim";
import { initialStore, reducer } from "./store";
import type { RogueNote } from "./store";
import type { AtlasEvent, Decision, FeedMsg, Transmission } from "./types";
import { AGENTS, fmtTime, idOf, toMs } from "./types";

const params = new URLSearchParams(window.location.search);
const MOCK = params.get("mock") === "1";
const SPEED = Number(params.get("speed") ?? "1") || 1;

function useNow(running: boolean) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    let id = 0;
    const loop = () => { setTick((x) => x + 1); id = requestAnimationFrame(loop); };
    id = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(id);
  }, [running]);
}

export default function App() {
  const [store, dispatch] = useReducer(reducer, undefined, initialStore);
  const [status, setStatus] = useState("starting");
  const [error, setError] = useState<string | null>(null);
  const feedRef = useRef<Feed | null>(null);

  useEffect(() => {
    const h = {
      onMsg: (msg: FeedMsg) => dispatch({ kind: "msg", msg }),
      onClock: (t: number | null, running: boolean) => dispatch({ kind: "clock", t, running }),
      onStatus: setStatus,
      onReset: () => dispatch({ kind: "reset" }),
      onRogue: (ok: boolean, text: string) => dispatch({ kind: "rogue", ok, text, t: null }),
    };
    const feed: Feed = MOCK ? new MockFeed(h, SPEED) : new LiveFeed(h);
    feedRef.current = feed;
    return () => feed.close();
  }, []);

  // In live mode the clock is "last event time + wall time since", at 1x.
  const live = !MOCK && status === "live";
  useNow(MOCK ? store.clock.running : live && store.clock.t != null);
  const now = store.clock.t == null
    ? null
    : MOCK
      ? store.clock.t + (store.clock.running ? (Date.now() - store.clock.wall) * SPEED : 0)
      : store.clock.t + (Date.now() - store.clock.wall) * SPEED;

  const timeline = useMemo(() => buildTimeline(store.transmissions, store.decisions, MOCK ? undefined : LIVE_LANDING_TO_D_S), [store.transmissions, store.decisions]);
  const sc = buildScene(timeline, now);

  const run = (label: string, fn: () => Promise<void>) => {
    setError(null);
    fn().catch((e: unknown) => setError(`${label}: ${e instanceof Error ? e.message : String(e)}`));
  };
  const feed = () => feedRef.current!;

  const perm = (a: string) => store.permissions[a]?.enabled ?? true;
  const safetyOn = perm("safety") && perm("system");

  const resolved = new Set(
    store.decisions.flatMap((d) => (d.outcome === "pending_approval" ? [] : [idOf(d.intent_id), idOf(d.decision_id), idOf(d.revokes)]))
      .filter((x): x is string => !!x),
  );
  // resolved by a later decision on the same intent (approved/executed) or a revocation of it
  const pending = store.decisions.filter((d) => d.outcome === "pending_approval" &&
    !resolved.has(idOf(d.intent_id) ?? "") && !resolved.has(idOf(d._id) ?? ""));
  const runway04 = store.runwayStatus["04"];
  const rwyBlocked = String(runway04?.state ?? runway04?.status ?? "").includes("block") && sc.lights !== "green";

  let banner: string | null = null;
  if (sc.phase === "frozen") banner = "No alert fired. Both clearances stand.|Surface radar silent: truck has no transponder.";
  else if (sc.phase === "held") banner = "STOP sent to TRUCK1 by name.|Lights held red. The convoy holds short.";
  else if (sc.phase === "landed") banner = "AC8646 lands and rolls past D.|The convoy is still holding short.";
  else if (sc.phase === "crossed" && timeline.approveT) banner = "Controller approved the crossing.|Convoy crosses after AC8646 passed.";

  const label = MOCK ? (safetyOn ? "Mock · safety agent on" : "Mock · safety agent off") : safetyOn ? "Live · safety agent on" : "Live · safety agent off";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-name">Hold Short</div>
          <div className="brand-sub">LGA tower · local control</div>
        </div>
        <div className="clock">
          <div className="clock-time">{fmtTime(sc.t)}</div>
          <div className="brand-sub">scenario time (UTC) · {status}</div>
        </div>
        <div className="controls">
          <button className="btn" onClick={() => run("play", () => feed().play(safetyOn))}>▶ Play</button>
          <button className="btn" onClick={() => run("reset", () => feed().reset())}>⟲ Reset</button>
          <button className="btn btn-rogue" onClick={() => run("rogue", () => feed().rogue())}>Rogue agent</button>
        </div>
        <div className="toggles">
          {AGENTS.map((a) => (
            <Toggle key={a} label={a} on={perm(a)} big={a === "safety"}
              onClick={() => run(a, () => feed().setPermission(a, !perm(a)))} />
          ))}
          <Toggle label="system" on={perm("system")} master
            onClick={() => run("system", () => feed().setPermission("system", !perm("system")))} />
        </div>
      </header>

      <main className="main">
        <section className="map-wrap">
          <div className={`run-chip ${safetyOn ? "chip-green" : "chip-dim"}`}>{label}</div>
          {pending.length > 0 && (
            <div className="pending">
              {pending.map((d) => (
                <div key={idOf(d._id) ?? String(d.t)} className="pending-card">
                  <div className="card-meta">{d.agent} agent · proposal · {fmtTime(toMs(d.t))} · needs controller</div>
                  <div className="card-title">{proposalText(d)}</div>
                  <div className="row">
                    <button className="btn btn-approve" onClick={() => run("approve", () => feed().approve(d))}>✓ Approve</button>
                    <button className="btn btn-reject" onClick={() => run("reject", () => feed().reject(d))}>✕ Reject</button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <AirfieldMap scene={sc} stopBanner={banner} lightsBlocked={rwyBlocked} />
          {sc.frozen && <div className="freeze-veil"><div>Freeze.</div><small>Press Reset, switch Safety on, play again.</small></div>}
          {error && <div className="error" onClick={() => setError(null)}>{error}</div>}
        </section>

        <aside className="side">
          <RadioPanel tx={store.transmissions} decisions={store.decisions} />
          <AlertsPanel decisions={store.decisions} rogue={store.rogue} runway04={runway04}
            onRevoke={(d) => run("revoke", () => feed().revoke(d))} />
          <AtlasStrip events={store.events} />
        </aside>
      </main>
    </div>
  );
}

function proposalText(d: Decision): string {
  if (d.text) return d.text;
  if (typeof d.instruction === "string") return d.instruction;
  if (d.instruction) return JSON.stringify(d.instruction);
  return `${d.action ?? "proposal"} ${d.runway ? `runway ${d.runway}` : ""}`;
}

function Toggle({ label, on, onClick, big, master }: { label: string; on: boolean; onClick: () => void; big?: boolean; master?: boolean }) {
  return (
    <button className={`toggle ${on ? "on" : "off"} ${big ? "big" : ""} ${master ? "master" : ""}`} onClick={onClick}
      title={master ? "Master switch (system): turns every agent off" : `${label} agent`}>
      <span className="knob" />
      <span className="toggle-label">{master ? "Master" : label[0].toUpperCase() + label.slice(1)}</span>
      {big && <span className="toggle-state">{on ? "ON" : "OFF"}</span>}
    </button>
  );
}

// ------------------------------------------------------------- radio ----
function Wave({ noisy }: { noisy: boolean }) {
  const pts = Array.from({ length: 28 }, (_, i) => {
    const y = noisy ? 6 + Math.sin(i * 2.7) * 5 * ((i * 7) % 3 === 0 ? 1 : 0.6) : 6 + (i > 8 && i < 20 ? Math.sin(i * 1.3) * 4 : 0);
    return `${i * 3},${y.toFixed(1)}`;
  }).join(" ");
  return <svg width={84} height={12} className="wave"><polyline points={pts} fill="none" stroke={noisy ? "#6b7586" : "#9aa5b8"} strokeWidth={1} /></svg>;
}

function isUnclear(x: Transmission) {
  return (x.flags ?? []).some((f) => f.includes("unclear")) || x.verified === false || (x.confidence ?? 1) < 0.6;
}

function RadioPanel({ tx, decisions }: { tx: Transmission[]; decisions: Decision[] }) {
  const stops = decisions.filter((d) => d.outcome === "executed" && isStop(d));
  type Item = { kind: "tx"; t: number; x: Transmission } | { kind: "stop"; t: number; d: Decision };
  const items: Item[] = [
    ...tx.map((x) => ({ kind: "tx" as const, t: toMs(x.t) ?? 0, x })),
    ...stops.map((d) => ({ kind: "stop" as const, t: toMs(d.t) ?? 0, d })),
  ].sort((a, b) => b.t - a.t);

  return (
    <section className="panel radio">
      <div className="panel-head"><span className="dot" /> Radio <span className="panel-sub">tower frequency · live transcript</span></div>
      <div className="radio-list">
        {items.length === 0 && <div className="empty">No transmissions yet. Press Play.</div>}
        {items.map((it) =>
          it.kind === "stop" ? (
            <div key={`s${idOf(it.d._id)}`} className="tx tx-stop">
              <div className="tx-waves"><span>voice</span><Wave noisy={false} /></div>
              <div className="tx-body">
                <div className="tx-head"><b className="red">SAFETY · AUTO</b> {fmtTime(it.t)}</div>
                <div className="tx-text">“{it.d.text ?? `STOP. Hold short runway ${it.d.runway}.`}”</div>
                <span className="badge badge-dim">to {(it.d.notified ?? []).join(", ")} by name</span>
              </div>
            </div>
          ) : (
            <div key={`t${idOf(it.x._id) ?? it.t}`} className={`tx ${isUnclear(it.x) ? "tx-unclear" : ""}`}>
              <div className="tx-waves">
                <span>raw</span><Wave noisy />
                <span>clean</span><Wave noisy={false} />
              </div>
              <div className="tx-body">
                <div className="tx-head">
                  <b>{it.x.from}</b>{it.x.to ? <> → {it.x.to}</> : null} {fmtTime(it.t)}
                  <span className="conf" title="transcription confidence">{Math.round((it.x.confidence ?? 0) * 100)}%</span>
                </div>
                {it.x.raw_transcript && <div className="tx-raw">raw: {it.x.raw_transcript}</div>}
                <div className="tx-text">“{it.x.transcript}”</div>
                <div className="row">
                  {isUnclear(it.x) && <span className="badge badge-amber">⚠ unclear - say again</span>}
                  {(it.x.flags ?? []).includes("no_callsign") && <span className="badge badge-amber">no callsign</span>}
                  {it.x.kind && it.x.kind !== "other" && <span className="badge badge-dim">{it.x.kind}</span>}
                  {it.x.readback === "matched" && <span className="badge badge-green">✓ readback matched</span>}
                  {it.x.readback && /mismatch|missing/.test(it.x.readback) && <span className="badge badge-red">✕ readback {it.x.readback}</span>}
                  {it.x.readback === "pending" && <span className="badge badge-dim">readback pending</span>}
                </div>
              </div>
            </div>
          ),
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------ alerts ----
function AlertsPanel({ decisions, rogue, runway04, onRevoke }: {
  decisions: Decision[];
  rogue: RogueNote[];
  runway04: Record<string, unknown> | undefined;
  onRevoke: (d: Decision) => void;
}) {
  const revoked = new Set(decisions.filter((d) => d.outcome === "revoked").map((d) => idOf(d.decision_id ?? d.revokes)));
  const shown = decisions.filter((d) => d.outcome !== "pending_approval").slice().reverse();
  const rwState = String(runway04?.state ?? runway04?.status ?? "");
  return (
    <section className="panel alerts">
      <div className="panel-head">Alerts
        {rwState && <span className={`chip ${rwState.includes("block") ? "chip-red" : rwState.includes("clear") || rwState === "open" ? "chip-green" : "chip-dim"}`}>runway 04 · {rwState}</span>}
      </div>
      <div className="alert-list">
        {rogue.map((r) => (
          <div key={r.key} className={`card ${r.ok ? "card-red" : "card-rogue"}`}>
            <div className="card-meta">rogue agent · write attempt</div>
            <div className="card-title">{r.ok ? "Rogue write was accepted!" : "Atlas rejected a \"clear\" written by an agent"}</div>
            <div className="mono small">{r.text}</div>
          </div>
        ))}
        {shown.length === 0 && rogue.length === 0 && <div className="empty">No decisions yet.</div>}
        {shown.map((d) => {
          const id = idOf(d._id);
          const stop = isStop(d) && d.outcome === "executed";
          const lifted = stop && revoked.has(id);
          const cls = stop && !lifted ? "card-stop" : d.outcome === "executed" ? "card-green" : d.outcome === "revoked" ? "card-green" : "card-dim";
          return (
            <div key={id ?? String(d.t)} className={`card ${cls}`}>
              {stop && !lifted ? (
                <>
                  <div className="stop-title">⛔ STOP</div>
                  <div className="stop-sub">Runway {d.runway ?? "?"} — {d.conflicts_with ?? "conflict"}</div>
                  <div className="small">Alert sent to {(d.notified ?? []).join(" + ")} by name. Lights held red.</div>
                </>
              ) : d.outcome === "revoked" ? (
                <div className="card-title">{isStop(d) ? "✓ Stop lifted" : d.action === "propose_instruction" ? `✕ Rejected by controller: ${d.text ?? "proposal"}` : `✓ Revoked: ${d.action}`} · {d.rule ?? "revoked"}</div>
              ) : (
                <div className="card-title">
                  {d.outcome === "executed" && d.approved_by ? "✓ " : d.outcome === "rejected" ? "✕ " : ""}
                  {d.text ?? `${d.action}${d.runway ? ` runway ${d.runway}` : ""}`}
                </div>
              )}
              <div className="card-audit">
                <span>{d.agent} · {d.action} · <b>{d.outcome}</b> · {fmtTime(toMs(d.t))}</span>
                {d.rule && <span>rule: <code>{d.rule}</code></span>}
                {d.reason && d.outcome !== "revoked" && <span>reason: <code>{d.reason}</code></span>}
                {d.authorized_by && (
                  <span>authorized_by: <code>{idOf(d.authorized_by.permission_id)}</code> · rules <code>{String(d.authorized_by.rule_set_version ?? "?")}</code></span>
                )}
                <span>approved_by: <code>{d.approved_by ?? (isStop(d) ? "— (autonomous STOP)" : "—")}</code></span>
              </div>
              {stop && !lifted && <button className="btn btn-small" onClick={() => onRevoke(d)}>Lift stop (revoke)</button>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ------------------------------------------------------------- atlas ----
function AtlasStrip({ events }: { events: AtlasEvent[] }) {
  return (
    <section className="atlas">
      <div className="atlas-head"><span className="dot" /> Atlas events <span className="panel-sub">change stream</span></div>
      <div className="atlas-list">
        {events.length === 0 && <div className="empty">waiting for change-stream events…</div>}
        {events.slice(0, 6).map((e) => (
          <div key={e.key} className={`atlas-ev tone-${e.tone ?? "neutral"}`}>
            <span className="ev-coll">{e.collection}</span>
            <span className="ev-op">{e.op} · {fmtTime(e.t)}</span>
            <span className="ev-body">{e.body}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
