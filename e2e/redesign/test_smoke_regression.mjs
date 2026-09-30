// Rebuilt after the earlier 18-check suite was lost with the scratchpad: the same ground, leaner.
// Nothing here calls Groq: /api/command is answered by a canned route.
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync(new URL('../session.txt', import.meta.url), 'utf8').trim();
const results = [];
function check(label, cond, detail) { results.push({ label, ok: !!cond, detail: cond ? '' : String(detail ?? '') }); }

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error' && !/401|Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });

await page.route('**/api/command', route => route.fulfill({ json: { response: 'Canned reply, sir.', audio: null, speak: false, persona: 'jarvis' } }));

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => { document.getElementById('gate-screen').style.display = 'none'; showWakeGate(); });
await page.waitForTimeout(400);
check('wake gate is up', await page.locator('#wake-gate.show').count() > 0);
await page.locator('#wake-gate-btn').click({ force: true });
await page.waitForTimeout(1500);
check('app screen loaded', await page.locator('#app-screen.show').count() > 0);
check('no JS errors on load', errors.length === 0, errors.join(' | '));

// text command path end to end (canned reply)
await page.fill('#cmd-input', 'hello');
await page.keyboard.press('Enter');
await page.waitForTimeout(1500);
check('typed command produced a reply on the orb', (await page.locator('#orb-response').innerText()).includes('Canned reply'), await page.locator('#orb-response').innerText());

for (const tab of ['agenda', 'inbox', 'tasks', 'settings', 'home']) {
  await page.evaluate(t => setTab(t), tab);
  await page.waitForTimeout(700);
  check(`${tab} tab visible`, await page.locator(`#tab-${tab}.show`).count() === 1);
}

// tasks: create through the UI, then complete it
await page.evaluate(() => setTab('tasks'));
await page.waitForTimeout(600);
const taskText = 'smoke-task-' + Date.now();
await page.locator('#fab').click();
await page.waitForTimeout(400);
await page.fill('#task-input', taskText);
await page.locator('#task-sheet .ds-btn.pri').click();
await page.locator('#tasks-list', { hasText: taskText }).waitFor({ state: 'attached', timeout: 8000 }).catch(() => {});
check('task added through the UI', (await page.locator('#tasks-list').innerText()).includes(taskText), await page.locator('#tasks-list').innerText());
check('task chart has 7 bars', await page.locator('#task-chart .task-bar-col, #task-chart > *').count() >= 7);
const created = await page.evaluate(async (t) => {
  const r = await fetch('/api/tasks'); const d = await r.json(); return (d.tasks || []).find(x => x.text === t) || null;
}, taskText);
check('task exists server-side', !!created);
if (created) {
  await page.evaluate(async (id) => { await fetch(`/api/tasks/${id}`, { method: 'DELETE' }); }, created.id);
}

// settings content
await page.evaluate(() => setTab('settings'));
await page.waitForTimeout(500);
check('settings: voice diagnostics present', await page.locator('#voice-diag').count() === 1);
check('settings: shortcut registry present', (await page.locator('#shortcut-registry').innerHTML()).length > 50);
check('settings: 6 theme swatches', await page.locator('#theme-row .theme-swatch').count() === 6, await page.locator('#theme-row .theme-swatch').count());
check('settings: alarm category is gone', await page.locator('#alarm-setup').count() === 0);

// the confirmation sheet still opens for a calendar proposal (shared code path with the new kind)
await page.evaluate(() => showConfirmModal({ type: 'confirmation_required', kind: 'calendar_event', message: 'x', token: 'tok',
  details: { summary: 'Smoke event', start_iso: '2026-10-01T10:00:00+03:00', end_iso: '2026-10-01T11:00:00+03:00', provider: 'google' } }));
check('calendar confirmation sheet still renders', (await page.locator('#confirm-detail-title').innerText()) === 'Smoke event');
check('calendar confirmation keeps its title', (await page.locator('#confirm-kind-label').innerText()) === 'הצעת אירוע ביומן');

// Escape on the approval sheet must send the SAME "no" the reject button does -- not just hide it --
// since it gates a real calendar/email write (found missing by a review agent). Spying on the real
// function rather than completing a full server round-trip on a fake token, same rigor the rest of
// this file already uses for this sheet (it never resolves a real confirm either, just renders it).
const rejectCall = await page.evaluate(() => {
  return new Promise(resolve => {
    const original = window.resolveConfirmation;
    window.resolveConfirmation = (approve) => { window.resolveConfirmation = original; resolve(approve); };
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    setTimeout(() => resolve('TIMEOUT'), 2000);
  });
});
check('Escape on the approval sheet calls resolveConfirmation(false) -- the exact same "no" the reject button sends', rejectCall === false, rejectCall);

// The re-entrancy guard a review agent asked for: a second resolveConfirmation() call while one is
// already in flight (a double-click, or -- now that Escape reaches this function too -- Escape
// racing an approve click) must be ignored, not send a second POST for the same token. Simulated by
// setting #confirming-overlay (the function's own "in flight" signal) to show before calling it.
const reentrantFetchCalls = await page.evaluate(() => {
  return new Promise(resolve => {
    document.getElementById('confirming-overlay').classList.add('show');
    let fetchCalls = 0;
    const original = window.fetchWithTimeout;
    window.fetchWithTimeout = (...args) => { fetchCalls++; return original(...args); };
    resolveConfirmation(true);
    setTimeout(() => {
      window.fetchWithTimeout = original;
      document.getElementById('confirming-overlay').classList.remove('show');
      resolve(fetchCalls);
    }, 300);
  });
});
check('resolveConfirmation ignores a re-entrant call while one is already in flight (no second POST for the same token)', reentrantFetchCalls === 0, reentrantFetchCalls);
await page.evaluate(() => { hideConfirmModal(); pendingConfirmToken = null; });

// Escape now also closes the other sheets a review agent found it silently skipped
await page.evaluate(() => { openComposeModal(); });
await page.keyboard.press('Escape');
check('Escape closes the compose sheet', !(await page.evaluate(() => document.getElementById('compose-sheet').classList.contains('show'))));
await page.evaluate(() => { $('switch-account-sheet').classList.add('show'); });
await page.keyboard.press('Escape');
check('Escape closes the switch-account sheet', !(await page.evaluate(() => document.getElementById('switch-account-sheet').classList.contains('show'))));

check('no JS errors across the whole run', errors.length === 0, errors.join(' | '));
await browser.close();

console.log('\n=== SMOKE REGRESSION ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 300)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
