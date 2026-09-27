import { chromium, devices } from 'playwright';
import fs from 'fs';
import { installMocks, enterApp } from './mockdata.mjs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync(new URL('../session.txt', import.meta.url), 'utf8').trim();
const results = [];
function check(label, cond, detail) { results.push({ label, ok: !!cond, detail: cond ? '' : String(detail ?? '') }); }

async function newPage(browser, { denied = false } = {}) {
  const ctx = await browser.newContext({ ...devices['Pixel 7'], locale: 'he-IL' });
  await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
  const page = await ctx.newPage();
  const errors = []; const commands = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/command', async route => { commands.push(JSON.parse(route.request().postData()).command); await route.fulfill({ json: { response: 'Canned reply, sir.', audio: null, speak: false, persona: 'jarvis' } }); });
  await installMocks(page);
  if (denied) await page.addInitScript(() => { navigator.permissions.query = async () => ({ state: 'denied', onchange: null }); });
  return { ctx, page, errors, commands };
}
const texts = (page, sel) => page.$$eval(sel, els => els.map(e => e.innerText.replace(/\s+/g, ' ').trim()));

const browser = await chromium.launch();

// ============ first run: coach card, tour, guide
{
  const { page, errors, commands } = await newPage(browser);
  await enterApp(page, BASE, 3000);
  const visible = sel => page.evaluate(s => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0; }, sel);

  // --- coach
  check('first run: the coach card is shown', await visible('#coach'));
  const coachText = await page.locator('#coach').innerText();
  check('coach says: speak or type / answers in English / nothing changes without approval', coachText.includes('לחץ על הכדור') && coachText.includes('באנגלית') && coachText.includes('בלי אישור שלך'), coachText);
  const orbBox = await page.locator('#orb-body').boundingBox();
  const coachBox = await page.locator('#coach').boundingBox();
  check('the orb stays the first thing you see: it sits above the coach card', orbBox.y + orbBox.height < coachBox.y, JSON.stringify({ orbBox, coachBox }));
  check('the orb keeps its size (200px body)', Math.round(orbBox.width) === 200, orbBox.width);
  const dockBox = await page.locator('#dock').boundingBox();
  check('the whole coach card (buttons included) is visible above the dock, with no scrolling', coachBox.y + coachBox.height <= dockBox.y, JSON.stringify({ coachBox, dockBox }));
  check('the coach does not cover the orb', orbBox.y + orbBox.height <= coachBox.y, JSON.stringify({ orbBox, coachBox }));
  check('the coach buttons are 44px tall', await page.evaluate(() => [...document.querySelectorAll('#coach .ds-btn, #coach .coach-x')].every(b => b.getBoundingClientRect().height >= 44)));

  // --- header help button
  const hb = await page.locator('#help-btn').boundingBox();
  check('the "?" button is in the header, 44px', hb && hb.width >= 44 && hb.height >= 44 && hb.y < 80, JSON.stringify(hb));
  check('the header stays compact (under 70px tall)', (await page.locator('#app-header').boundingBox()).height < 70, (await page.locator('#app-header').boundingBox()).height);

  await page.evaluate(() => setTab('agenda'));
  check('the coach is only on Home', !(await visible('#coach')));
  await page.evaluate(() => setTab('home'));
  check('...and comes back when you return to Home (until dismissed)', await visible('#coach'));
  // --- tour
  await page.locator('#coach .ds-btn.pri').click();
  await page.waitForTimeout(500);
  check('the tour opened and the coach card is gone for good', await visible('#tour') && !(await visible('#coach')) && (await page.evaluate(() => localStorage.getItem('jarvis_coach_v1'))) === '1');
  const stepInfo = async () => page.evaluate(() => { const r = document.getElementById('tour-ring').getBoundingClientRect(); const c = document.getElementById('tour-card').getBoundingClientRect(); const h = document.getElementById('device-screen').getBoundingClientRect(); return { title: document.getElementById('tour-title').innerText, step: document.getElementById('tour-step').innerText, next: document.getElementById('tour-next').innerText, ringInside: r.left >= h.left - 20 && r.right <= h.right + 20 && r.top >= h.top - 20 && r.bottom <= h.bottom + 20, cardInside: c.top >= h.top && c.bottom <= h.bottom && c.left >= h.left && c.right <= h.right, overlap: !(c.bottom < r.top || c.top > r.bottom), ringW: Math.round(r.width), ring: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)], host: [Math.round(h.left), Math.round(h.top), Math.round(h.right), Math.round(h.bottom)] }; });
  const seen = [];
  for (let i = 0; i < 4; i++) {
    await page.waitForTimeout(450);
    const s = await stepInfo(); seen.push(s);
    if (i < 3) await page.locator('#tour-next').click();
  }
  check('tour has four steps in order', seen.map(s => s.title).join('|') === 'כאן מדברים|או מקלידים|הלשוניות|המדריך', JSON.stringify(seen.map(s => s.title)));
  check('every step shows "n מתוך 4", the last says "סיום"', seen[0].step === '1 מתוך 4' && seen[3].step === '4 מתוך 4' && seen[3].next === 'סיום' && seen[0].next === 'הבא');
  check('the spotlight ring stays on screen for every step', seen.every(s => s.ringInside), JSON.stringify(seen));
  check('the tip card stays on screen and never covers what it points at', seen.every(s => s.cardInside && !s.overlap), JSON.stringify(seen));
  check('step 1 spotlights the orb (about its size plus padding)', seen[0].ringW >= 200 && seen[0].ringW <= 230, seen[0].ringW);
  await page.locator('#tour-next').click();
  await page.waitForTimeout(300);
  check('finishing closes the tour', !(await visible('#tour')));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.evaluate(() => { document.getElementById('gate-screen').style.display = 'none'; showWakeGate(); });
  await page.waitForTimeout(300);
  await page.locator('#wake-gate-btn').click({ force: true });
  await page.waitForTimeout(1500);
  check('after a reload the coach card does not come back', !(await visible('#coach')));

  // --- tour can be skipped and escaped
  await page.evaluate(() => startTour());
  await page.waitForTimeout(300);
  await page.keyboard.press('Escape');
  check('Escape ends the tour', !(await visible('#tour')));

  // --- guide
  await page.locator('#help-btn').click();
  await page.waitForTimeout(400);
  check('the guide opens from the header', await visible('#help-screen'));
  const helpText = await page.locator('#help-body').innerText();
  check('guide has the hero, what-to-say, how-it-works, tab-by-tab and troubleshooting sections', ['הכל מתחיל בקול', 'מה אפשר להגיד?', 'איך זה עובד', 'מסך אחרי מסך', 'משהו לא עובד?'].every(t => helpText.includes(t)), helpText.slice(0, 200));
  const cats = await texts(page, '.help-cat');
  check('six categories', JSON.stringify(cats) === JSON.stringify(['יומן', 'דואר', 'משימות', 'מידע', 'דפים', 'ניווט']), JSON.stringify(cats));
  check('the default category shows calendar phrases', (await texts(page, '.help-phrase')).includes('מה יש לי היום?'));
  await page.locator('.help-cat', { hasText: 'דפים' }).click();
  const web = await texts(page, '.help-phrase');
  check('the pages category shows the Wikipedia / flight / site phrases', web.length === 3 && web[0].includes('ויקיפדיה') && web[1].includes('טיסה') && web[2].includes('ynet'), JSON.stringify(web));
  check('every phrase row and category chip is at least 44px tall', await page.evaluate(() => [...document.querySelectorAll('.help-phrase, .help-cat')].every(b => b.getBoundingClientRect().height >= 44)));
  check('every troubleshooting question is a 44px+ row', await page.evaluate(() => [...document.querySelectorAll('.faq summary')].length === 5 && [...document.querySelectorAll('.faq summary')].every(s => s.getBoundingClientRect().height >= 44)));
  await page.locator('.faq summary', { hasText: 'אין לי התראות בטלפון' }).click();
  check('a question expands to its answer (iPhone home-screen requirement included)', (await page.locator('.faq[open] p').innerText()).includes('הוסף למסך הבית'));
  check('the sign-in FAQ carries the "approved accounts only" note that left Settings', (await page.locator('#help-body').evaluate(e => e.textContent)).includes('רק חשבונות שנוספו מראש'));
  const bodyHeights = await page.evaluate(() => { const b = document.getElementById('help-body'); return { sh: b.scrollHeight, ch: b.clientHeight }; });
  check('the guide scrolls (content is longer than the screen)', bodyHeights.sh > bodyHeights.ch, JSON.stringify(bodyHeights));
  await page.screenshot({ path: 'shots/step3_help.png' });

  // run:true phrase runs, write phrase only fills
  await page.locator('.help-cat', { hasText: 'יומן' }).click();
  const before = commands.length;
  await page.locator('.help-phrase', { hasText: 'מה יש לי היום?' }).click();
  await page.waitForTimeout(800);
  check('a read-only phrase is sent on tap, and the guide closes onto Home', commands[before] === 'מה יש לי היום?' && !(await visible('#help-screen')) && (await page.evaluate(() => currentTab)) === 'home', JSON.stringify({ c: commands.slice(before) }));
  await page.locator('#help-btn').click();
  await page.locator('.help-phrase', { hasText: 'קבע פגישה עם דנה' }).click();
  await page.waitForTimeout(500);
  const n = commands.length;
  check('a WRITE phrase is only placed in the command bar, not sent', (await page.inputValue('#cmd-input')) === 'קבע פגישה עם דנה מחר ב-10:00' && commands.length === n, JSON.stringify({ v: await page.inputValue('#cmd-input') }));
  await page.fill('#cmd-input', '');
  await page.locator('#help-btn').click();
  await page.keyboard.press('Escape');
  check('Escape closes the guide', !(await visible('#help-screen')));
  await page.locator('#help-btn').click();
  await page.locator('.help-hero .ds-btn').click();
  await page.waitForTimeout(300);
  check('the guide can start the tour', await visible('#tour') && !(await visible('#help-screen')));
  await page.locator('#tour .ds-btn:not(.pri)').click();

  // Settings has a way in
  await page.evaluate(() => setTab('settings'));
  await page.locator('#tab-settings .set', { hasText: 'מדריך ושאלות נפוצות' }).click();
  check('Settings has a "guide and FAQ" button that opens it', await visible('#help-screen'));
  await page.locator('.help-head .hdr-btn').click();
  check('the back button closes the guide', !(await visible('#help-screen')));
  check('no JS errors (first-run flow)', errors.length === 0, errors.join(' | '));
  await page.context().close();
}

// ============ mic notes
{
  const { page, errors } = await newPage(browser, { denied: true });
  await enterApp(page, BASE, 2500);
  const note = await page.evaluate(() => { const n = document.getElementById('mic-note'); return { shown: getComputedStyle(n).display !== 'none', text: n.innerText }; });
  check('a blocked microphone is explained on Home, with what to do', note.shown && note.text.includes('חסום') && note.text.includes('להקליד'), JSON.stringify(note));
  check('no JS errors (denied-mic run)', errors.length === 0, errors.join(' | '));
  await page.context().close();
}
{
  const { page } = await newPage(browser);
  await enterApp(page, BASE, 2500);
  const shown = await page.evaluate(() => getComputedStyle(document.getElementById('mic-note')).display !== 'none');
  check('a working / not-yet-asked microphone shows no note (nothing to explain)', !shown);
  await page.context().close();
}

await browser.close();
console.log('\n=== STEP 3: GUIDE ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 500)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
