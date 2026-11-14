import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MapView } from './components/MapView';
import { Menu } from './components/Menu';
import { Requests } from './components/Requests';
import { agentSummary, AgentSettings } from './components/AgentPills';
import { useStored } from './collapse';
import { NeedsYou } from './components/NeedsYou';
import { Radio, type NowPlaying } from './components/Radio';
import { isMajor, Timeline } from './components/Timeline';
import { lerp, lerpAngle } from './geo';
import { createSource, VIZ_API } from './source';
import type { Clip, Decision, LiveMsg, Mark, ObjectDoc, Snapshot, Tracks, Transmission, World } from './types';
import { activeStops, ago, hhmmss, stationName } from './util';

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

const COLL: Record<string, string> = { object: 'objects', lane_status: 'lane_status', permission: 'permissions', weather: 'weather', transmission: 'transmissions', decision: 'decisions', clip: 'clips' };
const KEYS = ['_id', 'callsign', 'from', 'to', 'transcript', 'agent', 'intent_action', 'outcome', 'rule', 'state', 'reason', 'enabled', 'mode', 'tier', 'lane_id', 'status', 'alt_ft', 'wind', 'visibility_sm'];
/** One change event as a compact line for the database strip. */
function dbLine(m: LiveMsg): { coll: string; json: string; t: string; at: number } | null {
  const coll = COLL[m.type];
  if (!coll || !m.doc || typeof m.doc !== 'object') return null;
  const parts: string[] = [];
  for (const k of KEYS) {
    const v = m.doc[k];
    if (v == null || v === '' || typeof v === 'object') continue;
    if (k === '_id' && String(v).length > 16) continue;
    parts.push(`${k}: ${typeof v === 'string' ? JSON.stringify(v.length > 60 ? v.slice(0, 57) + '…' : v) : typeof v === 'number' ? Math.round(v * 10) / 10 : v}`);
    if (parts.length >= 6) break;
  }
  return { coll, json: `{${parts.join(', ')}}`, t: hhmmss(Date.now()), at: Date.now() };
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
  const [drawer, setDrawer] = useState(false);
  const [settings, setSettings] = useState(false);
  const [sideHidden, setSideHidden] = useStored('side.hidden');
  const [dbEvent, setDbEvent] = useState<{ n: number; coll: string; json: string; t: string; at: number } | null>(null);

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
  const txLabel = (t: Transmission) => `${hhmmss(t.t)} ${stationName(t.from)} → ${stationName(t.to)}`;

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
        const ev = dbLine(m);
        if (ev) setDbEvent((p) => (ev.coll === 'objects' && p && p.coll !== 'objects' && Date.now() - p.at < 12000 ? p : { ...ev, n: (p?.n ?? 0) + 1 }));
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
    (t: number, play = false) => {
      const lo = from ?? Date.now() - 60_000;
      if (modeRef.current === 'live') silence();
      setMode('history');
      setPlaying(play);
      setPlayhead(Math.max(lo, Math.min(Date.now() - 500, t)));
    },
    [from, silence],
  );

  // ---- timeline window: the bar shows the last N minutes, not the whole database ----
  const [win, setWin] = useState(15);
  const winAuto = useRef(false);
  useEffect(() => {
    if (winAuto.current || marks.length === 0) return;
    winAuto.current = true;
    const runs = marks.filter((m) => m.type === 'run');
    const anchor = Date.parse((runs.length ? runs[runs.length - 1] : marks[marks.length - 1]).t) - 15_000;
    setWin([5, 15, 60].find((m) => Date.now() - m * 60_000 <= anchor) ?? 0);
  }, [marks]);
  const keyRef = useRef<(e: KeyboardEvent) => void>();
  useEffect(() => {
    const h = (e: KeyboardEvent) => keyRef.current?.(e);
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

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
        <h1>Phylax Tower</h1>
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
  const tlFrom = Math.max(from ?? now - 60_000, win ? now - win * 60_000 : 0);
  const cur = live ? Date.now() : playhead;
  const togglePlay = () => (live ? scrub(Date.now() - 1000, false) : setPlaying(!playing));
  const seek = (delta: number) => (cur + delta >= Date.now() - 600 ? goLive() : scrub(cur + delta, playing && !live));
  const stepEvent = (dir: -1 | 1) => {
    const ts = marks.filter(isMajor).map((m) => Date.parse(m.t) - 2000).sort((a, b) => a - b);
    const target = dir < 0 ? [...ts].reverse().find((t) => t < cur - 1500) : ts.find((t) => t > cur + 500);
    if (target == null) return dir > 0 ? goLive() : undefined;
    if (target < tlFrom) setWin(0);
    scrub(target, playing && !live);
  };
  keyRef.current = (e) => {
    const el = e.target as HTMLElement | null;
    if (e.metaKey || e.ctrlKey || e.altKey || (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable || el.closest('.menu.open')))) return;
    if (e.key === ' ' && el && el.tagName === 'BUTTON') return; // the focused button takes the space bar
    const views = ['all', ...world.controllers.map((c) => c._id)];
    if (e.key === ' ') togglePlay();
    else if (e.key === 'ArrowLeft') e.shiftKey ? stepEvent(-1) : seek(-5000);
    else if (e.key === 'ArrowRight') e.shiftKey ? stepEvent(1) : live ? undefined : seek(5000);
    else if (e.key === 'l' || e.key === 'L') goLive();
    else if (e.key === ']') setSideHidden(!sideHidden);
    else if (/^[1-9]$/.test(e.key) && views[+e.key - 1]) setController(views[+e.key - 1]);
    else return;
    e.preventDefault();
  };
  const laneName = (id: string | null) => world.lanes.find((l) => l._id === id)?.name.replace(/^Runway/, 'runway').replace(/^Taxiway/, 'taxiway') ?? id ?? '';
  const conflict = (snap?.lane_status ?? []).find((l) => l.state === 'conflict');
  const situation: { tone: 'ok' | 'warn' | 'stop'; text: string } = stopsAll[0]
    ? { tone: 'stop', text: `Stop in effect · ${stopsAll[0].target.join(', ') || 'traffic'} held at ${laneName(stopsAll[0].lane_id)}` }
    : conflict
      ? { tone: 'stop', text: `Conflict · ${laneName(conflict._id)}` }
      : pendingShown.length
        ? { tone: 'warn', text: `${pendingShown.length} proposal${pendingShown.length > 1 ? 's' : ''} waiting for you` }
        : { tone: 'ok', text: 'All clear' };
  const wx = snap?.weather;
  const agents = agentSummary(snap?.permissions ?? []);
  const needCount = stopsShown.length + pendingShown.length;
  const onAir = (snap?.transmissions ?? []).some((t) => {
    const t0 = Date.parse(t.t);
    return viewT >= t0 && viewT <= (t.t_end ? Date.parse(t.t_end) : t0 + 3000) + 1500;
  });
  return (
    <div className={`app ${live ? 'mode-live' : 'mode-history'}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            <i />
            <i />
            <i />
          </span>
          <div>
            <b>Phylax Tower</b>
            <span>
              {world.tower.name} · {world.tower.icao}
            </span>
          </div>
        </div>
        <Menu
          label="View as"
          prefix="View as:"
          left
          value={controller}
          onChange={setController}
          options={[
            { value: 'all', label: 'All controllers', detail: 'Every lane and stream · choose who to act as in Needs you', badge: (snap?.pending.length ?? 0) + stopsAll.length, badgeTone: stopsAll.length ? ('stop' as const) : ('warn' as const) },
            ...world.controllers.map((c) => {
              const st = stopsAll.filter((d) => d.controller_id === c._id).length;
              const n = (snap?.pending ?? []).filter((d) => d.controller_id === c._id).length;
              const lanesTxt = c.lanes.map((id) => world.lanes.find((l) => l._id === id)).map((l) => (l ? (l.kind === 'runway' ? `RWY ${l.designators.join('/')}` : l.kind === 'taxiway' ? `TWY ${l.designators[0]}` : 'Apron') : '')).filter(Boolean).join(', ');
              const streamsTxt = world.streams.filter((x) => c.streams.includes(x._id) || x.controller_id === c._id).map((x) => x.name).join(', ');
              return { value: c._id, label: c.name, detail: c.role === 'supervisor' ? 'Supervisor · sees and may act on everything' : `${c.role[0].toUpperCase() + c.role.slice(1)} · ${lanesTxt || 'no lanes'} · ${streamsTxt || 'no streams'}`, badge: st + n, badgeTone: st ? ('stop' as const) : ('warn' as const) };
            }),
          ]}
        />
        <div className="clock-block" title={wx ? `wind ${wx.wind ?? '---'} · visibility ${wx.visibility_sm ?? '--'} SM` : undefined}>
          <span className="clock mono">{hhmmss(viewT)}</span>
          {live ? (
            <span className={`pill ${conn === 'open' ? 'live' : 'down'}`}>
              <span className="dot" /> {conn === 'open' ? 'LIVE' : conn === 'connecting' ? 'CONNECTING' : 'FEED LOST'}
            </span>
          ) : (
            <span className="pill history">HISTORY</span>
          )}
          {ds.mock && <span className="pill quiet">Mock data</span>}
        </div>
        <div className="top-right">
          <button className={`agents-chip tone-${agents.tone}`} onClick={() => setSettings(true)} title={`${agents.title}. Click for settings.`}>
            <span className="dot" /> {agents.text}
          </button>
          <button className="btn" onClick={() => setSettings(true)}>
            Settings
          </button>
          <button className="btn" onClick={() => setDrawer(true)}>
            Audit log
          </button>
        </div>
      </header>

      {!live && (
        <div className="history-banner" role="status">
          <span>
            Viewing <b className="mono">{hhmmss(playhead)}</b> · not live · {ago(now - playhead)} ago · actions disabled
          </span>
          <button className="btn amber" onClick={goLive}>
            Back to live
          </button>
        </div>
      )}

      <main className={`main ${sideHidden ? 'side-hidden' : ''}`}>
        <section className="map-wrap">
          <MapView world={world} objects={objects} laneStatus={snap?.lane_status ?? []} ownedLanes={ownedLanes} flagged={flagged} smooth={live} />
          <div className={`situation tone-${situation.tone}`}>{situation.text}</div>
          {selected && selected.role !== 'supervisor' && <div className="map-filter">{selected.name} · own lanes highlighted</div>}
        </section>
        <button className="side-tab" onClick={() => setSideHidden(!sideHidden)} aria-label={sideHidden ? 'Show the side panel' : 'Hide the side panel'} title={`${sideHidden ? 'Show' : 'Hide'} the side panel ( ] )`}>
          {sideHidden ? '‹' : '›'}
        </button>
        {sideHidden && (
          <aside className="rail">
            <button className={`rail-badge ${stopsShown.length ? 'c-stop' : needCount ? 'c-warn' : ''}`} onClick={() => setSideHidden(false)} title={`Needs you: ${needCount}`}>
              {needCount}
            </button>
            <span className="rail-cap">Needs</span>
            <span className={`rail-act ${onAir ? 'on' : ''}`} title={onAir ? 'A stream is on air' : 'Streams quiet'}>
              <i />
              <i />
              <i />
            </span>
            <span className="rail-cap">Radio</span>
          </aside>
        )}
        <aside className={`side ${sideHidden ? 'hidden' : ''}`}>
          <NeedsYou
            ds={ds}
            world={world}
            stops={stopsShown}
            pending={pendingShown}
            decisions={snap?.decisions ?? []}
            transmissions={snap?.transmissions ?? []}
            actor={actor}
            showActAs={controller === 'all'}
            onActAs={setActAs}
            readOnly={!live}
          />
          <Radio
            world={world}
            streams={streams}
            transmissions={snap?.transmissions ?? []}
            decisions={snap?.decisions ?? []}
            loading={!snap}
            solo={solo}
            onSolo={(id) => {
              silence();
              setSolo(id);
            }}
            live={live}
            viewT={viewT}
            audioUrl={ds.audioUrl}
            mock={ds.mock}
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
        </aside>
      </main>

      <footer className="bottom">
        <Timeline from={tlFrom} to={now} playhead={playhead} live={live} playing={playing} speed={speed} marks={marks} win={win} onWin={setWin} onScrub={scrub} onLive={goLive} onToggle={togglePlay} onSeek={seek} onStep={stepEvent} onSpeed={setSpeed}>
          <div className="dbstrip">
            <div className="db-title">
              <b>
                <span className="dot" /> Live database
              </b>
              <span>MongoDB Atlas · change stream</span>
            </div>
            <div className="db-event mono" key={dbEvent?.n ?? 0}>
              {dbEvent ? (
                <>
                  <span className="db-coll">{dbEvent.coll}</span>
                  <span className="db-json">{dbEvent.json}</span>
                  <span className="db-t">{dbEvent.t}</span>
                </>
              ) : (
                <span className="db-json">waiting for the first change event…</span>
              )}
            </div>
          </div>
        </Timeline>
      </footer>

      {settings && (
        <div className="drawer-back" onClick={() => setSettings(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Settings">
            <header className="drawer-head">
              <h2>Settings</h2>
              <button className="btn" onClick={() => setSettings(false)}>
                Close
              </button>
            </header>
            <div className="drawer-body">
              <AgentSettings ds={ds} permissions={snap?.permissions ?? []} agentState={snap?.agent_state ?? liveSnap?.agent_state ?? []} by={actor} readOnly={!live} />
            </div>
          </div>
        </div>
      )}
      {drawer && (
        <div className="drawer-back" onClick={() => setDrawer(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Audit log">
            <header className="drawer-head">
              <h2>Audit log</h2>
              <button className="btn" onClick={() => setDrawer(false)}>
                Close
              </button>
            </header>
            <div className="drawer-body">
              <Requests ds={ds} world={world} pending={snap?.pending ?? []} decisions={snap?.decisions ?? []} controller={controller} actAs={actAs} onActAs={setActAs} readOnly={!live} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
