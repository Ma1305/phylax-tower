// Pre-render step: reads the real repository files named in sources.json and the
// contents of assets/, and writes src/generated/data.json for the compositions.
// Nothing shown on screen as code is typed into a component; it all comes from here.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const videoDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(videoDir, "..");
const readJson = (p) => JSON.parse(fs.readFileSync(path.join(videoDir, p), "utf8"));
const sources = readJson("sources.json");
const assetsManifest = readJson("assets.json");
const script = readJson("script.json");

const open = []; // every placeholder still open, printed and written to data.json

const readRepo = (rel) => {
  const p = path.join(repoRoot, rel);
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8").replace(/\r\n/g, "\n") : null;
};
const firstExisting = (files) => {
  for (const f of files) {
    const text = readRepo(f);
    if (text !== null) return { file: f, text };
  }
  return null;
};
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const hasWord = (text, word) => new RegExp(`(?<![A-Za-z0-9_])${esc(word)}(?![A-Za-z0-9_])`).test(text);
const findIn = (files, word) => {
  for (const f of files) {
    const text = readRepo(f);
    if (text !== null && hasWord(text, word)) return f;
  }
  return null;
};
const lookup = (name, codeFiles, docFiles) => {
  const code = findIn(codeFiles, name);
  if (code) return { name, status: "code", source: code };
  const doc = findIn(docFiles, name);
  if (doc) return { name, status: "docs", source: doc };
  return { name, status: "missing", source: null };
};

// ---------------------------------------------------------------- assets
const assets = {};
for (const a of assetsManifest) {
  const present = fs.existsSync(path.join(videoDir, "assets", a.file));
  assets[a.file] = { ...a, present };
  if (!present) open.push(`asset ${a.file}${a.optional ? " (optional)" : ""}: ${a.contains}`);
}

// ---------------------------------------------------------------- config placeholders
const cfg = script.config;
for (const [k, v] of Object.entries(cfg)) {
  const vals = Array.isArray(v) ? v : [v];
  if (vals.some((x) => typeof x === "string" && /^\[.*\]$/.test(x))) open.push(`script.json config.${k} is still a placeholder`);
}

// ---------------------------------------------------------------- world
let world = { status: "missing", source: null, streams: [], stream: null, lane: null, controller: null };
const w = firstExisting(sources.world.files);
if (w) {
  const j = JSON.parse(w.text);
  const lane = (j.lanes || []).find((l) => (l.designators || []).includes(sources.world.runway_designator));
  const controller = lane && (j.controllers || []).find((c) => (c.lanes || []).includes(lane._id));
  const streams = (j.streams || []).map((s) => ({ id: s._id, name: s.name, controller_id: s.controller_id }));
  const stream = controller && streams.find((s) => s.controller_id === controller._id);
  world = {
    status: "code", source: w.file, streams,
    stream: stream || null,
    lane: lane ? { id: lane._id, name: lane.name, kind: lane.kind } : null,
    controller: controller ? { id: controller._id, name: controller.name, role: controller.role } : null,
  };
}
if (!world.stream || !world.lane || !world.controller) open.push(`world file (${sources.world.files.join(" | ")}): stream / lane / owning controller not found`);

// ---------------------------------------------------------------- collections
const collections = sources.collections.wanted.map((name) => {
  let r = lookup(name, sources.collections.code_files, sources.collections.doc_files);
  const m = /^intents_([a-z]+)$/.exec(name);
  if (r.status !== "code" && m) {
    // setup builds these names as f"intents_{agent}" from the agent list in contracts
    const setup = sources.collections.code_files.map(readRepo).filter(Boolean).join("\n");
    if (/intents_\{/.test(setup) && new RegExp(`"${m[1]}"`).test(setup)) {
      r = { name, status: "code", source: sources.collections.code_files.join(" + ") };
    }
  }
  return r;
});

// ---------------------------------------------------------------- identifiers
const ids = {};
for (const [key, spec] of Object.entries(sources.identifiers)) {
  ids[key] = lookup(spec.name, spec.code_files, spec.doc_files);
}

// ---------------------------------------------------------------- models
const tokens = {
  $stream_id: world.stream?.id, $lane_id: world.lane?.id, $controller_id: world.controller?.id,
  $rule_id: ids.rule_id.status === "missing" ? undefined : ids.rule_id.name,
  $card2: script.scenes[0].cards?.[1]?.text,
};
const resolve = (v) => (typeof v === "string" && v.startsWith("$") ? tokens[v] : v);
const classFields = (text, cls) => {
  const m = new RegExp(`^class ${esc(cls)}\\(.*?\\):\\n([\\s\\S]*?)(?=^\\S|\\Z)`, "m").exec(text + "\n");
  if (!m) return null;
  const out = [];
  for (const line of m[1].split("\n")) {
    const f = /^ {4}([a-z_][a-z0-9_]*): /.exec(line);
    if (!f || f[1] === "model_config") continue;
    const alias = /alias="([a-z_]+)"/.exec(line);
    out.push(alias ? alias[1] : f[1]);
  }
  return out;
};
const models = {};
for (const [key, spec] of Object.entries(sources.models)) {
  const src = firstExisting(spec.files);
  const fields = src ? classFields(src.text, spec.class) : null;
  const coll = collections.find((c) => c.name === spec.collection) ||
    lookup(spec.collection, sources.collections.code_files, sources.collections.doc_files);
  let status, source, names;
  if (fields) {
    status = "code"; source = `${src.file} (class ${spec.class})`;
    names = fields.filter((f) => f in spec.sample);
  } else {
    const docs = sources.collections.doc_files.map(readRepo).filter(Boolean).join("\n");
    names = coll.status === "missing" ? [] : Object.keys(spec.sample).filter((f) => hasWord(docs, f));
    status = names.length ? "docs" : "missing"; source = names.length ? sources.collections.doc_files[0] : null;
  }
  const rows = names
    .map((f) => [f, resolve(spec.sample[f])])
    .filter(([, v]) => v !== undefined)
    .slice(0, spec.max_fields)
    .map(([f, v]) => [f, JSON.stringify(v)]);
  models[key] = { collection: spec.collection, collection_status: coll.status, status, source, rows };
}

// ---------------------------------------------------------------- rules file excerpt
let rules = { status: "missing", source: null, lines: [], first_line: 0, highlight: -1 };
const rf = firstExisting(sources.rules_file.files);
if (rf) {
  const all = rf.text.split("\n");
  let hit = -1;
  for (const pat of sources.rules_file.highlight) {
    hit = all.findIndex((l) => l.includes(pat));
    if (hit >= 0) break;
  }
  if (hit >= 0) {
    const from = Math.max(0, hit - sources.rules_file.lines_before);
    const to = Math.min(all.length, hit + sources.rules_file.lines_after + 1);
    rules = { status: "code", source: rf.file, lines: all.slice(from, to), first_line: from + 1, highlight: hit - from };
  }
}
if (rules.status === "missing") open.push(`rules file (${sources.rules_file.files.join(" | ")}): highlighted rule not found`);

for (const c of collections) if (c.status !== "code") open.push(`collection ${c.name}: ${c.status === "docs" ? `only in ${c.source}, not in code yet` : "not found, shown as placeholder"}`);
for (const [k, v] of Object.entries(ids)) if (v.status !== "code") open.push(`identifier ${v.name} (${k}): ${v.status === "docs" ? `only in ${v.source}, not in code yet` : "not found, shown as placeholder"}`);
for (const [k, v] of Object.entries(models)) if (v.status !== "code") open.push(`document shape ${k} (${v.collection}): ${v.status === "docs" ? `fields from ${v.source}, no model class in code yet` : "not found, shown as placeholder"}`);

const data = { assets, world, collections, ids, models, rules, open };
fs.mkdirSync(path.join(videoDir, "src", "generated"), { recursive: true });
fs.writeFileSync(path.join(videoDir, "src", "generated", "data.json"), JSON.stringify(data, null, 2));
fs.mkdirSync(path.join(videoDir, "out"), { recursive: true });

console.log(`prepare: wrote src/generated/data.json from ${repoRoot}`);
console.log(open.length ? `prepare: ${open.length} placeholder(s) still open:` : "prepare: no placeholders open");
for (const o of open) console.log(`  - ${o}`);
