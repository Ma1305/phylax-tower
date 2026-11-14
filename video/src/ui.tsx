// Shared style and building blocks for every scene.
import React, { createContext, useContext, useEffect, useState } from "react";
import {
  AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, continueRender, delayRender,
  random, staticFile, useCurrentFrame, useVideoConfig,
} from "remotion";
import "@fontsource/inter/400.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/800.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import dataJson from "./generated/data.json";
import scriptJson from "../script.json";

// ------------------------------------------------------------------ data
export type Status = "code" | "docs" | "missing";
export type Named = { name: string; status: Status; source: string | null };
export type Model = { collection: string; collection_status: Status; status: Status; source: string | null; rows: [string, string][] };
export type Asset = { file: string; kind: "video" | "audio" | "image"; scene: string; optional: boolean; contains: string; present: boolean; credit?: string; with_audio?: boolean };
export const data = dataJson as unknown as {
  assets: Record<string, Asset>;
  world: {
    status: Status; source: string | null;
    streams: { id: string; name: string }[];
    stream: { id: string; name: string } | null;
    lane: { id: string; name: string; kind: string } | null;
    controller: { id: string; name: string; role: string } | null;
  };
  collections: Named[];
  ids: Record<string, Named>;
  models: Record<string, Model>;
  rules: { status: Status; source: string | null; lines: string[]; first_line: number; highlight: number };
  open: string[];
};
export type SceneSpec = {
  id: string; start_s: number; end_s: number; vo_start_s?: number; vo_end_s?: number; vo_text: string;
  title?: string; tagline?: string; built_on?: string; punchline?: string; cards_note?: string;
  cards?: { time?: string; from: string; to: string; text: string }[];
  news?: { kicker: string; headline: string; subline: string; facts: string[]; source: string };
};
export const script = scriptJson as unknown as {
  config: { product_name: string; hide_missing_optional?: boolean; team_names: string[]; width: number; height: number; fps: number };
  scenes: SceneSpec[];
};
/** A name that came from the repo, or a visible placeholder when it was not found. */
export const nm = (n: Named | undefined, fallback = "?") =>
  !n ? `[PLACEHOLDER ${fallback}]` : n.status === "missing" ? `[PLACEHOLDER ${n.name}]` : n.name;
export const coll = (name: string) => nm(data.collections.find((c) => c.name === name), name);

// ------------------------------------------------------------------ style
export const C = {
  bg: "#04080b", panel: "rgba(8,16,20,0.92)", line: "rgba(120,210,190,0.22)", grid: "rgba(120,210,190,0.06)",
  text: "#dce8e4", dim: "#6f8782", amber: "#ffb020", red: "#ff3d3d", green: "#00ed64", cyan: "#27d7f5",
};
export const SANS = "Inter, system-ui, sans-serif";
export const MONO = '"JetBrains Mono", ui-monospace, monospace';

/** Holds rendering until the bundled fonts are ready (capped at 6 s so it can never hang). Mount once at the root. */
export const FontGate: React.FC = () => {
  const [handle] = useState(() => delayRender("fonts", { timeoutInMilliseconds: 60000 }));
  useEffect(() => {
    let done = false;
    const go = () => { if (!done) { done = true; continueRender(handle); } };
    const faces = ["400 20px Inter", "600 20px Inter", "800 20px Inter", '400 20px "JetBrains Mono"', '700 20px "JetBrains Mono"'];
    Promise.all(faces.map((f) => document.fonts.load(f))).then(go, go);
    const timer = setTimeout(go, 6000);
    return () => clearTimeout(timer);
  }, [handle]);
  return null;
};

// ------------------------------------------------------------------ timing
export const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
export const ease = (x: number) => 1 - Math.pow(1 - clamp01(x), 3);
/** Progress 0..1 of p between a and b, eased. */
export const seg = (p: number, a: number, b: number) => ease((p - a) / (b - a));
export const lin = (p: number, a: number, b: number) => clamp01((p - a) / (b - a));
export const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
export const typed = (text: string, k: number) => text.slice(0, Math.round(text.length * clamp01(k)));

const SceneCtx = createContext(1);
export const SceneFrame: React.FC<{ frames: number; children: React.ReactNode }> = ({ frames, children }) => (
  <SceneCtx.Provider value={frames}>{children}</SceneCtx.Provider>
);
/** frame and seconds inside the scene, and p = 0..1 across it. Animations key off p so retiming scales them. */
export const useScene = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const frames = useContext(SceneCtx);
  return { frame, fps, frames, t: frame / fps, dur: frames / fps, p: frame / frames };
};

// ------------------------------------------------------------------ background
export const Radar: React.FC<{ children?: React.ReactNode; dim?: number }> = ({ children, dim = 1 }) => (
  <AbsoluteFill style={{ background: C.bg, color: C.text, fontFamily: SANS, fontVariantLigatures: "none" }}>
    <AbsoluteFill style={{
      opacity: dim,
      backgroundImage: `linear-gradient(${C.grid} 1px, transparent 1px), linear-gradient(90deg, ${C.grid} 1px, transparent 1px)`,
      backgroundSize: "60px 60px",
    }} />
    <svg width={1920} height={1080} style={{ position: "absolute", opacity: 0.5 * dim }}>
      {[260, 520, 780, 1040].map((r) => <circle key={r} cx={960} cy={540} r={r} fill="none" stroke={C.grid} strokeWidth={1.5} />)}
    </svg>
    <AbsoluteFill style={{ background: "radial-gradient(ellipse at center, transparent 45%, rgba(0,0,0,0.75) 100%)" }} />
    {children}
  </AbsoluteFill>
);

// ------------------------------------------------------------------ boxes and text
export const Box: React.FC<{
  x: number; y: number; w: number; h?: number; color?: string; label?: string; labelRight?: boolean; glow?: number;
  opacity?: number; dashed?: boolean; style?: React.CSSProperties; children?: React.ReactNode;
}> = ({ x, y, w, h, color = C.line, label, labelRight, glow = 0, opacity = 1, dashed, style, children }) => (
  <div style={{
    position: "absolute", left: x, top: y, width: w, height: h, opacity, boxSizing: "border-box",
    border: `1.5px ${dashed ? "dashed" : "solid"} ${color}`, background: C.panel, borderRadius: 4,
    boxShadow: glow ? `0 0 ${28 * glow}px ${color}, inset 0 0 ${18 * glow}px ${color}33` : undefined, ...style,
  }}>
    {label && (
      <div style={{ position: "absolute", [labelRight ? "right" : "left"]: 14, top: -13, padding: "0 8px", background: C.bg, color, fontFamily: MONO, fontSize: 18, fontWeight: 700, letterSpacing: 2 }}>
        {label}
      </div>
    )}
    {children}
  </div>
);

export const Chip: React.FC<{ color: string; children: React.ReactNode; size?: number; fill?: boolean; style?: React.CSSProperties }> = ({ color, children, size = 22, fill, style }) => (
  <span style={{
    display: "inline-block", fontFamily: MONO, fontSize: size, fontWeight: 700, color: fill ? C.bg : color,
    background: fill ? color : `${color}1c`, border: `1.5px solid ${color}`, borderRadius: 4, padding: `${size * 0.22}px ${size * 0.55}px`,
    whiteSpace: "nowrap", ...style,
  }}>{children}</span>
);

export const Cursor: React.FC<{ color?: string }> = ({ color = C.amber }) => {
  const frame = useCurrentFrame();
  return <span style={{ color, opacity: frame % 16 < 9 ? 1 : 0 }}>▌</span>;
};

/** Key/value rows from a real model, revealed line by line. */
export const Doc: React.FC<{ model: Model; k: number; size?: number; color?: string }> = ({ model, k, size = 24, color = C.cyan }) => {
  if (model.status === "missing") {
    return <div style={{ fontFamily: MONO, fontSize: size, color: C.red }}>[PLACEHOLDER: {model.collection} document shape not found in repo]</div>;
  }
  const n = Math.ceil(model.rows.length * clamp01(k));
  return (
    <div style={{ fontFamily: MONO, fontSize: size, lineHeight: 1.42, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
      <span style={{ color: C.dim }}>{"{"}</span>
      {model.rows.slice(0, n).map(([f, v]) => (
        <div key={f} style={{ paddingLeft: size * 1.2 }}>
          <span style={{ color }}>{f}</span><span style={{ color: C.dim }}>: </span>
          <span style={{ color: /^"/.test(v) ? C.text : C.amber }}>{v}</span><span style={{ color: C.dim }}>,</span>
        </div>
      ))}
      <span style={{ color: C.dim }}>{"}"}</span>
    </div>
  );
};

// ------------------------------------------------------------------ waveform
export const Wave: React.FC<{
  seed: string; w: number; h: number; color: string; bars?: number; noise?: number; amp?: number; speed?: number;
}> = ({ seed, w, h, color, bars = 64, noise = 0, amp = 1, speed = 1 }) => {
  const frame = useCurrentFrame();
  const bw = w / bars;
  return (
    <svg width={w} height={h} style={{ display: "block" }}>
      {Array.from({ length: bars }, (_, i) => {
        const ph = random(`${seed}-${i}`) * 6.28;
        const voice = Math.abs(Math.sin(frame * 0.19 * speed + ph) * Math.sin(frame * 0.053 * speed + i * 0.35 + ph));
        const hiss = random(`${seed}-${i}-${frame}`);
        const v = clamp01(amp * (voice * (1 - noise * 0.5) + hiss * noise * 0.9) + 0.04);
        const bh = Math.max(3, v * h);
        return <rect key={i} x={i * bw + bw * 0.2} y={(h - bh) / 2} width={bw * 0.6} height={bh} rx={bw * 0.25} fill={color} opacity={0.35 + 0.65 * v} />;
      })}
    </svg>
  );
};

// ------------------------------------------------------------------ asset slots
/** A recording / image slot: the real file when it is in assets/, else a labelled placeholder card. */
export const Slot: React.FC<{
  file: string; x: number; y: number; w: number; h: number; filter?: string; style?: React.CSSProperties; padRight?: number;
}> = ({ file, x, y, w, h, filter, style, padRight = 0 }) => {
  const frame = useCurrentFrame();
  const a = data.assets[file];
  if (!a?.present && a?.optional && script.config.hide_missing_optional) return null;
  const base: React.CSSProperties = { position: "absolute", left: x, top: y, width: w, height: h, overflow: "hidden", boxSizing: "border-box", borderRadius: 4, ...style };
  if (a?.present) {
    return (
      <div style={{ ...base, border: `1.5px solid ${C.line}`, background: "#000" }}>
        {a.kind === "image"
          ? <Img src={staticFile(file)} style={{ width: "100%", height: "100%", objectFit: "cover", filter }} />
          : <OffthreadVideo muted={!a.with_audio} src={staticFile(file)} style={{ width: "100%", height: "100%", objectFit: "cover", filter }} />}
      </div>
    );
  }
  const small = w < 520;
  return (
    <div style={{
      ...base, border: `2px dashed ${C.dim}`, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      gap: small ? 6 : 16, padding: small ? 10 : 40, paddingRight: padRight || (small ? 10 : 40), textAlign: "center",
      background: `repeating-linear-gradient(135deg, rgba(120,210,190,0.05) 0 24px, rgba(120,210,190,0.015) 24px 48px), ${C.bg}`,
    }}>
      <div style={{ fontFamily: MONO, fontSize: small ? 14 : 22, letterSpacing: 4, color: C.dim }}>
        ASSET SLOT{a?.optional ? " · OPTIONAL" : ""} <span style={{ opacity: frame % 30 < 15 ? 1 : 0.25, color: C.amber }}>●</span>
      </div>
      <div style={{ fontFamily: MONO, fontWeight: 700, fontSize: small ? 22 : Math.min(54, (w - padRight) / 16), color: C.text }}>{file}</div>
      <div style={{ fontFamily: SANS, fontSize: small ? 15 : 26, lineHeight: 1.35, color: C.dim, maxWidth: (w - padRight) * 0.8 }}>{a?.contains ?? "not listed in assets.json"}</div>
      {!small && <div style={{ fontFamily: MONO, fontSize: 18, color: C.dim, opacity: 0.7 }}>drop into video/assets/ and re-render</div>}
    </div>
  );
};

/** An audio slot: plays the file when present, else shows a small placeholder tag. */
export const AudioSlot: React.FC<{ file: string; from: number; x: number; y: number; show?: boolean }> = ({ file, from, x, y, show = true }) => {
  const a = data.assets[file];
  if (a?.present) return <Sequence from={from} layout="none"><Audio src={staticFile(file)} /></Sequence>;
  if (!show) return null;
  return (
    <div style={{ position: "absolute", left: x, top: y, fontFamily: MONO, fontSize: 17, color: C.dim, border: `1.5px dashed ${C.dim}`, borderRadius: 4, padding: "6px 12px" }}>
      AUDIO SLOT · <b style={{ color: C.text }}>{file}</b> · {a?.contains ?? ""}
    </div>
  );
};

// ------------------------------------------------------------------ wordmark
/** Product wordmark (script.json config.product_name) over a runway holding-position marking (two solid, two dashed lines). */
export const Wordmark: React.FC<{ k: number; size?: number }> = ({ k, size = 150 }) => {
  const name = script.config.product_name.toUpperCase();
  const w = size * 0.66 * name.length;
  const bar = (dashed: boolean, i: number) => (
    <div key={i} style={{
      height: size * 0.055, marginTop: size * 0.045, width: `${100 * ease(k * 1.4 - i * 0.1)}%`,
      background: dashed ? `repeating-linear-gradient(90deg, ${C.amber} 0 ${size * 0.3}px, transparent ${size * 0.3}px ${size * 0.5}px)` : C.amber,
    }} />
  );
  return (
    <div style={{ width: w, textAlign: "center" }}>
      <div style={{
        fontFamily: SANS, fontWeight: 800, fontSize: size, letterSpacing: lerp(size * 0.3, size * 0.06, ease(k)), lineHeight: 1,
        color: C.text, opacity: clamp01(k * 3), transform: `scale(${lerp(1.18, 1, ease(k))})`, whiteSpace: "nowrap",
      }}>{name}</div>
      <div style={{ marginTop: size * 0.12 }}>{[false, false, true, true].map(bar)}</div>
    </div>
  );
};
