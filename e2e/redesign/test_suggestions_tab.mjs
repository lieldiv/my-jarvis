// "הצעות מערכת" tab -- the ONE thing the backend unit tests (Python,
// hitting Flask directly) can't prove: that the frontend actually renders
// this data correctly in a real browser, and that the buttons wired to it
// do what they claim. Covers both card kinds that share this tab: email
// suggestions (buildSuggestionCard, reused from the Mail-tab-era code,
// now living here) and generic system notices (buildNoticeCard, new).
import { chromium, devices } from 'playwright';
import fs from 'fs';
import { installMocks, enterApp, SUGGESTIONS } from './mockdata.mjs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync(new URL('../session.txt', import.meta.url), 'utf8').trim();
const results = [];
function check(label, cond, detail) { results.push({ label, ok: !!cond, detail: cond ? '' : String(detail ?? '') }); }

async function newPage(browser, over = {}, { pastFirstRun = true, noticeDismissFails = false } = {}) {
  const ctx = await browser.newContext({ ...devices['Pixel 7'], locale: 'he-IL' });
  await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
  const page = await ctx.newPage();
  const errors = [], calls = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/command', r => r.fulfill({ json: { response: 'ok', audio: null, speak: false, persona: 'jarvis' } }));
  await page.route('**/api/speak', r => r.fulfill({ json: { audio: null } }));
  await installMocks(page, over);
  const rec = (r) => calls.push({ m: r.request().method(), url: r.request().url(), body: r.request().postData() });
  await page.route('**/api/inbox/suggestions/*/send', async r => { rec(r); await r.fulfill({ json: { status: 'confirmation_required', kind: 'email', message: 'x', token: 't-sugg', details: { to: 'dana@example.com', subject: 'Re: אישור פגישה', body: 'שלום דנה, תודה, מאשר.' } } }); });
  await page.route('**/api/inbox/suggestions/*/dismiss', async r => { rec(r); await r.fulfill({ json: { ok: true } }); });
  await page.route('**/api/suggestions/notices/*/dismiss', async r => { rec(r); await (noticeDismissFails ? r.fulfill({ json: { ok: false } }) : r.fulfill({ json: { ok: true } })); });
  if (pastFirstRun) await page.addInitScript(() => { try { localStorage.setItem('jarvis_coach_v1', '1'); } catch (e) {} });
  await enterApp(page, BASE, 2800);
  return { ctx, page, errors, calls };
}
const texts = (page, sel) => page.$$eval(sel, els => els.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
const visible = (page, sel) => page.evaluate(s => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0; }, sel);
const goTab = async (page, key, wait = 900) => { await page.evaluate(k => setTab(k), key); await page.waitForTimeout(wait); };

const browser = await chromium.launch();

// ================================================================= nav + untouched Mail tab
{
  const { ctx, page, errors } = await newPage(browser);
  const navLabels = await texts(page, '#bottom-nav .nav-item .nav-label');
  check('bottom nav includes "הצעות" (new) alongside the original five', navLabels.join(',') === 'בית,יומן,דואר,משימות,הצעות,הגדרות', navLabels.join(','));
  await goTab(page, 'inbox');
  check('Mail tab shows only real emails, no suggestion cards at all (left untouched)', (await page.locator('#inbox-list .suggestion-card').count()) === 0 && (await page.locator('#inbox-list .mail-card').count()) === 2);
  check('no JS errors (nav run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= populated tab
{
  const { ctx, page, errors, calls } = await newPage(browser);
  await goTab(page, 'suggestions');
  check('Suggestions: title and a live count', (await page.locator('#tab-suggestions .page-title').innerText()) === 'הצעות מערכת' && (await page.locator('#suggestions-sub').innerText()) === '4 הצעות ממתינות', await page.locator('#suggestions-sub').innerText());

  const pendingCard = await page.locator('.suggestion-card').nth(0).innerText();
  check('Suggestions: a not-yet-drafted email suggestion shows "preparing a draft", dismiss only (no send button)', pendingCard.includes('מכין טיוטת תשובה') && (await page.locator('.suggestion-card').nth(0).locator('.ds-btn.pri').count()) === 0, pendingCard);

  const draftedCard = await page.locator('.suggestion-card').nth(1).innerText();
  check('Suggestions: a drafted email suggestion shows the draft text and a "שלח לאישור" button', draftedCard.includes('תודה, מאשר') && draftedCard.includes('שלח לאישור'), draftedCard);

  const conflictCard = await page.locator('.notice-conflict').innerText();
  check('Suggestions: a notice WITH an action_label shows that button plus "התעלם"', conflictCard.includes('שתי פגישות חופפות') && conflictCard.includes('פתח יומן') && conflictCard.includes('התעלם'), conflictCard);

  const holidayCard = await page.locator('.notice-card:not(.notice-conflict):not(.notice-market)').innerText();
  check('Suggestions: a notice with NO action_label shows only "הבנתי" (dismiss), never "שלח" anywhere', holidayCard.includes('פסח מתקרב') && holidayCard.includes('הבנתי') && !holidayCard.includes('שלח'), holidayCard);

  // sending a drafted email suggestion opens the real confirm-to-act modal, nothing sent directly
  await page.locator('.suggestion-card').nth(1).locator('.ds-btn.pri').click();
  await page.waitForTimeout(500);
  const sendCall = calls.find(c => /\/suggestions\/102\/send$/.test(c.url));
  check('Suggestions: "שלח לאישור" calls the real send route and opens the approval sheet -- nothing sent directly', !!sendCall && (await visible(page, '#confirm-overlay.show')), JSON.stringify(sendCall));
  await page.evaluate(() => { hideConfirmModal(); pendingConfirmToken = null; });

  // dismissing a notice removes it and calls the real dismiss route
  const beforeCount = await page.locator('#suggestions-list .mail-card').count();
  await page.locator('.notice-conflict .ds-btn', { hasText: 'התעלם' }).click();
  await page.waitForTimeout(400);
  const dismissCall = calls.find(c => /\/api\/suggestions\/notices\/201\/dismiss$/.test(c.url));
  check('Suggestions: dismissing a notice calls the real dismiss route and removes the card', !!dismissCall && (await page.locator('#suggestions-list .mail-card').count()) === beforeCount - 1, JSON.stringify(dismissCall));

  check('no JS errors (populated run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= notice action button does the real thing, not just dismiss
{
  const { ctx, page, errors, calls } = await newPage(browser);
  await goTab(page, 'suggestions');
  check('starts on the Suggestions tab, not Agenda', (await visible(page, '#tab-suggestions')) && !(await visible(page, '#tab-agenda')));
  await page.locator('.notice-conflict .ds-btn', { hasText: 'פתח יומן' }).click();
  await page.waitForTimeout(500);
  check('clicking the conflict notice\'s "פתח יומן" action actually switches to the Agenda tab (not just a relabeled dismiss)', await visible(page, '#tab-agenda') && !(await visible(page, '#tab-suggestions')));
  const dismissCall = calls.find(c => /\/api\/suggestions\/notices\/201\/dismiss$/.test(c.url));
  check('...and still dismisses the notice it acted on', !!dismissCall, JSON.stringify(dismissCall));
  check('no JS errors (action-button run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= empty state
{
  const { ctx, page, errors } = await newPage(browser, { suggestions: { email_suggestions: [], notices: [] } });
  await goTab(page, 'suggestions');
  const empty = await page.locator('#suggestions-list .empty-card').innerText();
  check('Suggestions empty: says so plainly, no stray cards', empty.includes('אין הצעות כרגע') && (await page.locator('#suggestions-list .mail-card').count()) === 0, empty);
  check('Suggestions empty: the live count reads zero, not blank or "undefined"', (await page.locator('#suggestions-sub').innerText()) === 'אין הצעות כרגע');
  check('no JS errors (empty run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= dismissNotice: a failed server call must NOT desync the UI
{
  const { ctx, page, errors, calls } = await newPage(browser, {}, { noticeDismissFails: true });
  await goTab(page, 'suggestions');
  const beforeCount = await page.locator('#suggestions-list .mail-card').count();
  await page.locator('.notice-conflict .ds-btn', { hasText: 'התעלם' }).click();
  await page.waitForTimeout(400);
  const dismissCall = calls.find(c => /\/api\/suggestions\/notices\/201\/dismiss$/.test(c.url));
  check('a failed dismiss call still fires the request', !!dismissCall, JSON.stringify(dismissCall));
  check('...but the card STAYS (never optimistically removed ahead of a confirmed server success)',
        (await page.locator('#suggestions-list .mail-card').count()) === beforeCount);
  check('...and an error toast tells the user it did not work', (await page.locator('#toast').innerText()).includes('לא הצלחתי'));
  check('no JS errors (dismiss-failure run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= nav "new" dot: brand-new user never sees it (nothing is "new" to them)
{
  const { ctx, page, errors } = await newPage(browser, {}, { pastFirstRun: false });
  check('brand-new user (never finished the original first-run coach): no "new" dot on the Suggestions tab',
        (await page.locator('.nav-item .nav-dot-new').count()) === 0);
  await goTab(page, 'home');
  check('...still none after switching tabs (not a one-render flash that would reappear)',
        (await page.locator('.nav-item .nav-dot-new').count()) === 0);
  check('no JS errors (brand-new-user run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= nav "new" dot: existing user sees it, and it clears on visiting the tab
{
  const { ctx, page, errors } = await newPage(browser);   // pastFirstRun: true (default), intro not yet seen
  check('existing user who hasn\'t seen the update yet: the "new" dot IS shown', (await page.locator('.nav-item .nav-dot-new').count()) === 1);
  await goTab(page, 'agenda');
  check('...survives switching to an unrelated tab (not cleared by every renderNav() call)', (await page.locator('.nav-item .nav-dot-new').count()) === 1);
  await goTab(page, 'suggestions');
  check('...clears once they actually visit the Suggestions tab directly (discovery, not just dismissing the Home banner)',
        (await page.locator('.nav-item .nav-dot-new').count()) === 0);
  check('no JS errors (existing-user run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

await browser.close();
let failed = 0;
for (const r of results) { console.log((r.ok ? 'PASS  ' : 'FAIL  ') + r.label + (r.ok ? '' : '\n      ' + r.detail.slice(0, 300))); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
