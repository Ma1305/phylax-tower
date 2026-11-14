import { useEffect, useRef, useState } from 'react';
import { useStored } from '../collapse';
import { ApiError, type DataSource, type Decision, type Transmission, type World } from '../types';
import { ago, hhmmss } from '../util';
import { Menu } from './Menu';

interface Props {
  ds: DataSource;
  world: World;
  stops: Decision[];
  pending: Decision[];
  decisions: Decision[];
  transmissions: Transmission[];
  actor: string;
  showActAs: boolean;
  onActAs: (id: string) => void;
  readOnly: boolean;
}

/** Stops in effect, proposals waiting, and recent advisories: the only things that need a person. */
export function NeedsYou({ ds, world, stops, pending, decisions, transmissions, actor, showActAs, onActAs, readOnly }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const ctlName = (id: string | null | undefined) => world.controllers.find((c) => c._id === id)?.name ?? id ?? 'nobody';
  const streamName = (id: string | null) => world.streams.find((s) => s._id === id)?.name ?? id;
  const run = async (d: Decision, what: 'approve' | 'reject' | 'revoke') => {
    setBusy(d._id);
    setErrors((e) => ({ ...e, [d._id]: '' }));
    try {
      await ds[what](d._id, actor);
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      const msg = err instanceof Error ? err.message : String(err);
      setErrors((e) => ({ ...e, [d._id]: status ? `${status} · ${msg}` : `Tower API not reachable: ${msg}` }));
    } finally {
      setBusy(null);
    }
  };
  const rank: Record<string, number> = { critical: 0, warning: 1, caution: 2, info: 3 };
  const list = [...pending].sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9) || b.t.localeCompare(a.t));
  const advisories = decisions
    .filter((d) => d.intent_action === 'warn' && d.outcome === 'executed')
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 3);
  const calm = stops.length === 0 && list.length === 0;
  const [collapsed, setCollapsed] = useStored('needs.collapsed');
  const count = stops.length + list.length;
  const prev = useRef(count);
  useEffect(() => {
    if (count > prev.current) setCollapsed(false); // something arrived: open
    prev.current = count;
  }, [count, setCollapsed]);
  return (
    <section className={`needs ${collapsed ? 'collapsed' : ''}`}>
      <header className="sec-head toggle-head" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed}>
        <i className="chev" />
        <h2>Needs you</h2>
        {stops.length + list.length > 0 && <span className={`countpill ${stops.length ? 'c-stop' : 'c-warn'}`}>{stops.length + list.length}</span>}
        <span className="grow" />
        {showActAs && (
          <Menu label="Act as" prefix="Act as" value={actor} onChange={onActAs} disabled={readOnly} options={world.controllers.map((c) => ({ value: c._id, label: c.name }))} />
        )}
      </header>
      <div className="needs-body scroll">
        {stops.map((d) => {
          const spoken = transmissions.find((t) => t.decision_id === d._id);
          return (
            <article key={d._id} className="stop" data-decision={d._id} role="alert">
              <div className="stop-top">
                                <b>Stop in effect</b>
                <span className="mono">
                  {hhmmss(d.t)} · {ago(Date.now() - Date.parse(d.t))}
                </span>
              </div>
              <p className="call">{d.text ?? '(no text)'}</p>
              <p className="told">
                {d.approved_by ? `Approved by ${ctlName(d.approved_by)}` : 'Issued by Phylax'} to {d.target.join(', ') || 'traffic'}
                {spoken ? ` on ${streamName(spoken.stream_id)}` : d.stream_id ? ` on ${streamName(d.stream_id)}` : ''} · {ctlName(d.controller_id)} told
              </p>
              {errors[d._id] && !readOnly && (
                <div className="req-error" role="alert">
                  {errors[d._id]}
                </div>
              )}
              <div className="req-actions">
                <button className="btn lift" disabled={readOnly || busy === d._id} onClick={() => run(d, 'revoke')}>
                  {busy === d._id ? 'Lifting…' : 'Lift stop'}
                </button>
                <button className="btn" onClick={() => window.dispatchEvent(new Event('phylax:focus-conflict'))} title="Frame the conflict on the map">
                  Focus conflict
                </button>
              </div>
            </article>
          );
        })}
        {list.map((d) => (
          <article key={d._id} className="req">
            <div className="req-top">
              <b>{d.intent_action === 'stop' ? 'Stop proposed' : 'Proposed instruction'}</b>
              <span className="mono">
                {hhmmss(d.t)}
                {d.stream_id ? ` · ${streamName(d.stream_id)}` : ''}
              </span>
            </div>
            <p className="call">{d.text ?? '(no text)'}</p>
            {(d.intent?.detail || d.intent?.reason) && <p className="told">{d.intent?.detail ?? d.intent?.reason}</p>}
            {errors[d._id] && (
              <div className="req-error" role="alert">
                {errors[d._id]}
              </div>
            )}
            <div className="req-actions">
              <button className="btn approve" disabled={readOnly || busy === d._id} onClick={() => run(d, 'approve')}>
                Approve
              </button>
              <button className="btn reject" disabled={readOnly || busy === d._id} onClick={() => run(d, 'reject')}>
                Reject
              </button>
            </div>
          </article>
        ))}
        {calm && (
          <div className="calm">
            <b>No alerts</b>
            <span>{readOnly ? 'Nothing needed a controller at this time.' : 'Phylax is watching every runway and taxiway.'}</span>
          </div>
        )}
        {advisories.map((d) => (
          <div key={d._id} className="advisory" title={d.rule}>
            <span className="mono">{hhmmss(d.t)}</span>
            <span className="adv-text">{d.text ?? d.intent?.detail ?? d.rule}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
