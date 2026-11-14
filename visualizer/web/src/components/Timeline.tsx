import { useRef } from 'react';
import type { Mark, Stream } from '../types';
import { ago, hhmmss } from '../util';

interface Props {
  from: number;
  to: number; // "now"
  playhead: number;
  live: boolean;
  playing: boolean;
  speed: number;
  marks: Mark[];
  streams: Stream[];
  onScrub: (t: number) => void;
  onLive: () => void;
  onPlay: (playing: boolean) => void;
  onSpeed: (s: number) => void;
}

const SPEEDS = [1, 4, 16];

export function Timeline(p: Props) {
  const track = useRef<HTMLDivElement>(null);
  const span = Math.max(1000, p.to - p.from);
  const x = (t: number) => `${Math.max(0, Math.min(100, ((t - p.from) / span) * 100))}%`;
  const at = (clientX: number) => {
    const r = track.current!.getBoundingClientRect();
    return p.from + Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * span;
  };
  const down = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    p.onScrub(at(e.clientX));
  };
  const move = (e: React.PointerEvent) => {
    if (e.buttons & 1) p.onScrub(at(e.clientX));
  };

  const rows = [
    ...p.streams.map((s) => ({ id: s._id, label: s.station })),
    { id: '_agents', label: 'AGENTS' },
  ];
  const step = span > 20 * 60_000 ? 300_000 : span > 6 * 60_000 ? 60_000 : 30_000;
  const ticks: number[] = [];
  for (let t = Math.ceil(p.from / step) * step; t < p.to; t += step) ticks.push(t);
  const runs = p.marks.filter((m) => m.type === 'run');

  return (
    <footer className={`timeline ${p.live ? 'is-live' : 'is-history'}`}>
      <div className="tl-controls">
        <button className={`btn live-btn ${p.live ? 'on' : ''}`} onClick={p.onLive} title="Return to the live picture">
          <span className="dot" /> LIVE
        </button>
        <button className="btn icon" disabled={p.live} onClick={() => p.onPlay(!p.playing)} aria-label={p.playing ? 'Pause' : 'Play'} title={p.live ? 'Drag the bar to enter history' : p.playing ? 'Pause' : 'Play'}>
          {p.playing && !p.live ? '❚❚' : '▶'}
        </button>
        <div className="seg small" role="group" aria-label="Playback speed">
          {SPEEDS.map((s) => (
            <button key={s} className={p.speed === s && !p.live ? 'on' : ''} disabled={p.live} onClick={() => p.onSpeed(s)}>
              {s}x
            </button>
          ))}
        </div>
        <div className="tl-readout">
          <span className="mono big">{hhmmss(p.live ? p.to : p.playhead)}Z</span>
          <span className={p.live ? 'muted small' : 'hist-text small'}>{p.live ? 'live · drag the bar to look back' : `${ago(p.to - p.playhead)} behind live`}</span>
        </div>
      </div>

      <div className="tl-main">
        <div className="tl-labels">
          {rows.map((r) => (
            <span key={r.id}>{r.label}</span>
          ))}
        </div>
        <div className="tl-track" ref={track} onPointerDown={down} onPointerMove={move} role="slider" aria-label="Timeline" aria-valuemin={p.from} aria-valuemax={p.to} aria-valuenow={p.playhead} tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') p.onScrub(p.playhead - 5000);
            if (e.key === 'ArrowRight') p.onScrub(p.playhead + 5000);
          }}>
          {ticks.map((t) => (
            <div key={t} className="tl-tick" style={{ left: x(t) }}>
              <span className="mono">{hhmmss(t).slice(0, step >= 60_000 ? 5 : 8)}</span>
            </div>
          ))}
          {runs.map((m, i) => (
            <div key={i} className="tl-run" style={{ left: x(Date.parse(m.t)) }} title={m.label}>
              <span>▸ sim run</span>
            </div>
          ))}
          {rows.map((r, i) => (
            <div key={r.id} className="tl-row" style={{ top: 16 + i * 17 }}>
              {p.marks
                .filter((m) => (r.id === '_agents' ? m.type === 'decision' || m.type === 'alert' || m.type === 'permission' : m.type === 'transmission' && m.stream_id === r.id))
                .map((m, j) => (
                  <i key={j} className={`mk mk-${m.type} sev-${m.severity ?? 'none'}`} style={{ left: x(Date.parse(m.t)) }} title={`${hhmmss(m.t)}  ${m.label}`} />
                ))}
            </div>
          ))}
          {!p.live && <div className="tl-future" style={{ left: x(p.playhead) }} />}
          <div className="tl-head" style={{ left: x(p.live ? p.to : p.playhead) }}>
            <span className="mono">{p.live ? 'LIVE' : hhmmss(p.playhead)}</span>
          </div>
        </div>
      </div>
      <div className="tl-key">
        <span><i className="mk mk-transmission" /> transmission</span>
        <span><i className="mk mk-alert" /> advisory</span>
        <span><i className="mk mk-decision" /> stop / proposal</span>
        <span><i className="mk mk-permission" /> switch change</span>
      </div>
    </footer>
  );
}
