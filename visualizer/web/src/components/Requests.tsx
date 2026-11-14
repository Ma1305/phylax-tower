import { useState } from 'react';
import { ApiError, type Controller, type DataSource, type Decision, type World } from '../types';
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
  const trail = decisions
    .filter((d) => d.outcome !== 'pending_approval')
    .filter(mine)
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 30);

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
            Agent requests <span className={`count ${list.length ? 'hot' : ''}`}>{list.length}</span>
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
              No request is waiting{controller === 'all' ? '' : ` for ${selected?.name}`}.
              <span>Agents can warn, stop or propose. They never clear traffic.</span>
            </div>
          )}
          {list.map((d) => (
            <article key={d._id} className={`req sev-${d.severity}`}>
              <div className="req-top">
                <span className={`sev sev-${d.severity}`}>
                  {SEV_GLYPH[d.severity]} {d.severity.toUpperCase()}
                </span>
                <span className="req-kind">{ACTION[d.intent_action] ?? d.intent_action}</span>
                <span className="mono muted">{hhmmss(d.t)}</span>
              </div>
              <div className="req-call">
                <span className="cap">Exact call{d.stream_id ? ` · on ${streamName(d.stream_id)}` : ''}</span>
                <p>“{d.text ?? '(no text)'}”</p>
              </div>
              {(d.detail || d.reason) && <p className="req-reason">{d.detail ?? d.reason}</p>}
              <dl className="kv">
                <dt>Targets</dt>
                <dd className="mono">{d.target.length ? d.target.join(', ') : 'none'}</dd>
                <dt>Rule</dt>
                <dd className="mono">{d.reason ? `${d.reason} · ${d.rule}` : d.rule}</dd>
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
        </div>
      </section>

      <section className="panel audit">
        <header className="panel-head">
          <h2>Audit trail</h2>
          <span className="muted small">advisories and decisions, newest first</span>
        </header>
        <div className="panel-body">
          {trail.length === 0 && <div className="empty">Nothing recorded yet.</div>}
          {trail.map((d) => {
            const outcome = d.intent_action === 'warn' && d.outcome === 'executed' ? 'advisory' : d.outcome;
            return (
              <article key={d._id} className={`trail out-${outcome}`}>
                <div className="trail-top">
                  <span className="mono muted">{hhmmss(d.t)}</span>
                  <span className={`badge out-${outcome}`}>
                    {outcome === 'executed' ? '✓ EXECUTED' : outcome === 'advisory' ? '△ ADVISORY' : outcome === 'revoked' ? '✕ REJECTED' : '✕ ' + outcome.toUpperCase()}
                  </span>
                  <span className={`sev sev-${d.severity}`}>{d.severity}</span>
                  <span className="muted small">{d.agent}</span>
                </div>
                {d.text && <p className="trail-text">{d.text}</p>}
                <div className="trail-meta mono">
                  <span title="rule / gate reason code">rule {d.reason ? `${d.reason} · ` : ''}{d.rule}</span>
                  <span title="authorized_by: permission and rule set">
                    auth {d.authorized_by ? `${d.authorized_by.permission_id}/${d.authorized_by.rule_set_version ?? '-'}` : '-'}
                  </span>
                  <span title="approved_by">
                    {outcome === 'revoked' ? 'rejected by' : 'approved by'} {d.approved_by ? ctlName(d.approved_by) : outcome === 'advisory' ? 'not needed' : 'none'}
                  </span>
                </div>
              </article>
            );
          })}
        </div>
      </section>
    </>
  );
}
