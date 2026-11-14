// Scene 6: close. Wordmark, tagline, repo and demo URLs (script.json config), plain-text credits.
import React from "react";
import { C, MONO, Radar, SANS, SceneSpec, Wordmark, lerp, script, seg, useScene } from "../ui";

export const Close: React.FC<{ scene: SceneSpec }> = ({ scene }) => {
  const { p } = useScene();
  const up = (a: number): React.CSSProperties => ({ opacity: seg(p, a, a + 0.08), transform: `translateY(${lerp(24, 0, seg(p, a, a + 0.08))}px)` });
  const { repo_url, demo_url } = script.config;
  return (
    <Radar>
      <div style={{ position: "absolute", left: 0, right: 0, top: 190, display: "flex", flexDirection: "column", alignItems: "center" }}>
        <Wordmark k={seg(p, 0.0, 0.2)} size={170} />
        <div style={{ marginTop: 50, fontFamily: SANS, fontWeight: 600, fontSize: 64, ...up(0.2) }}>
          <span style={{ color: C.red }}>{(scene.tagline ?? "").split(",")[0]},</span>
          <span style={{ color: C.text }}>{(scene.tagline ?? "").split(",").slice(1).join(",")}</span>
        </div>
        <div style={{ marginTop: 56, display: "flex", gap: 60, fontFamily: MONO, fontSize: 32, ...up(0.32) }}>
          <span><span style={{ color: C.dim }}>repo </span><span style={{ color: C.cyan }}>{repo_url}</span></span>
          <span><span style={{ color: C.dim }}>demo </span><span style={{ color: C.cyan }}>{demo_url}</span></span>
        </div>
        <div style={{ marginTop: 44, fontFamily: SANS, fontSize: 30, color: C.dim, letterSpacing: 1, ...up(0.42) }}>{scene.built_on}</div>
      </div>
    </Radar>
  );
};
