import { useEffect, useRef, useState } from 'react';
import { useStored } from '../collapse';
import type { Decision, Stream, Transmission, World } from '../types';
import { stationName } from '../util';
import { endOf, ListenButton, N, peaksFor, short, startOf, tag } from './Radio';
import type { NowPlaying } from './Radio';

interface Props {
  world: World;
  streams: Stream[];
  transmissions: Transmission[];
  decisions: Decision[];
  /** The timeline's window: the lanes use the same scale, so marks line up with the audio. */
  from: number;
  to: number;
  cur: number;
  live: boolean;
  mock: boolean;
  audioUrl: (clipId: string) => string;
  nowPlaying: NowPlaying | null;
  solo: string;
  onSolo: (id: string) => void;
  onPick: (tx: Transmission) => void;
}

/** The streams as a multitrack view above the timeline, on the timeline's own time axis. */
export function StreamDock(p: Props) {
  const [collapsed, setCollapsed] = useStored('dock.collapsed');
  const [track, setTrack] = useState({ left: 520, width: 400 });
  const root = useRef<HTMLDivElement>(null);
  const canvases = useRef<Record<string, HTMLCanvasElement | null>>({});
  const latest = useRef(p);
  latest.current = p;
  const ctlName = (id: string) => p.world.controllers.find((c) => c._id === id)?.name ?? id;
  const upTo = p.transmissions.filter((t) => startOf(t) <= p.cur + 500);
  const onAir = (id: string) => upTo.some((t) => t.stream_id === id && p.cur >= startOf(t) && p.cur <= endOf(t) + 1500);

  useEffect(() => {
    let raf = 0;
    let n = 0;
    const draw = () => {
      raf = requestAnimationFrame(draw);
      const q = latest.current;
      // follow the timeline track, so both share one horizontal scale
      if (n++ % 12 === 0 && root.current) {
        const tr = document.querySelector('.tl-track')?.getBoundingClientRect();
        const rr = root.current.getBoundingClientRect();
        if (tr && tr.width > 20) setTrack((old) => (Math.abs(old.left - (tr.left - rr.left)) > 0.5 || Math.abs(old.width - tr.width) > 0.5 ? { left: tr.left - rr.left, width: tr.width } : old));
      }
      const span = Math.max(1000, q.to - q.from);
      const dpr = window.devicePixelRatio || 1;
      for (const s of q.streams) {
        const cv = canvases.current[s._id];
        if (!cv) continue;
        const w = cv.clientWidth;
        const h = cv.clientHeight;
        if (w < 4) continue;
        if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
          cv.width = Math.round(w * dpr);
          cv.height = Math.round(h * dpr);
        }
        const g = cv.getContext('2d');
        if (!g) continue;
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, w, h);
        const mid = Math.floor(h / 2);
        const xOf = (t: number) => ((t - q.from) / span) * w;
        const xc = Math.min(w - 1, xOf(q.cur));
        g.fillStyle = '#30363d';
        g.fillRect(0, mid, w, 1);
        for (const tx of q.transmissions) {
          if (tx.stream_id !== s._id) continue;
          const t0 = startOf(tx);
          const t1 = endOf(tx);
          if (t1 < q.from || t0 > q.to) continue;
          const pk = peaksFor(tx, q.mock ? null : q.audioUrl);
          const x0 = xOf(t0);
          const x1 = Math.max(xOf(t1), x0 + 3); // a clip is never thinner than 3 px
          const playing = !!q.nowPlaying && tx.clip_id === q.nowPlaying.clipId;
          for (let x = Math.max(0, Math.floor(x0)); x < Math.min(w, x1); x++) {
            const amp = Math.max(2, Math.round(pk[Math.min(N - 1, Math.floor(((x - x0) / (x1 - x0)) * N))] * (mid - 2)));
            g.fillStyle = playing ? '#4c9aff' : x > xc ? '#4a5463' : '#8b949e'; // after the playhead: dimmed
            g.fillRect(x, mid - amp, 1, amp * 2 + 1);
          }
        }
        g.fillStyle = q.live ? '#e6edf3' : '#d29922';
        g.fillRect(Math.max(0, xc - 0.5), 0, 1.5, h);
      }
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  const pick = (s: Stream, e: React.MouseEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const span = Math.max(1000, p.to - p.from);
    const t = p.from + ((e.clientX - r.left) / r.width) * span;
    const tol = (5 / r.width) * span;
    let best: Transmission | null = null;
    let bd = Infinity;
    for (const tx of p.transmissions) {
      if (tx.stream_id !== s._id) continue;
      const d = t < startOf(tx) ? startOf(tx) - t : t > endOf(tx) ? t - endOf(tx) : 0;
      if (d <= tol && d < bd) [best, bd] = [tx, d];
    }
    if (best) p.onPick(best);
  };

  return (
    <section className={`sdock ${collapsed ? 'collapsed' : ''}`} ref={root} aria-label="Streams">
      <header className="sdock-head" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed}>
        <i className={`chev ${collapsed ? 'shut' : ''}`} />
        <h2>Streams</h2>
        {collapsed ? (
          p.streams.map((s) => (
            <span key={s._id} className="schip">
              <i className={`dot ${onAir(s._id) ? 'on' : ''}`} />
              {short(s)} <span className="num">{s.frequency_mhz?.toFixed(1)}</span>
              {p.solo === s._id && <span className="schip-on">listening</span>}
            </span>
          ))
        ) : (
          <span className="sec-note">on the timeline's scale · click a clip to play it</span>
        )}
      </header>
      {!collapsed &&
        p.streams.map((s) => {
          const mine = upTo.filter((t) => t.stream_id === s._id);
          const last = mine.reduce<Transmission | undefined>((m, t) => (!m || t.t > m.t ? t : m), undefined);
          const b = last ? tag(last, p.decisions) : null;
          return (
            <div key={s._id} className="strack" data-stream={s._id} style={{ gridTemplateColumns: `${track.left}px ${track.width}px minmax(0, 1fr)` }}>
              <div className="strack-label">
                <span className="strack-name">
                  <i className={`dot ${onAir(s._id) ? 'on' : ''}`} />
                  <b>{short(s)}</b>
                  <span className="num">{s.frequency_mhz?.toFixed(1)}</span>
                  <span className="strack-owner">{ctlName(s.controller_id)}</span>
                </span>
                <ListenButton on={p.solo === s._id} onClick={() => p.onSolo(p.solo === s._id ? 'all' : s._id)} />
                <span className="strack-text">{last ? last.transcript : 'No traffic yet'}</span>
              </div>
              <canvas
                className="strack-wave"
                onClick={(e) => pick(s, e)}
                ref={(el) => {
                  canvases.current[s._id] = el;
                }}
              />
              <div className="strack-last">
                {last && (
                  <>
                    <span className="mono c-who">
                      {stationName(last.from)} → {stationName(last.to)}
                    </span>
                    {b && <span className={`tag tone-${b.tone}`}>{b.text}</span>}
                  </>
                )}
              </div>
            </div>
          );
        })}
    </section>
  );
}
