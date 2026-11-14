import { chromium } from 'playwright';
const [, , name = 'a', wait = '3000', w = '1600', h = '900', act = ''] = process.argv;
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: +w, height: +h } });
p.on('console', (m) => m.type() === 'error' && console.log('ERR', m.text()));
p.on('pageerror', (e) => console.log('PAGEERR', e.message));
await p.goto('http://127.0.0.1:5181/?mock=1');
await p.waitForTimeout(+wait);
if (act === 'ground') await p.getByRole('button', { name: /^Ground/ }).first().click();
if (act === 'history') { const r = await p.locator('.tl-track').boundingBox(); await p.mouse.click(r.x + r.width * 0.55, r.y + r.height / 2); }
if (act === 'drawer') await p.getByRole('button', { name: 'Audit log' }).click();
if (act === 'pop') await p.getByRole('button', { name: 'safety settings' }).click();
if (act) await p.waitForTimeout(1500);
console.log('overflow', await p.evaluate(() => [document.documentElement.scrollWidth, innerWidth, document.querySelector('.topbar').scrollWidth]));
await p.screenshot({ path: `/tmp/hq45-${name}.png` });
await b.close();
