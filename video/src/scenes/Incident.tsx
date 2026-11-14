// Scene 1: the incident. Recording slot (desaturated), title types on, two clearance cards, cut to black.
import React from "react";
import { AbsoluteFill } from "remotion";
import { C, Cursor, MONO, Radar, SANS, SceneSpec, Slot, lerp, lin, seg, typed, useScene } from "../ui";

export const Incident: React.FC<{ scene: SceneSpec }> = ({ scene }) => {
  const { p } = useScene();
  const title = scene.title ?? "";
  const cards = scene.cards ?? [];
  const cardAt = [0.3, 0.56];
  const freeze = p > 0.86;
  if (p > 0.965) return <AbsoluteFill style={{ background: "#000" }} />;
  return (
    <Radar>
      <Slot file="rec_safety_off.mp4" x={0} y={0} w={1920} h={1080} filter="grayscale(1) contrast(0.95) brightness(0.55)" style={{ borderRadius: 0, opacity: 0.9 }} padRight={1000} />
      <AbsoluteFill style={{ background: "linear-gradient(180deg, rgba(4,8,11,0.85) 0%, rgba(4,8,11,0.25) 30%, rgba(4,8,11,0.25) 60%, rgba(4,8,11,0.9) 100%)" }} />

      <div style={{ position: "absolute", left: 70, top: 56, fontFamily: MONO, fontSize: 24, fontWeight: 700, letterSpacing: 6, color: C.red, border: `2px solid ${C.red}`, padding: "8px 16px" }}>
        RECONSTRUCTION
      </div>
      <div style={{ position: "absolute", left: 70, top: 128, fontFamily: SANS, fontWeight: 800, fontSize: 68, letterSpacing: 4, color: C.text }}>
        {typed(title, lin(p, 0.03, 0.2))}{p < 0.24 && <Cursor />}
      </div>

      {cards.map((c, i) => {
        const k = seg(p, cardAt[i], cardAt[i] + 0.05);
        const y = 300 + i * 250;
        return (
          <div key={i} style={{
            position: "absolute", left: lerp(1920, 880, k), top: y, width: 960, boxSizing: "border-box", padding: "24px 30px",
            background: "rgba(10,14,12,0.94)", border: `2px solid ${C.amber}`, borderRadius: 4,
            boxShadow: `0 0 ${40 + (freeze ? 30 : 0)}px ${C.amber}88`,
          }}>
            <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 700, letterSpacing: 3, color: C.amber, marginBottom: 12 }}>
              CLEARANCE {i + 1} · {c.from} → {c.to}
            </div>
            <div style={{ fontFamily: MONO, fontSize: 31, lineHeight: 1.35, color: C.text }}>
              {typed(c.text, lin(p, cardAt[i] + 0.04, cardAt[i] + 0.2))}
            </div>
          </div>
        );
      })}

      <div style={{
        position: "absolute", left: 70, top: 800, fontFamily: SANS, fontWeight: 800, fontSize: 64, letterSpacing: 3, color: C.red,
        opacity: lin(p, 0.82, 0.84), textShadow: `0 0 30px ${C.red}`,
      }}>
        TWO CLEARANCES. ONE RUNWAY.
      </div>
    </Radar>
  );
};
