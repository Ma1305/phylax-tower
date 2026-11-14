import { useEffect, useRef, useState, type RefObject } from 'react';
import type { Decision, Stream, Transmission, World } from '../types';
import { hhmmss, stationName } from '../util';
import { useStored } from '../collapse';

export interface NowPlaying {
  clipId: string;
  label: string;
}

interface Props {
  world: World;
  streams: Stream[];
  transmissions: Transmission[];
  decisions: Decision[];
  loading: boolean;
  solo: string;
  onSolo: (id: string) => void;
  live: boolean;
  /** The time being looked at: now in live mode, the playhead in history. */
  viewT: number;
  audioRef: RefObject<HTMLAudioElement>;
  audioUrl: (clipId: string) => string;
  mock: boolean;
  nowPlaying: NowPlaying | null;
  blocked: boolean;
  onPlay: (tx: Transmission) => void;
  onEnded: () => void;
  emptyNote: string | null;
  /** The expanded row (shared with the stream dock, which selects a row when a clip is clicked). */
  open: string | null;
  onOpen: (id: string | null) => void;
}

export const WINDOW_MS = 60_000;
export const N = 240;
const SHORT: Record<string, string> = { tower: 'TWR', ground: 'GND', ops: 'OPS' };
export const short = (s?: Stream) => (s ? (SHORT[s.kind] ?? s.station.slice(0, 3)) : '');
export const startOf = (t: Transmission) => Date.parse(t.t);
export const endOf = (t: Transmission) => (t.t_end ? Date.parse(t.t_end) : startOf(t) + Math.max(1200, t.transcript.length * 65));

export function tag(t: Transmission, decisions: Decision[]): { text: string; tone: string } | null {
  if (t.synthetic) {
    const d = t.decision_id ? decisions.find((x) => x._id === t.decision_id) : undefined;
    return { text: 'Phylax', tone: d?.intent_action === 'stop' || t.kind === 'stop' ? 'stop' : 'info' };
  }
  if (!t.verified || t.flag) return { text: 'Unclear', tone: 'warn' };
  if (t.readback === 'matched') return { text: 'Readback OK', tone: 'ok' };
  if (t.readback === 'mismatch') return { text: 'Mismatch', tone: 'stop' };
  if (t.kind === 'request') return { text: 'Request', tone: 'plain' };
  if (t.kind === 'clearance') return { text: 'Clearance', tone: 'plain' };
  return null;
}

// ---- amplitude envelopes: decoded from the real clip when possible, else seeded from the clip ----
const peaksCache = new Map<string, Float32Array>();
const tried = new Set<string>();
let decodeCtx: AudioContext | null = null;

function seeded(t: Transmission): Float32Array {
  let h = 2166136261;
  const key = (t.clip_id ?? t._id) + t.transcript.length;
  for (let i = 0; i < key.length; i++) h = Math.imul(h ^ key.charCodeAt(i), 16777619) >>> 0;
  const rnd = () => ((h = (Math.imul(h, 1103515245) + 12345) >>> 0) >>> 8) / 16777216;
  const text = t.transcript || 'x';
  const raw = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const ch = text[Math.floor((i / N) * text.length)];
    const gap = ch === ' ' || ch === ',' || ch === '.';
    raw[i] = gap ? 0.04 + rnd() * 0.08 : 0.3 + rnd() * 0.7;
  }
  const out = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const edge = Math.min(1, i / 6, (N - 1 - i) / 6);
    out[i] = ((raw[Math.max(0, i - 1)] + raw[i] * 2 + raw[Math.min(N - 1, i + 1)]) / 4) * edge;
  }
  return out;
}

export function peaksFor(t: Transmission, url: ((id: string) => string) | null): Float32Array {
  const id = t.clip_id ?? t._id;
  const hit = peaksCache.get(id);
  if (hit) return hit;
  const fallback = seeded(t);
  peaksCache.set(id, fallback);
  if (url && t.clip_id && t.audio_id && !tried.has(id) && typeof AudioContext !== 'undefined') {
    tried.add(id);
    fetch(url(t.clip_id))
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status)))))
      .then((buf) => (decodeCtx ??= new AudioContext()).decodeAudioData(buf))
      .then((audio) => {
        const d = audio.getChannelData(0);
        const step = Math.max(1, Math.floor(d.length / N));
        const out = new Float32Array(N);
        let max = 0.0001;
        for (let i = 0; i < N; i++) {
          let pk = 0;
          for (let k = i * step; k < Math.min(d.length, (i + 1) * step); k++) pk = Math.max(pk, Math.abs(d[k]));
          out[i] = pk;
          max = Math.max(max, pk);
        }
        for (let i = 0; i < N; i++) out[i] /= max;
        peaksCache.set(id, out);
      })
      .catch(() => undefined); // keep the seeded envelope
  }
  return fallback;
}

/** One clip's own envelope; the played part turns blue while it plays. */
function ClipWave({ tx, playing, audioRef, url }: { tx: Transmission; playing: boolean; audioRef: RefObject<HTMLAudioElement>; url: ((id: string) => string) | null }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const hasAudio = !!tx.clip_id && !!tx.audio_id;
  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    let raf = 0;
    const timers: number[] = [];
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth;
      const h = cv.clientHeight;
      if (w < 4) return;
      if (cv.width !== Math.round(w * dpr)) {
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
      }
      const g = cv.getContext('2d');
      if (!g) return;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const mid = Math.floor(h / 2);
      g.fillStyle = '#30363d';
      g.fillRect(0, mid, w, 1);
      if (!hasAudio) return;
      const pk = peaksFor(tx, url);
      const a = audioRef.current;
      const prog = playing && a && Number.isFinite(a.duration) && a.duration > 0 ? a.currentTime / a.duration : 0;
      for (let x = 0; x < w; x++) {
        const amp = Math.round(pk[Math.min(N - 1, Math.floor((x / w) * N))] * (mid - 1));
        g.fillStyle = playing && x / w <= prog ? '#4c9aff' : '#8b949e';
        g.fillRect(x, mid - amp, 1, amp * 2 + 1);
      }
    };
    if (playing) {
      const loop = () => {
        draw();
        raf = requestAnimationFrame(loop);
      };
      loop();
    } else {
      draw();
      for (const ms of [500, 1500, 4000]) timers.push(window.setTimeout(draw, ms)); // the decoded envelope arrives later
    }
    return () => {
      cancelAnimationFrame(raf);
      timers.forEach(clearTimeout);
    };
  }, [tx, playing, audioRef, url, hasAudio]);
  return <canvas ref={ref} className="clip-wave" />;
}

export const Meter = () => (
  <span className="meter" aria-hidden>
    <i />
    <i />
    <i />
  </span>
);

/** Listen to one stream: its calls play aloud as they arrive. */
export function ListenButton({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className={`listen ${on ? 'on' : ''}`}
      aria-pressed={on}
      title="Play this stream's calls aloud as they arrive"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
        <path d="M1 4.2h2.2L6 1.8v8.4L3.2 7.8H1z" fill="currentColor" />
        <path d="M8 4a3 3 0 010 4M9.6 2.4a5.2 5.2 0 010 7.2" fill="none" stroke="currentColor" strokeWidth="1.1" />
      </svg>
      {on ? 'Listening live' : 'Listen live'}
      {on && <Meter />}
    </button>
  );
}

export function Radio(p: Props) {
  const [paused, setPaused] = useState(true);
  const [filter, setFilter] = useState('all');
  const open = p.open;
  const setOpen = p.onOpen;
  const [logOff, setLogOff] = useStored('log.collapsed');

  const shownIds = new Set(p.streams.map((s) => s._id));
  const activeFilter = filter !== 'all' && shownIds.has(filter) ? filter : 'all';
  const upTo = p.transmissions.filter((t) => startOf(t) <= p.viewT + 500);
  const lines = upTo
    .filter((t) => (activeFilter === 'all' ? shownIds.has(t.stream_id) : t.stream_id === activeFilter))
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 80);
  const streamOf = (id: string) => p.world.streams.find((s) => s._id === id);
  const ctlName = (id: string | null | undefined) => p.world.controllers.find((c) => c._id === id)?.name ?? id ?? '';
  const audible = !!p.nowPlaying && !paused;
  const url = p.mock ? null : p.audioUrl;

  const want = p.solo;

  useEffect(() => {
    const a = p.audioRef.current;
    if (!a) return;
    const sync = () => setPaused(a.paused || a.ended);
    const evs = ['play', 'playing', 'pause', 'ended', 'emptied'];
    for (const ev of evs) a.addEventListener(ev, sync);
    return () => {
      for (const ev of evs) a.removeEventListener(ev, sync);
    };
  }, [p.audioRef]);

  // a row selected from the stream dock scrolls into view
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) listRef.current?.querySelector(`[data-tx="${open}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [open]);

  const playBtn = (t: Transmission, playing: boolean) => {
    const a = p.audioRef.current;
    if (playing && a) {
      if (a.paused) void a.play().catch(() => undefined);
      else a.pause();
    } else p.onPlay(t);
  };

  return (
    <section className={`radio ${logOff ? 'log-shut' : ''}`}>
      <header className="sec-head toggle-head" onClick={() => setLogOff(!logOff)} aria-expanded={!logOff}>
        <i className={`chev ${logOff ? 'shut' : ''}`} />
        <h2>Radio</h2>
        <span className="countpill">{lines.length}</span>
        <span className="grow" />
        <span className="radio-tools" onClick={(e) => e.stopPropagation()}>
          <div className="seg" role="group" aria-label="Show stream">
            <button className={activeFilter === 'all' ? 'on' : ''} onClick={() => setFilter('all')}>
              All
            </button>
            {p.streams.map((s) => (
              <button key={s._id} className={activeFilter === s._id ? 'on' : ''} onClick={() => setFilter(s._id)}>
                {s.name.replace(/^Airport /, '').replace(/\s*[\d.]+$/, '')}
              </button>
            ))}
          </div>
        </span>
      </header>
      {want !== 'all' && (
        <div className="listening-bar">
          <Meter />
          <span>
            Listening to <b>{streamOf(want)?.name ?? want}</b> · new calls play aloud as they arrive
          </span>
          <button className="btn" onClick={() => p.onSolo('all')}>
            Stop
          </button>
        </div>
      )}
      <div className={`log scroll ${logOff ? 'shut' : ''}`} ref={listRef}>
        {p.blocked && <div className="calm warn-text">The browser blocked autoplay. Click a row once to allow sound.</div>}
        {lines.length === 0 && p.streams.length > 0 && <div className="calm">{p.loading ? 'Loading transmissions…' : p.live ? 'No transmissions yet.' : 'Nothing had been said at this time.'}</div>}
        {lines.map((t) => {
          const playing = p.nowPlaying?.clipId === t.clip_id && !!t.clip_id;
          const hasAudio = !!t.clip_id && !!t.audio_id;
          const b = tag(t, p.decisions);
          const isOpen = open === t._id;
          const dec = t.decision_id ? p.decisions.find((d) => d._id === t.decision_id) : undefined;
          const lane = t.lane_id ? p.world.lanes.find((l) => l._id === t.lane_id)?.name : null;
          return (
            <div key={t._id} data-tx={t._id} data-clip={t.clip_id ?? ''} className={`clip ${playing ? 'playing' : ''} ${isOpen ? 'open' : ''} ${hasAudio ? '' : 'mute'}`}>
              <div
                className="clip-row"
                onClick={() => {
                  setOpen(isOpen ? null : t._id);
                  if (!isOpen && hasAudio && !(playing && audible)) p.onPlay(t);
                }}
              >
                {hasAudio ? (
                  <button
                    type="button"
                    className="clip-play"
                    aria-label={playing && audible ? 'Pause' : `Play ${stationName(t.from)} to ${stationName(t.to)}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      playBtn(t, playing);
                    }}
                  >
                    {playing && audible ? <i className="ico-pause" /> : <i className="ico-play" />}
                  </button>
                ) : (
                  <span className="clip-none">no audio</span>
                )}
                <ClipWave tx={t} playing={playing} audioRef={p.audioRef} url={url} />
                <div className="clip-main">
                  <div className="clip-meta">
                    <span className="c-time mono">{hhmmss(t.t)}</span>
                    <span className="tag">{short(streamOf(t.stream_id))}</span>
                    <span className="c-who mono">
                      {stationName(t.from)} → {stationName(t.to)}
                    </span>
                    <span className="grow" />
                    {b && <span className={`tag tone-${b.tone}`}>{b.text}</span>}
                  </div>
                  {!isOpen && <div className="clip-text">{t.transcript}</div>}
                </div>
              </div>
              {isOpen && (
                <div className="clip-detail">
                  <p>{t.transcript}</p>
                  <div className="clip-facts">
                    <span>confidence {t.confidence.toFixed(2)}</span>
                    {t.readback !== 'not_applicable' && <span>readback {t.readback}</span>}
                    {lane && <span>{lane}</span>}
                    {t.flag && <span className="warn-text">flag: {t.flag}</span>}
                    {!t.verified && !t.flag && <span className="warn-text">not verified</span>}
                    {t.synthetic && (
                      <span>
                        spoken by Phylax
                        {dec ? ` · ${dec.agent} ${dec.intent_action.replace('_', ' ')} · ${dec.approved_by ? `approved by ${ctlName(dec.approved_by)}` : 'autonomous'} · ${dec.rule}` : ''}
                      </span>
                    )}
                    {!hasAudio && <span>no audio stored for this call</span>}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <audio ref={p.audioRef} preload="none" onEnded={p.onEnded} />
    </section>
  );
}
