// Scene 3: what it is. SIMULATOR -> TOWER (listener -> Atlas -> safety agent -> gate -> speaker) -> VISUALIZER.
// Collection names inside Atlas come from the repo via data.collections.
import React from "react";
import { Box, C, Chip, MONO, Radar, SANS, SceneSpec, Wave, data, lerp, lin, nm, seg, useScene } from "../ui";

const Flow: React.FC<{ x1: number; y1: number; x2: number; y2: number; color: string; show: number; pulses?: boolean; offset?: number }> = ({ x1, y1, x2, y2, color, show, pulses, offset = 0 }) => {
  const { frame } = useScene();
  const ex = lerp(x1, x2, show), ey = lerp(y1, y2, show);
  const dots = pulses && show >= 1 ? [0, 0.5].map((o) => ((frame + offset) / 26 + o) % 1) : [];
  return (
    <g>
      <line x1={x1} y1={y1} x2={ex} y2={ey} stroke={color} strokeWidth={2.5} opacity={0.75} />
      {show >= 1 && <circle cx={x2} cy={y2} r={5} fill={color} />}
      {dots.map((d, i) => (
        <circle key={i} cx={lerp(x1, x2, d)} cy={lerp(y1, y2, d)} r={9} fill={color} opacity={0.95} style={{ filter: `drop-shadow(0 0 8px ${color})` }} />
      ))}
    </g>
  );
};

const Node: React.FC<{ x: number; w: number; title: string; sub: string; color: string; k: number }> = ({ x, w, title, sub, color, k }) => (
  <Box x={x} y={250 + lerp(20, 0, k)} w={w} h={110} color={color} opacity={k}>
    <div style={{ padding: "18px 0 0", textAlign: "center", fontFamily: SANS, fontWeight: 800, fontSize: 28, color: C.text }}>{title}</div>
    <div style={{ textAlign: "center", fontFamily: MONO, fontSize: 17, color, marginTop: 6 }}>{sub}</div>
  </Box>
);

export const What: React.FC<{ scene: SceneSpec }> = () => {
  const { p, frame } = useScene();
  const streams = data.world.streams.slice(0, 3);
  return (
    <Radar>
      <div style={{ position: "absolute", left: 60, top: 64, fontFamily: SANS, fontWeight: 800, fontSize: 52, color: C.text }}>
        A safety net for control towers
      </div>

      {/* SIMULATOR */}
      <Box x={60} y={170} w={330} h={620} label="SIMULATOR" color={C.amber} opacity={seg(p, 0, 0.06)}>
        <div style={{ padding: "34px 22px 0" }}>
          {(streams.length ? streams : [{ id: "[PLACEHOLDER stream]", name: "" }]).map((s, i) => (
            <div key={s.id} style={{ marginBottom: 22 }}>
              <div style={{ fontFamily: MONO, fontSize: 18, color: C.amber, marginBottom: 6 }}>stream {s.id}</div>
              <Wave seed={`w3-${i}`} w={286} h={58} color={C.amber} bars={40} noise={0.35} amp={0.9} />
            </div>
          ))}
          <div style={{ fontFamily: MONO, fontSize: 18, color: C.dim, margin: "8px 0 8px" }}>positions</div>
          <svg width={286} height={170}>
            <line x1={10} y1={150} x2={276} y2={30} stroke={C.line} strokeWidth={10} />
            <line x1={120} y1={20} x2={200} y2={160} stroke={C.line} strokeWidth={5} />
            <circle cx={lerp(10, 190, (frame % 240) / 240)} cy={lerp(150, 69, (frame % 240) / 240)} r={8} fill={C.amber} />
            <circle cx={lerp(196, 150, (frame % 240) / 240)} cy={lerp(152, 72, (frame % 240) / 240)} r={7} fill={C.text} />
            <circle cx={240} cy={130} r={6} fill={C.dim} />
          </svg>
        </div>
      </Box>

      {/* TOWER */}
      <Box x={450} y={170} w={1040} h={620} label="TOWER" color={C.line} opacity={seg(p, 0.07, 0.13)} />
      <Node x={490} w={200} title="Listener" sub="speech → text" color={C.amber} k={seg(p, 0.12, 0.17)} />
      <Node x={760} w={220} title="Safety agent" sub="rules" color={C.red} k={seg(p, 0.3, 0.35)} />
      <Node x={1040} w={170} title="Gate" sub="policy first" color={C.red} k={seg(p, 0.36, 0.41)} />
      <Node x={1270} w={180} title="Speaker" sub="on the radio" color={C.amber} k={seg(p, 0.42, 0.47)} />

      <Box x={490} y={480} w={960} h={270} label="MONGODB ATLAS" labelRight color={C.green} opacity={seg(p, 0.18, 0.24)} glow={0.35 * seg(p, 0.18, 0.3)}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16, padding: "40px 30px 0" }}>
          {data.collections.map((c, i) => {
            const k = seg(p, 0.24 + i * 0.035, 0.27 + i * 0.035);
            return (
              <Chip key={c.name} color={c.status === "missing" ? C.red : C.green} size={26} style={{ opacity: k, transform: `scale(${lerp(1.5, 1, k)})` }}>
                {nm(c)}
              </Chip>
            );
          })}
        </div>
        <div style={{ position: "absolute", left: 30, bottom: 22, fontFamily: MONO, fontSize: 19, color: C.dim, opacity: lin(p, 0.5, 0.55) }}>
          validators · change streams · insert-only logs
        </div>
      </Box>

      {/* VISUALIZER */}
      <Box x={1550} y={170} w={310} h={620} label="VISUALIZER" color={C.cyan} opacity={seg(p, 0.55, 0.61)}>
        <svg width={310} height={420} style={{ position: "absolute", top: 40 }}>
          <line x1={40} y1={330} x2={270} y2={60} stroke={C.line} strokeWidth={14} />
          <line x1={90} y1={70} x2={230} y2={300} stroke={C.line} strokeWidth={7} />
          <circle cx={150} cy={200} r={9} fill={C.amber} />
          <circle cx={182} cy={222} r={8} fill={C.red} />
          {[0, 1, 2, 3].map((i) => <rect key={i} x={30} y={360 + i * 14} width={250 - i * 40} height={6} fill={C.line} />)}
        </svg>
        <div style={{
          position: "absolute", left: 28, top: 480, fontFamily: MONO, fontWeight: 700, fontSize: 38, color: C.cyan, border: `4px solid ${C.cyan}`,
          padding: "6px 14px", letterSpacing: 3, opacity: lin(p, 0.68, 0.7), transform: `rotate(-8deg) scale(${lerp(2.2, 1, seg(p, 0.68, 0.74))})`,
        }}>READ-ONLY</div>
      </Box>

      <svg width={1920} height={1080} style={{ position: "absolute" }}>
        <Flow x1={390} y1={305} x2={490} y2={305} color={C.amber} show={seg(p, 0.1, 0.14)} pulses />
        <Flow x1={590} y1={360} x2={590} y2={480} color={C.cyan} show={seg(p, 0.22, 0.27)} pulses />
        <Flow x1={870} y1={480} x2={870} y2={360} color={C.cyan} show={seg(p, 0.33, 0.38)} pulses offset={7} />
        <Flow x1={1125} y1={480} x2={1125} y2={360} color={C.cyan} show={seg(p, 0.39, 0.44)} pulses offset={14} />
        <Flow x1={980} y1={305} x2={1040} y2={305} color={C.red} show={seg(p, 0.38, 0.41)} />
        <Flow x1={1210} y1={305} x2={1270} y2={305} color={C.red} show={seg(p, 0.44, 0.47)} />
        <Flow x1={1450} y1={615} x2={1550} y2={615} color={C.cyan} show={seg(p, 0.58, 0.62)} pulses offset={3} />
      </svg>
      <div style={{ position: "absolute", left: 606, top: 404, fontFamily: MONO, fontSize: 18, color: C.cyan, opacity: lin(p, 0.27, 0.3) }}>every call</div>
      <div style={{ position: "absolute", left: 886, top: 404, fontFamily: MONO, fontSize: 18, color: C.cyan, opacity: lin(p, 0.4, 0.43) }}>change streams</div>
      <div style={{ position: "absolute", left: 980, top: 196, width: 300, textAlign: "center", fontFamily: MONO, fontSize: 19, color: C.red, opacity: lin(p, 0.46, 0.5) }}>
        intent: {nm(data.ids.action_stop)}
      </div>
    </Radar>
  );
};
