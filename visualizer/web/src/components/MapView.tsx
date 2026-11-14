import { useEffect, useMemo, useRef, useState } from 'react';
import { bearing, distM, makeProjection, NM, type XY } from '../geo';
import type { Lane, LaneState, LaneStatus, ObjectDoc, World } from '../types';

export const STATE_META: Record<LaneState, { label: string; glyph: string; hint: string }> = {
  clear: { label: 'CLEAR', glyph: '', hint: 'no traffic, no clearance' },
  reserved: { label: 'RESERVED', glyph: '◷', hint: 'an active clearance' },
  occupied: { label: 'OCCUPIED', glyph: '●', hint: 'something is on it' },
  conflict: { label: 'CONFLICT', glyph: '▲', hint: 'clearance + other traffic' },
  closed: { label: 'CLOSED', glyph: '✕', hint: 'not in use' },
};
const STATES = Object.keys(STATE_META) as LaneState[];

interface Props {
  world: World;
  objects: ObjectDoc[];
  laneStatus: LaneStatus[];
  ownedLanes: Set<string> | null; // null = no controller filter
  flagged: Set<string>; // callsigns targeted by a pending request
  smooth: boolean;
}

type Zoom = 'airfield' | 'approach';

export function MapView({ world, objects, laneStatus, ownedLanes, flagged, smooth }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [zoom, setZoom] = useState<Zoom>('airfield');

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const geom = useMemo(() => {
    const proj = makeProjection(world.tower.center.coordinates);
    const pts: XY[] = [];
    for (const l of world.lanes) {
      l.centerline?.coordinates.forEach((c) => pts.push(proj.toM(c)));
      l.area?.coordinates[0]?.forEach((c) => pts.push(proj.toM(c)));
    }
    world.hold_points.forEach((h) => pts.push(proj.toM(h.position.coordinates)));
    const box = (p: XY[], pad: number) => {
      const xs = p.map((q) => q[0]);
      const ys = p.map((q) => q[1]);
      return { x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad, y0: Math.min(...ys) - pad, y1: Math.max(...ys) + pad };
    };
    // Final to runway 04: the extended centerline behind the first designator's threshold.
    const rwy = world.lanes.find((l) => l.designators.includes('04') && l.centerline) ?? world.lanes.find((l) => l.kind === 'runway' && l.centerline);
    let final: { thr: XY; ux: number; uy: number; name: string } | null = null;
    const approachPts = [...pts];
    if (rwy?.centerline) {
      const c = rwy.centerline.coordinates;
      const first = rwy.designators[0] === '04' || !rwy.designators.includes('04');
      const thr = proj.toM(first ? c[0] : c[c.length - 1]);
      const far = proj.toM(first ? c[c.length - 1] : c[0]);
      const len = Math.hypot(far[0] - thr[0], far[1] - thr[1]);
      const ux = (thr[0] - far[0]) / len;
      const uy = (thr[1] - far[1]) / len;
      final = { thr, ux, uy, name: first ? rwy.designators[0] : '04' };
      approachPts.push([thr[0] + ux * 7.5 * NM, thr[1] + uy * 7.5 * NM]);
    }
    return { proj, airfield: box(pts, 260), approach: box(approachPts, 2600), final };
  }, [world]);

  const view = zoom === 'airfield' ? geom.airfield : geom.approach;
  const scale = Math.min(size.w / (view.x1 - view.x0), size.h / (view.y1 - view.y0));
  const cx = (view.x0 + view.x1) / 2;
  const cy = (view.y0 + view.y1) / 2;
  const px = (lonLat: number[]): XY => {
    const m = geom.proj.toM(lonLat);
    return [(m[0] - cx) * scale + size.w / 2, (m[1] - cy) * scale + size.h / 2];
  };
  const mpx = (m: XY): XY => [(m[0] - cx) * scale + size.w / 2, (m[1] - cy) * scale + size.h / 2];
  const far = zoom === 'approach';

  const statusOf = (id: string) => laneStatus.find((s) => s._id === id);
  const dim = (id: string) => ownedLanes !== null && !ownedLanes.has(id);
  const order = { apron: 0, taxiway: 1, runway: 2 };
  const lanes = [...world.lanes].sort((a, b) => order[a.kind] - order[b.kind]);

  const thr04 = geom.final ? geom.proj.toLonLat(geom.final.thr) : null;
  const airborne = objects
    .filter((o) => o.status === 'airborne')
    .map((o) => ({ o, d: thr04 ? distM(o.position.coordinates, thr04) / NM : 0 }))
    .sort((a, b) => a.d - b.d);

  return (
    <div className="map-col">
    <div className="map" ref={ref}>
      <svg width={size.w} height={size.h} role="img" aria-label="Airfield map">
        <defs>
          <pattern id="hatch-conflict" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="4" height="8" fill="rgba(10,12,16,0.55)" />
          </pattern>
          <pattern id="hatch-closed" width="9" height="9" patternUnits="userSpaceOnUse">
            <path d="M1,1 L8,8 M8,1 L1,8" stroke="#9aa3ad" strokeWidth="1.2" />
          </pattern>
        </defs>

        {/* range marks on the final */}
        {geom.final && far && (
          <g className="final">
            {(() => {
              const f = geom.final;
              const a = mpx(f.thr);
              const b = mpx([f.thr[0] + f.ux * 8 * NM, f.thr[1] + f.uy * 8 * NM]);
              const ang = Math.atan2(f.uy, f.ux);
              return (
                <>
                  <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} className="final-line" />
                  {[2, 4, 6].map((nm) => {
                    const r = nm * NM * scale;
                    const s = [a[0] + r * Math.cos(ang - 0.42), a[1] + r * Math.sin(ang - 0.42)];
                    const e = [a[0] + r * Math.cos(ang + 0.42), a[1] + r * Math.sin(ang + 0.42)];
                    return (
                      <g key={nm}>
                        <path d={`M${s[0]},${s[1]} A${r},${r} 0 0 1 ${e[0]},${e[1]}`} className="final-arc" />
                        <text x={e[0] - 6} y={e[1] + 14} className="final-label">
                          {nm} nm
                        </text>
                      </g>
                    );
                  })}
                  <text x={b[0] + 8} y={b[1] - 4} className="final-label">
                    FINAL RWY {f.name}
                  </text>
                </>
              );
            })()}
          </g>
        )}

        {lanes.map((l) => (
          <LaneShape key={l._id} lane={l} state={statusOf(l._id)?.state ?? 'clear'} px={px} scale={scale} far={far} dim={dim(l._id)} owned={ownedLanes?.has(l._id) ?? false} />
        ))}

        {!far &&
          world.hold_points.map((h) => {
            const lane = world.lanes.find((l) => l._id === h.lane_id);
            const c = lane?.centerline?.coordinates;
            const ang = c ? bearing(c[0], c[1]) : 0;
            const p = px(h.position.coordinates);
            return (
              <g key={h._id} transform={`translate(${p[0]},${p[1]}) rotate(${ang})`} opacity={dim(h.lane_id) ? 0.3 : 1}>
                <title>{`Hold point ${h._id} (protects ${h.protects})`}</title>
                <rect x={-9} y={-2} width={18} height={4} className="holdpoint" />
              </g>
            );
          })}

        {lanes.map((l) => (
          <LaneLabel key={l._id} lane={l} status={statusOf(l._id)} px={px} far={far} dim={dim(l._id)} scale={scale} />
        ))}

        {objects.map((o) => {
          const p = px(o.position.coordinates);
          const name = o.callsign ?? o.label ?? o._id;
          const air = o.status === 'airborne';
          const showLabel = !far || air;
          const hot = flagged.has(o.callsign ?? o._id);
          return (
            <g key={o._id} className={`obj obj-${o.kind} ${air ? 'air' : 'gnd'} ${smooth ? 'smooth' : ''}`} style={{ transform: `translate(${p[0]}px, ${p[1]}px)` }}>
              <title>{`${name} · ${o.kind} · ${o.status}${o.lane_id ? ' · on ' + o.lane_id : ''}`}</title>
              {hot && <circle r={17} className="obj-ring" />}
              <ObjGlyph o={o} small={far && !air} />
              {showLabel && (
                <text x={13} y={-7} className="obj-label">
                  <tspan className={o.callsign ? 'obj-name' : 'obj-name soft'}>{name}</tspan>
                  {air && (
                    <tspan x={13} dy={13} className="obj-data">
                      {o.alt_ft != null ? `${Math.round(o.alt_ft / 10) * 10} ft` : '--- ft'} · {o.speed_kt != null ? `${Math.round(o.speed_kt)} kt` : '--- kt'}
                    </tspan>
                  )}
                  {!air && o.callsign && o.hold_short && (
                    <tspan x={13} dy={12} className="obj-data">
                      holding short
                    </tspan>
                  )}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div className="map-tools">
        <div className="seg" role="group" aria-label="Map zoom">
          <button className={zoom === 'airfield' ? 'on' : ''} onClick={() => setZoom('airfield')}>
            Airfield
          </button>
          <button className={zoom === 'approach' ? 'on' : ''} onClick={() => setZoom('approach')}>
            Approach · 7 nm
          </button>
        </div>
      </div>

      <div className="map-air">
        <div className="cap">Airborne · distance to RWY 04</div>
        {airborne.length === 0 && <div className="muted">No airborne traffic</div>}
        {airborne.map(({ o, d }) => (
          <button key={o._id} className="air-row" onClick={() => setZoom('approach')} title="Show on the approach view">
            <span className="mono strong">{o.callsign ?? o._id}</span>
            <span className="mono">{d.toFixed(1)} nm</span>
            <span className="mono">{o.alt_ft != null ? Math.round(o.alt_ft / 10) * 10 : '---'} ft</span>
            <span className="mono">{o.speed_kt != null ? Math.round(o.speed_kt) : '---'} kt</span>
          </button>
        ))}
      </div>

    </div>
      <div className="map-legend">
        <div className="legend-row">
          {STATES.map((s) => (
            <span key={s} className="legend-item" title={STATE_META[s].hint}>
              <svg width="26" height="12">
                <rect x="1" y="1" width="24" height="10" rx="2" className={`lane-fill st-${s}`} />
                {s === 'conflict' && <rect x="1" y="1" width="24" height="10" rx="2" fill="url(#hatch-conflict)" />}
                {s === 'closed' && <rect x="1" y="1" width="24" height="10" rx="2" fill="url(#hatch-closed)" />}
                {s === 'reserved' && <line x1="3" y1="6" x2="23" y2="6" className="lane-dash" />}
              </svg>
              {STATE_META[s].glyph} {STATE_META[s].label}
            </span>
          ))}
        </div>
        <div className="legend-row">
          <span className="legend-item">
            <svg width="16" height="16" viewBox="-10 -10 20 20"><path d="M0,-9 L6,7 L0,3 L-6,7 Z" className="glyph" /></svg>
            aircraft
          </span>
          <span className="legend-item">
            <svg width="16" height="16" viewBox="-10 -10 20 20"><rect x="-6" y="-6" width="12" height="12" className="glyph" /></svg>
            vehicle
          </span>
          <span className="legend-item">
            <svg width="16" height="16" viewBox="-10 -10 20 20"><circle r="5" className="glyph" /></svg>
            person
          </span>
          <span className="legend-item">
            <svg width="16" height="16" viewBox="-10 -10 20 20"><path d="M0,-8 L8,0 L0,8 L-8,0 Z" className="glyph hollow" /></svg>
            unknown
          </span>
          <span className="legend-item">
            <svg width="18" height="12"><rect x="0" y="4" width="18" height="4" className="holdpoint" /></svg>
            hold point
          </span>
        </div>
      </div>
    </div>
  );
}

function ObjGlyph({ o, small }: { o: ObjectDoc; small: boolean }) {
  const k = small ? 0.6 : 1;
  const hdg = o.heading_deg;
  if (o.kind === 'aircraft') {
    return <path d="M0,-11 L7.5,8 L0,3.5 L-7.5,8 Z" className="glyph" transform={`rotate(${hdg ?? 0}) scale(${k})`} />;
  }
  const tick = hdg != null && (o.speed_kt ?? 0) > 0 && (
    <line x1={0} y1={0} x2={0} y2={-16} className="hdg-tick" transform={`rotate(${hdg})`} />
  );
  if (o.kind === 'vehicle')
    return (
      <g transform={`scale(${k})`}>
        {tick}
        <rect x={-6} y={-6} width={12} height={12} className="glyph" />
      </g>
    );
  if (o.kind === 'person')
    return (
      <g transform={`scale(${k})`}>
        {tick}
        <circle r={5} className="glyph" />
      </g>
    );
  return (
    <g transform={`scale(${k})`}>
      {tick}
      <path d="M0,-9 L9,0 L0,9 L-9,0 Z" className="glyph hollow" />
      <text y={4} textAnchor="middle" className="glyph-q">
        ?
      </text>
    </g>
  );
}

interface LaneShapeProps {
  lane: Lane;
  state: LaneState;
  px: (c: number[]) => XY;
  scale: number;
  far: boolean;
  dim: boolean;
  owned: boolean;
}

function LaneShape({ lane, state, px, scale, far, dim, owned }: LaneShapeProps) {
  const cls = `lane st-${state} ${dim ? 'dim' : ''}`;
  if (lane.centerline) {
    const [a, b] = [px(lane.centerline.coordinates[0]), px(lane.centerline.coordinates[lane.centerline.coordinates.length - 1])];
    const min = lane.kind === 'runway' ? (far ? 5 : 12) : far ? 2.5 : 7;
    const w = Math.max((lane.width_m ?? 20) * scale, min);
    const line = { x1: a[0], y1: a[1], x2: b[0], y2: b[1] };
    return (
      <g className={cls}>
        <title>{`${lane.name}: ${state}`}</title>
        {owned && <line {...line} strokeWidth={w + 8} className="lane-owned" />}
        <line {...line} strokeWidth={w + 2.5} className="lane-edge" />
        <line {...line} strokeWidth={w} className="lane-body" />
        {state === 'conflict' && <line {...line} strokeWidth={w} stroke="url(#hatch-conflict)" />}
        {state === 'closed' && <line {...line} strokeWidth={w} stroke="url(#hatch-closed)" />}
        {state === 'reserved' && <line {...line} className="lane-dash" />}
        {lane.kind === 'runway' && state === 'clear' && !far && <line {...line} className="rwy-center" />}
      </g>
    );
  }
  const ring = lane.area?.coordinates[0];
  if (!ring) return null;
  const d = ring.map((c, i) => `${i ? 'L' : 'M'}${px(c).join(',')}`).join(' ') + ' Z';
  return (
    <g className={cls}>
      <title>{`${lane.name}: ${state}`}</title>
      {owned && <path d={d} className="lane-owned" strokeWidth={6} fill="none" />}
      <path d={d} className="lane-poly" />
      {state === 'conflict' && <path d={d} fill="url(#hatch-conflict)" />}
      {state === 'closed' && <path d={d} fill="url(#hatch-closed)" />}
    </g>
  );
}

function LaneLabel({ lane, status, px, far, dim, scale }: { lane: Lane; status?: LaneStatus; px: (c: number[]) => XY; far: boolean; dim: boolean; scale: number }) {
  if (far && lane.kind !== 'runway') return null;
  const state = status?.state ?? 'clear';
  const meta = STATE_META[state];
  const short = lane.kind === 'runway' ? `RWY ${lane.designators.join('/')}` : lane.kind === 'taxiway' ? `TWY ${lane.designators[0]}` : lane.name.toUpperCase();
  const text = state === 'clear' ? short : `${short}  ${meta.glyph} ${meta.label}`;
  let p: XY;
  let ends: { p: XY; t: string }[] = [];
  if (lane.centerline) {
    const c = lane.centerline.coordinates;
    const a = px(c[0]);
    const b = px(c[c.length - 1]);
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const w = Math.max((lane.width_m ?? 20) * scale, 12);
    const ux0 = (b[0] - a[0]) / len;
    const uy0 = (b[1] - a[1]) / len;
    if (lane.kind === 'runway') {
      // beside the runway, on its upper-left side, so the chip never sits on it
      let nx = uy0;
      let ny = -ux0;
      if (ny > 0 || (ny === 0 && nx > 0)) [nx, ny] = [-nx, -ny];
      const off = w / 2 + 16;
      p = [a[0] + (b[0] - a[0]) * 0.86 + nx * off, a[1] + (b[1] - a[1]) * 0.86 + ny * off];
    } else {
      // taxiways: just past the upper end, clear of traffic on the lane
      const up = a[1] < b[1] ? a : b;
      const s = a[1] < b[1] ? -1 : 1;
      p = [up[0] + s * ux0 * 30, up[1] + s * uy0 * 18];
    }
    if (lane.kind === 'runway' && !far) {
      const ux = (b[0] - a[0]) / len;
      const uy = (b[1] - a[1]) / len;
      ends = [
        { p: [a[0] - ux * 20, a[1] - uy * 20], t: lane.designators[0] },
        { p: [b[0] + ux * 20, b[1] + uy * 20], t: lane.designators[1] ?? '' },
      ];
    }
  } else {
    const ring = lane.area?.coordinates[0] ?? [];
    if (!ring.length) return null;
    const q = ring.slice(0, -1).map(px);
    const c: XY = [q.reduce((s, v) => s + v[0], 0) / q.length, q.reduce((s, v) => s + v[1], 0) / q.length];
    const low = q.reduce((m, v) => (v[1] > m[1] ? v : m), q[0]);
    p = [c[0] + (low[0] - c[0]) * 0.55, c[1] + (low[1] - c[1]) * 0.55];
  }
  const w = text.length * 6.4 + 14;
  return (
    <g className={`lane-label st-${state} ${dim ? 'dim' : ''}`}>
      {ends.map((e) => (
        <text key={e.t} x={e.p[0]} y={e.p[1] + 5} textAnchor="middle" className="rwy-end">
          {e.t}
        </text>
      ))}
      <g transform={`translate(${p[0] - w / 2},${p[1] - 9})`}>
        <title>{status?.reason ?? `${lane.name}: ${state}`}</title>
        <rect width={w} height={18} rx={3} className="chip-bg" />
        <text x={7} y={13} className="chip-text">
          {text}
        </text>
      </g>
      {status?.reason && !far && (state === 'conflict' || state === 'closed') && (
        <text x={p[0] - w / 2 + 2} y={p[1] + 22} className="chip-reason">
          {status.reason}
        </text>
      )}
    </g>
  );
}
