import type { Scene } from "./sim";
import {
  D_CROSS, D_FROM, D_TO, HOLD_LINE, RWY04_DIR, RWY04_END, RWY04_THR, RWY13_END,
  RWY13_THR, RWY_HALF_W, SE, pointOn04,
} from "./sim";

const deg = (v: { x: number; y: number }) => (Math.atan2(v.y, v.x) * 180) / Math.PI;
const ANG04 = deg(RWY04_DIR);

function Runway({ a, b, w, color }: { a: { x: number; y: number }; b: { x: number; y: number }; w: number; color: string }) {
  return (
    <g>
      <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={color} strokeWidth={w} strokeLinecap="butt" />
      <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#8a94a6" strokeOpacity={0.55}
        strokeWidth={1.4} strokeDasharray="10 9" />
    </g>
  );
}

function Plane({ x, y, angle }: { x: number; y: number; angle: number }) {
  // simple top-down silhouette pointing along +x before rotation
  return (
    <g transform={`translate(${x} ${y}) rotate(${angle})`}>
      <path
        d="M 18 0 L 10 -2 L 2 -2 L -4 -15 L -8 -15 L -5 -2 L -12 -2 L -15 -6 L -18 -6 L -16 0 L -18 6 L -15 6 L -12 2 L -5 2 L -8 15 L -4 15 L 2 2 L 10 2 Z"
        fill="#f4f6fa" stroke="#0b1018" strokeWidth={0.8}
      />
    </g>
  );
}

function Truck({ x, y, angle, lead }: { x: number; y: number; angle: number; lead: boolean }) {
  return (
    <g transform={`translate(${x} ${y}) rotate(${angle})`}>
      <rect x={-9} y={-5} width={18} height={10} rx={2} fill="#e5343f" stroke={lead ? "#ffd4d7" : "#3b0a0e"} strokeWidth={lead ? 1.2 : 0.8} />
      <rect x={5} y={-4} width={3.5} height={8} fill="#ffd4d7" opacity={0.9} />
    </g>
  );
}

export function AirfieldMap({ scene, stopBanner, lightsBlocked }: { scene: Scene; stopBanner: string | null; lightsBlocked: boolean }) {
  const lights = lightsBlocked ? "red" : scene.lights;
  const lightColor = lights === "red" ? "#ff3b47" : lights === "green" ? "#36d77b" : "#5c6676";
  const glow = lights !== "off";
  const runway04Color = lights === "red" ? "#3a2328" : "#2b313b";
  // stop-bar lights across taxiway D at the hold-short line
  const along = RWY04_DIR;
  const bar = [-2, -1, 0, 1, 2].map((k) => ({ x: HOLD_LINE.x + along.x * k * 5, y: HOLD_LINE.y + along.y * k * 5 }));
  // runway-entrance lights on the 04 centerline either side of D
  const entrance = [-3, -2, -1, 1, 2, 3].map((k) => pointOn04(200 + k * 9));
  const truckAngle = deg({ x: -SE.x, y: -SE.y });
  const lead = scene.trucks[0];

  return (
    <svg className="map" viewBox="0 0 1000 720" preserveAspectRatio="xMidYMid meet">
      <defs>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      {/* water (Flushing Bay) */}
      <path d="M 720 0 C 700 140 800 260 760 420 C 730 560 860 640 1000 700 L 1000 0 Z" fill="#0e1a26" />
      <text x={850} y={70} className="map-water">Flushing Bay</text>

      {/* terminals: plain blocks, no logos */}
      <text x={120} y={150} className="map-faint">Terminals</text>
      <rect x={110} y={165} width={150} height={44} rx={4} className="terminal" />
      <rect x={128} y={225} width={132} height={44} rx={4} className="terminal" />
      <rect x={146} y={285} width={114} height={44} rx={4} className="terminal" />

      {/* approach path for runway 04 */}
      <line x1={RWY04_THR.x} y1={RWY04_THR.y} x2={RWY04_THR.x - RWY04_DIR.x * 420} y2={RWY04_THR.y - RWY04_DIR.y * 420}
        stroke="#5c6676" strokeDasharray="6 8" strokeWidth={1.4} />

      {/* taxiway D */}
      <line x1={D_FROM.x} y1={D_FROM.y} x2={D_TO.x} y2={D_TO.y} stroke="#252b34" strokeWidth={9} />
      <line x1={D_FROM.x} y1={D_FROM.y} x2={D_TO.x} y2={D_TO.y} stroke="#c9a227" strokeOpacity={0.6} strokeWidth={1} />
      <text x={D_TO.x + 6} y={D_TO.y + 18} className="map-label">TWY D</text>

      {/* runways */}
      <Runway a={RWY13_THR} b={RWY13_END} w={RWY_HALF_W * 2} color="#2b313b" />
      <Runway a={RWY04_THR} b={RWY04_END} w={RWY_HALF_W * 2} color={runway04Color} />
      <text x={RWY04_THR.x - 70} y={RWY04_THR.y + 10} className="map-label">RWY 04</text>
      <text x={RWY04_END.x + 12} y={RWY04_END.y + 4} className="map-label">RWY 22</text>
      <text x={RWY13_THR.x - 30} y={RWY13_THR.y - 18} className="map-label">RWY 13</text>
      <text x={RWY13_END.x + 8} y={RWY13_END.y + 22} className="map-label">RWY 31</text>

      {/* hold-short line: two solid + two dashed bars across D */}
      <g transform={`translate(${HOLD_LINE.x} ${HOLD_LINE.y}) rotate(${ANG04})`}>
        <line x1={-8} y1={-1.5} x2={8} y2={-1.5} stroke="#e8c547" strokeWidth={1.2} />
        <line x1={-8} y1={0.5} x2={8} y2={0.5} stroke="#e8c547" strokeWidth={1.2} />
        <line x1={-8} y1={2.5} x2={8} y2={2.5} stroke="#e8c547" strokeWidth={1.2} strokeDasharray="2 2" />
        <line x1={-8} y1={4.5} x2={8} y2={4.5} stroke="#e8c547" strokeWidth={1.2} strokeDasharray="2 2" />
      </g>

      {/* lights */}
      <g filter={glow ? "url(#glow)" : undefined} className={lights === "red" ? "blink" : undefined}>
        {bar.map((p, i) => <circle key={`b${i}`} cx={p.x} cy={p.y} r={2.4} fill={lightColor} />)}
        {entrance.map((p, i) => <circle key={`e${i}`} cx={p.x} cy={p.y} r={1.8} fill={lightColor} />)}
      </g>
      {lights === "red" && (
        <rect x={D_CROSS.x - 22} y={D_CROSS.y - 22} width={44} height={44} transform={`rotate(${ANG04} ${D_CROSS.x} ${D_CROSS.y})`}
          fill="none" stroke="#ff3b47" strokeWidth={2} className="pulse" />
      )}

      {/* vehicles */}
      {scene.trucks.slice().reverse().map((t) => (
        <Truck key={t.id} x={t.p.x} y={t.p.y} angle={truckAngle} lead={t.id === "TRUCK1"} />
      ))}
      <text x={lead.p.x + 40} y={lead.p.y + 10} className="map-callsign">TRUCK1 + company</text>
      <text x={lead.p.x + 40} y={lead.p.y + 28} className={`map-status tone-${scene.truckTone}`}>{scene.truckLabel}</text>

      {scene.jet && (
        <g>
          <Plane x={scene.jet.p.x} y={scene.jet.p.y} angle={ANG04} />
          <text x={scene.jet.p.x + (scene.jet.s > 260 ? 26 : -150)} y={scene.jet.p.y + (scene.jet.s > 260 ? 34 : -8)} className="map-callsign">AC8646</text>
          <text x={scene.jet.p.x + (scene.jet.s > 260 ? 26 : -150)} y={scene.jet.p.y + (scene.jet.s > 260 ? 52 : 10)} className="map-status">{scene.jet.label}</text>
        </g>
      )}

      {stopBanner && (
        <g>
          <rect x={540} y={600} width={445} height={92} rx={10} fill="#12151c" stroke="#2c323d" />
          <text x={580} y={636} className="map-caption">{stopBanner.split("|")[0]}</text>
          <text x={580} y={666} className="map-caption">{stopBanner.split("|")[1] ?? ""}</text>
        </g>
      )}
    </svg>
  );
}
