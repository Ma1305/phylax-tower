import { useState } from 'react';
import { ApiError, type AuditAnswer, type Controller, type DataSource, type Decision, type World } from '../types';
import { hhmmss } from '../util';

interface Props {
  ds: DataSource;
  world: World;
  pending: Decision[];
  decisions: Decision[];
  controller: string; // 'all' or a controller id
  actAs: string;
  onActAs: (id: string) => void;
  readOnly: boolean;
}

const ACTION: Record<string, string> = {
  stop: 'STOP',
  propose_instruction: 'PROPOSED INSTRUCTION',
  warn: 'ADVISORY',
  close_lane: 'CLOSE LANE',
  reopen_lane: 'REOPEN LANE',
  set_rule_set: 'RULE SET',
};
const SEV_GLYPH: Record<string, string> = { info: 'i', caution: '!', warning: '!!', critical: '!!!' };

export function Requests({ ds, world, pending, decisions, controller, actAs, onActAs, readOnly }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const ctl = (id: string | null | undefined): Controller | undefined => world.controllers.find((c) => c._id === id);
  const ctlName = (id: string | null | undefined) => ctl(id)?.name ?? id ?? 'nobody';
  const laneName = (id: string | null) => world.lanes.find((l) => l._id === id)?.name ?? id ?? 'none';
  const streamName = (id: string | null) => world.streams.find((s) => s._id === id)?.name ?? id ?? 'not spoken';

  const selected = ctl(controller);
  const seesAll = controller === 'all' || selected?.role === 'supervisor';
  const mine = (d: Decision) => seesAll || d.controller_id === controller || d.approved_by === controller;
  const actor = controller === 'all' ? actAs : controller;
  const rank: Record<string, number> = { critical: 0, warning: 1, caution: 2, info: 3 };
  const list = pending.filter(mine).sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9) || b.t.localeCompare(a.t));
  const settled = new Set(decisions.flatMap((d) => (d.outcome !== 'pending_approval' ? [d.intent_id] : [])));
  const lifted = new Set(decisions.map((d) => d.revokes).filter(Boolean));
  const trail = decisions
    .filter(mine)
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 40);

  const act = async (d: Decision, approve: boolean) => {
    setBusy(d._id);
    setErrors((e) => ({ ...e, [d._id]: '' }));
    try {
      await (approve ? ds.approve(d._id, actor) : ds.reject(d._id, actor));
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      const msg = err instanceof Error ? err.message : String(err);
      setErrors((e) => ({ ...e, [d._id]: status ? `${status} · ${msg}` : `Tower API not reachable: ${msg}` }));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <section className="panel requests">
        <header className="panel-head">
          <h2>
            Awaiting approval <span className={`count ${list.length ? 'hot' : ''}`}>{list.length}</span>
          </h2>
          {controller === 'all' ? (
            <label className="actas">
              act as
              <select value={actAs} onChange={(e) => onActAs(e.target.value)} disabled={readOnly}>
                {world.controllers.map((c) => (
                  <option key={c._id} value={c._id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <span className="muted small">routed to {selected?.name}</span>
          )}
        </header>
        <div className="panel-body">
          {list.length === 0 && (
            <div className="empty">
              Nothing is waiting{controller === 'all' ? '' : ` for ${selected?.name}`}.
              <span>Only proposed instructions wait here. A stop from an autonomous agent runs at once and is shown at the top; it never waits.</span>
            </div>
          )}
          {list.map((d) => (
            <article key={d._id} className={`req sev-${d.severity}`}>
              <div className="req-top">
                <span className={`sev sev-${d.severity}`}>
                  {SEV_GLYPH[d.severity]} {d.severity.toUpperCase()}
                </span>
                <span className="req-kind">{d.intent_action === 'stop' ? 'STOP · NEEDS YOUR APPROVAL' : (ACTION[d.intent_action] ?? d.intent_action)}</span>
                <span className="mono muted">{hhmmss(d.t)}</span>
              </div>
              <div className="req-call">
                <span className="cap">Exact call{d.stream_id ? ` · on ${streamName(d.stream_id)}` : ''}</span>
                <p>“{d.text ?? '(no text)'}”</p>
              </div>
              {(d.intent?.detail || d.intent?.reason) && <p className="req-reason">{d.intent?.detail ?? d.intent?.reason}</p>}
              <dl className="kv">
                <dt>Targets</dt>
                <dd className="mono">{d.target.length ? d.target.join(', ') : 'none'}</dd>
                <dt>Rule</dt>
                <dd className="mono">{d.intent?.reason ? `${d.intent.reason} · ${d.rule}` : d.rule}</dd>
                <dt>Lane</dt>
                <dd>{laneName(d.lane_id)}</dd>
                <dt>Agent</dt>
                <dd>
                  {d.agent} <span className="muted">({d.mode})</span>
                </dd>
                <dt>Routed to</dt>
                <dd>{ctlName(d.controller_id)}</dd>
              </dl>
              <div className="req-actions">
                <button className="btn approve" disabled={readOnly || busy === d._id} onClick={() => act(d, true)}>
                  ✓ Approve
                </button>
                <button className="btn reject" disabled={readOnly || busy === d._id} onClick={() => act(d, false)}>
                  ✕ Reject
                </button>
                <span className="muted small">{readOnly ? 'history: actions disabled' : `as ${ctlName(actor)}`}</span>
              </div>
              {errors[d._id] && (
                <div className="req-error" role="alert">
                  {errors[d._id]}
                </div>
              )}
            </article>
          ))}
          <AuditBox ds={ds} ctlName={ctlName} />
        </div>
      </section>

      <section className="panel audit">
        <header className="panel-head">
          <h2>Decision log</h2>
          <span className="muted small">every gate decision, newest first</span>
        </header>
        <div className="panel-body">
          {trail.length === 0 && <div className="empty">{readOnly ? 'Nothing had been decided at this time.' : 'Nothing decided yet.'}</div>}
          {trail.map((d) => {
            const o = describe(d, settled.has(d.intent_id), lifted.has(d._id));
            return (
              <article key={d._id} className={`trail out-${o.cls}`} data-outcome={d.outcome} data-rule={d.rule}>
                <div className="trail-top">
                  <span className="mono muted">{hhmmss(d.t)}</span>
                  <span className={`badge out-${o.cls}`}>{o.label}</span>
                  <span className="trail-what">
                    {d.agent} · {(ACTION[d.intent_action] ?? d.intent_action).toLowerCase()}
                  </span>
                  <span className={`sev sev-${d.severity}`}>{d.severity}</span>
                </div>
                {d.text && <p className="trail-text">{d.text}</p>}
                {o.note && <p className="trail-note">{o.note}</p>}
                <dl className="trail-meta">
                  <div title="rule: why the agent raised it, and the gate's reason code">
                    <dt>rule</dt>
                    <dd className="mono">{d.intent?.reason ? `${d.intent.reason} · ${d.rule}` : d.rule}</dd>
                  </div>
                  <div title="authorized_by: the permission record and rule set version in force">
                    <dt>authorized by</dt>
                    <dd className="mono">{d.authorized_by ? `${d.authorized_by.permission_id} / ${d.authorized_by.rule_set_version ?? 'no rule set'}` : 'nothing'}</dd>
                  </div>
                  <div title="approved_by: the controller who approved, rejected or lifted it">
                    <dt>{o.byLabel}</dt>
                    <dd>{d.approved_by ? ctlName(d.approved_by) : o.noBy}</dd>
                  </div>
                </dl>
              </article>
            );
          })}
        </div>
      </section>
    </>
  );
}

/** One log line: a text label and glyph per outcome, never colour alone. */
function describe(d: Decision, settled: boolean, lifted: boolean) {
  const stop = d.intent_action === 'stop';
  if (d.outcome === 'pending_approval')
    return { cls: 'pending', label: settled ? '… ASKED' : '… PENDING', note: settled ? null : 'Waiting for the controller.', byLabel: 'approved by', noBy: settled ? 'see the later entry' : 'nobody yet' };
  if (d.outcome === 'rejected')
    return { cls: 'rejected', label: '⊘ REFUSED BY GATE', note: 'Never shown as a request, never spoken.', byLabel: 'approved by', noBy: 'not asked' };
  if (d.outcome === 'revoked')
    return d.rule === 'controller_rejected'
      ? { cls: 'revoked', label: '✕ REJECTED', note: null, byLabel: 'rejected by', noBy: 'unknown' }
      : { cls: 'revoked', label: stop ? '↺ STOP LIFTED' : '↺ REVOKED', note: null, byLabel: stop ? 'lifted by' : 'revoked by', noBy: 'unknown' };
  if (d.intent_action === 'warn') return { cls: 'advisory', label: '△ ADVISORY', note: null, byLabel: 'approved by', noBy: 'not needed (advisory)' };
  if (stop && !d.approved_by)
    return { cls: 'stop', label: '■ STOP EXECUTED', note: lifted ? 'Later lifted by a controller.' : 'Autonomous: spoken at once, the controller was told.', byLabel: 'approved by', noBy: 'not needed (autonomous)' };
  return { cls: 'executed', label: '✓ EXECUTED', note: null, byLabel: 'approved by', noBy: 'not needed (autonomous)' };
}

/** "Ask the audit agent": GET /control/audit?q=... A read-only question, so it also works in history mode. */
function AuditBox({ ds, ctlName }: { ds: DataSource; ctlName: (id: string | null | undefined) => string }) {
  const [q, setQ] = useState('Why was TRUCK1 held?');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [ans, setAns] = useState<AuditAnswer | null>(null);
  const ask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!q.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      setAns(await ds.audit(q.trim()));
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      const msg = err instanceof Error ? err.message : String(err);
      setAns(null);
      setError(status ? `${status} · ${msg}` : `Tower API not reachable: ${msg}`);
    } finally {
      setBusy(false);
    }
  };
  const cited = new Set(ans?.cited ?? []);
  return (
    <div className="askaudit">
      <form onSubmit={ask}>
        <label className="cap" htmlFor="askaudit-q">
          Ask the audit agent
        </label>
        <div className="askaudit-row">
          <input id="askaudit-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Why was TRUCK1 held?" />
          <button className="btn" disabled={busy || !q.trim()}>
            {busy ? 'Asking…' : 'Ask'}
          </button>
        </div>
      </form>
      {error && (
        <div className="req-error" role="alert">
          {error}
        </div>
      )}
      {ans && (
        <div className="askaudit-answer">
          <p className="askaudit-narrative">{ans.narrative}</p>
          <span className="cap">
            Timeline from the stored records · {ans.timeline.length} entries, {cited.size} cited{ans.source ? ` · answer by ${ans.source}` : ''}
          </span>
          {ans.timeline.length === 0 && <div className="empty">No record matches this question.</div>}
          <ol className="askaudit-timeline">
            {ans.timeline.map((it) => (
              <li key={it.doc_id} className={cited.has(it.doc_id) ? 'cited' : ''} data-kind={it.kind} title={`${it.kind} ${it.doc_id}`}>
                <span className="mono muted">{hhmmss(it.t)}</span>
                <span className="badge">{cited.has(it.doc_id) ? '★ ' : ''}{it.kind}</span>
                <span className="askaudit-sum">
                  {it.summary}
                  {(it.rule || it.authorized_by || it.approved_by) && (
                    <span className="muted">
                      {' '}
                      · rule {it.rule ?? 'none'}
                      {it.authorized_by ? ` · authorized by ${it.authorized_by.permission_id} / ${it.authorized_by.rule_set_version ?? 'no rule set'}` : ''}
                      {it.approved_by ? ` · by ${ctlName(it.approved_by)}` : ''}
                    </span>
                  )}
                  <span className="mono faint"> [{it.doc_id.slice(-6)}]</span>
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
