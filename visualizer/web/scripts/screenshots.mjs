// Takes the review screenshots from mock mode. Usage: node scripts/screenshots.mjs [baseUrl]
import { chromium } from 'playwright';

const base = process.argv[2] || 'http://localhost:18531';
const out = new URL('../docs/', import.meta.url).pathname;
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
const errors = [];

async function open(width, height) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  await page.goto(`${base}/?mock=1`);
  await page.waitForSelector('.map svg');
  await page.waitForTimeout(1500);
  return page;
}

let page = await open(1600, 950);
await page.screenshot({ path: `${out}01-live.png` });
await page.getByRole('button', { name: /Approach/ }).click();
await page.waitForTimeout(800);
await page.screenshot({ path: `${out}02-live-approach.png` });
await page.getByRole('button', { name: 'Airfield' }).click();

// controller-filtered view, with a solo stream
await page.locator('.ctl-switch button', { hasText: 'Ground' }).click();
await page.locator('.radio .seg button', { hasText: 'OPS' }).click();
await page.waitForTimeout(800);
await page.screenshot({ path: `${out}03-controller-ground.png` });

// 403: act on Ground's request as Local, then approve the stop as its owner
await page.locator('.ctl-switch button', { hasText: 'All' }).click();
await page.locator('.radio .seg button', { hasText: 'All' }).click();
await page.locator('.actas select').selectOption('ctl_local');
await page.locator('.req', { hasText: 'Truck 1' }).getByRole('button', { name: /Approve/ }).click();
await page.locator('.req', { hasText: 'go around' }).getByRole('button', { name: /Approve/ }).click();
await page.waitForTimeout(2500);
await page.screenshot({ path: `${out}04-approve-and-403.png` });
await page.close();

// history mode: drag the timeline back
page = await open(1600, 950);
await page.locator('.radio .seg button', { hasText: 'TOWER' }).click();
const box = await page.locator('.tl-track').boundingBox();
await page.mouse.move(box.x + box.width * 0.9, box.y + 40);
await page.mouse.down();
await page.mouse.move(box.x + box.width * 0.4, box.y + 40, { steps: 8 });
await page.mouse.up();
await page.waitForTimeout(1200);
await page.screenshot({ path: `${out}05-history.png` });
await page.getByRole('button', { name: 'Play', exact: true }).click();
await page.getByRole('button', { name: '4x', exact: true }).click();
await page.waitForTimeout(2500);
await page.screenshot({ path: `${out}06-history-playing.png` });
await page.close();

page = await open(1100, 800);
await page.screenshot({ path: `${out}07-medium-1100.png`, fullPage: true });
await page.close();

page = await open(480, 900);
await page.screenshot({ path: `${out}08-narrow-480.png`, fullPage: true });
await page.close();

await browser.close();
if (errors.length) {
  console.error('browser errors:\n' + errors.join('\n'));
  process.exit(1);
}
console.log('screenshots written to', out);
