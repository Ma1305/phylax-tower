// The paired check: the built web app against the seeded visualizer API (not mock mode).
// Usage: node scripts/pair.mjs <webUrl> <vizApiUrl> [--shots]
//   --shots also writes the review screenshots to docs/.
// scripts/poke.py changes the scratch database (MONGODB_DB) to exercise the live WebSocket.
import { execFileSync } from 'node:child_process';
import { chromium } from 'playwright';

const [web, api] = process.argv.slice(2);
const shots = process.argv.includes('--shots');
const out = new URL('../docs/', import.meta.url).pathname;
const here = new URL('.', import.meta.url).pathname;
const poke = (what) => execFileSync('uv', ['run', '--project', `${here}../../api`, 'python', `${here}poke.py`, what], { stdio: 'pipe' });

const results = [];
const check = (name, ok, extra = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `  (${extra})` : ''}`);
};
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
const pageErrors = [];

async function open(width = 1600, height = 950, query = '') {
  const page = await browser.newPage({ viewport: { width, height } });
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  const seen = { urls: [], ws: [], posts: [] };
  page.on('response', (r) => seen.urls.push({ url: r.url(), status: r.status() }));
  page.on('request', (r) => r.method() === 'POST' && seen.posts.push({ url: r.url(), body: r.postData() }));
  page.on('websocket', (ws) => ws.on('framereceived', (f) => { try { seen.ws.push(JSON.parse(f.payload).type); } catch { /* not json */ } }));
  await page.goto(`${web}/${query}`);
  await page.waitForSelector('.map svg');
  await page.waitForSelector('.obj');
  await page.waitForTimeout(700);
  return { page, seen };
}
const shot = async (page, name, opts = {}) => shots && page.screenshot({ path: `${out}${name}.png`, ...opts });
const count = (page, sel) => page.locator(sel).count();

// ------------------------------------------------------------------ world, map, snapshot
let { page, seen } = await open();
const world = await (await fetch(`${api}/viz/world?tower_id=LGA`)).json();
check('world loads from /viz/world (not mock)', seen.urls.some((u) => u.url.startsWith(`${api}/viz/world`) && u.status === 200) && (await count(page, '.pill.mock')) === 0);
check('map draws every lane of the world', (await count(page, '.map svg .lane-label')) === world.lanes.length, `${world.lanes.length} lanes`);
check('map draws every hold point', (await count(page, '.map svg rect.holdpoint')) === world.hold_points.length);
check('first WebSocket message is the snapshot', seen.ws[0] === 'snapshot');
check('objects come from the snapshot', (await count(page, '.map svg .obj')) === 6);
check('lane states come from the snapshot', (await count(page, '.lane-label.st-occupied')) >= 2 && (await count(page, '.lane-label.st-closed')) === 0);
check('LIVE pill shows the feed is open', (await page.locator('.pill.live').innerText()).includes('LIVE'));

// ------------------------------------------------------------------ autonomous stop: told, not asked
const stop = page.locator('.stops .stop');
const stopText = (await stop.first().innerText()).replace(/\s+/g, ' ');
check('executed stop is shown as an alert', (await stop.count()) === 1 && /STOP IN EFFECT/.test(stopText) && /not a request/.test(stopText));
check('stop alert has the exact call, rule and target', stopText.includes('TRUCK1, hold position. Traffic landing runway 04.') && stopText.includes('no_crossing_during_active_landing') && /Target TRUCK1/i.test(stopText), stopText.slice(0, 80));
check('stop alert has no Approve / Reject, only Lift stop', (await stop.getByRole('button').allInnerTexts()).join('|') === 'Lift stop');
const syn = page.locator('.line.synthetic', { hasText: 'TRUCK1, hold position' });
const synText = (await syn.innerText()).replace(/\s+/g, ' ');
check("Phylax's own outbound call is marked in its stream", /PHYLAX · OUTBOUND/.test(synText) && /STOP · autonomous · in effect/.test(synText), synText.slice(0, 90));
check('pending holds only the proposal', (await count(page, '.requests .req')) === 1 && (await page.locator('.requests .req').innerText()).includes('PROPOSED INSTRUCTION'));
await shot(page, '01-live');

await stop.getByRole('button', { name: 'Lift stop' }).click();
await page.waitForSelector('.stops .req-error');
const liftErr = await page.locator('.stops .req-error').innerText();
const revoke = seen.posts.find((p) => p.url.endsWith('/control/revoke'));
check('Lift stop posts /control/revoke {decision_id, controller_id}', !!revoke && /"decision_id":"[0-9a-f]{24}"/.test(revoke.body) && /"controller_id":"ctl_/.test(revoke.body), revoke?.body);
check('with no tower API the action fails with a clear inline error', /Stop not lifted/.test(liftErr), liftErr);
await page.locator('.requests .req').getByRole('button', { name: /Approve/ }).click();
await page.waitForSelector('.requests .req-error');
check('Approve fails inline too (tower API not running here)', /not reachable/.test(await page.locator('.requests .req-error').innerText()));
await shot(page, '02-action-error-inline');

// ------------------------------------------------------------------ decision log
const log = page.locator('.audit .trail');
const outcomes = new Set(await log.evaluateAll((els) => els.map((e) => e.dataset.outcome)));
check('decision log shows executed, pending, rejected and revoked', ['executed', 'pending_approval', 'rejected', 'revoked'].every((o) => outcomes.has(o)), [...outcomes].join(','));
const logText = (await page.locator('.audit').innerText()).replace(/\s+/g, ' ');
check('log lines carry rule, authorized_by and approved_by', /rule no_crossing_during_active_landing · tier_autonomous/.test(logText) && /authorized by safety \/ normal_v1/.test(logText) && /lifted by Ground/.test(logText) && /rejected by Ground/.test(logText) && /mode_reactive/.test(logText));

// ------------------------------------------------------------------ streams, transcripts, audio
check('right bar lists every stream', (await count(page, '.radio .stream')) === world.streams.length);
const lines = page.locator('.radio .line');
const nLines = await lines.count();
check('transcripts are listed', nLines === 11 && (await page.locator('.radio').innerText()).includes('cleared to land'), `${nLines} lines`);
let played = 0;
const plays = page.locator('.radio .line button.play');
const nPlay = await plays.count();
for (let i = 0; i < nPlay; i++) {
  const clip = await plays.nth(i).evaluate((b) => b.closest('.line').dataset.clip);
  const resp = page.waitForResponse((r) => r.url().includes(`/viz/audio/${clip}`));
  await plays.nth(i).click();
  const r = await resp;
  const type = r.headers()['content-type'] || '';
  const bytes = Number(r.headers()['content-length'] || 0);
  const ok = await page.waitForFunction((id) => { const a = document.querySelector('audio'); return a.src.endsWith(id) && !a.paused && a.currentTime > 0 && a.readyState >= 2; }, clip, { timeout: 8000 }).then(() => true, () => false);
  if ([200, 206].includes(r.status()) && type.startsWith('audio/') && bytes > 1000 && ok) played++;
  else console.log('   clip', clip, r.status(), type, bytes, 'playing:', ok);
}
check('every clip with audio really plays (/viz/audio returns audio bytes, element is playing)', nPlay === 11 && played === nPlay, `${played}/${nPlay}`);

// ------------------------------------------------------------------ live WebSocket changes
poke('lane');
await page.waitForSelector('.lane-label.st-closed', { timeout: 8000 }).catch(() => {});
check('a lane state change arrives over the WebSocket', (await count(page, '.lane-label.st-closed')) === 1 && seen.ws.includes('lane_status'));
await page.waitForFunction(() => document.querySelector('.map svg')?.textContent.includes('Person on foot (moved)'), null, { timeout: 8000 }).catch(() => {});
check('an object change arrives over the WebSocket', seen.ws.includes('object') && (await page.locator('.map svg').textContent()).includes('Person on foot (moved)'));

// ------------------------------------------------------------------ solo
await page.locator('.radio .panel-head .seg button', { hasText: 'OPS' }).click();
check('soloing a stream marks it and silences the others', (await count(page, '.stream.solo')) === 1 && (await count(page, '.stream.muted-stream')) === 2 && (await page.locator('.listen-note').innerText()).includes('Solo: Airport Ops'));
const auto = page.waitForResponse((r) => r.url().includes('/viz/audio/'), { timeout: 10000 }).then((r) => r.status(), () => 0);
poke('call');
const autoStatus = await auto;
await page.waitForSelector('.line.playing', { timeout: 5000 }).catch(() => {});
check('a new call on the soloed stream plays by itself', [200, 206].includes(autoStatus) && (await page.locator('.stream.solo .line.playing').innerText()).includes('holding position at Delta'));
await shot(page, '03-solo-stream');
await page.locator('.radio .panel-head .seg button', { hasText: 'All' }).click();

// ------------------------------------------------------------------ controller switcher
await page.locator('.ctl-switch button', { hasText: 'Ground' }).click();
await page.waitForTimeout(300);
const ground = world.controllers.find((c) => c._id === 'ctl_ground');
check('Ground: streams narrow to theirs', (await count(page, '.radio .stream')) === 2 && !(await page.locator('.radio').innerText()).includes('118.700'));
check('Ground: their lanes are highlighted', (await count(page, '.map svg .lane-owned')) === ground.lanes.length, `${ground.lanes.length} lanes`);
check('Ground: no request and no stop is routed to them', (await count(page, '.requests .req')) === 0 && (await count(page, '.stops .stop')) === 0);
await shot(page, '04-controller-ground');
await page.locator('.ctl-switch button', { hasText: 'Local' }).click();
await page.waitForTimeout(300);
check('Local: one stream, their two runways, the stop and the proposal', (await count(page, '.radio .stream')) === 1 && (await count(page, '.map svg .lane-owned')) === 2 && (await count(page, '.requests .req')) === 1 && (await count(page, '.stops .stop')) === 1);
await shot(page, '05-controller-local');
await page.locator('.ctl-switch button', { hasText: 'All' }).click();

// ------------------------------------------------------------------ approach zoom
await page.getByRole('button', { name: /Approach/ }).click();
await page.waitForTimeout(900);
check('approach zoom shows the final', (await count(page, '.map svg .final-line')) >= 1);
await shot(page, '06-approach-zoom');
await page.getByRole('button', { name: 'Airfield' }).click();

// ------------------------------------------------------------------ history and back
const box = await page.locator('.tl-track').boundingBox();
const bounds = await (await fetch(`${api}/viz/bounds?tower_id=LGA`)).json();
const span = Date.now() - Date.parse(bounds.from);
const xAt = (s) => box.x + box.width * Math.min(0.98, (s * 1000) / span); // s seconds after the first record
await page.mouse.move(box.x + box.width * 0.95, box.y + 40);
await page.mouse.down();
await page.mouse.move(xAt(100), box.y + 40, { steps: 6 });
await page.mouse.move(xAt(50), box.y + 40, { steps: 6 });
await page.mouse.up();
await page.waitForSelector('.history-banner');
await page.waitForSelector('.lane-label.st-reserved', { timeout: 8000 }).catch(() => {});
check('dragging the timeline asks for /viz/snapshot?t= and /viz/tracks', seen.urls.some((u) => /\/viz\/snapshot\?.*\bt=/.test(u.url) && u.status === 200) && seen.urls.some((u) => u.url.includes('/viz/tracks?') && u.status === 200));
check('history mode is unmistakable (frame, banner, stamp, pill)', (await count(page, '.app.mode-history')) === 1 && /NOT LIVE/.test(await page.locator('.history-banner').innerText()) && (await count(page, '.map-stamp')) === 1 && (await count(page, '.pill.history')) === 1);
check('the past differs: runway 04 reserved, nothing decided, no stop yet', (await count(page, '.lane-label.st-reserved')) === 1 && (await count(page, '.lane-label.st-closed')) === 0 && (await count(page, '.audit .trail')) === 0 && (await count(page, '.stops .stop')) === 0 && (await count(page, '.radio .line')) === 4);
await shot(page, '07-history-early');
// 108 s in: the autonomous stop is in effect, and a stop from a needs_approval agent is waiting
await page.mouse.move(xAt(50), box.y + 40);
await page.mouse.down();
await page.mouse.move(xAt(108), box.y + 40, { steps: 6 });
await page.mouse.up();
await page.waitForSelector('.stops .stop', { timeout: 8000 }).catch(() => {});
await page.waitForSelector('.requests .req', { timeout: 8000 }).catch(() => {});
const pend = (await count(page, '.requests .req')) ? await page.locator('.requests .req').first().innerText() : '';
check('history: a stop from a needs_approval agent was the pending item', /STOP · NEEDS YOUR APPROVAL/.test(pend) && pend.includes('JZ7801, hold position'));
check('history: actions are disabled', (await page.locator('.stops .btn.lift').isDisabled()) && (await page.locator('.requests .btn.approve').isDisabled()));
await shot(page, '08-history-stop-and-pending');
await page.locator('.timeline .live-btn').click();
await page.waitForSelector('.app.mode-live');
check('LIVE returns to the live picture', (await count(page, '.history-banner')) === 0 && (await count(page, '.lane-label.st-closed')) === 1 && (await count(page, '.pill.live')) === 1);
await page.close();

// ------------------------------------------------------------------ narrower windows (live API)
if (shots) {
  ({ page } = await open(1100, 800));
  await shot(page, '09-medium-1100', { fullPage: true });
  await page.close();
  ({ page } = await open(480, 900));
  await shot(page, '10-narrow-480', { fullPage: true });
  await page.close();
}
// ------------------------------------------------------------------ mock mode: an active conflict, and lifting a stop
({ page } = await open(1600, 950, '?mock=1'));
await page.waitForSelector('.stops .stop');
check('mock: conflict on the runway with an autonomous stop in effect', (await count(page, '.lane-label.st-conflict')) === 1 && /go around/.test(await page.locator('.stops .stop').innerText()) && (await count(page, '.requests .req')) >= 1);
await shot(page, '11-conflict-mock');
await page.locator('.actas select').selectOption('ctl_local');
await page.locator('.stops .btn.lift').click();
await page.waitForSelector('.stops .stop', { state: 'detached', timeout: 5000 }).catch(() => {});
check('mock: Lift stop removes the alert and logs "stop lifted"', (await count(page, '.stops .stop')) === 0 && /STOP LIFTED/.test(await page.locator('.audit').innerText()));
await page.close();

check('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | '));

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
