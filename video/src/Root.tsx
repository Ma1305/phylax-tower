import React from "react";
import { AbsoluteFill, Audio, Composition, Sequence, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { C, FontGate, SANS, SceneFrame, SceneSpec, data, script } from "./ui";
import { Incident } from "./scenes/Incident";
import { Team } from "./scenes/Team";
import { What } from "./scenes/What";
import { Catch } from "./scenes/Catch";
import { Audit } from "./scenes/Audit";
import { Close } from "./scenes/Close";

const SCENES: Record<string, React.FC<{ scene: SceneSpec }>> = {
  incident: Incident, team: Team, what: What, catch: Catch, audit: Audit, close: Close,
};

const { fps, width, height } = script.config;
const f = (s: number) => Math.round(s * fps);
const totalFrames = f(Math.max(...script.scenes.map((s) => s.end_s)));
const NAMES = script.config.team_names.length > 1
  ? `${script.config.team_names.slice(0, -1).join(", ")} and ${script.config.team_names.at(-1)}`
  : script.config.team_names.join("");
const voText = (s: SceneSpec) => s.vo_text.replace("{names}", NAMES).split("{product}").join(script.config.product_name);

/** Split a scene's vo_text into caption chunks, timed in proportion to their length. */
const captionsOf = (s: SceneSpec) => {
  const parts = voText(s).split(/(?<=[.!?])\s+/).filter(Boolean);
  const chunks: string[] = [];
  for (const part of parts) {
    const last = chunks[chunks.length - 1];
    if (last !== undefined && last.length < 28 && part.length < 28) chunks[chunks.length - 1] = `${last} ${part}`;
    else chunks.push(part);
  }
  const total = chunks.reduce((n, c) => n + c.length, 0);
  // captions follow the spoken window when the scene gives one (vo_start_s / vo_end_s), else the whole scene
  const from = s.vo_start_s ?? s.start_s, to = s.vo_end_s ?? s.end_s;
  let at = from;
  return chunks.map((text, i) => {
    const len = ((to - from) * text.length) / total;
    const last = i === chunks.length - 1;
    const c = { text, from: at, to: last ? Math.min(s.end_s, at + len + 0.4) : at + len };
    at += len;
    return c;
  });
};
const CAPTIONS = script.scenes.flatMap(captionsOf);

const Captions: React.FC = () => {
  const t = useCurrentFrame() / useVideoConfig().fps;
  const c = CAPTIONS.find((x) => t >= x.from && t < x.to);
  if (!c) return null;
  return (
    <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "center", paddingBottom: 38 }}>
      <div style={{
        maxWidth: 1500, textAlign: "center", fontFamily: SANS, fontWeight: 600, fontSize: 38, lineHeight: 1.28, color: "#fff",
        background: "rgba(0,0,0,0.78)", border: `1px solid ${C.line}`, borderRadius: 6, padding: "12px 28px",
      }}>{c.text}</div>
    </AbsoluteFill>
  );
};

const Video: React.FC = () => (
  <AbsoluteFill style={{ background: C.bg }}>
    {script.scenes.map((s) => {
      const Scene = SCENES[s.id];
      const frames = f(s.end_s) - f(s.start_s);
      return (
        <Sequence key={s.id} from={f(s.start_s)} durationInFrames={frames} name={s.id}>
          <SceneFrame frames={frames}>{Scene ? <Scene scene={s} /> : null}</SceneFrame>
        </Sequence>
      );
    })}
    <Captions />
    <FontGate />
    {data.assets["vo.wav"]?.present ? <Audio src={staticFile("vo.wav")} /> : null}
  </AbsoluteFill>
);

export const Root: React.FC = () => (
  <Composition id="HoldShort" component={Video} width={width} height={height} fps={fps} durationInFrames={totalFrames} />
);
