import { useEffect, useMemo, useRef, useState } from 'react';
import { distM, makeProjection, NM, type XY } from '../geo';
import type { Lane, LaneState, LaneStatus, ObjectDoc, World } from '../types';

export const STATE_META: Record<LaneState, { label: string; hint: string }> = {
  clear: { label: 'CLEAR', hint: 'no traffic, no clearance' },
  reserved: { label: 'RESERVED', hint: 'an active clearance' },
  occupied: { label: 'OCCUPIED', hint: 'something is on it' },
  conflict: { label: 'CONFLICT', hint: 'a clearance and other traffic' },
  closed: { label: 'CLOSED', hint: 'not in use' },
};
const SC: Record<LaneState, string> = { clear: '#5d6b78', reserved: '#e3b341', occupied: '#66b0ea', conflict: '#f0575c', closed: '#9aa4ad' };

interface Props {
  world: World;
  objects: ObjectDoc[];
  laneStatus: LaneStatus[];
  ownedLanes: Set<string> | null; // null = no controller filter
  flagged: Set<string>; // callsigns targeted by a pending request or a stop
  smooth: boolean;
}

type Zoom = 'airfield' | 'approach';
type Rect = [number, number, number, number];

const PLANE = 'M0,-15 C1.7,-15 2.4,-12 2.4,-9 L2.4,-3.5 L14,4 L14,6.8 L2.4,3.6 L2.1,9.5 L6,12.6 L6,14.4 L0,12.8 L-6,14.4 L-6,12.6 L-2.1,9.5 L-2.4,3.6 L-14,6.8 L-14,4 L-2.4,-3.5 L-2.4,-9 C-2.4,-12 -1.7,-15 0,-15 Z';

// ---- small vector helpers, in metres ----
const add = (p: XY, u: XY, d: number): XY => [p[0] + u[0] * d, p[1] + u[1] * d];
const unit = (a: XY, b: XY): XY => {
  const l = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
  return [(b[0] - a[0]) / l, (b[1] - a[1]) / l];
};
const perp = (u: XY): XY => [-u[1], u[0]];
const pts = (p: XY[]) => p.map((q) => `${q[0].toFixed(1)},${q[1].toFixed(1)}`).join(' ');
const band = (a: XY, b: XY, w: number): XY[] => {
  const n = perp(unit(a, b));
  return [add(a, n, w / 2), add(b, n, w / 2), add(b, n, -w / 2), add(a, n, -w / 2)];
};
function hull(points: XY[]): XY[] {
  const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: XY, a: XY, b: XY) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (list: XY[]) => {
    const h: XY[] = [];
    for (const q of list) {
      while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], q) <= 0) h.pop();
      h.push(q);
    }
    h.pop();
    return h;
  };
  return [...half(p), ...half([...p].reverse())];
}
const hit = (r: Rect, list: Rect[]) => list.some((o) => r[0] < o[2] && r[2] > o[0] && r[1] < o[3] && r[3] > o[1]);

export function MapView({ world, objects, laneStatus, ownedLanes, flagged, smooth }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [zoom, setZoom] = useState<Zoom>('airfield');
  const [help, setHelp] = useState(false);
  const [layers, setLayers] = useState({ labels: true, tags: true, final: true });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // Everything static, in metres around the tower.
  const geom = useMemo(() => {
    const proj = makeProjection(world.tower.center.coordinates);
    const all: XY[] = [];
    const lines = new Map<string, { a: XY; b: XY }>();
    const polys = new Map<string, XY[]>();
    for (const l of world.lanes) {
      if (l.centerline) {
        const c = l.centerline.coordinates;
        const a = proj.toM(c[0]);
        const b = proj.toM(c[c.length - 1]);
        lines.set(l._id, { a, b });
        all.push(a, b);
      } else if (l.area?.coordinates[0]) {
        const ring = l.area.coordinates[0].slice(0, -1).map((c) => proj.toM(c));
        polys.set(l._id, ring);
        all.push(...ring);
      }
    }
    const box = (p: XY[], pad: number) => {
      const xs = p.map((q) => q[0]);
      const ys = p.map((q) => q[1]);
      return { x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad, y0: Math.min(...ys) - pad, y1: Math.max(...ys) + pad };
    };
    const rwy = world.lanes.find((l) => l.designators.includes('04') && l.centerline) ?? world.lanes.find((l) => l.kind === 'runway' && l.centerline);
    let final: { thr: XY; ux: number; uy: number; name: string; lane: string } | null = null;
    const approachPts = [...all];
    const fieldPts = [...all];
    if (rwy && lines.has(rwy._id)) {
      const { a, b } = lines.get(rwy._id)!;
      const first = rwy.designators[0] === '04' || !rwy.designators.includes('04');
      const thr = first ? a : b;
      const u = unit(first ? b : a, thr);
      final = { thr, ux: u[0], uy: u[1], name: first ? rwy.designators[0] : '04', lane: rwy._id };
      approachPts.push(add(thr, u, 7.5 * NM));
      fieldPts.push(add(thr, u, 700));
    }
    // the apron's landside edge: its long edge farthest from the runways
    const apronId = world.lanes.find((l) => l.kind === 'apron' && polys.has(l._id))?._id;
    const apron = apronId ? polys.get(apronId)! : null;
    let land: { A: XY; B: XY; out: XY; L: number } | null = null;
    const rc = final?.thr ?? all[0];
    if (apron && apron.length >= 4 && rc) {
      const cen: XY = [apron.reduce((s, v) => s + v[0], 0) / apron.length, apron.reduce((s, v) => s + v[1], 0) / apron.length];
      let bd = -1;
      for (let i = 0; i < apron.length; i++) {
        const a = apron[i];
        const b = apron[(i + 1) % apron.length];
        const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
        const m: XY = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const d = Math.hypot(m[0] - rc[0], m[1] - rc[1]) + L;
        if (L > 400 && d > bd) {
          bd = d;
          let n = perp(unit(a, b));
          if ((m[0] - cen[0]) * n[0] + (m[1] - cen[1]) * n[1] < 0) n = [-n[0], -n[1]];
          land = { A: a, B: b, out: n, L };
        }
      }
    }
    const extra: XY[] = [];
    const ls = land as { A: XY; B: XY; out: XY; L: number } | null;
    if (ls) extra.push(add(ls.A, ls.out, 230), add(ls.B, ls.out, 230));
    const h = hull([...all, ...extra]);
    const c: XY = [h.reduce((s, v) => s + v[0], 0) / h.length, h.reduce((s, v) => s + v[1], 0) / h.length];
    const shore = h.map((q) => add(q, unit(c, q), 300));
    // fire station: at the north-west end of taxiway D
    const twyD = world.lanes.find((l) => l.kind === 'taxiway' && l.designators[0] === 'D' && lines.has(l._id));
    let fire: { p: XY; u: XY } | null = null;
    if (twyD) {
      const { a, b } = lines.get(twyD._id)!;
      const west = a[0] < b[0] ? a : b;
      const u = unit(a[0] < b[0] ? b : a, west);
      fire = { p: add(west, u, 55), u };
    }
    return { proj, lines, polys, airfield: box([...fieldPts, ...shore.filter((_, i) => i % 2 === 0).slice(0, 0)], 330), approach: box(approachPts, 2600), final, land: ls, shore, fire };
  }, [world]);

  const view = zoom === 'airfield' ? geom.airfield : geom.approach;
  const scale = Math.min(size.w / (view.x1 - view.x0), Math.max(120, size.h - 72) / (view.y1 - view.y0));
  const cx = (view.x0 + view.x1) / 2;
  const cy = (view.y0 + view.y1) / 2;
  const tx = size.w / 2 - cx * scale;
  const ty = size.h / 2 + 30 - cy * scale;
  const mpx = (m: XY): XY => [m[0] * scale + tx, m[1] * scale + ty];
  const px = (lonLat: number[]): XY => mpx(geom.proj.toM(lonLat));
  const far = zoom === 'approach';
  const inv = 1 / scale; // one pixel, in metres

  const statusOf = (id: string) => laneStatus.find((s) => s._id === id);
  const stateOf = (id: string): LaneState => statusOf(id)?.state ?? 'clear';
  const dim = (id: string) => ownedLanes !== null && !ownedLanes.has(id);
  const laneOf = (id: string | null) => world.lanes.find((l) => l._id === id);
  const widthOf = (l: Lane) => Math.max((l.width_m ?? 20) * 1.35, (l.kind === 'runway' ? 8 : 4.5) * inv);
  const order = { apron: 0, taxiway: 1, runway: 2 };
  const lanes = [...world.lanes].sort((a, b) => order[a.kind] - order[b.kind]);
  const thrPx = geom.final ? mpx(geom.final.thr) : null;
  const thrLL = geom.final ? geom.proj.toLonLat(geom.final.thr) : null;

  // label placement: nothing may sit on the situation chip, the toolbar or another label
  const placed: Rect[] = [
    [0, 0, 330, 52],
    [size.w - 340, 0, size.w, 124],
    [size.w - 190, size.h - 48, size.w, size.h],
  ];
  const place = (cands: Rect[]): Rect => {
    const pick = cands.find((r) => !hit(r, placed)) ?? cands[0];
    placed.push(pick);
    return pick;
  };

  // ---- pixel-space labels for the lanes ----
  const laneLabels: JSX.Element[] = [];
  if (!far) {
    for (const l of lanes) {
      const ln = geom.lines.get(l._id);
      const d = dim(l._id) ? 0.4 : 1;
      if (ln && l.kind === 'runway') {
        const u = unit(ln.a, ln.b);
        [ln.a, ln.b].forEach((e, i) => {
          const q = mpx(add(e, u, (i ? 1 : -1) * Math.max(110, 22 * inv)));
          placed.push([q[0] - 16, q[1] - 12, q[0] + 16, q[1] + 12]);
          laneLabels.push(
            <text key={`${l._id}-n${i}`} x={q[0]} y={q[1] + 7} textAnchor="middle" className="rwy-num quiet-label" opacity={d}>
              {l.designators[i] ?? ''}
            </text>,
          );
        });
      }
    }
    for (const l of lanes) {
      const ln = geom.lines.get(l._id);
      const st = stateOf(l._id);
      const d = dim(l._id) ? 0.4 : 1;
      const tagText = st === 'clear' ? '' : STATE_META[st].label;
      const tw = tagText ? tagText.length * 6.7 + 12 : 0;
      if (ln && l.kind === 'taxiway') {
        const n = perp(unit(ln.a, ln.b));
        const off = (widthOf(l) / 2) * scale + 15;
        const cands: Rect[] = [];
        for (const f of [0.5, 0.25, 0.75, 0.1, 0.9])
          for (const s of [1, -1]) {
            const c = mpx([ln.a[0] + (ln.b[0] - ln.a[0]) * f, ln.a[1] + (ln.b[1] - ln.a[1]) * f]);
            const q: XY = [c[0] + n[0] * off * s, c[1] + n[1] * off * s];
            cands.push([q[0] - 9, q[1] - 9, q[0] + 9 + (tw ? tw + 3 : 0), q[1] + 9]);
          }
        const r = place(cands);
        laneLabels.push(
          <g key={`${l._id}-b`} transform={`translate(${r[0]},${r[1]})`} opacity={d} className="quiet-label">
            <title>{statusOf(l._id)?.reason ?? `${l.name}: ${st}`}</title>
            <rect width={18} height={18} rx={2} className="twy-badge" />
            <text x={9} y={13.5} textAnchor="middle" className="twy-letter">
              {l.designators[0]}
            </text>
            {tagText && (
              <>
                <rect x={21} y={1} width={tw} height={16} rx={2} className="state-tag" stroke={SC[st]} />
                <text x={21 + tw / 2} y={13} textAnchor="middle" className="state-text" fill={SC[st]}>
                  {tagText}
                </text>
              </>
            )}
          </g>,
        );
      } else if (ln && l.kind === 'runway' && st !== 'clear') {
        const text = `RWY ${l.designators.join('/')} ${STATE_META[st].label}`;
        const w = text.length * 7 + (st === 'conflict' ? 30 : 14);
        const n = perp(unit(ln.a, ln.b));
        const off = (widthOf(l) / 2) * scale + 22;
        const cands: Rect[] = [];
        for (const f of [0.7, 0.3, 0.5, 0.85, 0.15])
          for (const s of [-1, 1]) {
            const c = mpx([ln.a[0] + (ln.b[0] - ln.a[0]) * f, ln.a[1] + (ln.b[1] - ln.a[1]) * f]);
            const q: XY = [c[0] + n[0] * off * s, c[1] + n[1] * off * s];
            const x0 = n[0] * s < 0 ? q[0] - w : q[0];
            cands.push([x0, q[1] - 10, x0 + w, q[1] + 10]);
          }
        const r = place(cands);
        laneLabels.push(
          <g key={`${l._id}-f`} transform={`translate(${r[0]},${r[1]})`} opacity={d} className={`rwy-flag st-${st}`}>
            <title>{statusOf(l._id)?.reason ?? `${l.name}: ${st}`}</title>
            <rect width={w} height={20} rx={2} fill={st === 'conflict' ? '#c2272d' : '#141a20'} stroke={SC[st]} />
            {st === 'conflict' && <path d="M6,15 L12,4.5 L18,15 Z" fill="#fff" />}
            <text x={st === 'conflict' ? 23 : 7} y={14} className="state-text" fill={st === 'conflict' ? '#fff' : SC[st]}>
              {text}
            </text>
          </g>,
        );
      } else if (!ln && st !== 'clear' && geom.polys.has(l._id)) {
        const ring = geom.polys.get(l._id)!.map(mpx);
        const top = ring.reduce((m, v) => (v[1] < m[1] ? v : m), ring[0]);
        const bot = ring.reduce((m, v) => (v[1] > m[1] ? v : m), ring[0]);
        const text = `APRON ${STATE_META[st].label}`;
        const w = text.length * 6.7 + 12;
        const r = place([
          [bot[0] + 12, bot[1] + 2, bot[0] + 12 + w, bot[1] + 18],
          [bot[0] - w / 2, bot[1] + 12, bot[0] + w / 2, bot[1] + 28],
          [top[0] + 14, top[1] + 30, top[0] + 14 + w, top[1] + 46],
          [top[0] - w / 2, top[1] - 24, top[0] + w / 2, top[1] - 8],
        ]);
        laneLabels.push(
          <g key={`${l._id}-t`} transform={`translate(${r[0]},${r[1]})`} opacity={d}>
            <rect width={w} height={16} rx={2} className="state-tag" stroke={SC[st]} />
            <text x={w / 2} y={12} textAnchor="middle" className="state-text" fill={SC[st]}>
              {text}
            </text>
          </g>,
        );
      }
    }
  }

  // ---- the conflict: the point on the runway and the line from the landing aircraft ----
  let conflict: { p: XY; from: XY | null } | null = null;
  const cl = world.lanes.find((l) => l.kind === 'runway' && stateOf(l._id) === 'conflict' && geom.lines.has(l._id));
  if (cl) {
    const { a, b } = geom.lines.get(cl._id)!;
    const u = unit(a, b);
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
    let best: { t: number; d: number } | null = null;
    for (const o of objects) {
      if (o.status === 'airborne') continue;
      const m = geom.proj.toM(o.position.coordinates);
      const t = (m[0] - a[0]) * u[0] + (m[1] - a[1]) * u[1];
      const d = Math.abs((m[0] - a[0]) * -u[1] + (m[1] - a[1]) * u[0]);
      const onIt = o.lane_id === cl._id || d < 45;
      if (onIt && t > -50 && t < L + 50 && (o.kind !== 'aircraft' || !best) && (!best || d < best.d)) best = { t: Math.max(0, Math.min(L, t)), d };
    }
    if (best) {
      const land = objects.filter((o) => o.kind === 'aircraft' && o.status === 'airborne').sort((x, y) => (x.alt_ft ?? 1e9) - (y.alt_ft ?? 1e9))[0];
      conflict = { p: mpx(add(a, u, best.t)), from: land ? px(land.position.coordinates) : null };
    }
  }

  // ---- inbound chip ----
  const inbound = objects
    .filter((o) => o.status === 'airborne' && o.kind === 'aircraft')
    .map((o) => ({ o, d: thrLL ? distM(o.position.coordinates, thrLL) / NM : 0 }))
    .sort((a, b) => a.d - b.d)[0];

  // ---- scale bar ----
  const barM = [100, 200, 500, 1000, 2000, 5000, 10000].find((m) => m * scale >= 70) ?? 10000;

  const overlay = (l: Lane, shape: XY[]) => {
    const st = stateOf(l._id);
    if (st === 'clear') return null;
    const common = { points: pts(shape), strokeWidth: 1.8 * inv, strokeLinejoin: 'round' as const };
    if (st === 'reserved') return <polygon {...common} fill="none" stroke={SC.reserved} strokeDasharray={`${8 * inv} ${5 * inv}`} />;
    if (st === 'occupied') return <polygon {...common} fill="url(#m-dots)" stroke={SC.occupied} strokeDasharray={`${1.5 * inv} ${4 * inv}`} strokeLinecap="round" />;
    if (st === 'conflict') return <polygon {...common} fill="url(#m-hatch)" stroke={SC.conflict} strokeWidth={2.2 * inv} />;
    return <polygon {...common} fill="url(#m-cross)" stroke={SC.closed} />;
  };

  return (
    <div className={`map ${layers.labels ? '' : 'no-labels'} ${layers.tags ? '' : 'no-tags'} ${layers.final ? '' : 'no-final'}`} ref={ref}>
      <svg width={size.w} height={size.h} role="img" aria-label="Airfield map">
        <defs>
          <pattern id="m-wave" width="64" height="30" patternUnits="userSpaceOnUse">
            <path d="M0 15q8-5.5 16 0t16 0" fill="none" stroke="#2b4755" strokeWidth="1.2" />
          </pattern>
          <pattern id="m-grass" width="28" height="28" patternUnits="userSpaceOnUse" patternTransform={`scale(${inv})`}>
            <rect width="28" height="28" fill="#2d3c31" />
            <path d="M0 14h28" stroke="#314236" strokeWidth="9" />
          </pattern>
          <pattern id="m-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform={`scale(${inv}) rotate(45)`}>
            <rect width="8" height="8" fill="#f0575c" fillOpacity="0.16" />
            <rect width="2.6" height="8" fill="#f0575c" fillOpacity="0.85" />
          </pattern>
          <pattern id="m-dots" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform={`scale(${inv})`}>
            <circle cx="3.5" cy="3.5" r="1" fill="#66b0ea" fillOpacity="0.8" />
          </pattern>
          <pattern id="m-cross" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform={`scale(${inv})`}>
            <path d="M0 0L8 8M8 0L0 8" stroke="#9aa4ad" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width={size.w} height={size.h} fill="#1d323d" />
        <rect width={size.w} height={size.h} fill="url(#m-wave)" />

        {/* scenery, drawn in metres */}
        <g transform={`translate(${tx},${ty}) scale(${scale})`}>
          <polygon points={pts(geom.shore)} fill="url(#m-grass)" stroke="#6b6a55" strokeWidth={Math.max(14, 3 * inv)} strokeLinejoin="round" />
          {lanes.map((l) => {
            const ring = geom.polys.get(l._id);
            if (!ring) return null;
            const t = geom.land;
            const ue = t ? unit(t.A, t.B) : null;
            return (
              <g key={l._id} opacity={dim(l._id) ? 0.45 : 1} className={`lane lane-${l.kind} st-${stateOf(l._id)}`}>
                <title>{`${l.name}: ${stateOf(l._id)}`}</title>
                <polygon points={pts(ring)} fill="#3e464f" stroke="#59636d" strokeWidth={Math.max(3, inv)} />
                {t && ue && !far && (
                  <>
                    {[0.2, 0.5, 0.8].map((f) => {
                      const c = add(t.A, ue, t.L * f);
                      return <polygon key={f} points={pts(band(add(c, t.out, 10), add(c, t.out, -170), 44))} fill="#59646f" stroke="#8a96a1" strokeWidth={Math.max(3, inv)} />;
                    })}
                    <polygon points={pts([add(t.A, t.out, 6), add(t.B, t.out, 6), add(t.B, t.out, 120), add(t.A, t.out, 120)])} fill="#59646f" stroke="#8a96a1" strokeWidth={Math.max(3, inv)} strokeLinejoin="round" />
                    {[0.35, 0.65].flatMap((f) =>
                      [70, 150].map((dd) => {
                        const c = add(add(t.A, ue, t.L * f), t.out, -dd);
                        return <path key={`${f}-${dd}`} d={PLANE} transform={`translate(${c[0]},${c[1]}) rotate(${(Math.atan2(t.out[1], t.out[0]) * 180) / Math.PI + 90}) scale(1.5)`} fill="none" stroke="#aab6c1" strokeWidth={0.9} />;
                      }),
                    )}
                  </>
                )}
                {overlay(l, ring)}
              </g>
            );
          })}
          {geom.fire && !far && <polygon points={pts(band(add(geom.fire.p, geom.fire.u, -35), add(geom.fire.p, geom.fire.u, 35), 80))} fill="#7a4a44" stroke="#b58a84" strokeWidth={Math.max(3, inv)} />}
          {lanes.map((l) => {
            const ln = geom.lines.get(l._id);
            if (!ln) return null;
            const { a, b } = ln;
            const w = widthOf(l);
            const u = unit(a, b);
            const n = perp(u);
            const seg = { x1: a[0], y1: a[1], x2: b[0], y2: b[1] };
            const rw = l.kind === 'runway';
            return (
              <g key={l._id} opacity={dim(l._id) ? 0.45 : 1} className={`lane lane-${l.kind} st-${stateOf(l._id)}`}>
                <title>{`${l.name}: ${stateOf(l._id)}`}</title>
                {rw ? (
                  <>
                    <line {...seg} stroke="#3a434b" strokeWidth={w + 14} />
                    <line {...seg} stroke="#12161a" strokeWidth={w} />
                    {!far && (
                      <>
                        <polygon points={pts(band(a, b, w * 0.86))} fill="none" stroke="#fff" strokeOpacity={0.5} strokeWidth={Math.max(1.6, 0.6 * inv)} />
                        <line x1={a[0] + u[0] * 130} y1={a[1] + u[1] * 130} x2={b[0] - u[0] * 130} y2={b[1] - u[1] * 130} stroke="#fff" strokeOpacity={0.85} strokeWidth={Math.max(3, 1.1 * inv)} strokeDasharray="38 26" />
                        {([[a, 1], [b, -1]] as [XY, number][]).map(([e, d], i) => {
                          const c = add(e, u, d * 48);
                          const q = add(e, u, d * 330);
                          return (
                            <g key={i}>
                              <line x1={c[0] - n[0] * w * 0.38} y1={c[1] - n[1] * w * 0.38} x2={c[0] + n[0] * w * 0.38} y2={c[1] + n[1] * w * 0.38} stroke="#fff" strokeOpacity={0.9} strokeWidth={46} strokeDasharray={`${w * 0.085} ${w * 0.068}`} />
                              <line x1={q[0] - n[0] * w * 0.34} y1={q[1] - n[1] * w * 0.34} x2={q[0] + n[0] * w * 0.34} y2={q[1] + n[1] * w * 0.34} stroke="#fff" strokeOpacity={0.8} strokeWidth={54} strokeDasharray={`${w * 0.2} ${w * 0.28} ${w * 0.2}`} />
                            </g>
                          );
                        })}
                      </>
                    )}
                  </>
                ) : (
                  <>
                    <line {...seg} stroke="#474f58" strokeWidth={w} strokeLinecap="round" />
                    {!far && <line {...seg} stroke="#e8bd2c" strokeWidth={Math.max(3, 1 * inv)} />}
                  </>
                )}
                {overlay(l, band(add(a, u, -14), add(b, u, 14), w + 26))}
              </g>
            );
          })}
          {!far &&
            world.hold_points.map((h) => {
              const ln = geom.lines.get(h.lane_id);
              const lane = laneOf(h.lane_id);
              if (!ln || !lane) return null;
              const u = unit(ln.a, ln.b);
              const n = perp(u);
              const p = geom.proj.toM(h.position.coordinates);
              const mid: XY = [(ln.a[0] + ln.b[0]) / 2, (ln.a[1] + ln.b[1]) / 2];
              const toward = (mid[0] - p[0]) * u[0] + (mid[1] - p[1]) * u[1] > 0 ? 1 : -1; // dashes face the runway
              const hw = widthOf(lane) / 2 + 4;
              const q = add(p, u, toward * 8);
              return (
                <g key={h._id} data-hp={h._id} transform={`translate(${p[0]},${p[1]})`} opacity={dim(h.lane_id) ? 0.4 : 1}>
                  <title>{`Hold point ${h._id} (protects ${h.protects})`}</title>
                  <line x1={-n[0] * hw} y1={-n[1] * hw} x2={n[0] * hw} y2={n[1] * hw} stroke="#f1c832" strokeWidth={Math.max(5, 1.6 * inv)} />
                  <line x1={q[0] - p[0] - n[0] * hw} y1={q[1] - p[1] - n[1] * hw} x2={q[0] - p[0] + n[0] * hw} y2={q[1] - p[1] + n[1] * hw} stroke="#f1c832" strokeWidth={Math.max(3.5, 1.2 * inv)} strokeDasharray="6 4" />
                </g>
              );
            })}
        </g>

        {/* fixed labels */}
        {!far &&
          (() => {
            const p = px([-73.8575, 40.7822]);
            if (p[0] > size.w - 150) p[0] = size.w - 150;
            if (p[1] < 150) p[1] = 150;
            const t = geom.land;
            const out: JSX.Element[] = [
              <text key="bay" x={p[0]} y={p[1]} className="water-label quiet-label">
                FLUSHING BAY
              </text>,
            ];
            if (t) {
              const ue = unit(t.A, t.B);
              let ang = (Math.atan2(ue[1], ue[0]) * 180) / Math.PI;
              if (ang > 90 || ang < -90) ang += 180;
              const c1 = mpx(add(add(t.A, ue, t.L * 0.5), t.out, 63));
              const c2 = mpx(add(add(t.A, ue, t.L * 0.5), t.out, -215));
              out.push(
                <text key="term" transform={`translate(${c1[0]},${c1[1] + 4}) rotate(${ang})`} textAnchor="middle" className="slab-label quiet-label">
                  TERMINAL
                </text>,
                <text key="apron" transform={`translate(${c2[0]},${c2[1] + 4}) rotate(${ang})`} textAnchor="middle" className="slab-label dimmer quiet-label">
                  MAIN APRON
                </text>,
              );
            }
            if (geom.fire) {
              const f = mpx(add(geom.fire.p, geom.fire.u, 60));
              placed.push([f[0] - 104, f[1] - 18, f[0] + 4, f[1] + 10]);
              out.push(
                <text key="fire" x={f[0] - 8} y={f[1]} textAnchor="end" className="slab-label quiet-label">
                  Fire station
                </text>,
              );
            }
            return out;
          })()}

        {geom.final && thrPx && (
          <g className="final">
            {(() => {
              const f = geom.final;
              const b = mpx([f.thr[0] + f.ux * 8 * NM, f.thr[1] + f.uy * 8 * NM]);
              const ang = Math.atan2(f.uy, f.ux);
              return (
                <>
                  <line x1={thrPx[0]} y1={thrPx[1]} x2={b[0]} y2={b[1]} className="final-line" />
                  {far &&
                    [2, 4, 6].map((nm) => {
                      const r = nm * NM * scale;
                      const s = [thrPx[0] + r * Math.cos(ang - 0.3), thrPx[1] + r * Math.sin(ang - 0.3)];
                      const e = [thrPx[0] + r * Math.cos(ang + 0.3), thrPx[1] + r * Math.sin(ang + 0.3)];
                      return (
                        <g key={nm}>
                          <path d={`M${s[0]},${s[1]} A${r},${r} 0 0 1 ${e[0]},${e[1]}`} className="final-arc" />
                          <text x={e[0] - 8} y={e[1] + 18} className="final-label">
                            {nm} nm
                          </text>
                        </g>
                      );
                    })}
                </>
              );
            })()}
          </g>
        )}

        {laneLabels}

        {conflict && (
          <g className="conflict-mark">
            {conflict.from && <line x1={conflict.from[0]} y1={conflict.from[1]} x2={conflict.p[0]} y2={conflict.p[1]} />}
            <circle cx={conflict.p[0]} cy={conflict.p[1]} r={13} />
          </g>
        )}

        {objects.map((o) => {
          const p = px(o.position.coordinates);
          const name = o.callsign ?? (o.kind === 'person' ? 'Person' : o.kind === 'unknown' ? 'Unknown' : (o.label ?? o._id));
          const air = o.status === 'airborne';
          const lane = laneOf(o.lane_id);
          const hot = flagged.has(o.callsign ?? o._id);
          const alert = hot || (!!lane && stateOf(lane._id) === 'conflict');
          if (p[0] < 0 || p[0] > size.w || p[1] < 0 || p[1] > size.h) {
            // outside the view: a marker on the map border pointing toward it
            const dx = p[0] - size.w / 2;
            const dy = p[1] - size.h / 2;
            const k = Math.min((size.w / 2 - 24) / Math.abs(dx || 1e-6), (size.h / 2 - 24) / Math.abs(dy || 1e-6));
            const ex = size.w / 2 + dx * k;
            let ey = Math.max(136, size.h / 2 + dy * k);
            const right = ex > size.w / 2;
            const ew = name.length * 7.3 + 70;
            const er = (): Rect => [right ? ex - ew : ex - 6, ey - 12, right ? ex + 10 : ex + ew, ey + 8];
            for (let i = 0; i < 6 && hit(er(), placed); i++) ey -= 18; // keep edge markers apart
            placed.push(er());
            const nm = distM(o.position.coordinates, world.tower.center.coordinates) / NM;
            return (
              <g key={o._id} data-id={o._id} className={`obj-edge ${alert ? 'hot' : ''}`} transform={`translate(${ex},${ey})`}>
                <title>{`${name} is outside this view`}</title>
                <path d="M9,0 L-3,-5 L-3,5 Z" transform={`rotate(${(Math.atan2(dy, dx) * 180) / Math.PI})`} />
                <text x={right ? -10 : 14} y={ey > size.h - 40 ? -10 : 4} textAnchor={right ? 'end' : 'start'}>
                  {name} · {nm.toFixed(1)} nm
                </text>
              </g>
            );
          }
          const spd = o.speed_kt != null ? Math.round(o.speed_kt) : null;
          const des = lane ? (lane.kind === 'runway' ? lane.designators.join('/') : lane.designators[0]) : '';
          const sub = air
            ? `${o.alt_ft != null ? Math.round(o.alt_ft / 10) * 10 : '---'} ft · ${spd ?? '---'} kt`
            : o.hold_short
              ? `${hot ? 'STOPPED' : 'holding'} · hold ${des}`
              : hot && (spd ?? 0) < 1
                ? `STOPPED${des ? ` · ${lane?.kind === 'runway' ? 'rwy ' : ''}${des}` : ''}`
                : (spd ?? 0) >= 1
                  ? `${o.kind === 'aircraft' ? 'taxi' : o.kind === 'person' ? 'on foot' : 'moving'} · ${spd} kt`
                  : o.kind === 'person'
                    ? 'on foot'
                    : lane?.kind === 'runway'
                      ? `on rwy ${des}`
                      : des
                        ? `on ${des}`
                        : '';
          const showLabel = !far || air || alert;
          const tw = Math.max(name.length, sub.length) * 7.3 + 15;
          const th = sub ? 34 : 20;
          let box: Rect | null = null;
          if (showLabel) {
            const cands: Rect[] = [];
            for (const [dx, dy] of [[20, -th - 8], [20, 10], [-20 - tw, -th - 8], [-20 - tw, 10], [26, -th - 44], [26, 46], [-26 - tw, -th - 44], [-26 - tw, 46], [-tw / 2, -th - 60], [-tw / 2, 62]])
              cands.push([p[0] + dx, p[1] + dy, p[0] + dx + tw, p[1] + dy + th]);
            box = place(cands);
          }
          const bx = box ? box[0] - p[0] : 0;
          const by = box ? box[1] - p[1] : 0;
          return (
            <g key={o._id} data-id={o._id} className={`obj obj-${o.kind} ${air ? 'air' : 'gnd'} ${smooth ? 'smooth' : ''} ${alert ? 'hot' : ''}`} style={{ transform: `translate(${p[0]}px, ${p[1]}px)` }}>
              <title>{`${name} · ${o.kind} · ${o.status}${o.lane_id ? ' · on ' + o.lane_id : ''}`}</title>
              {box && <line x1={0} y1={0} x2={bx < 0 ? bx + tw : bx} y2={by < 0 ? by + th : by} className="leader" />}
              {hot && <circle r={o.kind === 'aircraft' ? 17 : 12} className="obj-ring" />}
              <ObjGlyph o={o} small={far} />
              {box && (
                <g transform={`translate(${bx},${by})`} className="obj-label">
                  <rect width={tw} height={th} rx={3} className="tag-box" />
                  <text x={7} y={14} className="obj-name">
                    {name}
                  </text>
                  {sub && (
                    <text x={7} y={28} className="obj-data">
                      {sub}
                    </text>
                  )}
                </g>
              )}
            </g>
          );
        })}

        {/* north arrow and scale bar */}
        <g transform={`translate(${size.w - 30},${96})`} className="north">
          <circle r={16} />
          <path d="M0,-11 L5,6 L0,3 L-5,6 Z" />
          <text y={30} textAnchor="middle">
            N
          </text>
        </g>
        <g transform={`translate(${size.w - 24 - barM * scale},${size.h - 22})`} className="scalebar">
          <path d={`M0,-5 V0 H${barM * scale} V-5 M${(barM * scale) / 2},-3 V0`} />
          <text x={(barM * scale) / 2} y={-9} textAnchor="middle">
            {barM >= 1000 ? `${barM / 1000} km` : `${barM} m`}
          </text>
        </g>
      </svg>

      <div className="map-tools">
        <div className="seg small" role="group" aria-label="Map zoom">
          <button className={zoom === 'airfield' ? 'on' : ''} onClick={() => setZoom('airfield')}>
            Airfield
          </button>
          <button className={zoom === 'approach' ? 'on' : ''} onClick={() => setZoom('approach')}>
            Approach
          </button>
        </div>
        <button className="menu-btn" aria-expanded={help} onClick={() => setHelp(!help)}>
          Layers / legend
          <svg width="8" height="5" viewBox="0 0 8 5" aria-hidden>
            <path d="M1 1l3 3 3-3" fill="none" stroke="currentColor" strokeWidth="1.25" />
          </svg>
        </button>
        {help && (
          <div className="popover help">
            <div className="pop-title">Layers</div>
            {(['labels', 'tags', 'final'] as const).map((k) => (
              <label key={k} className="layer-row">
                <button type="button" role="switch" aria-checked={layers[k]} className={`toggle ${layers[k] ? 'on' : ''}`} onClick={() => setLayers({ ...layers, [k]: !layers[k] })}>
                  <span />
                </button>
                {k === 'labels' ? 'Lane names' : k === 'tags' ? 'Object tags' : 'Final approach line'}
              </label>
            ))}
            <div className="pop-title">Legend</div>
            <p><i className="key k-reserved" /> Reserved: an active clearance</p>
            <p><i className="key k-occupied" /> Occupied: something is on it</p>
            <p><i className="key k-conflict" /> Conflict: a clearance and other traffic</p>
            <p><i className="key k-closed" /> Closed</p>
            <p><i className="key k-hold" /> Hold-short bar</p>
            <p><i className="key k-veh" /> Vehicle · <i className="key k-person" /> person</p>
            <p className="pop-note">A red ring marks the target of a stop or a waiting proposal, and the conflict point on a runway.</p>
          </div>
        )}
      </div>
      {inbound && (
        <button className="inbound-chip" onClick={() => setZoom('approach')} title="Nearest inbound aircraft. Click for the approach view.">
          <b className="mono">{inbound.o.callsign ?? inbound.o._id}</b> · {inbound.d.toFixed(1)} nm final · runway {geom.final?.name ?? ''}
        </button>
      )}
    </div>
  );
}

function ObjGlyph({ o, small }: { o: ObjectDoc; small: boolean }) {
  const hdg = o.heading_deg ?? 0;
  const moving = (o.speed_kt ?? 0) > 1;
  if (o.kind === 'aircraft')
    return (
      <g transform={`rotate(${hdg})`}>
        {moving && !small && <line y1={-13} y2={-13 - Math.min(22, 6 + (o.speed_kt ?? 0) / 8)} className="track" />}
        <path d={PLANE} className="glyph plane" transform={`scale(${small ? 0.5 : 0.8})`} />
      </g>
    );
  if (o.kind === 'vehicle') return <rect x={-3.5} y={-6} width={7} height={12} rx={1} className="glyph vehicle" transform={`rotate(${hdg})`} />;
  if (o.kind === 'person') return <circle r={3} className="glyph person" />;
  return <path d="M0,-6 L6,0 L0,6 L-6,0 Z" className="glyph unknown" />;
}
