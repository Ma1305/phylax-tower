import { useState } from 'react';
import { ApiError, type DataSource, type Decision, type Transmission, type World } from '../types';
import { ago, hhmmss } from '../util';

interface Props {
  ds: DataSource;
  world: World;
  stops: Decision[]; // executed stops still in effect, already narrowed to the selected controller
  transmissions: Transmission[];
  actor: string; // controller id the "Lift stop" action is sent as
  viewT: number;
  readOnly: boolean;
}

/**
 * Stops are autonomous by default (PLAN.md decision 4): Hold Short has already made the call.
 * The owning controller is told, not asked, and can lift the stop (POST /control/revoke).
 */
export function StopAlerts({ ds, world, stops, transmissions, actor, viewT, readOnly }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  if (stops.length === 0) return null;
  const ctlName = (id: string | null | undefined) => world.controllers.find((c) => c._id === id)?.name ?? id ?? 'nobody';
  const laneName = (id: string | null) => world.lanes.find((l) => l._id === id)?.name ?? id ?? 'no lane';
  const streamName = (id: string | null) => world.streams.find((s) => s._id === id)?.name ?? id;

  const lift = async (d: Decision) => {
    setBusy(d._id);
    setErrors((e) => ({ ...e, [d._id]: '' }));
    try {
      await ds.revoke(d._id, actor);
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      const msg = err instanceof Error ? err.message : String(err);
      setErrors((e) => ({ ...e, [d._id]: status ? `Stop not lifted: ${status} · ${msg}` : `Stop not lifted: the tower API is not reachable (${msg}).` }));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="stops" role="alert" aria-label="Stops in effect">
      {stops.map((d) => {
        const spoken = transmissions.find((t) => t.decision_id === d._id);
        const auto = !d.approved_by;
        return (
          <article key={d._id} className="stop" data-decision={d._id}>
            <div className="stop-flag">
              <span className="stop-sign" aria-hidden>
                ■
              </span>
              <div>
                <b>STOP IN EFFECT</b>
                <span>{auto ? 'issued by Hold Short · not a request' : `approved by ${ctlName(d.approved_by)}`}</span>
              </div>
            </div>
            <div className="stop-body">
              <p className="stop-call">
                <span className="cap">{spoken ? `Transmitted ${hhmmss(spoken.t)}Z on ${streamName(spoken.stream_id)}` : d.stream_id ? `Exact call · ${streamName(d.stream_id)}` : 'Exact call'}</span>
                “{d.text ?? '(no text)'}”
              </p>
              <dl className="stop-kv">
                <div>
                  <dt>Target</dt>
                  <dd className="mono strong">{d.target.length ? d.target.join(', ') : 'none'}</dd>
                </div>
                <div>
                  <dt>Rule</dt>
                  <dd className="mono">{d.intent?.reason ?? d.rule}</dd>
                </div>
                <div>
                  <dt>Lane</dt>
                  <dd>{laneName(d.lane_id)}</dd>
                </div>
                <div>
                  <dt>Told</dt>
                  <dd>{ctlName(d.controller_id)}</dd>
                </div>
                <div>
                  <dt>Since</dt>
                  <dd className="mono">
                    {hhmmss(d.t)}Z · {ago(viewT - Date.parse(d.t))}
                  </dd>
                </div>
              </dl>
              {d.intent?.detail && <p className="stop-why">{d.intent.detail}</p>}
              {errors[d._id] && !readOnly && (
                <div className="req-error" role="alert">
                  {errors[d._id]}
                </div>
              )}
            </div>
            <div className="stop-act">
              <button className="btn lift" disabled={readOnly || busy === d._id} onClick={() => lift(d)} title="POST /control/revoke: the stop no longer applies; you take it from here">
                {busy === d._id ? 'Lifting…' : 'Lift stop'}
              </button>
              <span className="small">{readOnly ? 'history: actions disabled' : `as ${ctlName(actor)}`}</span>
            </div>
          </article>
        );
      })}
    </section>
  );
}
