// Scene 4: the catch. Left: rec_safety_on.mp4 slot. Right: five animated beats (a)-(e).
// Stream ids, document fields, the rules file excerpt, rule ids and the owning controller all come from data.json.
import React from "react";
import {
  AudioSlot, Box, C, Chip, Cursor, Doc, MONO, Radar, SANS, SceneSpec, Slot, Wave, coll, data, lerp, lin, nm, script, seg, typed, useScene,
} from "../ui";

// beat boundaries as fractions of the scene, so retiming the scene scales every beat
const BEATS = [0, 0.19, 0.35, 0.62, 0.86, 1];
const STEPS = ["ISOLATE", "STORE", "RULES", "GATE", "SPEAK"];
const X = 980, W = 900, Y = 200;

const Enter: React.FC<{ q: number; children: React.ReactNode }> = ({ q, children }) => (
  <div style={{ position: "absolute", inset: 0, opacity: lin(q, 0, 0.06), transform: `translateX(${lerp(60, 0, seg(q, 0, 0.08))}px)` }}>{children}</div>
);

const BeatA: React.FC<{ q: number; transcript: string }> = ({ q, transcript }) => {
  const focus = data.world.stream;
  const others = data.world.streams.filter((s) => s.id !== focus?.id).slice(0, 2);
  const rows = [focus ?? { id: "[PLACEHOLDER stream]", name: "" }, ...others];
  const clean = seg(q, 0.18, 0.42);
  return (
    <Enter q={q}>
      {rows.map((s, i) => (
        <div key={s.id} style={{ position: "absolute", left: X, top: Y + i * 150, width: W }}>
          <div style={{ fontFamily: MONO, fontSize: 20, color: i === 0 ? C.amber : C.dim, marginBottom: 6 }}>
            stream {s.id} <span style={{ color: C.dim }}>· {s.name}</span>
          </div>
          <Wave seed={`a-${i}`} w={W} h={92} bars={90} color={i === 0 ? (clean > 0.5 ? C.amber : C.dim) : C.dim} noise={i === 0 ? 0.9 * (1 - clean) : 0.85} amp={i === 0 ? 0.95 : 0.6} />
        </div>
      ))}
      <div style={{ position: "absolute", left: X + 470, top: Y - 8, opacity: lin(q, 0.2, 0.26) }}>
        <Chip color={C.cyan} size={22}>ElevenLabs voice isolation</Chip>
      </div>
      <Box x={X} y={Y + 470} w={W} h={200} color={C.amber} opacity={lin(q, 0.4, 0.45)}>
        <div style={{ padding: "22px 26px" }}>
          <Chip color={C.amber} size={20} fill>{focus?.id ?? "[PLACEHOLDER stream]"}</Chip>
          <div style={{ fontFamily: MONO, fontSize: 30, lineHeight: 1.4, color: C.text, marginTop: 16 }}>
            {typed(transcript, lin(q, 0.45, 0.92))}<Cursor />
          </div>
        </div>
      </Box>
    </Enter>
  );
};

const BeatB: React.FC<{ q: number }> = ({ q }) => {
  const fly = seg(q, 0.62, 0.86);
  const m = data.models.transmission;
  return (
    <Enter q={q}>
      <Box x={X + 560} y={Y + 520} w={340} h={150} color={C.green} label="MONGODB ATLAS" glow={0.9 * lin(q, 0.84, 0.9) * (1 - lin(q, 0.95, 1) * 0.5)}>
        <div style={{ padding: "50px 0 0", textAlign: "center" }}><Chip color={C.green} size={28}>{coll(m.collection)}</Chip></div>
      </Box>
      <div style={{
        position: "absolute", left: X, top: Y, width: 760, transformOrigin: "100% 100%",
        transform: `translate(${lerp(0, 60, fly)}px, ${lerp(0, 150, fly)}px) scale(${lerp(1, 0.12, fly)})`, opacity: 1 - lin(q, 0.84, 0.88),
      }}>
        <Box x={0} y={0} w={760} color={C.cyan} label="transmission document" glow={0.5 * fly} style={{ position: "relative" }}>
          <div style={{ padding: "26px 26px 20px" }}><Doc model={m} k={lin(q, 0.04, 0.5)} size={25} /></div>
        </Box>
      </div>
      <div style={{ position: "absolute", left: X, top: Y + 610, fontFamily: MONO, fontSize: 21, color: C.dim, opacity: lin(q, 0.1, 0.2) }}>
        fields: {m.source ?? "[PLACEHOLDER]"}
      </div>
    </Enter>
  );
};

const BeatC: React.FC<{ q: number }> = ({ q }) => {
  const r = data.rules;
  const LH = 31, ROWS = 13;
  let last = r.highlight;
  for (let i = r.highlight; i < Math.min(r.lines.length, r.highlight + 7); i++) if (r.lines[i].includes("append")) { last = i; break; }
  const scroll = Math.max(0, r.highlight - 5) * LH * seg(q, 0.12, 0.4);
  const hl = lin(q, 0.4, 0.45);
  const pulse = lin(q, 0.04, 0.2);
  return (
    <Enter q={q}>
      <div style={{ position: "absolute", left: X, top: Y }}><Chip color={C.green} size={24}>{coll("transmissions")}</Chip></div>
      <svg width={1920} height={1080} style={{ position: "absolute", left: 0, top: 0 }}>
        <line x1={X + 250} y1={Y + 22} x2={X + 600} y2={Y + 22} stroke={C.cyan} strokeWidth={2.5} strokeDasharray="10 8" opacity={0.7} />
        <circle cx={lerp(X + 250, X + 600, pulse)} cy={Y + 22} r={11} fill={C.cyan} style={{ filter: `drop-shadow(0 0 10px ${C.cyan})` }} />
      </svg>
      <div style={{ position: "absolute", left: X + 330, top: Y - 14, fontFamily: MONO, fontSize: 18, color: C.cyan }}>change stream</div>
      <Box x={X + 610} y={Y - 14} w={290} h={72} color={C.red} glow={pulse >= 1 ? 0.5 : 0}>
        <div style={{ textAlign: "center", lineHeight: "70px", fontFamily: SANS, fontWeight: 800, fontSize: 28 }}>Safety agent</div>
      </Box>
      <Box x={X} y={Y + 96} w={W} h={ROWS * LH + 62} color={C.dim} label={r.source ?? "[PLACEHOLDER rules file]"}>
        {r.status === "missing" ? (
          <div style={{ padding: 40, fontFamily: MONO, fontSize: 26, color: C.red }}>[PLACEHOLDER: rules file not found in repo]</div>
        ) : (
          <div style={{ position: "absolute", left: 0, right: 0, top: 30, height: ROWS * LH, overflow: "hidden" }}>
            <div style={{ transform: `translateY(${-scroll}px)` }}>
              {r.lines.map((line, i) => {
                const on = i >= r.highlight && i <= last;
                return (
                  <div key={i} style={{
                    height: LH, lineHeight: `${LH}px`, fontFamily: MONO, fontSize: 21, whiteSpace: "pre", overflow: "hidden",
                    color: on && hl ? "#fff" : C.dim, background: on ? `rgba(255,176,32,${0.26 * hl})` : undefined,
                    borderLeft: on ? `5px solid rgba(255,176,32,${hl})` : "5px solid transparent",
                  }}>
                    <span style={{ display: "inline-block", width: 62, textAlign: "right", marginRight: 18, opacity: 0.5 }}>{r.first_line + i}</span>{line}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </Box>
      <div style={{ position: "absolute", left: X, top: Y + 590, width: W, display: "flex", alignItems: "center", gap: 20, opacity: lin(q, 0.46, 0.5) }}>
        <Chip color={C.red} size={22}>{nm(data.ids.rule_id)}</Chip>
      </div>
      <div style={{ position: "absolute", left: X, top: Y + 650, fontFamily: SANS, fontWeight: 800, fontSize: 40, color: C.text, opacity: lin(q, 0.5, 0.55) }}>
        Deterministic rules. <span style={{ color: C.amber }}>No LLM decides.</span>
      </div>
    </Enter>
  );
};

const BeatD: React.FC<{ q: number }> = ({ q }) => {
  const move = seg(q, 0.04, 0.34);
  const dec = seg(q, 0.36, 0.5);
  const draw = seg(q, 0.56, 0.76);
  const { lane, controller } = data.world;
  return (
    <Enter q={q}>
      <Box x={X + 330} y={Y - 10} w={120} h={250} color={q > 0.3 && q < 0.45 ? C.green : C.line} glow={q > 0.3 && q < 0.45 ? 0.6 : 0}>
        <div style={{ position: "absolute", top: 100, width: "100%", textAlign: "center", fontFamily: SANS, fontWeight: 800, fontSize: 30 }}>GATE</div>
        <div style={{ position: "absolute", bottom: 14, width: "100%", textAlign: "center", fontFamily: MONO, fontSize: 15, color: C.dim }}>policy first</div>
      </Box>
      <div style={{ position: "absolute", left: lerp(X, X + 180, move), top: Y + 60, opacity: 1 - lin(q, 0.34, 0.38) }}>
        <div style={{ fontFamily: MONO, fontSize: 18, color: C.red, marginBottom: 6 }}>{coll("intents_safety")}</div>
        <Chip color={C.red} size={30} fill>{nm(data.ids.action_stop)}</Chip>
      </div>
      <Box x={X + 480} y={Y - 10} w={420} color={C.red} label={coll("decisions")} opacity={dec} glow={0.4 * dec} style={{ transform: `translateX(${lerp(-40, 0, dec)}px)` }}>
        <div style={{ padding: "26px 22px 18px" }}><Doc model={data.models.decision} k={lin(q, 0.4, 0.56)} size={22} color={C.red} /></div>
      </Box>
      <svg width={1920} height={1080} style={{ position: "absolute", left: 0, top: 0 }}>
        <line x1={X + 690} y1={Y + 250} x2={lerp(X + 690, X + 520, draw)} y2={lerp(Y + 250, Y + 440, draw)} stroke={C.red} strokeWidth={4} />
        {draw >= 1 && <circle cx={X + 520} cy={Y + 440} r={9} fill={C.red} />}
      </svg>
      <Box x={X + 120} y={Y + 440} w={680} h={200} color={C.amber} label="OWNING CONTROLLER" opacity={lin(q, 0.5, 0.56)} glow={0.7 * lin(q, 0.76, 0.8)}>
        <div style={{ padding: "34px 30px" }}>
          <div style={{ fontFamily: SANS, fontWeight: 800, fontSize: 44, color: C.text }}>{controller?.name ?? "[PLACEHOLDER controller]"}</div>
          <div style={{ fontFamily: MONO, fontSize: 24, color: C.amber, marginTop: 14 }}>
            {controller?.id ?? "?"} <span style={{ color: C.dim }}>owns lane</span> {lane?.id ?? "?"} <span style={{ color: C.dim }}>({lane?.kind})</span>
          </div>
        </div>
      </Box>
    </Enter>
  );
};

const BeatE: React.FC<{ q: number }> = ({ q }) => (
  <Enter q={q}>
    <div style={{ position: "absolute", left: X, top: Y + 10, fontFamily: MONO, fontSize: 22, color: C.red }}>
      speaker → stream {data.world.stream?.id ?? "[PLACEHOLDER stream]"}
    </div>
    <div style={{ position: "absolute", left: X, top: Y + 70 }}>
      <Wave seed="spk" w={W} h={300} bars={70} color={C.red} amp={lin(q, 0.02, 0.1) * (1 - lin(q, 0.8, 0.95) * 0.8)} speed={1.6} />
    </div>
    <div style={{ position: "absolute", left: X, top: Y + 420, fontFamily: SANS, fontWeight: 800, fontSize: 72, color: C.text }}>Hold Short speaks.</div>
  </Enter>
);

export const Catch: React.FC<{ scene: SceneSpec }> = () => {
  const { p, frames } = useScene();
  const beat = Math.min(4, BEATS.findIndex((b, i) => p >= b && p < BEATS[i + 1]));
  const b = beat < 0 ? 4 : beat;
  const q = lin(p, BEATS[b], BEATS[b + 1]);
  const transcript = script.scenes[0].cards?.[1]?.text ?? "[PLACEHOLDER transcript]";
  return (
    <Radar>
      <div style={{ position: "absolute", left: 40, top: 70, display: "flex", gap: 16, alignItems: "center" }}>
        <Chip color={C.green} size={26} fill>SAFETY ON</Chip>
        <span style={{ fontFamily: SANS, fontWeight: 800, fontSize: 44 }}>Same calls. This time it is caught.</span>
      </div>
      <Slot file="rec_safety_on.mp4" x={40} y={200} w={880} h={495} />

      <div style={{ position: "absolute", left: X, top: 110, width: W, display: "flex", gap: 10 }}>
        {STEPS.map((s, i) => (
          <div key={s} style={{
            flex: 1, textAlign: "center", fontFamily: MONO, fontWeight: 700, fontSize: 18, letterSpacing: 2, padding: "8px 0",
            color: i === b ? C.bg : i < b ? C.text : C.dim, background: i === b ? C.amber : "transparent",
            border: `1.5px solid ${i <= b ? C.amber : C.line}`, borderRadius: 3,
          }}>{s}</div>
        ))}
      </div>

      {b === 0 && <BeatA q={q} transcript={transcript} />}
      {b === 1 && <BeatB q={q} />}
      {b === 2 && <BeatC q={q} />}
      {b === 3 && <BeatD q={q} />}
      {b === 4 && <BeatE q={q} />}
      <AudioSlot file="stop_spoken.wav" from={Math.round(BEATS[4] * frames)} x={X} y={Y + 560} show={b === 4} />
    </Radar>
  );
};
