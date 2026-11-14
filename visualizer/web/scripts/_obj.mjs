import { chromium } from 'playwright';
const API = 'http://localhost:18745';
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
p.on('pageerror', (e) => console.log('PAGEERR', e.message));
await p.goto('http://127.0.0.1:18746/');
await p.waitForTimeout(3500);
const snap = await (await fetch(`${API}/viz/snapshot?tower_id=LGA`)).json();
const world = await (await fetch(`${API}/viz/world?tower_id=LGA`)).json();
const dom = await p.evaluate(() => {
  const rect = document.querySelector('.map').getBoundingClientRect();
  const tr = (e) => (e.style.transform || e.getAttribute('transform') || '').match(/translate\(([-\d.]+)(?:px)?,\s*([-\d.]+)/)?.slice(1, 3).map(Number);
  return { w: rect.width, h: rect.height, objs: [...document.querySelectorAll('[data-id]')].map((e) => ({ id: e.dataset.id, cls: e.className.baseVal, xy: tr(e), tag: e.querySelector('text')?.textContent ?? null, glyph: !!e.querySelector('.glyph, path') })), hps: [...document.querySelectorAll('[data-hp]')].map((e) => ({ id: e.dataset.hp, xy: tr(e) })) };
});
console.log('snapshot objects', snap.objects.length, 'drawn', dom.objs.length, 'map', dom.w, dom.h);
// fit a projection from two hold points, then predict every object's pixel
const R = Math.PI / 180, k = Math.cos(world.tower.center.coordinates[1] * R);
const m = (c) => [(c[0] - world.tower.center.coordinates[0]) * R * 6371000 * k, -(c[1] - world.tower.center.coordinates[1]) * R * 6371000];
const h0 = world.hold_points[0], h1 = world.hold_points[world.hold_points.length - 1];
const d0 = dom.hps.find((x) => x.id === h0._id).xy, d1 = dom.hps.find((x) => x.id === h1._id).xy;
const m0 = m(h0.position.coordinates), m1 = m(h1.position.coordinates);
const sc = Math.hypot(d1[0] - d0[0], d1[1] - d0[1]) / Math.hypot(m1[0] - m0[0], m1[1] - m0[1]);
for (const o of snap.objects) {
  const mo = m(o.position.coordinates);
  const want = [d0[0] + (mo[0] - m0[0]) * sc, d0[1] + (mo[1] - m0[1]) * sc];
  const d = dom.objs.find((x) => x.id === o._id);
  const err = d?.xy ? Math.hypot(d.xy[0] - want[0], d.xy[1] - want[1]) : null;
  console.log(o._id.padEnd(8), o.kind.padEnd(9), o.status.padEnd(8), d ? `${d.cls.split(' ').slice(0, 2).join('.')} at ${d.xy?.map(Math.round)} want ${want.map(Math.round)} err ${err?.toFixed(1)}px tag="${d.tag}" glyph=${d.glyph}` : 'NOT DRAWN');
}
await p.screenshot({ path: '/tmp/hq45-O1.png' });
console.log('BEFORE', JSON.stringify(dom.objs.find((x) => x.id === 'TRUCK1')?.xy));
await b.close();
