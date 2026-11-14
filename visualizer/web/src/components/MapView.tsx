import { useEffect, useMemo, useRef, useState } from 'react';
import { bearing, makeProjection, NM, type XY } from '../geo';
import type { Lane, LaneState, LaneStatus, ObjectDoc, World } from '../types';

export const STATE_META: Record<LaneState, { label: string; hint: string }> = {
  clear: { label: 'CLEAR', hint: 'no traffic, no clearance' },
  reserved: { label: 'RESERVED', hint: 'an active clearance' },
  occupied: { label: 'OCCUPIED', hint: 'something is on it' },
  conflict: { label: 'CONFLICT', hint: 'a clearance and other traffic' },
  closed: { label: 'CLOSED', hint: 'not in use' },
};

interface Props {
  world: World;
  objects: ObjectDoc[];
  laneStatus: LaneStatus[];
  ownedLanes: Set<string> | null; // null = no controller filter
  flagged: Set<string>; // callsigns targeted by a pending request or a stop
  smooth: boolean;
}

type Zoom = 'airfield' | 'approach';

// Flushing Bay and Bowery Bay, drawn by hand around the field (lon, lat).
const WATER: number[][][] = [
  [[-73.866, 40.805], [-73.866, 40.789], [-73.856, 40.779], [-73.851, 40.772], [-73.851, 40.76], [-73.82, 40.76], [-73.82, 40.805]],
  [[-73.91, 40.805], [-73.91, 40.781], [-73.886, 40.781], [-73.874, 40.789], [-73.866, 40.789], [-73.866, 40.805]],
];
const PLANE = 'M0,-15 C1.7,-15 2.4,-12 2.4,-9 L2.4,-3.5 L14,4 L14,6.8 L2.4,3.6 L2.1,9.5 L6,12.6 L6,14.4 L0,12.8 L-6,14.4 L-6,12.6 L-2.1,9.5 L-2.4,3.6 L-14,6.8 L-14,4 L-2.4,-3.5 L-2.4,-9 C-2.4,-12 -1.7,-15 0,-15 Z';

export function MapView({ world, objects, laneStatus, ownedLanes, flagged, smooth }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [zoom, setZoom] = useState<Zoom>('airfield');
  const [help, setHelp] = useState(false);

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
    const box = (p: XY[], pad: number) => {
      const xs = p.map((q) => q[0]);
      const ys = p.map((q) => q[1]);
      return { x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad, y0: Math.min(...ys) - pad, y1: Math.max(...ys) + pad };
    };
    const rwy = world.lanes.find((l) => l.designators.includes('04') && l.centerline) ?? world.lanes.find((l) => l.kind === 'runway' && l.centerline);
    let final: { thr: XY; ux: number; uy: number; name: string } | null = null;
    const approachPts = [...pts];
    const fieldPts = [...pts];
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
      fieldPts.push([thr[0] + ux * 900, thr[1] + uy * 900]); // room for short final
    }
    // terminal blocks along the landside edge of the apron
    const terminals: { c: XY; ang: number; len: number }[] = [];
    let termLabel: XY | null = null;
    const apron = world.lanes.find((l) => l.kind === 'apron' && l.area)?.area?.coordinates[0]?.map((c) => proj.toM(c));
    const rc = rwy?.centerline ? proj.toM(rwy.centerline.coordinates[0]) : null;
    if (apron && apron.length >= 4 && rc) {
      const cen: XY = [(apron[0][0] + apron[2][0]) / 2, (apron[0][1] + apron[2][1]) / 2];
      let best = 0;
      let bd = -1;
      for (let i = 0; i < 4; i++) {
        const a = apron[i];
        const b = apron[i + 1] ?? apron[0];
        const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
        const m: XY = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        const d = Math.hypot(m[0] - rc[0], m[1] - rc[1]) + L; // long edge, away from the runway
        if (L > 500 && d > bd) {
          bd = d;
          best = i;
        }
      }
      const a = apron[best];
      const b = apron[best + 1] ?? apron[0];
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      const m: XY = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      let n: XY = [-(b[1] - a[1]) / L, (b[0] - a[0]) / L];
      if ((m[0] - cen[0]) * n[0] + (m[1] - cen[1]) * n[1] < 0) n = [-n[0], -n[1]];
      termLabel = [a[0] + (b[0] - a[0]) * 0.5 + n[0] * 230, a[1] + (b[1] - a[1]) * 0.5 + n[1] * 230];
      for (const f of [0.2, 0.5, 0.8]) {
        terminals.push({ c: [a[0] + (b[0] - a[0]) * f + n[0] * 95, a[1] + (b[1] - a[1]) * f + n[1] * 95], ang: (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI, len: L * 0.24 });
      }
    }
    return { proj, airfield: box(fieldPts, 220), approach: box(approachPts, 2600), final, terminals, termLabel };
  }, [world]);

  const view = zoom === 'airfield' ? geom.airfield : geom.approach;
  const scale = Math.min(size.w / (view.x1 - view.x0), Math.max(120, size.h - 72) / (view.y1 - view.y0));
  const cx = (view.x0 + view.x1) / 2;
  const cy = (view.y0 + view.y1) / 2;
  const mpx = (m: XY): XY => [(m[0] - cx) * scale + size.w / 2, (m[1] - cy) * scale + size.h / 2 + 30];
  const px = (lonLat: number[]): XY => mpx(geom.proj.toM(lonLat));
  const far = zoom === 'approach';

  const statusOf = (id: string) => laneStatus.find((s) => s._id === id);
  const dim = (id: string) => ownedLanes !== null && !ownedLanes.has(id);
  const order = { apron: 0, taxiway: 1, runway: 2 };
  const lanes = [...world.lanes].sort((a, b) => order[a.kind] - order[b.kind]);
  const laneOf = (id: string | null) => world.lanes.find((l) => l._id === id);
  const thrPx = geom.final ? mpx(geom.final.thr) : null;

  return (
    <div className="map" ref={ref}>
      <svg width={size.w} height={size.h} role="img" aria-label="Airfield map">
        {WATER.map((ring, i) => (
          <path key={i} className="water" d={ring.map((c, j) => `${j ? 'L' : 'M'}${px(c).join(',')}`).join(' ') + ' Z'} />
        ))}
        {!far &&
          (() => {
            const p = px([-73.8585, 40.7855]);
            return (
              <text x={p[0]} y={p[1]} className="water-label">
                Flushing Bay
              </text>
            );
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

        {!far &&
          geom.terminals.map((t, i) => {
            const c = mpx(t.c);
            const w = t.len * scale;
            const h = 120 * scale;
            return <rect key={i} className="terminal" x={-w / 2} y={-h / 2} width={w} height={h} transform={`translate(${c[0]},${c[1]}) rotate(${t.ang})`} />;
          })}
        {!far &&
          geom.termLabel &&
          (() => {
            const c = mpx(geom.termLabel);
            return (
              <text x={c[0]} y={c[1] + 5} className="quiet-label small">
                Terminals
              </text>
            );
          })()}

        {lanes.map((l) => (
          <LaneShape key={l._id} lane={l} state={statusOf(l._id)?.state ?? 'clear'} px={px} scale={scale} far={far} dim={dim(l._id)} owned={ownedLanes?.has(l._id) ?? false} />
        ))}

        {!far &&
          world.hold_points.map((h) => {
            const lane = laneOf(h.lane_id);
            const c = lane?.centerline?.coordinates;
            const ang = c ? bearing(c[0], c[1]) : 0;
            const p = px(h.position.coordinates);
            const w = Math.max((lane?.width_m ?? 23) * scale, 9) / 2 + 1;
            return (
              <g key={h._id} transform={`translate(${p[0]},${p[1]}) rotate(${ang})`} opacity={dim(h.lane_id) ? 0.3 : 1} className="holdpoint">
                <title>{`Hold point ${h._id} (protects ${h.protects})`}</title>
                <line x1={-w} x2={w} y1={-1.6} y2={-1.6} />
                <line x1={-w} x2={w} y1={1.6} y2={1.6} strokeDasharray="3 2" />
              </g>
            );
          })}

        {lanes.map((l) => (
          <LaneLabel key={l._id} lane={l} status={statusOf(l._id)} px={px} far={far} dim={dim(l._id)} scale={scale} />
        ))}

        {objects.map((o, idx) => {
          const p = px(o.position.coordinates);
          const name = o.callsign ?? o.label ?? o._id;
          const air = o.status === 'airborne';
          const hot = flagged.has(o.callsign ?? o._id);
          const showLabel = (!far || air) && (o.kind !== 'person' || !!o.callsign) && (o.kind === 'aircraft' || !!o.callsign || !!o.label || hot);
          const sub =
            o.kind === 'aircraft'
              ? air
                ? `${(o.alt_ft ?? 9999) < 1500 ? 'short final' : 'on approach'} · ${o.alt_ft != null ? Math.round(o.alt_ft / 10) * 10 : '---'} ft · ${o.speed_kt != null ? Math.round(o.speed_kt) : '---'} kt`
                : o.hold_short
                  ? 'holding short'
                  : (o.speed_kt ?? 0) > 1
                    ? `taxiing${laneOf(o.lane_id)?.designators[0] ? ' · ' + laneOf(o.lane_id)!.designators[0] : ''}`
                    : laneOf(o.lane_id)?.kind === 'runway'
                      ? 'on the runway'
                      : 'on the ground'
              : o.hold_short
                ? 'holding short'
                : laneOf(o.lane_id)?.kind === 'runway'
                  ? `on runway ${laneOf(o.lane_id)!.designators.join('/')}`
                  : '';
          const below = idx % 2 === 1;
          const lx = 16;
          const ly = below ? 20 : sub ? -18 : -8;
          return (
            <g key={o._id} className={`obj obj-${o.kind} ${air ? 'air' : 'gnd'} ${smooth ? 'smooth' : ''} ${hot ? 'hot' : ''}`} style={{ transform: `translate(${p[0]}px, ${p[1]}px)` }}>
              <title>{`${name} · ${o.kind} · ${o.status}${o.lane_id ? ' · on ' + o.lane_id : ''}`}</title>
              {hot && <circle r={11} className="obj-ring" />}
              <ObjGlyph o={o} small={far} />
              {showLabel && (
                <>
                  <line x1={6} y1={below ? 6 : -6} x2={lx - 3} y2={below ? ly - 6 : ly + 4} className="leader" />
                  <text x={lx} y={ly} className="obj-label">
                    <tspan className="obj-name">{name}</tspan>
                    {sub && (
                      <tspan x={lx} dy={14} className="obj-data">
                        {sub}
                      </tspan>
                    )}
                  </text>
                </>
              )}
            </g>
          );
        })}
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
        <button className="help-btn" aria-label="Map key" onClick={() => setHelp(!help)}>
          ?
        </button>
        {help && (
          <div className="popover help" onClick={() => setHelp(false)}>
            <div className="pop-title">Map key</div>
            <p><i className="key k-reserved" /> Reserved: an active clearance</p>
            <p><i className="key k-occupied" /> Occupied: something is on it</p>
            <p><i className="key k-conflict" /> Conflict: a clearance and other traffic</p>
            <p><i className="key k-hold" /> Hold-short line</p>
            <p><i className="key k-veh" /> Vehicle · <i className="key k-person" /> person</p>
            <p className="pop-note">A red ring marks the target of a stop or a waiting proposal.</p>
          </div>
        )}
      </div>
    </div>
  );
}

function ObjGlyph({ o, small }: { o: ObjectDoc; small: boolean }) {
  const hdg = o.heading_deg ?? 0;
  if (o.kind === 'aircraft') return <path d={PLANE} className="glyph plane" transform={`rotate(${hdg}) scale(${small ? 0.45 : 0.55})`} />;
  if (o.kind === 'vehicle') return <rect x={-2.5} y={-4} width={5} height={8} className="glyph vehicle" transform={`rotate(${hdg})`} />;
  if (o.kind === 'person') return <circle r={2} className="glyph person" />;
  return <path d="M0,-5 L5,0 L0,5 L-5,0 Z" className="glyph unknown" />;
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
  const cls = `lane lane-${lane.kind} st-${state} ${dim ? 'dim' : ''} ${owned ? 'owned' : ''}`;
  if (lane.centerline) {
    const c = lane.centerline.coordinates;
    const a = px(c[0]);
    const b = px(c[c.length - 1]);
    const rw = lane.kind === 'runway';
    const w = Math.max((lane.width_m ?? 20) * scale, rw ? (far ? 3 : 11) : far ? 2 : 6);
    const line = { x1: a[0], y1: a[1], x2: b[0], y2: b[1] };
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const ang = (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI;
    const bars = [-0.36, -0.22, -0.08, 0.08, 0.22, 0.36];
    return (
      <g className={cls}>
        <title>{`${lane.name}: ${state}`}</title>
        {state !== 'clear' && <line {...line} strokeWidth={w + 3} className="lane-outline" />}
        <line {...line} strokeWidth={w} className="lane-body" />
        {rw && !far && <line {...line} className="rwy-center" strokeDasharray={`${len / 42} ${len / 42}`} strokeDashoffset={-len / 28} />}
        {!rw && !far && <line {...line} className="twy-center" />}
        {rw &&
          !far &&
          [a, b].map((e, i) => (
            <g key={i} transform={`translate(${e[0]},${e[1]}) rotate(${ang + (i ? 180 : 0)})`} className="thr">
              {bars.map((f) => (
                <line key={f} x1={7} x2={7 + w * 1.1} y1={f * w} y2={f * w} />
              ))}
            </g>
          ))}
      </g>
    );
  }
  const ring = lane.area?.coordinates[0];
  if (!ring) return null;
  const d = ring.map((c, i) => `${i ? 'L' : 'M'}${px(c).join(',')}`).join(' ') + ' Z';
  return (
    <g className={cls}>
      <title>{`${lane.name}: ${state}`}</title>
      <path d={d} className="lane-poly" />
    </g>
  );
}

function LaneLabel({ lane, status, px, far, dim, scale }: { lane: Lane; status?: LaneStatus; px: (c: number[]) => XY; far: boolean; dim: boolean; scale: number }) {
  if (far && lane.kind !== 'runway') return null;
  const state = status?.state ?? 'clear';
  const labels: { p: XY; t: string; anchor?: 'start' | 'middle' | 'end' }[] = [];
  let chip: XY;
  if (lane.centerline) {
    const c = lane.centerline.coordinates;
    const a = px(c[0]);
    const b = px(c[c.length - 1]);
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const ux = (b[0] - a[0]) / len;
    const uy = (b[1] - a[1]) / len;
    const w = Math.max((lane.width_m ?? 20) * scale, 12);
    // the side normal that points down-right, so labels sit beside the pavement
    let nx = -uy;
    let ny = ux;
    if (nx < 0) [nx, ny] = [-nx, -ny];
    if (lane.kind === 'runway') {
      const off = w / 2 + 14;
      if (!far) {
        labels.push({ p: [a[0] + ux * 30 + nx * off, a[1] + uy * 30 + ny * off + 6], t: `RWY ${lane.designators[0]}` });
        labels.push({ p: [b[0] - ux * 10 + nx * off, b[1] - uy * 10 + ny * off + 6], t: `RWY ${lane.designators[1] ?? ''}` });
      }
      chip = [a[0] + (b[0] - a[0]) * 0.3 - nx * (off + 36), a[1] + (b[1] - a[1]) * 0.3 - ny * (off + 6)];
    } else {
      const hi = a[1] < b[1] ? a : b;
      const long = len > 400;
      const lp: XY = long ? [(a[1] > b[1] ? a : b)[0] + 16, (a[1] > b[1] ? a : b)[1] + 24] : [hi[0] - 52, hi[1] - 8];
      labels.push({ p: lp, t: `TWY ${lane.designators[0]}` });
      chip = [lp[0] + 20, lp[1] - 22];
    }
  } else {
    const ring = lane.area?.coordinates[0] ?? [];
    if (!ring.length) return null;
    const q = ring.slice(0, -1).map(px);
    const cen: XY = [q.reduce((s, v) => s + v[0], 0) / q.length, q.reduce((s, v) => s + v[1], 0) / q.length];
    const lowq = q.reduce((m, v) => (v[1] > m[1] ? v : m), q[0]);
    labels.push({ p: [cen[0] + (lowq[0] - cen[0]) * 0.6, cen[1] + (lowq[1] - cen[1]) * 0.6], t: 'Apron', anchor: 'middle' });
    chip = [cen[0], cen[1] + 30];
  }
  const text = STATE_META[state].label;
  const cw = text.length * 6.6 + 14;
  return (
    <g className={`lane-label st-${state} ${dim ? 'dim' : ''}`}>
      {labels.map((l) => (
        <text key={l.t} x={l.p[0]} y={l.p[1]} textAnchor={l.anchor ?? 'start'} className={`quiet-label ${lane.kind === 'runway' ? '' : 'small'}`}>
          {l.t}
        </text>
      ))}
      {state !== 'clear' && (lane.kind === 'runway' || state === 'conflict' || state === 'closed') && (
        <g transform={`translate(${chip[0] - cw / 2},${chip[1] - 10})`} className="chip">
          <title>{status?.reason ?? `${lane.name}: ${state}`}</title>
          <rect width={cw} height={20} rx={4} className="chip-bg" />
          <text x={cw / 2} y={14} textAnchor="middle" className="chip-text">
            {text}
          </text>
        </g>
      )}
    </g>
  );
}
