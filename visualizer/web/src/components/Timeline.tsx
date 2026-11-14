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

/** One slim scrub bar: event marks, the playhead, and the way back to live. */
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
  return (
    <div className={`timeline ${p.live ? 'is-live' : 'is-history'}`}>
      <button className="tl-play" disabled={p.live} onClick={() => p.onPlay(!p.playing)} aria-label={p.playing ? 'Pause' : 'Play'} title={p.live ? 'Drag the bar to look back' : p.playing ? 'Pause' : 'Play'}>
        {p.playing && !p.live ? '❚❚' : '▶'}
      </button>
      <div className="tl-mid">
        <div className="tl-ends mono">
          <span>{hhmmss(p.from)}</span>
          <span className="tl-hint">{p.live ? 'Drag to look back' : `${hhmmss(p.playhead)} · ${ago(p.to - p.playhead)} behind live`}</span>
          <span>{hhmmss(p.to)}</span>
        </div>
        <div
          className="tl-track"
          ref={track}
          onPointerDown={down}
          onPointerMove={move}
          role="slider"
          aria-label="Timeline"
          aria-valuemin={p.from}
          aria-valuemax={p.to}
          aria-valuenow={p.playhead}
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') p.onScrub(p.playhead - 5000);
            if (e.key === 'ArrowRight') p.onScrub(p.playhead + 5000);
          }}
        >
          <div className="tl-rail" />
          <div className="tl-fill" style={{ width: x(p.live ? p.to : p.playhead) }} />
          {p.marks
            .filter((m) => m.type !== 'run')
            .map((m, j) => (
              <i key={j} className={`mk mk-${m.type} sev-${m.severity ?? 'none'}`} style={{ left: x(Date.parse(m.t)) }} title={`${hhmmss(m.t)}  ${m.label}`} />
            ))}
          <div className="tl-head" style={{ left: x(p.live ? p.to : p.playhead) }} />
        </div>
      </div>
      <div className="seg small" role="group" aria-label="Playback speed">
        {SPEEDS.map((s) => (
          <button key={s} className={p.speed === s && !p.live ? 'on' : ''} disabled={p.live} onClick={() => p.onSpeed(s)}>
            {s}x
          </button>
        ))}
      </div>
      <button className={`live-btn ${p.live ? 'on' : ''}`} onClick={p.onLive} title="Return to the live picture">
        <span className="dot" /> LIVE
      </button>
    </div>
  );
}
