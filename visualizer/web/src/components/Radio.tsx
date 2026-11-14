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
  audioRef: RefObject<HTMLAudioElement>;
  nowPlaying: NowPlaying | null;
  blocked: boolean;
  onPlay: (tx: Transmission) => void;
  onEnded: () => void;
  emptyNote: string | null;
  /** Draw the real signal from a WebAudio analyser. Only safe when the clips are same-origin (mock mode): a cross-origin clip without CORS headers would play silent through WebAudio. */
  analyse?: boolean;
}

const SHORT: Record<string, string> = { tower: 'TWR', ground: 'GND', ops: 'OPS' };
const short = (s?: Stream) => (s ? (SHORT[s.kind] ?? s.station.slice(0, 3)) : '');
const mmss = (s: number) => (Number.isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '-:--');

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

export function Radio(p: Props) {
  const [paused, setPaused] = useState(true);
  const canvas = useRef<HTMLCanvasElement>(null);
  const clock = useRef<HTMLSpanElement>(null);
  const shownIds = new Set(p.streams.map((s) => s._id));
  const lines = p.transmissions
    .filter((t) => (p.solo === 'all' ? shownIds.has(t.stream_id) : t.stream_id === p.solo))
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 80);
  const streamOf = (id: string) => p.world.streams.find((s) => s._id === id);
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

  // The trace: one thin line. Live time-domain signal when an analyser is available, a slow sine otherwise.
  useEffect(() => {
    const a = p.audioRef.current;
    const cv = canvas.current;
    if (!a || !cv) return;
    let ctx: AudioContext | null = null;
    let an: AnalyserNode | null = null;
    let raf = 0;
    const start = () => {
      if (!p.analyse || typeof AudioContext === 'undefined') return;
      try {
        if (!ctx) {
          ctx = new AudioContext();
          an = ctx.createAnalyser();
          an.fftSize = 1024;
          ctx.createMediaElementSource(a).connect(an);
          an.connect(ctx.destination);
        }
        void ctx.resume();
      } catch {
        an = null;
      }
    };
    a.addEventListener('play', start);
    const data = new Uint8Array(1024);
    const draw = (ts: number) => {
      raf = requestAnimationFrame(draw);
      const dpr = window.devicePixelRatio || 1;
      const w = cv.clientWidth;
      const h = cv.clientHeight;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
      }
      const g = cv.getContext('2d');
      if (!g || w < 4) return;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const on = !a.paused && !a.ended && !!a.currentSrc;
      const dur = a.duration;
      const prog = a.currentSrc && Number.isFinite(dur) && dur > 0 ? Math.min(1, a.currentTime / dur) : 0;
      if (clock.current) clock.current.textContent = a.currentSrc && Number.isFinite(dur) ? `${mmss(a.currentTime)} / ${mmss(dur)}` : '-:-- / -:--';
      if (on && an) an.getByteTimeDomainData(data);
      const y = (x: number) => {
        const f = x / w;
        if (!on) return h / 2;
        if (an) return h / 2 + ((data[Math.floor(f * (data.length - 1))] - 128) / 128) * (h / 2 - 2) * 2.2;
        const env = Math.sin(f * Math.PI);
        return h / 2 + env * (h / 2 - 3) * (0.55 * Math.sin(f * 22 + ts / 260) + 0.3 * Math.sin(f * 47 - ts / 410));
      };
      const pass = (x0: number, x1: number, colour: string) => {
        if (x1 <= x0) return;
        g.save();
        g.beginPath();
        g.rect(x0, 0, x1 - x0, h);
        g.clip();
        g.beginPath();
        for (let x = 0; x <= w; x += 1.5) {
          const yy = Math.max(1, Math.min(h - 1, y(x)));
          if (x === 0) g.moveTo(x, yy);
          else g.lineTo(x, yy);
        }
        g.lineWidth = 1.25;
        g.lineJoin = 'round';
        g.strokeStyle = colour;
        g.stroke();
        g.restore();
      };
      pass(0, prog * w, '#4c9aff');
      pass(prog * w, w, '#4a5463');
    };
    raf = requestAnimationFrame(draw);
    return () => {
      a.removeEventListener('play', start);
      cancelAnimationFrame(raf);
      void ctx?.close();
    };
  }, [p.audioRef, p.analyse]);

  const toggle = () => {
    const a = p.audioRef.current;
    if (!a) return;
    if (p.nowPlaying) {
      if (a.paused) void a.play().catch(() => undefined);
      else a.pause();
    } else {
      const first = lines.find((t) => t.clip_id && t.audio_id);
      if (first) p.onPlay(first);
    }
  };
  const soloStream = p.streams.find((s) => s._id === p.solo);
  return (
    <section className="radio">
      <header className="sec-head">
        <h2>Radio</h2>
        <span className="grow" />
        <div className="seg" role="group" aria-label="Listen to">
          <button className={p.solo === 'all' ? 'on' : ''} onClick={() => p.onSolo('all')}>
            All
          </button>
          {p.streams.map((s) => (
            <button key={s._id} className={p.solo === s._id ? 'on' : ''} onClick={() => p.onSolo(s._id)} title={`Show and listen to ${s.name}`}>
              {short(s)} <span className="num">{s.frequency_mhz?.toFixed(1)}</span>
            </button>
          ))}
        </div>
      </header>
      <div className="log scroll">
        {lines.length === 0 && <div className="calm">{p.streams.length === 0 ? (p.emptyNote ?? 'No streams.') : p.loading ? 'Loading transmissions…' : p.live ? 'No transmissions yet.' : 'Nothing had been said at this time.'}</div>}
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
      <footer className={`dock ${audible ? 'on' : ''}`}>
        <button className="dock-play" onClick={toggle} aria-label={audible ? 'Pause' : 'Play'}>
          {audible ? <i className="ico-pause" /> : <i className="ico-play" />}
        </button>
        <div className="dock-main">
          <div className="dock-top">
            <span className="dock-stream">{playingTx ? `${short(streamOf(playingTx.stream_id))} · ${stationName(playingTx.from)}` : soloStream ? `${short(soloStream)} · listening` : 'No clip'}</span>
            <canvas ref={canvas} className="trace" />
            <span className="mono dock-time" ref={clock} />
          </div>
          <p className="caption">
            {p.blocked ? <span className="warn-text">The browser blocked autoplay. Press play once to allow sound.</span> : playingTx ? playingTx.transcript : p.nowPlaying ? p.nowPlaying.label : soloStream ? (p.live ? 'New calls on this stream play as they arrive.' : 'Calls play at their original times (1x).') : 'Select a row to play it, or pick a stream to listen.'}
          </p>
        </div>
        <audio ref={p.audioRef} preload="none" onEnded={p.onEnded} />
      </footer>
    </section>
  );
}
