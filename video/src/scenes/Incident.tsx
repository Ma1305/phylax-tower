// Scene 1: the incident, as a news report. Real media (NTSB photo, optional clip, NTSB map) with a
// lower third, then the two clearances, then cut to black. All text comes from script.json.
import React from "react";
import { AbsoluteFill } from "remotion";
import { C, MONO, Radar, SANS, SceneSpec, Slot, data, lerp, lin, seg, typed, useScene } from "../ui";

const Credit: React.FC<{ file: string; opacity: number }> = ({ file, opacity }) => {
  const a = data.assets[file];
  if (!a?.present || !a.credit) return null;
  return (
    <div style={{ position: "absolute", right: 40, top: 36, opacity, fontFamily: SANS, fontSize: 22, color: "#fff", background: "rgba(0,0,0,0.6)", padding: "6px 14px", borderRadius: 3 }}>
      {a.credit}
    </div>
  );
};

export const Incident: React.FC<{ scene: SceneSpec }> = ({ scene }) => {
  const { p } = useScene();
  const cards = scene.cards ?? [];
  const news = scene.news;
  if (p > 0.965) return <AbsoluteFill style={{ background: "#000" }} />;

  const first = data.assets["incident_clip.mp4"]?.present ? "incident_clip.mp4" : "incident_photo.jpg";
  const toMap = seg(p, 0.4, 0.46); // photo/clip -> map
  const cardAt = [0.48, 0.62];
  const punch = lin(p, 0.82, 0.84);
  const zoomA = lerp(1.04, 1.16, lin(p, 0, 0.46));
  const zoomB = lerp(1.0, 1.08, lin(p, 0.4, 0.9)); // holds from 0.9: the freeze

  return (
    <Radar>
      {/* real media, full frame, slow push-in */}
      <AbsoluteFill style={{ opacity: 1 - toMap, transform: `scale(${zoomA})`, transformOrigin: "55% 45%" }}>
        <Slot file={first} x={0} y={0} w={1920} h={1080} style={{ borderRadius: 0, border: "none" }} />
      </AbsoluteFill>
      <AbsoluteFill style={{ opacity: toMap, transform: `scale(${zoomB})`, transformOrigin: "38% 52%" }}>
        <Slot file="incident_map.png" x={0} y={0} w={1920} h={1080} style={{ borderRadius: 0, border: "none" }} filter="brightness(0.8)" />
      </AbsoluteFill>
      <AbsoluteFill style={{ background: "linear-gradient(180deg, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0) 22%, rgba(0,0,0,0) 55%, rgba(0,0,0,0.92) 88%)" }} />
      <Credit file={first} opacity={1 - toMap} />
      <Credit file="incident_map.png" opacity={toMap} />

      {/* top bar */}
      {news && (
        <div style={{ position: "absolute", left: 40, top: 36, display: "flex", alignItems: "stretch", fontFamily: SANS, fontWeight: 800, fontSize: 26, letterSpacing: 2 }}>
          <div style={{ background: C.red, color: "#fff", padding: "8px 16px" }}>{news.kicker}</div>
          <div style={{ background: "rgba(0,0,0,0.75)", color: "#fff", padding: "8px 16px", fontWeight: 600 }}>{typed(scene.title ?? "", lin(p, 0.02, 0.14))}</div>
        </div>
      )}

      {/* the two clearances, over the map */}
      {cards.map((c, i) => {
        const k = seg(p, cardAt[i], cardAt[i] + 0.05);
        return (
          <div key={i} style={{
            position: "absolute", left: lerp(1920, 1010, k), top: 130 + i * 225, width: 870, boxSizing: "border-box", padding: "20px 26px",
            background: "rgba(8,10,10,0.92)", border: `2px solid ${C.amber}`, borderRadius: 4, boxShadow: `0 0 ${36 + 30 * punch}px ${C.amber}99`,
          }}>
            <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 700, letterSpacing: 2, color: C.amber, marginBottom: 10 }}>
              {c.time ? `${c.time} · ` : ""}CLEARANCE {i + 1} · {c.from} → {c.to}
            </div>
            <div style={{ fontFamily: MONO, fontSize: 28, lineHeight: 1.35, color: C.text }}>{typed(c.text, lin(p, cardAt[i] + 0.04, cardAt[i] + 0.17))}</div>
          </div>
        );
      })}
      {scene.cards_note && (
        <div style={{ position: "absolute", left: 1010, top: 130 + cards.length * 225 - 10, fontFamily: SANS, fontSize: 20, color: "#fff", opacity: 0.8 * lin(p, cardAt[0] + 0.05, cardAt[0] + 0.08), background: "rgba(0,0,0,0.6)", padding: "4px 10px" }}>
          {scene.cards_note}
        </div>
      )}

      {/* lower third */}
      {news && (
        <div style={{ position: "absolute", left: 40, right: 40, top: 700, transform: `translateY(${lerp(60, 0, seg(p, 0.03, 0.09))}px)`, opacity: lin(p, 0.03, 0.07) }}>
          <div style={{ display: "inline-block", background: "#fff", color: "#0a0a0a", fontFamily: SANS, fontWeight: 800, fontSize: 64, padding: "8px 26px", lineHeight: 1.15 }}>
            {punch > 0 ? scene.punchline : news.headline}
          </div>
          <div style={{ background: "rgba(6,10,14,0.92)", borderLeft: `8px solid ${C.red}`, color: "#fff", fontFamily: SANS, fontWeight: 600, fontSize: 32, padding: "12px 22px", width: 1500 }}>
            {news.subline}
          </div>
          <div style={{ display: "flex", background: C.red, color: "#fff", fontFamily: SANS, fontWeight: 600, fontSize: 24, width: 1522 }}>
            {news.facts.map((f, i) => (
              <div key={i} style={{ padding: "8px 22px", borderRight: "2px solid rgba(255,255,255,0.35)", opacity: lin(p, 0.1 + i * 0.05, 0.13 + i * 0.05) }}>{f}</div>
            ))}
          </div>
        </div>
      )}
    </Radar>
  );
};
