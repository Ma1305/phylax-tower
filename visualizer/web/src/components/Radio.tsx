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
  /** Drive the bars from a WebAudio analyser. Only safe when the clips are same-origin (mock mode): a cross-origin clip without CORS headers would play silent through WebAudio. */
  analyse?: boolean;
}

function badge(t: Transmission, decisions: Decision[]): { text: string; tone: string } {
  if (t.synthetic) {
    const d = t.decision_id ? decisions.find((x) => x._id === t.decision_id) : undefined;
    return d?.intent_action === 'stop' || t.kind === 'stop' ? { text: 'Phylax · stop', tone: 'stop' } : { text: 'Phylax', tone: 'plain' };
  }
  if (!t.verified || t.flag) return { text: 'Unclear, say again', tone: 'warn' };
  if (t.readback === 'matched') return { text: '✓ Readback matched', tone: 'ok' };
  if (t.readback === 'mismatch') return { text: 'Readback mismatch', tone: 'stop' };
  if (t.kind === 'request') return { text: 'Request', tone: 'plain' };
  if (t.kind === 'clearance') return { text: 'Clearance', tone: 'plain' };
  if (t.kind === 'readback') return { text: 'Readback', tone: 'plain' };
  return { text: t.kind === 'other' ? 'Call' : t.kind[0].toUpperCase() + t.kind.slice(1), tone: 'plain' };
}

/** Waveform bars: heights are stable per id; they move (CSS) while the clip plays. */
function Wave({ seed, active, n = 22 }: { seed: string; active: boolean; n?: number }) {
  let h = 7;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const bars = Array.from({ length: n }, () => {
    h = (h * 1103515245 + 12345) >>> 0;
    return 0.25 + ((h >>> 8) % 100) / 133;
  });
  return (
    <span className={`wave ${active ? 'active' : ''}`} aria-hidden>
      {bars.map((b, i) => (
        <i key={i} style={{ height: `${Math.round(b * 100)}%`, animationDelay: `${-(i * 137) % 900}ms` }} />
      ))}
    </span>
  );
}

export function Radio(p: Props) {
  const [paused, setPaused] = useState(true);
  const shownIds = new Set(p.streams.map((s) => s._id));
  const lines = p.transmissions
    .filter((t) => (p.solo === 'all' ? shownIds.has(t.stream_id) : t.stream_id === p.solo))
    .sort((a, b) => b.t.localeCompare(a.t))
    .slice(0, 60);
  const streamOf = (id: string) => p.world.streams.find((s) => s._id === id);
  const playingTx = p.nowPlaying ? p.transmissions.find((t) => t.clip_id === p.nowPlaying!.clipId) : undefined;
  const audible = !!p.nowPlaying && !paused;
  useEffect(() => {
    const a = p.audioRef.current;
    if (!a) return;
    const sync = () => setPaused(a.paused || a.ended);
    for (const ev of ['play', 'playing', 'pause', 'ended', 'emptied']) a.addEventListener(ev, sync);
    return () => {
      for (const ev of ['play', 'playing', 'pause', 'ended', 'emptied']) a.removeEventListener(ev, sync);
    };
  }, [p.audioRef]);
  // Real levels: an AnalyserNode on the audio element moves the bars of the dock and the playing card.
  const root = useRef<HTMLElement>(null);
  const [analysing, setAnalysing] = useState(false);
  useEffect(() => {
    const a = p.audioRef.current;
    if (!a || !p.analyse || typeof AudioContext === 'undefined') return;
    let ctx: AudioContext | null = null;
    let an: AnalyserNode | null = null;
    let raf = 0;
    const draw = () => {
      raf = requestAnimationFrame(draw);
      if (!an || !root.current || a.paused) return;
      const data = new Uint8Array(an.fftSize);
      an.getByteTimeDomainData(data);
      root.current.querySelectorAll<HTMLElement>('.wave.active').forEach((w) => {
        const bars = w.children;
        const step = Math.floor(data.length / bars.length);
        for (let i = 0; i < bars.length; i++) {
          let peak = 0;
          for (let k = 0; k < step; k++) peak = Math.max(peak, Math.abs(data[i * step + k] - 128));
          (bars[i] as HTMLElement).style.transform = `scaleY(${(0.15 + Math.min(1, peak / 24)).toFixed(2)})`;
        }
      });
    };
    const start = () => {
      try {
        if (!ctx) {
          ctx = new AudioContext();
          an = ctx.createAnalyser();
          an.fftSize = 512;
          ctx.createMediaElementSource(a).connect(an);
          an.connect(ctx.destination);
          setAnalysing(true);
          raf = requestAnimationFrame(draw);
        }
        void ctx.resume();
      } catch {
        setAnalysing(false);
      }
    };
    a.addEventListener('play', start);
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
    <section className={`radio ${analysing ? 'analysing' : ''}`} ref={root}>
      <header className="sec-head">
        <h2>Radio</h2>
        <div className="tabs" role="group" aria-label="Listen to">
          <button className={p.solo === 'all' ? 'on' : ''} onClick={() => p.onSolo('all')}>
            All
          </button>
          {p.streams.map((s) => (
            <button key={s._id} className={p.solo === s._id ? 'on' : ''} onClick={() => p.onSolo(s._id)} title={`Show and listen to ${s.name}`}>
              {s.name.replace(/^Airport /, '').replace(/\s*[\d.]+$/, '')} <span className="mono">{s.frequency_mhz?.toFixed(1)}</span>
            </button>
          ))}
        </div>
      </header>
      <div className="tx-list scroll fade">
        {lines.length === 0 && (
          <div className="calm small">
            <span>{p.streams.length === 0 ? (p.emptyNote ?? 'No streams.') : p.loading ? 'Loading transmissions…' : p.live ? 'No transmissions yet.' : 'Nothing had been said at this time.'}</span>
          </div>
        )}
        {lines.map((t) => {
          const playing = p.nowPlaying?.clipId === t.clip_id && !!t.clip_id;
          const hasAudio = !!t.clip_id && !!t.audio_id;
          const b = badge(t, p.decisions);
          return (
            <article key={t._id} data-clip={t.clip_id ?? ''} className={`tx tone-${b.tone} ${playing ? 'playing' : ''} ${hasAudio ? 'can-play' : ''}`} onClick={() => hasAudio && p.onPlay(t)} title={hasAudio ? 'Play this clip' : undefined}>
              <Wave seed={t._id} active={playing && audible} />
              <div className="tx-main">
                <div className="tx-head mono">
                  <b>{stationName(t.from)}</b>
                  <span>→ {stationName(t.to)}</span>
                  <span>{hhmmss(t.t)}</span>
                  {p.solo === 'all' && <span className="tx-stream">{streamOf(t.stream_id)?.station ?? ''}</span>}
                </div>
                <p className="tx-quote">“{t.transcript}”</p>
                <span className={`badge tone-${b.tone}`}>{b.text}</span>
              </div>
            </article>
          );
        })}
      </div>
      <footer className={`dock ${audible ? 'on' : ''}`}>
        <button className="dock-play" onClick={toggle} aria-label={audible ? 'Pause' : 'Play'}>
          {audible ? '❚❚' : '▶'}
        </button>
        <div className="dock-main">
          <div className="dock-top">
            <Wave seed="dock" active={audible} n={36} />
            <span className="dock-stream">{playingTx ? (streamOf(playingTx.stream_id)?.name ?? '') : soloStream ? `${soloStream.name} · listening` : 'Now playing'}</span>
          </div>
          <p className="caption">
            {p.blocked ? (
              <span className="warn-text">The browser blocked autoplay. Press play once to allow sound.</span>
            ) : playingTx ? (
              <>
                <b className="mono">{stationName(playingTx.from)}</b> {playingTx.transcript}
              </>
            ) : p.nowPlaying ? (
              p.nowPlaying.label
            ) : soloStream ? (
              p.live ? 'New calls on this stream play as they arrive.' : 'Calls play at their original times (1x).'
            ) : (
              'Pick a stream to listen, or click a card to play it.'
            )}
          </p>
        </div>
        <audio ref={p.audioRef} preload="none" onEnded={p.onEnded} />
      </footer>
    </section>
  );
}
