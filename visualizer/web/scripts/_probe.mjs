import { chromium } from 'playwright';
const live = process.env.LIVE;
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
p.on('pageerror', (e) => console.log('PAGEERR', e.message));
p.on('console', (m) => m.type() === 'error' && console.log('ERR', m.text().slice(0, 200)));
await p.goto('http://127.0.0.1:5181/' + (live ? '' : '?mock=1'));
await p.waitForTimeout(3000);
const snap = () => p.evaluate(() => ({ clock: document.querySelector('.clock')?.textContent, objs: [...document.querySelectorAll('.obj')].map((e) => e.style.transform.replace(/translate\(([-\d.]+)px, ([-\d.]+)px\)/, (_, a, b) => Math.round(a) + ',' + Math.round(b))).join(' '), rows: document.querySelectorAll('.row').length, st: [...document.querySelectorAll('.lane')].map((e) => e.className.baseVal.match(/st-\w+/)?.[0].slice(3, 6)).join(','), needs: document.querySelectorAll('.needs .stop, .needs .req').length, lines: [...document.querySelectorAll('.slane-text')].map((e) => e.textContent.slice(0, 18)).join(' | '), mode: document.querySelector('.app').className.slice(9), win: document.querySelector('[aria-label="Timeline window"]')?.textContent, range: [...document.querySelectorAll('.tl-end')].map((e) => e.textContent).join('-') }));
console.log('live    ', JSON.stringify(await snap()));
await p.keyboard.press('Shift+ArrowLeft'); await p.waitForTimeout(300);
await p.keyboard.press('Shift+ArrowLeft'); await p.waitForTimeout(300);
await p.keyboard.press('Shift+ArrowLeft'); await p.waitForTimeout(1500);
console.log('3x prev ', JSON.stringify(await snap()));
await p.screenshot({ path: `/tmp/hq45-${live ? 'L' : 'M'}1.png` });
await p.keyboard.press('Space');
for (let i = 0; i < 3; i++) { await p.waitForTimeout(2000); console.log('play+' + (i + 1) * 2, JSON.stringify(await snap())); }
await p.getByRole('button', { name: 'Playback speed' }).click(); await p.getByRole('option', { name: '16x' }).click();
await p.waitForTimeout(2000); console.log('16x +2  ', JSON.stringify(await snap()));
await p.screenshot({ path: `/tmp/hq45-${live ? 'L' : 'M'}2.png` });
if (!live) { await p.waitForTimeout(9000); console.log('16x +11 ', JSON.stringify(await snap())); }
await p.keyboard.press('l'); await p.waitForTimeout(500); await p.keyboard.press('3'); await p.waitForTimeout(300);
console.log('L, 3    ', JSON.stringify(await snap()), await p.locator('.topbar .seg button.on').textContent());
await b.close();
