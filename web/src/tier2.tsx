// Tier-2 polish: similar past incidents on STOP cards, confusable-callsign warnings in
// the transcript, the "Ask the audit agent" box, and playback of spoken STOP alerts.
// All of it is read-only GETs to the API; any failure just hides the extra (the core
// demo never depends on it). Nothing here runs in ?mock=1.
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { API_BASE } from "./feed";
import type { Decision, Transmission } from "./types";
import { fmtTime, idOf, normCallsign, toMs } from "./types";

async function getJson<T>(path: string, params: [string, string][]): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`${API_BASE}${path}?${qs}`);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? String((data as { detail: unknown }).detail) : "";
    throw new Error(`${path} -> HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
  }
  return data as T;
}

// --------------------------------------------------- similar incidents ----
export interface Incident { id: string; title: string; airport: string; date: string; summary: string; score: number; method: string }

const incidentCache = new Map<string, Promise<Incident[]>>();

/** What the STOP was about, as a search query for holdshort.search.incidents.similar. */
export function incidentQuery(d: Decision): string {
  const target = (d.notified ?? []).filter((c) => c !== d.conflicts_with?.split(" ")[0]).join(" ") || "vehicle";
  return `runway incursion: ${target} cleared to cross runway ${d.runway ?? ""} while ${d.conflicts_with ?? "an aircraft is cleared to land"}`;
}

export function SimilarIncidents({ d }: { d: Decision }) {
  const [items, setItems] = useState<Incident[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const q = incidentQuery(d);
  useEffect(() => {
    let live = true;
    if (!incidentCache.has(q)) {
      const p = getJson<{ incidents: Incident[] }>("/incidents/similar", [["q", q], ["k", "3"]]).then((r) => r.incidents);
      p.catch(() => incidentCache.delete(q)); // retry on the next render after a failure
      incidentCache.set(q, p);
    }
    incidentCache.get(q)!.then((x) => live && setItems(x), (e: unknown) => live && setErr(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [q]);
  if (err) return <div className="similar"><div className="similar-head">Similar past incidents unavailable</div></div>;
  if (!items) return <div className="similar"><div className="similar-head">Searching similar past incidents…</div></div>;
  if (items.length === 0) return null;
  return (
    <div className="similar">
      <div className="similar-head">Similar past incidents <span>· Atlas {items[0].method === "vector" ? "Vector Search" : "Search"}</span></div>
      {items.map((i) => (
        <details key={i.id} className="similar-item">
          <summary><b>{i.airport}</b> {i.date} · {i.title}</summary>
          <div className="small">{i.summary}</div>
        </details>
      ))}
    </div>
  );
}

// ------------------------------------------------ confusable callsigns ----
export interface Confusable { other: string; reason: string; score: number }

/** Confusable callsigns (holdshort.search.callsigns.similar_on_frequency) for every
 *  callsign heard so far; each callsign is asked about once. */
export function useConfusables(tx: Transmission[], enabled: boolean): Record<string, Confusable[]> {
  const [known, setKnown] = useState<Record<string, Confusable[]>>({});
  const [asked] = useState(() => new Set<string>());
  const heard = [...new Set(tx.flatMap((x) => [x.from, x.to]).map((c) => normCallsign(c)).filter((c) => c && c !== "UNKNOWN"))];
  const fresh = heard.filter((c) => !asked.has(c));
  const key = fresh.join(",");
  useEffect(() => {
    if (!enabled || fresh.length === 0) return;
    fresh.forEach((c) => asked.add(c));
    getJson<{ confusable: Record<string, Confusable[]> }>("/callsigns/confusable", fresh.map((c) => ["callsign", c]))
      .then((r) => setKnown((k) => ({ ...k, ...r.confusable })))
      .catch(() => fresh.forEach((c) => asked.delete(c)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);
  return known;
}

export function ConfusableBadges({ x, conf }: { x: Transmission; conf: Record<string, Confusable[]> }) {
  const parties = [...new Set([x.from, x.to].map((c) => normCallsign(c)).filter(Boolean))];
  return (
    <>
      {parties.flatMap((c) => (conf[c] ?? []).length === 0 ? [] : [
        <span key={c} className="badge badge-amber badge-confusable"
          title={(conf[c] ?? []).map((o) => `${o.other}: ${o.reason}`).join("\n")}>
          ⚠ {c} sounds like {(conf[c] ?? []).map((o) => o.other).join(" / ")}
        </span>,
      ])}
    </>
  );
}

// ---------------------------------------------------------- audit box ----
interface AuditItem { t: string; kind: string; actor?: string; summary?: string; rule?: string | null; approved_by?: string | null; doc_id: string }
interface AuditAnswer { entity: string | null; narrative: string; source: string; timeline: AuditItem[] }

export function AuditBox() {
  const [q, setQ] = useState("Why was TRUCK1 held?");
  const [busy, setBusy] = useState(false);
  const [ans, setAns] = useState<AuditAnswer | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(true);

  const ask = (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim() || busy) return;
    setBusy(true); setErr(null);
    getJson<{ ok: boolean; result: AuditAnswer }>("/audit", [["q", q.trim()]])
      .then((r) => { setAns(r.result); setOpen(true); })
      .catch((x: unknown) => setErr(x instanceof Error ? x.message : String(x)))
      .finally(() => setBusy(false));
  };
  const ids = new Map((ans?.timeline ?? []).map((it, n) => [it.doc_id, n + 1]));
  // narrative cites source documents as [<24-hex id>]; show them as [n] into the timeline
  const narrative = (ans?.narrative ?? "").replace(/\[([0-9a-f]{24})\]/g, (_m, id: string) => (ids.has(id) ? `[${ids.get(id)}]` : ""));

  return (
    <section className="panel audit">
      <form className="audit-form" onSubmit={ask}>
        <span className="audit-label">Ask the audit agent</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Why was TRUCK1 held?" />
        <button className="btn btn-small" disabled={busy}>{busy ? "…" : "Ask"}</button>
        {ans && <button type="button" className="btn btn-small" onClick={() => setOpen(!open)}>{open ? "hide" : "show"}</button>}
      </form>
      {err && <div className="small red">{err}</div>}
      {ans && open && (
        <div className="audit-answer">
          {ans.source === "llm" && <div className="audit-narr">{narrative}</div>}
          {ans.source !== "llm" && ans.timeline.length === 0 && <div className="audit-narr">{narrative}</div>}
          <div className="card-meta">{ans.source === "llm" ? "LLM narrative, every [n] checked against the timeline" : "timeline (no LLM key: deterministic)"} · {ans.timeline.length} records from Atlas{ans.entity ? ` about ${ans.entity}` : ""}</div>
          <ol className="audit-tl">
            {ans.timeline.map((it) => (
              <li key={it.doc_id}>
                <span className="mono">{fmtTime(toMs(it.t))}</span> <b>{it.kind}</b> {it.actor} · {it.summary}
                {it.rule && <> · rule <code>{it.rule}</code></>}
                {it.approved_by && <> · approved_by <code>{it.approved_by}</code></>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------- voice alerts ----
const spokenIds = new Set<string>();
let voiceOn: Promise<boolean> | null = null;

/** holdshort.voice_alert writes an mp3 per executed STOP (when VOICE_ENABLED); poll for
 *  it for a few seconds and play it. Silent no-op when voice is off (404s). */
export function playVoiceAlert(d: Decision) {
  const id = idOf(d._id);
  if (!id || spokenIds.has(id) || d.outcome !== "executed" || (d.action ?? d.intent_action) !== "stop") return;
  spokenIds.add(id);
  voiceOn ??= fetch(`${API_BASE}/health`).then((r) => r.json()).then((h) => !!h.voice_enabled, () => false);
  let tries = 0;
  const attempt = () => {
    fetch(`${API_BASE}/voice/alerts/${id}`)
      .then(async (res) => {
        if (res.ok) {
          const url = URL.createObjectURL(await res.blob());
          const a = new Audio(url);
          a.onended = () => URL.revokeObjectURL(url);
          await a.play().catch(() => undefined); // autoplay blocked: nothing to do
        } else if (res.status === 404 && ++tries < 16) setTimeout(attempt, 500);
      })
      .catch(() => undefined);
  };
  void voiceOn.then((on) => on && attempt());
}
