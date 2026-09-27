import { chromium, devices } from 'playwright';
import fs from 'fs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync(new URL('../session.txt', import.meta.url), 'utf8').trim();
const results = [];
function check(label, cond, detail) { results.push({ label, ok: !!cond, detail: cond ? '' : String(detail ?? '') }); }

const browser = await chromium.launch();
const ctx = await browser.newContext({ ...devices['Pixel 7'], locale: 'he-IL' });
await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.route('**/api/command', r => r.fulfill({ json: { response: 'Canned reply, sir.', audio: null, speak: false, persona: 'jarvis' } }));
await page.route('**/api/speak', r => r.fulfill({ json: { audio: null } }));   // no network text-to-speech: the wake greeting must not make timing depend on it
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => { document.getElementById('gate-screen').style.display = 'none'; showWakeGate(); });
await page.waitForTimeout(400);
await page.locator('#wake-gate-btn').click({ force: true });
await page.waitForTimeout(2000);

const rect = sel => page.evaluate(s => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top) }; }, sel);

// ---- one opaque dock
const dockBg = await page.evaluate(() => getComputedStyle(document.getElementById('dock')).backgroundColor);
check('dock background is fully opaque', /^rgb\(/.test(dockBg) && !/rgba/.test(dockBg), dockBg);
check('command bar and nav share the dock (no separate translucent bars)', await page.evaluate(() => document.getElementById('dock').contains(document.querySelector('.cmd-input-row')) && document.getElementById('dock').contains(document.getElementById('bottom-nav'))));
const tabs = await page.$$('#bottom-nav .nav-item');
check('five nav items', tabs.length === 5, tabs.length);
check('every nav item carries an svg icon and a label', await page.evaluate(() => [...document.querySelectorAll('#bottom-nav .nav-item')].every(b => b.querySelector('svg.i') && b.querySelector('.nav-label').textContent.trim().length > 0)));
check('nav touch targets are at least 44px tall', (await page.evaluate(() => [...document.querySelectorAll('#bottom-nav .nav-item')].map(b => Math.round(b.getBoundingClientRect().height)))).every(h => h >= 44));
const mic = await rect('#dock-mic'), send = await rect('.cmd-send-btn'), inp = await rect('#cmd-input');
check('dock mic and send buttons are 44px', mic && send && mic.w >= 44 && mic.h >= 44 && send.w >= 44 && send.h >= 44, JSON.stringify({ mic, send }));
check('command input is at least 44px tall', inp && inp.h >= 44, JSON.stringify(inp));
const navLabelColor = await page.evaluate(() => getComputedStyle(document.querySelector('#bottom-nav .nav-item:not(.active) .nav-label')).color);
check('inactive nav labels use the readable dim colour (not the old #4d6a80)', navLabelColor !== 'rgb(77, 106, 128)', navLabelColor);
check('nav labels are at least 12px', await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.nav-label')).fontSize) >= 12));

// ---- type
const bodyFont = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
check('body font is Rubik first (Hebrew no longer borrows spaces from the mono face)', /^"?Rubik/.test(bodyFont), bodyFont);

// ---- status bar / browser chrome colour
const themeColor = await page.evaluate(() => document.querySelector('meta[name=theme-color]').content);
check('browser bar colour is the dark background, not the neon accent', /^(#020813|rgb\(2,\s*8,\s*19\))$/i.test(themeColor), themeColor);
await page.evaluate(() => setTheme('green'));
const themeGreen = await page.evaluate(() => document.querySelector('meta[name=theme-color]').content);
check('browser bar follows the theme background when the accent changes', /^#02120a$/i.test(themeGreen), themeGreen);
await page.evaluate(() => setTheme('cyan'));

// ---- calm surfaces on content tabs, full HUD on Home
const decor = () => page.evaluate(() => getComputedStyle(document.getElementById('bg-decor')).display);
check('Home keeps the ambient decoration', (await decor()) !== 'none');
for (const t of ['agenda', 'inbox', 'tasks', 'settings']) {
  await page.evaluate(k => setTab(k), t);
  await page.waitForTimeout(250);
  check(`${t}: ambient particles/radar are off`, (await decor()) === 'none');
  const panelBg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--color-panel'));
  const screenPanel = await page.evaluate(() => getComputedStyle(document.getElementById('device-screen')).getPropertyValue('--color-panel').trim());
  check(`${t}: cards sit on the opaque surface (no alpha channel: nothing shows through)`, /^rgb\(6,\s*26,\s*48\)$/.test(screenPanel), screenPanel);
}
await page.evaluate(() => setTab('home'));
await page.waitForTimeout(250);
check('back on Home the decoration returns', (await decor()) !== 'none');

// ---- an answer typed on another tab is visible (it used to be spoken but never shown)
await page.evaluate(() => setTab('tasks'));
await page.fill('#cmd-input', 'hello');
await page.keyboard.press('Enter');
await page.waitForFunction(() => document.getElementById('reply-bubble').classList.contains('show'), null, { timeout: 8000 }).catch(() => {});
await page.waitForTimeout(450);   // the bubble fades up over .25s; measure it at rest
const bubbleText = await page.evaluate(() => document.getElementById('reply-bubble').classList.contains('show') ? document.getElementById('reply-bubble-text').innerText : null);
check('reply bubble shows the answer on a non-Home tab', bubbleText && bubbleText.includes('Canned reply'), bubbleText);
const bubbleRect = await rect('#reply-bubble');
const dockRect = await rect('#dock');
check('the bubble sits above the dock, not behind it', bubbleRect && dockRect && bubbleRect.top + bubbleRect.h <= dockRect.top + 1, JSON.stringify({ bubbleRect, dockRect }));
await page.locator('.reply-x').click();
check('the bubble can be dismissed', !(await page.evaluate(() => document.getElementById('reply-bubble').classList.contains('show'))));
await page.fill('#cmd-input', 'hello again');
await page.keyboard.press('Enter');
await page.waitForTimeout(1200);
await page.evaluate(() => setTab('home'));
check('going Home hides the bubble (the orb shows the answer there)', !(await page.evaluate(() => document.getElementById('reply-bubble').classList.contains('show'))));

// ---- dock height feeds the scroll padding
const dockH = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--dock-h').trim());
check('--dock-h reflects the real dock height', Math.abs(parseFloat(dockH) - dockRect.h) <= 2, dockH + ' vs ' + dockRect.h);
await page.evaluate(() => setTab('settings'));
await page.evaluate(() => { const b = document.getElementById('app-body'); b.scrollTop = b.scrollHeight; });
await page.waitForTimeout(300);
const lastBtn = await page.evaluate(() => { const b = document.querySelector('#tab-settings .signout').getBoundingClientRect(); const d = document.getElementById('dock').getBoundingClientRect(); return { bottom: Math.round(b.bottom), dockTop: Math.round(d.top) }; });
check('the last Settings button clears the dock when scrolled to the end', lastBtn.bottom <= lastBtn.dockTop, JSON.stringify(lastBtn));

// ---- no emoji left in the global chrome
const chromeText = await page.evaluate(() => (document.getElementById('dock').innerText + document.getElementById('quick-actions-row').innerText + document.querySelector('.confirm-btn-row').innerText));
check('no emoji glyphs in dock / quick actions / confirm buttons', !/[\u{1F300}-\u{1FAFF}☀-➿⬀-⯿]/u.test(chromeText), chromeText);
check('developer-only "בשלב בדיקות" note is gone from Settings', !(await page.evaluate(() => document.getElementById('tab-settings').innerText.includes('בשלב בדיקות'))));
check('the sign-in screen now says access is limited to approved accounts', await page.evaluate(() => document.getElementById('gate-signin-wrap').innerText.includes('אושרו מראש') || document.getElementById('gate-signin-wrap').textContent.includes('אושרו מראש')));

check('no JS errors', errors.length === 0, errors.join(' | '));
await page.screenshot({ path: 'shots/step1_settings_end.png' });
await browser.close();

console.log('\n=== STEP 1 FOUNDATION ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 300)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
