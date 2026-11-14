import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Agents } from './components/Agents';
import { MapView } from './components/MapView';
import { Requests } from './components/Requests';
import { StopAlerts } from './components/StopAlerts';
import { Streams, type NowPlaying } from './components/Streams';
import { Timeline } from './components/Timeline';
import { lerp, lerpAngle } from './geo';
import { createSource, VIZ_API } from './source';
import type { Clip, Decision, LiveMsg, Mark, ObjectDoc, Snapshot, Tracks, Transmission, World } from './types';
import { activeStops, ago, hhmmss } from './util';

const iso = (ms: number) => new Date(ms).toISOString();
const byId = <T extends { _id: string }>(list: T[], doc: T) => [...list.filter((x) => x._id !== doc._id), doc];

function applyLive(s: Snapshot | null, m: LiveMsg): Snapshot | null {
  if (m.type === 'snapshot') return m.doc as Snapshot;
  if (!s) return s;
  switch (m.type) {
    case 'object':
      return { ...s, objects: byId(s.objects, m.doc) };
    case 'object_removed':
      return { ...s, objects: s.objects.filter((o) => o._id !== (m.doc._id ?? m.doc.ref ?? m.doc)) };
    case 'lane_status':
      return { ...s, lane_status: byId(s.lane_status, m.doc) };
    case 'permission':
      return { ...s, permissions: byId(s.permissions, m.doc) };
    case 'weather':
      return { ...s, weather: m.doc };
    case 'transmission':
      return { ...s, transmissions: byId(s.transmissions, m.doc).slice(-200) };
    case 'decision': {
      const d = m.doc as Decision;
      const pending = d.outcome === 'pending_approval' ? byId(s.pending, d) : s.pending.filter((p) => p.intent_id !== d.intent_id && p._id !== d.revokes);
      return { ...s, pending, decisions: byId(s.decisions, d).slice(-100) };
    }
    default:
      return s;
  }
}

/** Object positions at time t, interpolated from /viz/tracks; metadata from the snapshot. */
function objectsFromTracks(tracks: Tracks, t: number, snap: Snapshot | null): ObjectDoc[] {
  const out: ObjectDoc[] = [];
  for (const [id, tr] of Object.entries(tracks.objects)) {
    const pts = tr.points;
    let i = -1;
    for (let k = 0; k < pts.length; k++) {
      if (Date.parse(pts[k][0]) <= t) i = k;
      else break;
    }
    if (i < 0) continue;
    const a = pts[i];
    const ta = Date.parse(a[0]);
    if (t - ta > 30_000) continue; // same 30 s window as /viz/snapshot
    const b = pts[i + 1];
    const tb = b ? Date.parse(b[0]) : ta;
    const f = b && tb > ta && tb - ta < 30_000 ? (t - ta) / (tb - ta) : 0;
    const meta = snap?.objects.find((o) => o._id === id);
    const alt = a[3] != null && b && b[3] != null ? lerp(a[3], b[3], f) : a[3];
    out.push({
      _id: id,
      kind: tr.kind,
      callsign: tr.callsign,
      label: meta?.label ?? null,
      status: meta?.status ?? (alt != null && alt > 50 ? 'airborne' : 'ground'),
      position: { type: 'Point', coordinates: b ? [lerp(a[1], b[1], f), lerp(a[2], b[2], f)] : [a[1], a[2]] },
      alt_ft: alt,
      heading_deg: a[4] != null && b && b[4] != null ? lerpAngle(a[4], b[4], f) : a[4],
      speed_kt: meta?.speed_kt ?? null,
      lane_id: meta?.lane_id ?? null,
      hold_short: meta?.hold_short,
    });
  }
  return out;
}

export default function App() {
  const ds = useMemo(() => createSource(), []);
  const [world, setWorld] = useState<World | null>(null);
  const [loadError, setLoadError] = useState('');
  const [conn, setConn] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [liveSnap, setLiveSnap] = useState<Snapshot | null>(null);
  const [histSnap, setHistSnap] = useState<Snapshot | null>(null);
  const [from, setFrom] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [mode, setMode] = useState<'live' | 'history'>('live');
  const [playhead, setPlayhead] = useState(Date.now());
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [marks, setMarks] = useState<Mark[]>([]);
  const [controller, setController] = useState('all');
  const [actAs, setActAs] = useState('');
  const [solo, setSolo] = useState('all');
  const [tracks, setTracks] = useState<{ a: number; b: number; data: Tracks } | null>(null);
  const [histClips, setHistClips] = useState<Clip[]>([]);

  // ---- audio: one real <audio> element, a small queue for solo listening ----
  const audioRef = useRef<HTMLAudioElement>(null);
  const queue = useRef<NowPlaying[]>([]);
  const [nowPlaying, setNowPlaying] = useState<NowPlaying | null>(null);
  const [blocked, setBlocked] = useState(false);
  const startClip = useCallback(
    (it: NowPlaying) => {
      const a = audioRef.current;
      if (!a) return;
      a.src = ds.audioUrl(it.clipId);
      setNowPlaying(it);
      a.play().then(
        () => setBlocked(false),
        (e) => e?.name === 'NotAllowedError' && setBlocked(true),
      );
    },
    [ds],
  );
  const enqueue = useCallback(
    (it: NowPlaying) => {
      const a = audioRef.current;
      if (a && !a.paused && !a.ended) queue.current.push(it);
      else startClip(it);
    },
    [startClip],
  );
  const onEnded = useCallback(() => {
    const next = queue.current.shift();
    if (next) startClip(next);
    else setNowPlaying(null);
  }, [startClip]);
  const silence = useCallback(() => {
    queue.current = [];
    audioRef.current?.pause();
    setNowPlaying(null);
  }, []);
  const txLabel = (t: Transmission) => `${hhmmss(t.t)} ${t.from} → ${t.to}`;

  const modeRef = useRef(mode);
  const soloRef = useRef(solo);
  modeRef.current = mode;
  soloRef.current = solo;

  // ---- load the world, bounds, and the live feed ----
  useEffect(() => {
    let dead = false;
    const load = () =>
      Promise.all([ds.world(), ds.bounds()])
        .then(([w, b]) => {
          if (dead) return;
          setWorld(w);
          setActAs((cur) => cur || w.controllers[0]?._id || '');
          setFrom(Date.parse(b.from) || Date.now() - 60_000);
          setLoadError('');
        })
        .catch((e) => {
          if (dead) return;
          setLoadError(e instanceof Error ? e.message : String(e));
          setTimeout(() => !dead && load(), 3000);
        });
    load();
    const stop = ds.live(
      (m) => {
        setLiveSnap((s) => applyLive(s, m));
        if (m.type === 'transmission' && modeRef.current === 'live' && soloRef.current !== 'all') {
          const t = m.doc as Transmission;
          if (t.stream_id === soloRef.current && t.clip_id && t.audio_id) enqueue({ clipId: t.clip_id, label: txLabel(t) });
        }
      },
      (s) => setConn(s),
    );
    return () => {
      dead = true;
      stop();
    };
  }, [ds, enqueue]);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);

  // ---- timeline marks ----
  useEffect(() => {
    if (from == null) return;
    let dead = false;
    const load = () =>
      ds
        .timeline(iso(from), iso(Date.now()))
        .then((r) => !dead && setMarks(r.marks))
        .catch(() => undefined);
    load();
    const id = setInterval(load, 4000);
    return () => {
      dead = true;
      clearInterval(id);
    };
  }, [ds, from]);

  // ---- history: playback clock ----
  useEffect(() => {
    if (mode !== 'history' || !playing) return;
    let raf = 0;
    let last = performance.now();
    const step = (ts: number) => {
      const dt = ts - last;
      last = ts;
      setPlayhead((p) => p + dt * speed);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [mode, playing, speed]);

  const goLive = useCallback(() => {
    setMode('live');
    setPlaying(false);
    setHistSnap(null);
    silence();
  }, [silence]);

  useEffect(() => {
    if (mode === 'history' && playing && playhead >= Date.now() - 400) goLive();
  }, [mode, playing, playhead, goLive]);

  const scrub = useCallback(
    (t: number) => {
      const lo = from ?? Date.now() - 60_000;
      if (modeRef.current === 'live') silence();
      setMode('history');
      setPlaying(false);
      setPlayhead(Math.max(lo, Math.min(Date.now() - 500, t)));
    },
    [from, silence],
  );

  // ---- history: snapshot at the playhead (one request in flight, the latest wins) ----
  const quantum = Math.max(1000, speed * 500);
  const snapKey = mode === 'history' ? Math.floor(playhead / quantum) * quantum : null;
  const flight = useRef<{ busy: boolean; want: number | null }>({ busy: false, want: null });
  useEffect(() => {
    if (snapKey == null) return;
    const f = flight.current;
    const run = (t: number) => {
      f.busy = true;
      ds.snapshot(iso(t))
        .then((s) => modeRef.current === 'history' && setHistSnap(s))
        .catch(() => undefined)
        .finally(() => {
          f.busy = false;
          if (f.want != null) {
            const w = f.want;
            f.want = null;
            run(w);
          }
        });
    };
    if (f.busy) f.want = snapKey;
    else run(snapKey);
  }, [ds, snapKey]);

  // ---- history: tracks for smooth motion ----
  const trackKey = mode === 'history' && (!tracks || playhead < tracks.a + 2000 || playhead > tracks.b - 2000) ? Math.floor(playhead / 20_000) : null;
  useEffect(() => {
    if (trackKey == null) return;
    let dead = false;
    const a = playhead - 60_000;
    const b = Math.min(Date.now(), playhead + 120_000);
    ds.tracks(iso(a), iso(b), 1)
      .then((data) => !dead && setTracks({ a, b: Math.max(b, playhead + 30_000), data }))
      .catch(() => undefined);
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ds, trackKey]);

  // ---- history: the soloed stream's clips play at their original times ----
  useEffect(() => {
    if (mode !== 'history' || solo === 'all' || from == null) {
      setHistClips([]);
      return;
    }
    let dead = false;
    ds.clips(solo, iso(from), iso(Date.now()))
      .then((r) => !dead && setHistClips(r.clips))
      .catch(() => undefined);
    return () => {
      dead = true;
    };
  }, [ds, mode, solo, from]);
  const prevHead = useRef(playhead);
  useEffect(() => {
    const prev = prevHead.current;
    prevHead.current = playhead;
    if (mode !== 'history' || !playing || speed !== 1 || playhead <= prev || playhead - prev > 2000) return;
    for (const c of histClips) {
      const t = Date.parse(c.t_start);
      if (c.has_audio && t > prev && t <= playhead) {
        enqueue({ clipId: c.clip_id, label: c.transmission ? txLabel(c.transmission) : hhmmss(c.t_start) });
      }
    }
  }, [playhead, mode, playing, speed, histClips, enqueue]);

  // ---- derived view ----
  const live = mode === 'live';
  const snap = live ? liveSnap : histSnap;
  const viewT = live ? now : playhead;
  const objects = useMemo(() => {
    if (live || !tracks || playhead < tracks.a || playhead > tracks.b) return snap?.objects ?? [];
    return objectsFromTracks(tracks.data, playhead, snap);
  }, [live, tracks, playhead, snap]);

  const selected = world?.controllers.find((c) => c._id === controller);
  const seesAll = !selected || selected.role === 'supervisor';
  const ownedLanes = useMemo(() => (selected && selected.role !== 'supervisor' ? new Set(selected.lanes) : null), [selected]);
  const streams = useMemo(
    () => (world ? (seesAll ? world.streams : world.streams.filter((s) => selected!.streams.includes(s._id) || s.controller_id === selected!._id)) : []),
    [world, seesAll, selected],
  );
  useEffect(() => {
    if (solo !== 'all' && !streams.some((s) => s._id === solo)) {
      setSolo('all');
      silence();
    }
  }, [streams, solo, silence]);

  const pendingShown = useMemo(() => (snap?.pending ?? []).filter((d) => seesAll || d.controller_id === controller), [snap, seesAll, controller]);
  const stopT = live ? Math.floor(now / 5000) * 5000 : playhead;
  const stopsAll = useMemo(() => activeStops(snap?.decisions ?? [], stopT), [snap, stopT]);
  const stopsShown = useMemo(() => stopsAll.filter((d) => seesAll || d.controller_id === controller), [stopsAll, seesAll, controller]);
  const flagged = useMemo(() => new Set([...pendingShown, ...stopsShown].flatMap((d) => d.target)), [pendingShown, stopsShown]);

  if (!world) {
    return (
      <div className="boot">
        <h1>HOLD SHORT</h1>
        {loadError ? (
          <>
            <p className="warn-text">The visualizer API at {VIZ_API} is not answering.</p>
            <p className="muted mono">{loadError}</p>
            <p className="muted">
              Retrying every 3 s. To look at the app without a backend, open <a href="?mock=1">mock mode</a>.
            </p>
          </>
        ) : (
          <p className="muted">Loading the airfield…</p>
        )}
      </div>
    );
  }

  const actor = controller === 'all' ? actAs : controller;
  return (
    <div className={`app ${live ? 'mode-live' : 'mode-history'}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            <i />
            <i />
            <i />
            <i />
          </span>
          <b>HOLD SHORT</b>
          <span className="muted">
            {world.tower.name} · {world.tower.icao}
          </span>
        </div>
        <nav className="ctl-switch" aria-label="Controller view">
          <span className="cap">View as</span>
          <div className="seg" role="group">
            <button className={controller === 'all' ? 'on' : ''} onClick={() => setController('all')}>
              All
            </button>
            {world.controllers.map((c) => {
              const n = (snap?.pending ?? []).filter((d) => d.controller_id === c._id).length;
              const st = stopsAll.filter((d) => d.controller_id === c._id).length;
              return (
                <button key={c._id} className={controller === c._id ? 'on' : ''} onClick={() => setController(c._id)} title={c.role === 'supervisor' ? 'Supervisor: sees and may act on everything' : `${c.lanes.length} lanes, ${c.streams.length} streams`}>
                  {c.name}
                  {st > 0 && (
                    <span className="count stopct" title={`${st} stop in effect on their lanes`}>
                      ■ {st}
                    </span>
                  )}
                  {n > 0 && (
                    <span className="count hot" title={`${n} waiting for approval`}>
                      {n}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </nav>
        <div className="status">
          {ds.mock && <span className="pill mock">MOCK DATA</span>}
          {snap?.weather && (
            <span className="muted small wx">
              wind {snap.weather.wind ?? '---'} · vis {snap.weather.visibility_sm ?? '--'} SM
            </span>
          )}
          {live ? (
            <span className={`pill ${conn === 'open' ? 'live' : 'down'}`}>
              <span className="dot" /> {conn === 'open' ? 'LIVE' : conn === 'connecting' ? 'CONNECTING' : 'FEED LOST · RETRYING'}
            </span>
          ) : (
            <span className="pill history">◼ HISTORY</span>
          )}
          <span className="mono clock">{hhmmss(viewT)}Z</span>
        </div>
      </header>

      {!live && (
        <div className="history-banner" role="status">
          <span>
            <b>HISTORY · NOT LIVE.</b> You are looking at <span className="mono">{hhmmss(playhead)}Z</span>, {ago(now - playhead)} ago, rebuilt from the stored records. Controller actions are disabled.
          </span>
          <button className="btn" onClick={goLive}>
            Back to LIVE
          </button>
        </div>
      )}

      <StopAlerts ds={ds} world={world} stops={stopsShown} transmissions={snap?.transmissions ?? []} actor={actor} viewT={viewT} readOnly={!live} />

      <main className="main">
        <section className="map-wrap">
          <MapView world={world} objects={objects} laneStatus={snap?.lane_status ?? []} ownedLanes={ownedLanes} flagged={flagged} smooth={live} />
          {!live && <div className="map-stamp mono">HISTORY {hhmmss(playhead)}Z</div>}
          {selected && (
            <div className="map-filter">
              {selected.role === 'supervisor' ? 'Supervisor view: every lane and stream' : `${selected.name}: ${selected.lanes.length} lanes highlighted, the rest dimmed`}
            </div>
          )}
        </section>
        <div className="side">
          <Requests ds={ds} world={world} pending={snap?.pending ?? []} decisions={snap?.decisions ?? []} controller={controller} actAs={actAs} onActAs={setActAs} readOnly={!live} />
          <Agents ds={ds} permissions={snap?.permissions ?? []} agentState={snap?.agent_state ?? liveSnap?.agent_state ?? []} by={actor} readOnly={!live} />
        </div>
        <Streams
          world={world}
          streams={streams}
          transmissions={snap?.transmissions ?? []}
          decisions={snap?.decisions ?? []}
          activeStopIds={new Set(stopsAll.map((d) => d._id))}
          loading={!snap}
          solo={solo}
          onSolo={(id) => {
            silence();
            setSolo(id);
          }}
          viewT={viewT}
          live={live}
          audioRef={audioRef}
          nowPlaying={nowPlaying}
          blocked={blocked}
          onPlay={(t) => {
            queue.current = [];
            if (t.clip_id) startClip({ clipId: t.clip_id, label: txLabel(t) });
          }}
          onEnded={onEnded}
          emptyNote={selected ? `${selected.name} owns no radio stream.` : null}
        />
      </main>

      <Timeline
        from={from ?? now - 60_000}
        to={now}
        playhead={playhead}
        live={live}
        playing={playing}
        speed={speed}
        marks={marks}
        streams={world.streams}
        onScrub={scrub}
        onLive={goLive}
        onPlay={setPlaying}
        onSpeed={setSpeed}
      />
    </div>
  );
}
