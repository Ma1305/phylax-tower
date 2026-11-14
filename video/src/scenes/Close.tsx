// Scene 6: close. Wordmark, tagline, plain-text credits.
import React from "react";
import { C, MONO, Radar, SANS, SceneSpec, Slot, Wordmark, lerp, seg, useScene } from "../ui";

export const Close: React.FC<{ scene: SceneSpec }> = ({ scene }) => {
  const { p } = useScene();
  const up = (a: number): React.CSSProperties => ({ opacity: seg(p, a, a + 0.08), transform: `translateY(${lerp(24, 0, seg(p, a, a + 0.08))}px)` });
  return (
    <Radar>
      <div style={{ position: "absolute", left: 0, right: 0, top: 190, display: "flex", flexDirection: "column", alignItems: "center" }}>
        <Wordmark k={seg(p, 0.0, 0.2)} size={140} />
        <div style={{ marginTop: 50, fontFamily: SANS, fontWeight: 600, fontSize: 64, ...up(0.2) }}>
          <span style={{ color: C.red }}>{(scene.tagline ?? "").split(",")[0]},</span>
          <span style={{ color: C.text }}>{(scene.tagline ?? "").split(",").slice(1).join(",")}</span>
        </div>
        <div style={{ marginTop: 44, fontFamily: SANS, fontSize: 30, color: C.dim, letterSpacing: 1, ...up(0.42) }}>{scene.built_on}</div>
      </div>
      <Slot file="cam_close.mp4" x={1500} y={640} w={380} h={214} />
    </Radar>
  );
};
