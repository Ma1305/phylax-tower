// Scene 2: who we are. Names type on (from script.json config), optional team.png, wordmark locks in.
import React from "react";
import { C, Cursor, MONO, Radar, SceneSpec, Slot, Wordmark, data, lin, script, seg, typed, useScene } from "../ui";

export const Team: React.FC<{ scene: SceneSpec }> = () => {
  const { p } = useScene();
  const names = script.config.team_names;
  const hasPhoto = data.assets["team.png"]?.present;
  return (
    <Radar>
      <div style={{ position: "absolute", left: 160, top: 150, fontFamily: MONO, fontSize: 24, letterSpacing: 6, color: C.dim }}>WE ARE</div>
      <div style={{ position: "absolute", left: 160, top: 200 }}>
        {names.map((n, i) => {
          const a = 0.04 + i * 0.1;
          return (
            <div key={i} style={{ fontFamily: MONO, fontWeight: 700, fontSize: 58, lineHeight: 1.45, color: C.text, height: 84 }}>
              <span style={{ color: C.amber, opacity: lin(p, a, a + 0.01) }}>› </span>
              {typed(n, lin(p, a, a + 0.1))}
              {p >= a && p < a + 0.12 && <Cursor />}
            </div>
          );
        })}
      </div>
      {hasPhoto && !data.assets["cam_intro.mp4"]?.present
        ? <Slot file="team.png" x={1040} y={150} w={720} h={405} />
        : <Slot file="cam_intro.mp4" x={1040} y={150} w={720} h={405} />}
      <div style={{ position: "absolute", left: 0, right: 0, top: 640, display: "flex", justifyContent: "center", opacity: lin(p, 0.5, 0.52) }}>
        <Wordmark k={seg(p, 0.5, 0.72)} size={110} />
      </div>
    </Radar>
  );
};
