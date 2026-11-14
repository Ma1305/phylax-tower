// Scene 5: agents you can audit. Three beats, each a recording slot plus an overlay animation.
// Barrier labels, reason codes and row fields come from data.json (read from the repo by scripts/prepare.mjs).
import React from "react";
import { AbsoluteFill, random } from "remotion";
import { Box, C, Chip, Doc, MONO, Model, Named, Radar, SANS, SceneSpec, Slot, coll, data, lerp, lin, nm, seg, useScene } from "../ui";

const BEATS = [0, 0.46, 0.71, 1];
const TITLE_UNTIL = 0.26; // share of beat (a) spent on the title card
const LABELS = ["BLOCKED WRITE", "SWITCH OFF", "TIME TRAVEL"];
const FILES = ["rec_blocked_write.mp4", "rec_switch_off.mp4", "rec_time_travel.mp4"];
const X = 1190, W = 690;

const oneLine = (m: Model, n = 3) => m.rows.slice(0, n).map(([f, v]) => `${f}: ${v}`).join("   ");
const src = (n: Named | undefined) => (n && n.source ? n.source : "not in repo yet");

const Blocked: React.FC<{ q: number }> = ({ q }) => {
  const layers: [string, Named | undefined][] = [
    ["Atlas validator", data.ids.validator_fn], ["Scoped database", data.ids.scoped_db], ["Gate", data.ids.gate_fn],
  ];
  const drop = seg(q, 0.04, 0.22);
  const hit = lin(q, 0.22, 0.24);
  const row = seg(q, 0.5, 0.64);
  return (
    <>
      <div style={{ position: "absolute", left: X + 150, top: lerp(140, 218, drop), opacity: 1 - hit, textAlign: "center", width: 390 }}>
        <Chip color={C.red} size={26} fill>forbidden write · action: "{nm(data.ids.action_forbidden)}"</Chip>
      </div>
      {hit > 0 && Array.from({ length: 16 }, (_, i) => {
        const k = lin(q, 0.22, 0.5);
        const a = random(`sh-${i}`) * Math.PI * 2, d = 60 + random(`sd-${i}`) * 260;
        return (
          <div key={i} style={{
            position: "absolute", left: X + 345 + Math.cos(a) * d * k, top: 240 - Math.abs(Math.sin(a)) * d * k * 0.6 + 200 * k * k,
            width: 10 + random(`sw-${i}`) * 22, height: 8 + random(`sh2-${i}`) * 10, background: C.red, opacity: 1 - k,
            transform: `rotate(${a * 180 * k}deg)`,
          }} />
        );
      })}
      {layers.map(([label, id], i) => {
        const flash = lin(q, 0.22 + i * 0.05, 0.25 + i * 0.05) * (1 - lin(q, 0.38 + i * 0.05, 0.5 + i * 0.05));
        return (
          <Box key={label} x={X} y={270 + i * 92} w={W} h={76} color={flash > 0.05 ? C.red : C.line} glow={flash} dashed={id?.status === "missing"}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", height: "100%", padding: "0 22px" }}>
              <span style={{ fontFamily: SANS, fontWeight: 800, fontSize: 30 }}><span style={{ color: C.dim }}>{i + 1} </span>{label}</span>
              <span style={{ textAlign: "right", fontFamily: MONO }}>
                <div style={{ fontSize: 22, fontWeight: 700, color: id?.status === "missing" ? C.red : C.amber }}>{nm(id)}</div>
                <div style={{ fontSize: 15, color: C.dim }}>{src(id)}</div>
              </span>
            </div>
          </Box>
        );
      })}
      <Box x={X} y={lerp(560, 590, row)} w={W} color={C.red} label={coll("security_events")} opacity={row} glow={0.4 * row}>
        <div style={{ padding: "26px 22px 16px" }}><Doc model={data.models.security_event} k={lin(q, 0.52, 0.7)} size={22} color={C.red} /></div>
      </Box>
    </>
  );
};

const SwitchOff: React.FC<{ q: number }> = ({ q }) => {
  const off = seg(q, 0.1, 0.18);
  const row = seg(q, 0.24, 0.36);
  const go = lin(q, 0.5, 0.68), back = seg(q, 0.68, 0.84);
  const cx = lerp(lerp(X, X + 330, go * go), X + 150, back);
  const agent = JSON.parse(data.models.state_event.rows.find(([f]) => f === "ref")?.[1] ?? '"procedures"');
  return (
    <>
      <Box x={X} y={170} w={W} h={110} color={off > 0.5 ? C.red : C.green} label={nm(data.ids.permissions)}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", height: "100%", padding: "0 26px" }}>
          <span style={{ fontFamily: MONO, fontSize: 24 }}>agent: <b>"{agent}"</b></span>
          <span style={{ display: "flex", alignItems: "center", gap: 12, fontFamily: MONO, fontSize: 22, color: off > 0.5 ? C.red : C.green }}>
            enabled: {off > 0.5 ? "false" : "true"}
            <span style={{ width: 96, height: 46, borderRadius: 23, background: off > 0.5 ? `${C.red}55` : `${C.green}66`, position: "relative", display: "inline-block" }}>
              <span style={{ position: "absolute", top: 5, left: lerp(55, 5, off), width: 36, height: 36, borderRadius: 18, background: off > 0.5 ? C.red : C.green }} />
            </span>
          </span>
        </div>
      </Box>
      <Box x={X} y={lerp(300, 320, row)} w={W} color={C.cyan} label={coll("state_events")} opacity={row}>
        <div style={{ padding: "26px 22px 16px" }}><Doc model={data.models.state_event} k={lin(q, 0.26, 0.44)} size={22} /></div>
      </Box>
      <Box x={X + 500} y={590} w={120} h={170} color={back > 0 ? C.red : C.line} glow={back > 0 ? 0.8 * (1 - back) + 0.2 : 0} opacity={lin(q, 0.44, 0.5)}>
        <div style={{ position: "absolute", top: 62, width: "100%", textAlign: "center", fontFamily: SANS, fontWeight: 800, fontSize: 30 }}>GATE</div>
      </Box>
      <div style={{ position: "absolute", left: cx, top: 648, opacity: lin(q, 0.46, 0.5) }}>
        <Chip color={C.amber} size={24} fill>intent</Chip>
      </div>
      <div style={{ position: "absolute", left: X, top: 790, opacity: lin(q, 0.7, 0.76), fontFamily: MONO, fontSize: 26 }}>
        <span style={{ color: C.red, fontWeight: 700 }}>{nm(data.ids.outcome_rejected)}</span>
        <span style={{ color: C.dim }}> · rule: </span><span style={{ color: C.text }}>{nm(data.ids.reason_disabled)}</span>
      </div>
    </>
  );
};

const TimeTravel: React.FC<{ q: number }> = ({ q }) => {
  const cards: [Model, string][] = [
    [data.models.transmission, C.amber], [data.models.intent, C.red], [data.models.decision, C.red], [data.models.state_event, C.cyan],
  ];
  const scrub = lerp(1, 0.08, seg(q, 0.05, 0.8));
  return (
    <>
      <svg width={W} height={60} style={{ position: "absolute", left: X, top: 160 }}>
        <line x1={0} y1={30} x2={W} y2={30} stroke={C.line} strokeWidth={4} />
        {Array.from({ length: 24 }, (_, i) => <line key={i} x1={(i * W) / 23} y1={22} x2={(i * W) / 23} y2={38} stroke={C.line} strokeWidth={2} />)}
        <line x1={scrub * W} y1={4} x2={scrub * W} y2={56} stroke={C.cyan} strokeWidth={5} />
        <text x={Math.min(scrub * W + 12, W - 120)} y={16} fill={C.cyan} fontFamily={MONO} fontSize={17}>◀◀ rewind</text>
      </svg>
      {cards.map(([m, color], i) => {
        const k = seg(q, 0.1 + i * 0.14, 0.2 + i * 0.14);
        return (
          <Box key={m.collection} x={X + i * 14} y={lerp(180, 240 + i * 128, k)} w={W - 42} h={112} color={color} opacity={k} dashed={m.status === "missing"}>
            <div style={{ padding: "14px 18px" }}>
              <Chip color={color} size={20}>{coll(m.collection)}</Chip>
              <div style={{ fontFamily: MONO, fontSize: 18, color: C.text, marginTop: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {m.status === "missing" ? "[PLACEHOLDER: shape not in repo]" : oneLine(m)}
              </div>
            </div>
          </Box>
        );
      })}
      <div style={{ position: "absolute", left: X, top: 780, width: W, fontFamily: SANS, fontWeight: 800, fontSize: 38, lineHeight: 1.2, color: C.text, opacity: lin(q, 0.66, 0.72) }}>
        Rebuilt from <span style={{ color: C.green }}>stored records</span> only.
      </div>
    </>
  );
};

export const Audit: React.FC<{ scene: SceneSpec }> = ({ scene }) => {
  const { p } = useScene();
  const b = p < BEATS[1] ? 0 : p < BEATS[2] ? 1 : 2;
  const q = lin(p, BEATS[b], BEATS[b + 1]);
  if (b === 0 && q < TITLE_UNTIL) {
    const k = q / TITLE_UNTIL;
    const [first, ...rest] = (scene.title ?? "").split(". ");
    return (
      <Radar>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", paddingBottom: 120 }}>
          <div style={{ fontFamily: MONO, fontSize: 24, letterSpacing: 6, color: C.dim, marginBottom: 30 }}>AGENTS YOU CAN AUDIT</div>
          <div style={{ fontFamily: SANS, fontWeight: 800, fontSize: 84, textAlign: "center", lineHeight: 1.2 }}>
            <div style={{ color: C.red, opacity: lin(k, 0.02, 0.1), transform: `translateX(${lerp(-80, 0, seg(k, 0.02, 0.2))}px)` }}>{first}.</div>
            <div style={{ color: C.text, opacity: lin(k, 0.3, 0.38), transform: `translateX(${lerp(80, 0, seg(k, 0.3, 0.48))}px)` }}>{rest.join(". ")}</div>
          </div>
        </AbsoluteFill>
      </Radar>
    );
  }
  const qq = b === 0 ? lin(q, TITLE_UNTIL, 1) : q;
  return (
    <Radar>
      <div style={{ position: "absolute", left: 40, top: 60, display: "flex", alignItems: "center", gap: 14 }}>
        <span style={{ fontFamily: SANS, fontWeight: 800, fontSize: 40, marginRight: 16 }}>Agents you can audit</span>
        {LABELS.map((l, i) => (
          <span key={l} style={{
            fontFamily: MONO, fontWeight: 700, fontSize: 18, letterSpacing: 2, padding: "7px 14px", borderRadius: 3,
            color: i === b ? C.bg : C.dim, background: i === b ? C.amber : "transparent", border: `1.5px solid ${i === b ? C.amber : C.line}`,
          }}>{i + 1} · {l}</span>
        ))}
      </div>
      <div key={b} style={{ position: "absolute", inset: 0, opacity: lin(qq, 0, 0.05), transform: `translateX(${lerp(50, 0, seg(qq, 0, 0.07))}px)` }}>
        <Slot file={FILES[b]} x={40} y={150} w={1000} h={562} />
        <Slot file="cam_audit.mp4" x={40} y={726} w={338} h={190} />
        {b === 0 && <Blocked q={qq} />}
        {b === 1 && <SwitchOff q={qq} />}
        {b === 2 && <TimeTravel q={qq} />}
      </div>
    </Radar>
  );
};
