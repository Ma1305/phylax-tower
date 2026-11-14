import { useEffect, useRef, useState, type RefObject } from 'react';
import type { Decision, Stream, Transmission, World } from '../types';
import { hhmmss, stationName } from '../util';

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
}

const WINDOW_MS = 60_000;
const N = 240;
const SHORT: Record<string, string> = { tower: 'TWR', ground: 'GND', ops: 'OPS' };
const short = (s?: Stream) => (s ? (SHORT[s.kind] ?? s.station.slice(0, 3)) : '');
const mmss = (s: number) => (Number.isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '-:--');
const startOf = (t: Transmission) => Date.parse(t.t);
const endOf = (t: Transmission) => (t.t_end ? Date.parse(t.t_end) : startOf(t) + Math.max(1200, t.transcript.length * 65));

function tag(t: Transmission, decisions: Decision[]): { text: string; tone: string } | null {
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

function peaksFor(t: Transmission, url: ((id: string) => string) | null): Float32Array {
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

export function Radio(p: Props) {
  const [paused, setPaused] = useState(true);
  const [filter, setFilter] = useState('all');
  const canvases = useRef<Record<string, HTMLCanvasElement | null>>({});
  const clock = useRef<HTMLSpanElement>(null);
  const latest = useRef(p);
  latest.current = p;

  const shownIds = new Set(p.streams.map((s) => s._id));
  const activeFilter = filter !== 'all' && shownIds.has(filter) ? filter : 'all';
  const upTo = p.transmissions.filter((t) => startOf(t) <= p.viewT + 500);
  const lines = upTo
    .filter((t) => (activeFilter === 'all' ? shownIds.has(t.stream_id) : t.stream_id === activeFilter))
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 80);
  const streamOf = (id: string) => p.world.streams.find((s) => s._id === id);
  const ctlName = (id: string) => p.world.controllers.find((c) => c._id === id)?.name ?? id;
  const playingTx = p.nowPlaying ? p.transmissions.find((t) => t.clip_id === p.nowPlaying!.clipId) : undefined;
  const audible = !!p.nowPlaying && !paused;

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

  // One animation loop draws every lane: the last 60 s, scrolling right to left.
  useEffect(() => {
    let raf = 0;
    const draw = () => {
      raf = requestAnimationFrame(draw);
      const q = latest.current;
      const T = q.live ? Date.now() : q.viewT;
      const a = q.audioRef.current;
      if (clock.current && a) clock.current.textContent = a.currentSrc && Number.isFinite(a.duration) ? mmss(a.currentTime) : '';
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
        g.fillStyle = '#30363d';
        g.fillRect(0, mid, w, 1);
        const xOf = (t: number) => w - ((T - t) / WINDOW_MS) * w;
        for (const tx of q.transmissions) {
          if (tx.stream_id !== s._id) continue;
          const t0 = startOf(tx);
          const t1 = endOf(tx);
          if (t0 > T || t1 < T - WINDOW_MS) continue;
          const pk = peaksFor(tx, q.mock ? null : q.audioUrl);
          const x0 = xOf(t0);
          const x1 = xOf(t1);
          g.fillStyle = q.nowPlaying && tx.clip_id === q.nowPlaying.clipId ? '#4c9aff' : '#8b949e';
          for (let x = Math.max(0, Math.ceil(x0)); x < Math.min(w - 1, x1); x++) {
            const amp = Math.round(pk[Math.min(N - 1, Math.floor(((x - x0) / (x1 - x0)) * N))] * (mid - 2));
            g.fillRect(x, mid - amp, 1, amp * 2 + 1);
          }
        }
        g.fillStyle = q.live ? '#4c9aff' : '#d29922';
        g.fillRect(w - 1, 0, 1, h);
      }
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  const toggle = () => {
    const a = p.audioRef.current;
    if (!a || !p.nowPlaying) return;
    if (a.paused) void a.play().catch(() => undefined);
    else a.pause();
  };

  return (
    <section className="radio">
      <header className="sec-head">
        <h2>Radio</h2>
        <span className="sec-note">last 60 s{p.blocked ? ' · the browser blocked autoplay: click a row once' : ''}</span>
        <span className="grow" />
        <div className="seg" role="group" aria-label="Show stream">
          <button className={activeFilter === 'all' ? 'on' : ''} onClick={() => setFilter('all')}>
            All
          </button>
          {p.streams.map((s) => (
            <button key={s._id} className={activeFilter === s._id ? 'on' : ''} onClick={() => setFilter(s._id)}>
              {short(s)}
            </button>
          ))}
        </div>
      </header>
      <div className="lanes">
        {p.streams.length === 0 && <div className="calm">{p.emptyNote ?? 'No streams.'}</div>}
        {p.streams.map((s) => {
          const mine = upTo.filter((t) => t.stream_id === s._id);
          const last = mine.reduce<Transmission | undefined>((m, t) => (!m || t.t > m.t ? t : m), undefined);
          const onAir = !!last && p.viewT >= startOf(last) && p.viewT <= endOf(last) + 1500;
          const solo = p.solo === s._id;
          const playsHere = !!playingTx && playingTx.stream_id === s._id;
          const b = last ? tag(last, p.decisions) : null;
          return (
            <div key={s._id} className={`slane ${activeFilter === s._id ? 'sel' : ''} ${playsHere ? 'playing' : ''}`} data-stream={s._id} onClick={() => setFilter(activeFilter === s._id ? 'all' : s._id)} title="Click to show only this stream in the log">
              <div className="slane-info">
                <div className="slane-name">
                  <i className={`dot ${onAir ? 'on' : ''}`} title={onAir ? 'on air' : 'quiet'} />
                  <b>{s.name.replace(/^Airport /, '').replace(/\s*[\d.]+$/, '')}</b>
                  <span className="num">{s.frequency_mhz?.toFixed(1)}</span>
                </div>
                <div className="slane-owner">{ctlName(s.controller_id)}</div>
                <div className="slane-ctl" onClick={(e) => e.stopPropagation()}>
                  <button type="button" role="switch" aria-checked={solo} aria-label={`Listen to ${s.name}`} className={`toggle ${solo ? 'on' : ''}`} onClick={() => p.onSolo(solo ? 'all' : s._id)}>
                    <span />
                  </button>
                  {playsHere ? (
                    <>
                      <button type="button" className="mini-play" onClick={toggle} aria-label={audible ? 'Pause' : 'Play'}>
                        {audible ? <i className="ico-pause" /> : <i className="ico-play" />}
                      </button>
                      <span className="mono slane-time" ref={clock} />
                    </>
                  ) : (
                    <span className="slane-listen">{solo ? 'Listening' : 'Listen'}</span>
                  )}
                </div>
              </div>
              <div className="slane-main">
                <canvas
                  className="slane-wave"
                  ref={(el) => {
                    canvases.current[s._id] = el;
                  }}
                />
                <div className="slane-line">
                  {last ? (
                    <>
                      <span className="mono c-who">
                        {stationName(last.from)} → {stationName(last.to)}
                      </span>
                      <span className="slane-text">{last.transcript}</span>
                      {b && <span className={`tag tone-${b.tone}`}>{b.text}</span>}
                    </>
                  ) : (
                    <span className="slane-text quiet">{p.loading ? 'Loading…' : 'No traffic yet'}</span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <div className="log scroll">
        {lines.length === 0 && p.streams.length > 0 && <div className="calm">{p.loading ? 'Loading transmissions…' : p.live ? 'No transmissions yet.' : 'Nothing had been said at this time.'}</div>}
        {lines.map((t) => {
          const playing = p.nowPlaying?.clipId === t.clip_id && !!t.clip_id;
          const hasAudio = !!t.clip_id && !!t.audio_id;
          const b = tag(t, p.decisions);
          return (
            <div key={t._id} data-clip={t.clip_id ?? ''} className={`row ${playing ? 'playing' : ''} ${hasAudio ? 'can-play' : ''}`} onClick={() => hasAudio && p.onPlay(t)} title={t.transcript}>
              <span className="c-time mono">{hhmmss(t.t)}</span>
              <span className="tag">{short(streamOf(t.stream_id))}</span>
              <span className="c-who mono">
                {stationName(t.from)} → {stationName(t.to)}
              </span>
              <span className="c-text">{t.transcript}</span>
              {b ? <span className={`tag tone-${b.tone}`}>{b.text}</span> : <span />}
            </div>
          );
        })}
      </div>
      <audio ref={p.audioRef} preload="none" onEnded={p.onEnded} />
    </section>
  );
}
