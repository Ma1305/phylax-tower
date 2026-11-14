import { useEffect, useRef, type RefObject } from 'react';
import type { Decision, Stream, Transmission, World } from '../types';
import { ago, hhmmss } from '../util';

export interface NowPlaying {
  clipId: string;
  label: string;
}

interface Props {
  world: World;
  streams: Stream[]; // already filtered for the selected controller
  transmissions: Transmission[];
  decisions: Decision[];
  activeStopIds: Set<string>;
  loading: boolean;
  solo: string; // 'all' or a stream id
  onSolo: (id: string) => void;
  viewT: number;
  live: boolean;
  audioRef: RefObject<HTMLAudioElement>;
  nowPlaying: NowPlaying | null;
  blocked: boolean;
  onPlay: (tx: Transmission) => void;
  onEnded: () => void;
  emptyNote: string | null;
}

const READBACK: Record<string, string> = { matched: '✓ readback', mismatch: '✕ readback mismatch', missing: '… no readback' };

export function Streams(p: Props) {
  const ctlName = (id: string) => p.world.controllers.find((c) => c._id === id)?.name ?? id;
  const soloStream = p.streams.find((s) => s._id === p.solo);
  return (
    <aside className="radio">
      <header className="panel-head">
        <h2>Radio streams</h2>
        <div className="seg small" role="group" aria-label="Listen to">
          <button className={p.solo === 'all' ? 'on' : ''} onClick={() => p.onSolo('all')} title="Show every stream; nothing plays by itself">
            All
          </button>
          {p.streams.map((s) => (
            <button key={s._id} className={p.solo === s._id ? 'on' : ''} onClick={() => p.onSolo(s._id)} title={`Solo ${s.name}`}>
              {s.station}
            </button>
          ))}
        </div>
      </header>
      <div className="listen-note">
        {soloStream ? (
          <>
            <b>Solo: {soloStream.name}.</b> {p.live ? 'New calls play as they arrive.' : 'Calls play at their original times (1x).'} Other streams are silent.
          </>
        ) : (
          <>All streams shown. Nothing plays by itself: press ▶ on a line, or pick a stream to listen.</>
        )}
      </div>

      <div className="stream-list">
        {p.streams.length === 0 && <div className="empty">{p.emptyNote ?? 'No streams.'}</div>}
        {p.streams.map((s) => (
          <StreamCard key={s._id} s={s} p={p} owner={ctlName(s.controller_id)} />
        ))}
      </div>

      <footer className="player">
        <div className="player-label">
          {p.blocked ? (
            <span className="warn-text">The browser blocked autoplay. Press play once to allow sound.</span>
          ) : p.nowPlaying ? (
            <>
              <span className="cap">Playing</span> <span className="mono">{p.nowPlaying.label}</span>
            </>
          ) : (
            <span className="muted">No clip loaded</span>
          )}
        </div>
        <audio ref={p.audioRef} controls preload="none" onEnded={p.onEnded} />
      </footer>
    </aside>
  );
}

function StreamCard({ s, p, owner }: { s: Stream; p: Props; owner: string }) {
  const lines = p.transmissions.filter((t) => t.stream_id === s._id).sort((a, b) => a.t.localeCompare(b.t));
  const last = lines[lines.length - 1];
  const lastEnd = last ? Date.parse(last.t_end ?? last.t) : 0;
  const lastStart = last ? Date.parse(last.t) : 0;
  const onAir = !!last && p.viewT >= lastStart && p.viewT - lastEnd < 2500;
  const solo = p.solo === s._id;
  const muted = p.solo !== 'all' && !solo;
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, solo]);

  return (
    <section className={`stream ${solo ? 'solo' : ''} ${muted ? 'muted-stream' : ''}`}>
      <button className="stream-head" onClick={() => p.onSolo(solo ? 'all' : s._id)} title={solo ? 'Stop solo' : `Solo ${s.name}`}>
        <span className={`activity ${onAir ? 'on' : ''}`} aria-hidden />
        <span className="stream-title">
          <strong>{s.name.replace(/\s*\d+(\.\d+)?$/, '')}</strong>
          <span className="mono freq">{s.frequency_mhz != null ? s.frequency_mhz.toFixed(3) : '---'} MHz</span>
        </span>
        <span className="stream-meta">
          <span>{owner}</span>
          <span className={onAir ? 'onair' : 'muted'}>{onAir ? 'ON AIR' : last ? `quiet ${ago(p.viewT - lastEnd)}` : 'no traffic yet'}</span>
        </span>
        <span className={`solo-tag ${solo ? 'on' : ''}`}>{solo ? 'SOLO ♪' : muted ? 'silent' : 'solo'}</span>
      </button>
      <div className="lines" ref={body}>
        {lines.length === 0 && <div className="empty small">{p.loading ? 'Loading transmissions…' : p.live ? 'No transmissions on this stream yet.' : 'Nothing had been said on this stream at this time.'}</div>}
        {lines.map((t) => {
          const playing = p.nowPlaying?.clipId === t.clip_id;
          const hasAudio = !!t.clip_id && !!t.audio_id;
          const dec = t.synthetic && t.decision_id ? p.decisions.find((d) => d._id === t.decision_id) : undefined;
          const src = dec ? { d: dec, stop: dec.intent_action === 'stop', lifted: p.decisions.some((x) => x.revokes === dec._id) } : null;
          return (
            <div key={t._id} data-clip={t.clip_id ?? ''} className={`line ${t.synthetic ? 'synthetic' : ''} ${t.verified ? '' : 'unverified'} ${playing ? 'playing' : ''}`}>
              {hasAudio ? (
                <button className="play" onClick={() => p.onPlay(t)} aria-label={`Play ${t.from} to ${t.to}`} title="Play this clip">
                  {playing ? '♪' : '▶'}
                </button>
              ) : (
                <span className="play none" title="No audio for this line">
                  –
                </span>
              )}
              <div className="line-main">
                <div className="line-head">
                  <span className="mono muted">{hhmmss(t.t)}</span>
                  <span className="mono who">
                    {t.from} <span className="arrow">→</span> {t.to}
                  </span>
                  {t.synthetic && <span className="badge syn" title="Spoken by Hold Short itself (synthetic voice), not by a person">◆ HOLD SHORT · OUTBOUND</span>}
                  {src && (
                    <span className={`badge ${src.stop ? 'syn-stop' : 'syn-ok'}`} title={`decision ${src.d.rule}`}>
                      {src.stop ? '■ STOP' : 'INSTRUCTION'} · {src.d.approved_by ? `approved by ${p.world.controllers.find((c) => c._id === src.d.approved_by)?.name ?? src.d.approved_by}` : 'autonomous'}
                      {src.stop && (p.activeStopIds.has(src.d._id) ? ' · in effect' : src.lifted ? ' · lifted' : '')}
                    </span>
                  )}
                </div>
                <p className="line-text">{t.transcript}</p>
                <div className="line-tags">
                  <span className={`conf ${t.confidence < 0.8 ? 'low' : ''}`} title="Speech-to-text confidence">
                    conf {t.confidence.toFixed(2)}
                  </span>
                  {READBACK[t.readback] && <span className={`badge rb-${t.readback}`}>{READBACK[t.readback]}</span>}
                  {(!t.verified || t.flag) && <span className="badge unclear">? {t.flag ?? 'unclear'}</span>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
