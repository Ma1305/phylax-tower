import { useRef, useState, type ReactNode } from 'react';
import type { Mark } from '../types';
import { ago, hhmmss } from '../util';
import { Menu } from './Menu';

interface Props {
  from: number;
  to: number; // "now"
  playhead: number;
  live: boolean;
  playing: boolean;
  speed: number;
  marks: Mark[];
  win: number; // minutes shown, 0 = everything
  onWin: (m: number) => void;
  onScrub: (t: number, play?: boolean) => void;
  onLive: () => void;
  onToggle: () => void;
  onSeek: (deltaMs: number) => void;
  onStep: (dir: -1 | 1) => void;
  onSpeed: (s: number) => void;
  /** Highlights run: which one is showing. */
  hl: { i: number; n: number; label: string } | null;
  hlCount: number;
  onHighlights: () => void;
  onHlStep: (dir: -1 | 1) => void;
  onHlExit: () => void;
  skip: boolean;
  onSkip: (v: boolean) => void;
  toast: string;
  children?: ReactNode;
}

/** Marks the skip buttons stop at: stops and proposals (decisions), conflicts (alerts), simulator runs. */
export const isMajor = (m: Mark) => m.type === 'decision' || m.type === 'run' || (m.type === 'alert' && (m.severity === 'critical' || m.severity === 'warning'));

const Icon = ({ d }: { d: string }) => (
  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
    <path d={d} fill="currentColor" />
  </svg>
);

/** The transport: a full-width scrub bar and, under it, the controls. */
export function Timeline(p: Props) {
  const track = useRef<HTMLDivElement>(null);
  const [typing, setTyping] = useState<string | null>(null);
  const span = Math.max(1000, p.to - p.from);
  const cur = p.live ? p.to : p.playhead;
  const x = (t: number) => `${Math.max(0, Math.min(100, ((t - p.from) / span) * 100))}%`;
  const at = (clientX: number) => {
    const r = track.current!.getBoundingClientRect();
    return p.from + Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * span;
  };
  const down = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    p.onScrub(at(e.clientX), p.playing && !p.live);
  };
  const move = (e: React.PointerEvent) => {
    if (e.buttons & 1) p.onScrub(at(e.clientX), false);
  };
  const jumpTyped = () => {
    const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec((typing ?? '').trim());
    setTyping(null);
    if (!m) return;
    const d = new Date(p.to);
    d.setUTCHours(+m[1], +m[2], +(m[3] ?? 0), 0);
    let t = d.getTime();
    if (t > p.to) t -= 86_400_000; // the most recent time with that reading
    p.onScrub(t, false);
  };
  return (
    <div className={`transport ${p.live ? 'is-live' : 'is-history'}`}>
      <div className="tl-bar">
        <span className="mono tl-end">{hhmmss(p.from)}</span>
        <div className="tl-track" ref={track} onPointerDown={down} onPointerMove={move} role="slider" aria-label="Timeline" aria-valuemin={p.from} aria-valuemax={p.to} aria-valuenow={cur} aria-valuetext={hhmmss(cur)} tabIndex={0}>
          <div className="tl-rail" />
          <div className="tl-fill" style={{ width: x(cur) }} />
          {p.marks
            .filter((m) => Date.parse(m.t) >= p.from)
            .map((m, j) => (
              <i
                key={j}
                className={`mk mk-${m.type} sev-${m.severity ?? 'none'} ${isMajor(m) ? 'major' : ''}`}
                style={{ left: x(Date.parse(m.t)) }}
                title={`${hhmmss(m.t)}  ${m.label} · click to jump`}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  p.onScrub(Date.parse(m.t) - 2000, p.playing && !p.live);
                }}
              />
            ))}
          <div className="tl-head" style={{ left: x(cur) }} />
        </div>
        <span className="mono tl-end">{hhmmss(p.to)}</span>
      </div>
      <div className="tl-controls">
        <div className="seg" role="group" aria-label="Transport">
          <button onClick={() => p.onStep(-1)} title="Previous event (Shift+Left)" aria-label="Previous event">
            <Icon d="M1 1h2v10H1zM11 1v10L4 6z" />
          </button>
          <button onClick={() => p.onSeek(-10_000)} title="Back 10 seconds" aria-label="Back 10 seconds" className="num">
            -10s
          </button>
          <button onClick={p.onToggle} title={p.live ? 'Pause here (Space)' : p.playing ? 'Pause (Space)' : 'Play from here (Space)'} aria-label={p.live || p.playing ? 'Pause' : 'Play'} className="tl-play">
            {p.live || p.playing ? <Icon d="M2 1h3v10H2zM7 1h3v10H7z" /> : <Icon d="M2 1l9 5-9 5z" />}
          </button>
          <button onClick={() => p.onSeek(10_000)} disabled={p.live} title="Forward 10 seconds" aria-label="Forward 10 seconds" className="num">
            +10s
          </button>
          <button onClick={() => p.onStep(1)} disabled={p.live} title="Next event (Shift+Right)" aria-label="Next event">
            <Icon d="M9 1h2v10H9zM1 1v10l7-5z" />
          </button>
        </div>
        {typing != null ? (
          <input
            className="tl-time mono"
            autoFocus
            value={typing}
            placeholder="HH:MM:SS"
            aria-label="Jump to time"
            onChange={(e) => setTyping(e.target.value)}
            onBlur={() => setTyping(null)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === 'Enter') jumpTyped();
              if (e.key === 'Escape') setTyping(null);
            }}
          />
        ) : (
          <button className="tl-time mono" onClick={() => setTyping(hhmmss(cur))} title="Click to type a time (HH:MM:SS) and jump">
            {hhmmss(cur)}
          </button>
        )}
        <span className="tl-state">{p.live ? 'live' : `${ago(p.to - p.playhead)} behind${p.playing ? '' : ' · paused'}`}</span>
        {p.toast && <span className="tl-toast">{p.toast}</span>}
        {p.hl ? (
          <div className="hl-bar" role="status">
            <button className="btn" onClick={() => p.onHlStep(-1)} aria-label="Previous highlight" title="Previous highlight">
              ‹
            </button>
            <span className="hl-text">
              <b>
                Highlight {p.hl.i + 1} of {p.hl.n}
              </b>{' '}
              · {p.hl.label}
            </span>
            <button className="btn" onClick={() => p.onHlStep(1)} aria-label="Next highlight" title="Next highlight">
              ›
            </button>
            <button className="btn" onClick={p.onHlExit} title="Leave highlights (Esc)">
              Exit
            </button>
          </div>
        ) : (
          <>
            <button className="btn" onClick={p.onHighlights} disabled={p.hlCount === 0} title={p.hlCount ? `Play only the ${p.hlCount} major events of this window` : 'No major event in this window'}>
              Highlights
            </button>
            <button className={`btn skip ${p.skip ? 'on' : ''}`} role="switch" aria-checked={p.skip} onClick={() => p.onSkip(!p.skip)} title="While playing history, jump over stretches longer than 4 s with nothing happening">
              <i className="dot" /> Skip quiet gaps
            </button>
            <Menu label="Playback speed" up value={p.speed} onChange={p.onSpeed} options={[1, 4, 16].map((s) => ({ value: s, label: `${s}x` }))} />
            <Menu
              label="Timeline window"
              up
              value={p.win}
              onChange={p.onWin}
              options={[
                { value: 5, label: 'Last 5 min' },
                { value: 15, label: 'Last 15 min' },
                { value: 60, label: 'Last hour' },
                { value: 0, label: 'Everything' },
              ]}
            />
          </>
        )}
        <button className={`live-btn ${p.live ? 'on' : ''}`} onClick={p.onLive} title="Return to the live picture (L)">
          <span className="dot" /> LIVE
        </button>
        <span className="grow" />
        {p.children}
      </div>
    </div>
  );
}
