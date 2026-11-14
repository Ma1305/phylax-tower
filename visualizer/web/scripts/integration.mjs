// The visualizer against the REAL tower (not seeded data): scripts/viz_integration.sh starts
// the stack on a throwaway database and runs this. It drives the web app with Playwright while
// tools/smoke_feed.py (scripts/feed.sh) feeds the tower through /ingest/*.
// Usage: node scripts/integration.mjs <webUrl> <vizApiUrl> <towerApiUrl>
// Screenshots of every step go to docs/v2/screens/.
import { spawn } from 'node:child_process';
import { chromium } from 'playwright';

const [web, viz, tower] = process.argv.slice(2);
const root = new URL('../../../', import.meta.url).pathname;
const out = `${root}docs/v2/screens/`;
const results = [];
const check = (name, ok, extra = '') => {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `  (${String(extra).slice(0, 160)})` : ''}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (base, path) => (await fetch(`${base}${path}`)).json();
const snapshot = (t) => api(viz, `/viz/snapshot?tower_id=LGA${t ? `&t=${encodeURIComponent(t)}` : ''}`);
const towerDecisions = async () => (await api(tower, '/control/decisions?tower_id=LGA&limit=200')).decisions;
async function until(fn, ms = 60000, every = 400) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() > end) return null;
    await sleep(every);
  }
}
function feed(scenario) {
  let log = '';
  const p = spawn(`${root}scripts/feed.sh`, [scenario], { cwd: root, env: { ...process.env, TOWER_API: tower, VOICE_ENABLED: 'false' } });
  p.stdout.on('data', (b) => (log += b));
  p.stderr.on('data', (b) => (log += b));
  const done = new Promise((r) => p.on('exit', r));
  return { done, log: () => log };
}
const meters = (a, b) => Math.hypot((a[0] - b[0]) * 84_400, (a[1] - b[1]) * 111_320);

const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const pageErrors = [];
const seen = { urls: [], posts: [] };
page.on('pageerror', (e) => pageErrors.push(String(e)));
page.on('response', (r) => seen.urls.push({ url: r.url(), status: r.status() }));
page.on('request', (r) => r.method() === 'POST' && seen.posts.push({ url: r.url(), body: r.postData() }));
const shot = (name) => page.screenshot({ path: `${out}${name}.png` });
const count = (sel) => page.locator(sel).count();
const text = async (sel) => ((await count(sel)) ? (await page.locator(sel).first().innerText()).replace(/\s+/g, ' ') : '');
const view = async (name) => { await page.locator('.ctl-switch button', { hasText: name }).first().click(); await page.waitForTimeout(350); };
const actAs = (id) => page.locator('.actas select').selectOption(id);
const objPos = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.map svg .obj')].map((g) => {
  const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(g.style.transform) || [];
  return [(g.querySelector('.obj-name')?.textContent || '').trim(), [Number(m[1]), Number(m[2])]];
})));
const logOutcomes = async () => new Set(await page.locator('.audit .trail').evaluateAll((els) => els.map((e) => e.dataset.outcome)));
async function scrubTo(tMs) {
  const tr = page.locator('.tl-track');
  const box = await tr.boundingBox();
  const lo = Number(await tr.getAttribute('aria-valuemin'));
  const hi = Number(await tr.getAttribute('aria-valuemax'));
  const x = box.x + box.width * Math.max(0, Math.min(0.999, (tMs - lo) / (hi - lo)));
  await page.mouse.move(x, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(x + 0.5, box.y + box.height / 2);
  await page.mouse.up();
  await page.waitForSelector('.app.mode-history');
  await page.waitForTimeout(1800);
}

// ------------------------------------------------------------------ 0. open, agent identities
await page.goto(web);
await page.waitForSelector('.map svg');
await page.waitForSelector('.agents .agent');
check('web app loads the world from the visualizer API (not mock)', seen.urls.some((u) => u.url.startsWith(`${viz}/viz/world`) && u.status === 200) && (await count('.pill.mock')) === 0);
await page.waitForSelector('.agents .ident', { timeout: 8000 }).catch(() => {});
const idents = (await page.locator('.agents .ident').allInnerTexts()).join(' | ');
check('agent switches show each agent identity from agent_state (generated Atlas user / static / shared)', /Atlas user hs-safety-/.test(idents) && /static Atlas user/.test(idents) && /shared connection/.test(idents), idents);
await shot('00-agent-identities');

// ------------------------------------------------------------------ 1. incident, live map
const run1 = feed('incident');
await page.waitForSelector('.map svg .obj', { timeout: 60000 });
await page.getByRole('button', { name: /Approach/ }).click();
const onFinal = await until(async () => (await snapshot()).objects.find((o) => o._id === 'AC8646' && o.status === 'airborne'), 20000);
await page.waitForTimeout(1200);
const approachObjs = await objPos();
check('approach view shows the aircraft on final', !!onFinal && (await count('.map svg .final-line')) >= 1 && 'AC8646' in approachObjs && /AC8646/.test(await text('.map-air')), `alt ${onFinal?.alt_ft} ft · ${await text('.map-air')}`);
await shot('01-approach-aircraft-on-final');
await page.getByRole('button', { name: 'Airfield' }).click();
await page.waitForTimeout(900);

const statesSeen = new Set();
const sample = async () => (await page.locator('.map svg .lane-label').evaluateAll((els) => els.map((e) => e.getAttribute('class')))).forEach((c) => statesSeen.add(c));
const p0 = await objPos();
await sample();
// t0: runway 04 reserved for the landing, before the crossing is cleared (so before the conflict)
const reserved = await until(async () => { await sample(); const s = await snapshot(); return s.lane_status.find((l) => l._id === 'RWY_04_22' && l.state === 'reserved') ? s : null; }, 60000, 300);
const t0 = reserved ? Date.parse(reserved.t) : Date.now();
const live0 = { dom: await objPos(), api: Object.fromEntries((reserved?.objects ?? []).map((o) => [o._id, o.position.coordinates])) };
await shot('02-live-runway-reserved');

// ------------------------------------------------------------------ 2. the autonomous stop
await page.waitForSelector('.stops .stop', { timeout: 90000 }).catch(() => {});
const tStop = Date.now();
await sample();
const stopText = await text('.stops .stop');
const dStop = (await towerDecisions()).find((d) => d.intent_action === 'stop' && d.outcome === 'executed');
check('autonomous stop appears at once as an alert (told, not asked)', /STOP IN EFFECT/.test(stopText) && /not a request/.test(stopText) && dStop && tStop - Date.parse(dStop.t) < 6000, `${dStop ? tStop - Date.parse(dStop.t) : '?'} ms after the decision`);
check('stop alert carries the exact call, the rule and "Lift stop"', dStop && stopText.includes(dStop.text) && stopText.includes('no_crossing_during_active_landing') && (await page.locator('.stops .stop').first().getByRole('button').allInnerTexts()).join('|') === 'Lift stop', stopText);
check('the stop executed without a controller (tier_autonomous, approved_by empty)', dStop?.rule === 'tier_autonomous' && !dStop?.approved_by && dStop?.controller_id === 'ctl_local');
await shot('03-autonomous-stop-alert');
const p1 = await objPos();
const moved = Object.keys(p0).filter((k) => p1[k] && Math.hypot(p1[k][0] - p0[k][0], p1[k][1] - p0[k][1]) > 3);
check('objects move on the map', moved.length >= 1, `moved: ${moved.join(', ')}`);
const states = [...statesSeen].join(' ');
check('lanes change state on the map (clear/occupied -> reserved -> conflict)', /st-reserved/.test(states) && /st-conflict/.test(states) && /st-(clear|occupied)/.test(states), [...new Set(states.match(/st-\w+/g))].join(','));

await page.waitForSelector('.line.synthetic', { timeout: 20000 }).catch(() => {});
const syn = await text('.line.synthetic');
check("the stop's outbound call is in its stream, marked as Phylax's own", /PHYLAX · OUTBOUND/.test(syn) && dStop && syn.includes(dStop.text) && /PHYLAX/.test(syn), syn);

// ------------------------------------------------------------------ 3. streams, transcripts, audio
check('right bar shows three streams with transcripts', (await count('.radio .stream')) === 3 && (await count('.radio .line')) >= 6 && /cleared to land/i.test(await text('.radio')), `${await count('.radio .line')} lines`);
const play = page.locator('.radio .line button.play').first();
const clip = await play.evaluate((b) => b.closest('.line').dataset.clip);
const respP = page.waitForResponse((r) => r.url().includes(`/viz/audio/${clip}`), { timeout: 10000 }).catch(() => null);
await play.click();
const resp = await respP;
const playing = await page.waitForFunction(() => { const a = document.querySelector('audio'); return a && !a.paused && a.currentTime > 0; }, null, { timeout: 8000 }).then(() => true, () => false);
const bytes = (await (await fetch(`${viz}/viz/audio/${clip}`)).arrayBuffer()).byteLength;
check('audio is playable: /viz/audio returns audio bytes and the element plays', resp && [200, 206].includes(resp.status()) && (resp.headers()['content-type'] || '').startsWith('audio/') && bytes > 1000 && playing, `${resp?.status()} ${resp?.headers()['content-type']} ${bytes} bytes, playing ${playing}`);
await shot('04-streams-transcripts-audio');

// ------------------------------------------------------------------ 4. the truck holds
await run1.done;
await sleep(1500);
const afterRun = await snapshot();
const truck = afterRun.objects.find((o) => o._id === 'TRUCK1');
const stops1 = (await towerDecisions()).filter((d) => d.intent_action === 'stop');
check('the truck holds: it obeyed the stop and never entered the runway', /STOP for TRUCK1/.test(run1.log()) && truck && truck.lane_id !== 'RWY_04_22' && stops1.length === 1, `TRUCK1 on ${truck?.lane_id}; ${stops1.length} stop decision(s); ${run1.log().split('\n').find((l) => /STOP for/.test(l)) ?? run1.log().slice(-200)}`);

// ------------------------------------------------------------------ 5. controller filter
const reqsBefore = seen.urls.length;
await view('Local');
check('"Local (Tower)" narrows the streams and the requests', (await count('.radio .stream')) === 1 && /118\.700|Tower/.test(await text('.radio .stream')) && (await count('.map svg .lane-owned')) === 2 && (await count('.stops .stop')) === 1 && (await count('.requests .req')) === 1, `${await count('.radio .stream')} stream, ${await count('.requests .req')} request, ${await count('.stops .stop')} stop`);
check('the stop alert is shown to the owning controller (Local owns runway 04)', /told Local \(Tower\)/i.test(await text('.stops .stop')));
await shot('05-view-as-local-tower');
await view('Ground');
check('Ground: two streams, no stop and no request (runway 04 is not theirs)', (await count('.radio .stream')) === 2 && (await count('.stops .stop')) === 0 && (await count('.requests .req')) === 0);
await view('All');
void reqsBefore;

// ------------------------------------------------------------------ 6. the proposal: 403 for Ground, Local approves, spoken
const req = page.locator('.requests .req').first();
const reqText = await text('.requests .req');
const dProp = (await towerDecisions()).find((d) => d.intent_action === 'propose_instruction' && d.outcome === 'pending_approval');
check('the proposal is pending for the lane owner', /PROPOSED INSTRUCTION/.test(reqText) && /Routed to Local \(Tower\)/.test(reqText) && dProp && reqText.includes(dProp.text), reqText);
await actAs('ctl_ground');
await req.getByRole('button', { name: /Approve/ }).click();
await page.waitForSelector('.requests .req-error', { timeout: 8000 }).catch(() => {});
const err403 = await text('.requests .req-error');
check('Ground approving it shows the 403 message', /^403/.test(err403) && /not_lane_owner/.test(err403) && seen.urls.some((u) => u.url.endsWith('/control/approve') && u.status === 403), err403);
await shot('06-ground-approve-403');
await actAs('ctl_local');
await req.getByRole('button', { name: /Approve/ }).click();
const spoken = dProp && (await until(async () => (await snapshot()).transmissions.find((t) => t.synthetic && t.decision_id && t.transcript === dProp.text), 25000));
await page.waitForFunction((t) => [...document.querySelectorAll('.line.synthetic')].some((l) => l.textContent.includes(t)), dProp?.text ?? '', { timeout: 8000 }).catch(() => {});
const approved = (await towerDecisions()).find((d) => d.intent_id === dProp?.intent_id && d.outcome === 'executed');
const spokenBytes = spoken?.clip_id ? (await (await fetch(`${viz}/viz/audio/${spoken.clip_id}`)).arrayBuffer()).byteLength : 0;
check('Local approving works (controller_approved, approved_by ctl_local)', approved?.rule === 'controller_approved' && approved?.approved_by === 'ctl_local' && (await count('.requests .req')) === 0);
check('the approved instruction is spoken: outbound call with audio in the Ground stream', !!spoken && spoken.stream_id === 'LGA_GND' && spokenBytes > 1000 && (await page.locator('.line.synthetic', { hasText: dProp?.text ?? '§' }).count()) === 1, `${spokenBytes} bytes on ${spoken?.stream_id}`);
await shot('07-local-approved-and-spoken');

// ------------------------------------------------------------------ 7. ask the audit agent
await page.locator('#askaudit-q').fill('why was TRUCK1 held?');
await page.locator('.askaudit button', { hasText: 'Ask' }).click();
await page.waitForSelector('.askaudit-answer', { timeout: 90000 }).catch(() => {});
const narrative = await text('.askaudit-narrative');
const nItems = await count('.askaudit-timeline li');
const nCited = await count('.askaudit-timeline li.cited');
check('"ask the audit agent" answers "why was TRUCK1 held?" with a cited timeline', /TRUCK1/.test(narrative) && nItems >= 5 && nCited >= 1 && /\[[0-9a-f]{24}\]/.test(narrative) && seen.urls.some((u) => u.url.includes('/control/audit?q=') && u.status === 200), `${nItems} entries, ${nCited} cited · ${narrative.slice(0, 90)}`);
await page.locator('.askaudit').scrollIntoViewIfNeeded();
await shot('08-audit-agent-answer');

// ------------------------------------------------------------------ 8. lift the stop, then needs_approval
await page.locator('.stops .stop').first().getByRole('button', { name: 'Lift stop' }).click();
await page.waitForSelector('.stops .stop', { state: 'detached', timeout: 10000 }).catch(() => {});
check('Lift stop (as Local) removes the alert; the log records it as revoked', (await count('.stops .stop')) === 0 && (await towerDecisions()).some((d) => d.outcome === 'revoked' && d.rule === 'controller_revoked' && d.revokes === dStop?._id && d.approved_by === 'ctl_local'));

await actAs('ctl_sup');
await page.locator('select[aria-label="safety tier"]').selectOption('needs_approval');
await page.locator('button[aria-label="procedures enabled"]').click(); // off: its next intent is refused by the gate
const switched = await until(async () => { const p = (await snapshot()).permissions; return p.find((x) => x._id === 'safety')?.tier === 'needs_approval' && p.find((x) => x._id === 'procedures')?.enabled === false; }, 10000);
await page.waitForTimeout(500);
check('agent switches: safety set to needs_approval, procedures switched off (by Supervisor)', !!switched && (await page.locator('select[aria-label="safety tier"]').inputValue()) === 'needs_approval' && (await count('.agents .req-error')) === 0, await text('.agents .req-error'));
await actAs('ctl_local');
const tRun2 = Date.now();
const run2 = feed('incident');
await page.waitForSelector('.requests .req', { timeout: 90000 }).catch(() => {});
await page.waitForTimeout(4000);
const pend = await text('.requests .req');
const run2Stops = async () => (await towerDecisions()).filter((d) => d.intent_action === 'stop' && Date.parse(d.t) > tRun2);
const before = await run2Stops();
check('needs_approval: the stop stays pending, nothing is in effect and nothing was spoken', /STOP · NEEDS YOUR APPROVAL/.test(pend) && (await count('.stops .stop')) === 0 && before.length >= 1 && before.every((d) => d.outcome === 'pending_approval') && !(await snapshot()).transmissions.some((t) => t.synthetic && Date.parse(t.t) > tRun2), pend);
await shot('09-needs-approval-stop-pending');
await page.locator('.requests .req', { hasText: 'STOP' }).first().getByRole('button', { name: /Approve/ }).click();
await page.waitForSelector('.stops .stop', { timeout: 15000 }).catch(() => {});
const stop2 = await text('.stops .stop');
check('approving it puts the stop in effect, approved by Local (Tower)', /STOP IN EFFECT/.test(stop2) && /approved by Local \(Tower\)/.test(stop2) && (await run2Stops()).some((d) => d.outcome === 'executed' && d.approved_by === 'ctl_local'), stop2);
await shot('10-stop-approved-in-effect');
await run2.done;
const refused = await until(async () => (await towerDecisions()).find((d) => d.outcome === 'rejected'), 30000);
check('a switched-off agent is refused by the gate (rejected / agent_disabled)', refused?.rule === 'agent_disabled' && refused?.agent === 'procedures', `${refused?.agent} ${refused?.rule}`);
// reject whatever is still pending, so the log also shows a controller rejection
if (await count('.requests .req')) await page.locator('.requests .req').first().getByRole('button', { name: /Reject/ }).click();

// ------------------------------------------------------------------ 9. switches back (mode and tier)
await actAs('ctl_sup');
await page.locator('[aria-label="weather mode"] button', { hasText: 'reactive' }).click();
const reactive = await until(async () => (await snapshot()).permissions.find((x) => x._id === 'weather')?.mode === 'reactive', 8000);
await page.locator('[aria-label="weather mode"] button', { hasText: 'proactive' }).click();
await page.locator('select[aria-label="safety tier"]').selectOption('autonomous');
await page.locator('button[aria-label="procedures enabled"]').click();
const restored = await until(async () => { const p = (await snapshot()).permissions; return p.find((x) => x._id === 'safety')?.tier === 'autonomous' && p.find((x) => x._id === 'procedures')?.enabled === true && p.find((x) => x._id === 'weather')?.mode === 'proactive'; }, 10000);
check('switching reactive <-> proactive and needs_approval <-> autonomous works from the agent switches', !!reactive && !!restored);

// ------------------------------------------------------------------ 10. decision log, live and history
await page.waitForTimeout(1500);
const all = await towerDecisions();
const want = ['executed', 'pending_approval', 'rejected', 'revoked'];
const liveOut = await logOutcomes();
const liveLog = await text('.audit');
check('decision log (live) lists every decision: executed, pending, rejected, revoked', want.every((o) => liveOut.has(o)) && (await count('.audit .trail')) === Math.min(40, all.length), `${await count('.audit .trail')} of ${all.length}: ${[...liveOut].join(',')}`);
check('each log line has rule, authorized_by and approved_by', /rule no_crossing_during_active_landing · tier_autonomous/.test(liveLog) && /authorized by safety \/ normal_v1/.test(liveLog) && /approved by Local \(Tower\)/.test(liveLog) && /lifted by Local \(Tower\)/.test(liveLog) && /agent_disabled/.test(liveLog) && (await count('.audit .trail-meta')) === (await count('.audit .trail')));
await page.locator('.audit').scrollIntoViewIfNeeded();
await shot('11-decision-log-live');
await scrubTo(Date.now() - 2500);
const histOut = await logOutcomes();
check('decision log (history mode) lists the same decisions from the stored records', (await count('.app.mode-history')) === 1 && want.every((o) => histOut.has(o)) && /authorized by safety \/ normal_v1/.test(await text('.audit')), [...histOut].join(','));
await shot('12-decision-log-history');

// ------------------------------------------------------------------ 11. time travel to before the conflict
const nowDom = await objPos();
await scrubTo(t0);
const stamp = await text('.map-stamp');
const past = await snapshot(new Date(t0).toISOString());
const pastLane = past.lane_status.find((l) => l._id === 'RWY_04_22');
const histDom = await objPos();
const apiDrift = Object.entries(live0.api).map(([id, c]) => { const o = past.objects.find((x) => x._id === id); return o ? meters(o.position.coordinates, c) : 1e9; });
const domDrift = Object.entries(live0.dom).map(([id, c]) => (histDom[id] ? Math.hypot(histDom[id][0] - c[0], histDom[id][1] - c[1]) : 1e9));
// the live map eases towards each new position, so the aircraft on final (130 kt) was read a little late
const wentBack = Object.entries(nowDom).map(([id, c]) => (histDom[id] ? Math.hypot(histDom[id][0] - c[0], histDom[id][1] - c[1]) : 0));
check('history before the conflict: runway 04 is not in conflict and no stop is in effect', (await count('.lane-label.st-conflict')) === 0 && pastLane?.state === 'reserved' && (await count('.stops .stop')) === 0 && (await count('.audit .trail')) === 0 && /NOT LIVE/.test(await text('.history-banner')), `${stamp} · RWY 04 ${pastLane?.state}`);
check('history before the conflict: the objects are where they were', apiDrift.length >= 2 && Math.max(...apiDrift) < 150 && domDrift.length >= 2 && Math.max(...domDrift) < 70 && Math.max(...wentBack) > 100, `went back ${wentBack.map((d) => `${d.toFixed(0)} px`).join(', ')} from where they are now; stored vs seen live: ${apiDrift.map((d) => `${d.toFixed(0)} m`).join(', ')}; on the map: ${domDrift.map((d) => `${d.toFixed(0)} px`).join(', ')}`);
check('history asks /viz/snapshot?t= and /viz/tracks', seen.urls.some((u) => /\/viz\/snapshot\?.*\bt=/.test(u.url) && u.status === 200) && seen.urls.some((u) => u.url.includes('/viz/tracks?') && u.status === 200));
await shot('13-history-before-conflict');
await page.locator('.timeline .live-btn').click();
await page.waitForSelector('.app.mode-live');
await page.waitForTimeout(800);
check('LIVE returns to now', (await count('.history-banner')) === 0 && (await count('.pill.live')) === 1 && (await count('.stops .stop')) === 1);
await shot('14-back-to-live');

check('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
