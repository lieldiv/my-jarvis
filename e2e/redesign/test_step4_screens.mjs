import { chromium, devices } from 'playwright';
import fs from 'fs';
import { installMocks, enterApp, TASKS, INBOX, EVENTS, REMINDERS } from './mockdata.mjs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync(new URL('../session.txt', import.meta.url), 'utf8').trim();
const results = [];
function check(label, cond, detail) { results.push({ label, ok: !!cond, detail: cond ? '' : String(detail ?? '') }); }

async function newPage(browser, over = {}, { denied = false } = {}) {
  const ctx = await browser.newContext({ ...devices['Pixel 7'], locale: 'he-IL' });
  await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
  const page = await ctx.newPage();
  const errors = [], calls = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/command', r => r.fulfill({ json: { response: 'ok', audio: null, speak: false, persona: 'jarvis' } }));
  await installMocks(page, over);
  const rec = (r, extra = {}) => calls.push({ m: r.request().method(), url: r.request().url(), body: r.request().postData(), ...extra });
  await page.route('**/api/tasks', async r => { if (r.request().method() === 'GET') return r.fallback(); rec(r); await r.fulfill({ json: { ok: true } }); });
  await page.route('**/api/tasks/*/complete', async r => { rec(r); await r.fulfill({ json: { ok: true } }); });
  await page.route('**/api/tasks/*', async r => { if (r.request().method() !== 'DELETE') return r.fallback(); rec(r); await r.fulfill({ json: { ok: true } }); });
  await page.route('**/api/inbox/draft-reply', async r => { rec(r); await r.fulfill({ json: { draft: 'שלום דנה, תודה על הסיכום.' } }); });
  await page.route('**/api/inbox/send-reply', async r => { rec(r); await r.fulfill({ json: { type: 'confirmation_required', kind: 'email', message: 'x', token: 't-mail', details: { to: 'dana@example.com', subject: 'Re: סיכום', body: 'שלום דנה' } } }); });
  await page.route('**/api/weekly-summary/send-now', async r => { rec(r); await r.fulfill({ json: { message: 'הסיכום נשלח.' } }); });
  await page.route('**/api/reset', async r => { rec(r); await r.fulfill({ json: { ok: true } }); });
  await page.route('**/api/inbox', async r => { if (r.request().method() === 'GET') calls.push({ m: 'GET', url: r.request().url() }); await r.fallback(); });
  if (denied) await page.addInitScript(() => { navigator.permissions.query = async () => ({ state: 'denied', onchange: null }); });
  await page.addInitScript(() => { try { localStorage.setItem('jarvis_coach_v1', '1'); } catch (e) {} });
  await enterApp(page, BASE, 2800);
  return { ctx, page, errors, calls };
}
const texts = (page, sel) => page.$$eval(sel, els => els.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
const visible = (page, sel) => page.evaluate(s => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0; }, sel);
const big = (page, sel) => page.evaluate(s => [...document.querySelectorAll(s)].filter(e => e.getBoundingClientRect().height > 0).every(e => { const r = e.getBoundingClientRect(); return r.height >= 43.5 && r.width >= 43.5; }), sel);
const goTab = async (page, key, wait = 900) => { await page.evaluate(k => setTab(k), key); await page.waitForTimeout(wait); };

const browser = await chromium.launch();

// ================================================================= TASKS
{
  const { ctx, page, errors, calls } = await newPage(browser);
  await goTab(page, 'tasks');
  check('Tasks: page title and the live counts', (await page.locator('#tab-tasks .page-title').innerText()) === 'המשימות שלי' && (await page.locator('#tasks-sub').innerText()) === '3 פתוחות · 1 הושלמו היום', await page.locator('#tasks-sub').innerText());
  const rows = await texts(page, '#tasks-list .task-row');
  check('Tasks: three open rows; the scheduled one (with its days) first', rows.length === 3 && rows[0].includes('להתאמן') && rows[0].includes('כל א׳, ג׳') && rows[1].includes('לקנות חלב'), JSON.stringify(rows));
  check('Tasks: every checkbox and ⋯ button is finger-sized (44px)', await big(page, '#tasks-list .chk') && await big(page, '#tasks-list .row-more'));
  check('Tasks: no red ✕ delete buttons any more, no old stats-first layout', (await page.locator('.task-delete-btn, .task-stats-card').count()) === 0 && !(await page.locator('#tasks-list').innerText()).includes('✕'));
  check('Tasks: checkboxes are real checkboxes for screen readers', (await page.locator('#tasks-list .chk[role="checkbox"][aria-checked="false"]').count()) === 3);
  check('Tasks: one create button, labelled "משימה", above the dock', (await visible(page, '#fab')) && (await page.locator('#fab-label').innerText()) === 'משימה');
  const prog = await page.evaluate(() => { const cols = [...document.querySelectorAll('#task-chart .task-bar-col')]; const x = i => cols[i].getBoundingClientRect().left; return { n: cols.length, first: cols[0].innerText.trim(), last: cols[6].innerText.trim(), firstRightOfLast: x(0) > x(6), week: document.getElementById('task-stat-week').innerText }; });
  check('Tasks: progress card — 7 days, the week runs right to left (Sunday on the right), weekly total 6', prog.n === 7 && prog.first === 'א' && prog.last === 'ש' && prog.firstRightOfLast && prog.week === '6', JSON.stringify(prog));

  // add sheet
  await page.locator('#fab').click();
  await page.waitForTimeout(600);
  check('Tasks: the create button opens a bottom sheet with the input focused', (await visible(page, '#task-sheet.show')) && (await page.evaluate(() => document.activeElement && document.activeElement.id)) === 'task-input');
  check('Tasks: recurrence chips are 44px tall', await big(page, '#task-sheet .task-recur-mode-chip') && await page.evaluate(() => [...document.querySelectorAll('#task-sheet .task-recur-day-chip')].every(b => b.getBoundingClientRect().height >= 43.5)));
  const dayX = await page.evaluate(() => { const x = d => document.querySelector(`.task-recur-day-chip[data-day="${d}"]`).getBoundingClientRect().left; return { sun: x('Sunday'), sat: x('Saturday') }; });
  check('Tasks: the day picker follows Hebrew reading order (א׳ on the right, ש׳ on the left)', dayX.sun > dayX.sat, JSON.stringify(dayX));
  await page.locator('#task-sheet .ds-btn.pri').click();
  await page.waitForTimeout(300);
  check('Tasks: an empty task is refused with a hint, the sheet stays open', (await visible(page, '#task-sheet.show')) && (await page.locator('#toast').innerText()).includes('מה צריך לעשות') && !calls.some(c => c.m === 'POST' && /\/api\/tasks$/.test(c.url)));
  await page.fill('#task-input', 'לקנות ביצים');
  await page.locator('.task-recur-mode-chip[data-mode="daily"]').click();
  await page.locator('#task-sheet .ds-btn.pri').click();
  await page.waitForTimeout(500);
  const post1 = calls.find(c => c.m === 'POST' && /\/api\/tasks$/.test(c.url));
  check('Tasks: "every day" task is posted with its text and recurrence, and the sheet closes', post1 && JSON.parse(post1.body).text === 'לקנות ביצים' && JSON.parse(post1.body).recurring_day === 'daily' && !(await visible(page, '#task-sheet.show')), JSON.stringify(post1));
  await page.locator('#fab').click();
  await page.waitForTimeout(500);
  check('Tasks: the sheet opens clean the next time (no leftover text or days)', (await page.inputValue('#task-input')) === '' && (await page.locator('.task-recur-mode-chip.active').getAttribute('data-mode')) === 'none');
  await page.fill('#task-input', 'שיעורי בית');
  await page.locator('.task-recur-day-chip[data-day="Sunday"]').click();
  await page.locator('.task-recur-day-chip[data-day="Tuesday"]').click();
  await page.locator('#task-input').press('Enter');
  await page.waitForTimeout(500);
  const post2 = calls.filter(c => c.m === 'POST' && /\/api\/tasks$/.test(c.url))[1];
  check('Tasks: specific days post as "Sunday,Tuesday" (Enter submits)', post2 && JSON.parse(post2.body).recurring_day === 'Sunday,Tuesday', JSON.stringify(post2));
  await page.locator('#fab').click();
  await page.waitForTimeout(400);
  await page.keyboard.press('Escape');
  check('Tasks: Escape closes the sheet', !(await visible(page, '#task-sheet.show')));

  // complete + delete
  await page.locator('#tasks-list .task-row').first().locator('.chk').click();
  await page.waitForTimeout(400);
  check('Tasks: ticking the circle completes that task', calls.some(c => c.m === 'POST' && /\/api\/tasks\/2\/complete$/.test(c.url)), JSON.stringify(calls.map(c => c.url)));
  await page.locator('#tasks-list .task-row').first().locator('.row-more').click();
  await page.waitForTimeout(300);
  check('Tasks: ⋯ opens a sheet whose only action is the red "מחק משימה"', (await texts(page, '#action-sheet .act-row')).join('|') === 'מחק משימה' && (await page.locator('#action-sheet .act-row.danger').count()) === 1, JSON.stringify(await texts(page, '#action-sheet .act-row')));
  await page.locator('#action-sheet .act-row.danger').click();
  await page.waitForTimeout(400);
  check('Tasks: deleting goes through the sheet (one deliberate tap), not a stray ✕', calls.some(c => c.m === 'DELETE' && /\/api\/tasks\/2$/.test(c.url)));

  // Home follows
  await goTab(page, 'home', 500);
  check('no JS errors (tasks run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}
{ // empty + all done
  const { ctx, page } = await newPage(browser, { tasks: { tasks: [] } });
  await goTab(page, 'tasks');
  const card = await page.locator('#tasks-list .empty-card').innerText();
  check('Tasks empty: says so, offers an example to say; the single create button is the FAB (no duplicate)', card.includes('אין משימות פתוחות') && card.includes('תוסיף משימה: לקנות חלב') && (await page.locator('#tasks-list .empty-card .ds-btn').count()) === 0, card);
  await page.locator('#tasks-list .try-chip').click();
  check('Tasks empty: the example chip fills the command bar', (await page.inputValue('#cmd-input')) === 'תוסיף משימה: לקנות חלב');
  await ctx.close();
}
{
  const { ctx, page } = await newPage(browser, { tasks: { tasks: [{ id: 5, text: 'לשלוח חשבונית', recurring_day: 'daily', done_this_period: true }] } });
  await goTab(page, 'tasks');
  const done = await page.locator('#tasks-list').innerText();
  check('Tasks all done: "הכל סגור להיום", the finished ones listed under "הושלמו", their checkboxes locked and ticked', done.includes('הכל סגור להיום') && done.includes('הושלמו') && (await page.locator('#tasks-list .chk[aria-checked="true"][disabled]').count()) === 1, done);
  await ctx.close();
}

// ================================================================= INBOX
{
  const { ctx, page, errors, calls } = await newPage(browser);
  await goTab(page, 'inbox');
  check('Inbox: title and an honest count ("unread", connected to Gmail)', (await page.locator('#tab-inbox .page-title').innerText()) === 'הדואר שלי' && (await page.locator('#inbox-sub').innerText()) === '2 הודעות שלא נקראו · מחובר ל-Gmail', await page.locator('#inbox-sub').innerText());
  const cards = await texts(page, '#inbox-list .mail-card');
  check('Inbox: two cards with sender, how long ago, subject and preview', cards.length === 2 && cards[0].includes('דנה כהן') && cards[0].includes('לפני שעתיים') && cards[0].includes('סיכום הפגישה של אתמול') && cards[1].includes('אתמול') && cards[1].includes('Google Cloud'), JSON.stringify(cards));
  check('Inbox: the preview is clamped to two lines; no meaningless "unread" dots (everything listed is unread)', (await page.evaluate(() => getComputedStyle(document.querySelector('.mail-preview')).webkitLineClamp)) === '2' && (await page.locator('.mail-unread').count()) === 0);
  check('Inbox: "השב עם JARVIS" and the refresh button are 44px', await big(page, '.mail-card .ds-btn') && await big(page, '#tab-inbox .hdr-btn'));
  check('Inbox: one create button "מייל חדש"', (await visible(page, '#fab')) && (await page.locator('#fab-label').innerText()) === 'מייל חדש');

  await page.locator('.mail-card .ds-btn').first().click();
  await page.waitForTimeout(300);
  check('Inbox: the reply box opens inside the card', await visible(page, '#inbox-reply-0.show') && (await page.locator('#inbox-instr-0').getAttribute('placeholder')) === 'מה תרצה להגיד?');
  await page.fill('#inbox-instr-0', 'תגיד שאני מאשר');
  await page.locator('#inbox-reply-0 .ds-btn', { hasText: 'נסח' }).click();
  await page.waitForTimeout(600);
  const dr = calls.find(c => /draft-reply$/.test(c.url));
  check('Inbox: "נסח" sends the instruction and shows the draft, with a send-for-approval button', dr && JSON.parse(dr.body).instructions === 'תגיד שאני מאשר' && (await page.inputValue('#inbox-draft-0')) === 'שלום דנה, תודה על הסיכום.' && (await visible(page, '#inbox-send-row-0')));
  // a background refresh must not rebuild the list under someone who is typing
  await page.fill('#inbox-draft-0', 'שלום דנה, אני מאשר.');
  await page.evaluate(() => loadInboxTab(true));
  await page.waitForTimeout(800);
  check('Inbox: a refresh while a reply is open does not wipe what was typed', (await page.inputValue('#inbox-draft-0')) === 'שלום דנה, אני מאשר.');
  await page.locator('#inbox-send-row-0 .ds-btn.pri').click();
  await page.waitForTimeout(600);
  const sr = calls.find(c => /send-reply$/.test(c.url));
  check('Inbox: sending goes to the approval sheet first — nothing is sent directly', sr && JSON.parse(sr.body).to === 'dana@example.com' && (await visible(page, '#confirm-overlay.show')), JSON.stringify(sr));
  await page.evaluate(() => { hideConfirmModal(); pendingConfirmToken = null; });
  const before = calls.filter(c => c.m === 'GET' && /\/api\/inbox$/.test(c.url)).length;
  await page.locator('#tab-inbox .hdr-btn').click();
  await page.waitForTimeout(600);
  check('Inbox: the refresh button asks the server again', calls.filter(c => c.m === 'GET' && /\/api\/inbox$/.test(c.url)).length === before + 1);
  await page.locator('#fab').click();
  await page.waitForTimeout(400);
  check('Inbox: "מייל חדש" opens the compose sheet', await visible(page, '#compose-sheet.show'));
  await page.evaluate(() => closeComposeModal());
  check('no JS errors (inbox run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}
{
  const many = { configured: true, emails: Array.from({ length: 8 }, (_, i) => ({ sender_name: 'שולח ' + i, sender_email: `s${i}@x.com`, subject: 'נושא ' + i, snippet: 'תוכן', date_ms: Date.now() - i * 3600000, source: 'Google' })) };
  const { ctx, page } = await newPage(browser, { inbox: many });
  await goTab(page, 'inbox');
  check('Inbox: at the fetch limit it says "8+", not a made-up exact count', (await page.locator('#inbox-sub').innerText()).startsWith('8+ הודעות שלא נקראו'), await page.locator('#inbox-sub').innerText());
  const u = await page.evaluate(() => [unreadText(1), unreadText(5), unreadText(8)]);
  check('Inbox: singular / plural / limit wording', JSON.stringify(u) === JSON.stringify(['הודעה אחת שלא נקראה', '5 הודעות שלא נקראו', '8+ הודעות שלא נקראו']), JSON.stringify(u));
  await ctx.close();
}
{
  const { ctx, page } = await newPage(browser, { inbox: { configured: true, emails: [] } });
  await goTab(page, 'inbox');
  const t = await page.locator('#inbox-list .empty-card').innerText();
  check('Inbox empty: "תיבת הדואר ריקה" with an example to say, no second refresh button', t.includes('תיבת הדואר ריקה') && t.includes('שלח מייל ל-dana@gmail.com') && (await page.locator('#inbox-list .empty-card .ds-btn').count()) === 0, t);
  await ctx.close();
}
{
  const { ctx, page } = await newPage(browser, { inbox: { configured: false, emails: [] } });
  await goTab(page, 'inbox');
  const t = await page.locator('#inbox-list .empty-card').innerText();
  check('Inbox not connected: says so and offers "התחבר עם Google" (never "empty")', t.includes('הדואר לא מחובר') && t.includes('התחבר עם Google') && !t.includes('ריקה'), t);
  check('Inbox not connected: no "new mail" button that could not work', !(await visible(page, '#fab')));
  await ctx.close();
}

// ================================================================= HOME: "your day"
{
  const { ctx, page, errors } = await newPage(browser);
  await page.evaluate(() => { window.nowHHMM = () => '18:47'; renderHomeUpcoming(); });
  await page.waitForTimeout(300);
  const rows = await texts(page, '#home-upcoming-list .set');
  check('Home: "היום שלך" has three rows — next event, tasks, mail', rows.length === 3 && rows[0].includes('20:00 · ארוחת ערב משפחתית') && rows[0].includes('הבא ביומן') && rows[1].includes('3 משימות פתוחות') && rows[1].includes('לקנות חלב') && rows[2].includes('2 הודעות שלא נקראו') && rows[2].includes('מאת דנה כהן'), JSON.stringify(rows));
  check('Home: the old "האירועים הקרובים" panel, its expand toggle and the fixed "Google מחובר" footer are gone', !(await page.locator('body').innerHTML()).includes('home-upcoming-toggle') && !(await page.locator('#tab-home').innerText()).includes('האירועים הקרובים') && !(await page.locator('#tab-home').innerText()).includes('Google מחובר'));
  check('Home: every row is a 44px+ button', await big(page, '#home-upcoming-list .set') && (await page.locator('#home-upcoming-list button.set').count()) === 3);
  const target = async i => { await page.locator('#home-upcoming-list .set').nth(i).click(); await page.waitForTimeout(200); const t = await page.evaluate(() => currentTab); await page.evaluate(() => setTab('home')); return t; };
  check('Home: the rows open the Agenda, Tasks and Inbox', [await target(0), await target(1), await target(2)].join() === 'agenda,tasks,inbox');
  const next = async (hhmm) => page.evaluate(h => { window.nowHHMM = () => h; renderHomeUpcoming(); return document.querySelector('#home-upcoming-list .set').innerText.replace(/\s+/g, ' ').trim(); }, hhmm);
  const at21 = await next('22:10');
  check('Home: once the dinner is over (ends 22:00), the next thing is the 22:30 reminder', at21.includes('22:30 · להכין תיק לנופש') && at21.includes('התזכורת הבאה'), at21);
  const at23 = await next('23:30');
  check('Home: nothing left today → tomorrow\'s first timed thing, and it says today is done', at23.includes('מחר 10:30 · פגישה עם נימרוד') && at23.includes('אין עוד להיום'), at23);
  const at1930 = await next('20:30');
  check('Home: an event in progress says so', at1930.includes('ארוחת ערב משפחתית') && at1930.includes('מתרחש עכשיו'), at1930);
  const set = async (ev, rem, tasks) => page.evaluate(([e, r, t]) => { upcomingEventsCache = e.events; upcomingConfigured = e.configured; homeRemindersCache = r; homeTasksCache = t; window.nowHHMM = () => '18:47'; renderHomeUpcoming(); return document.querySelector('#home-upcoming-list').innerText.replace(/\s+/g, ' ').trim(); }, [ev, rem, tasks]);
  let t1 = await set({ configured: true, events: [] }, [], []);
  check('Home: nothing scheduled, no tasks → says exactly that (connected calendar)', t1.includes('אין כלום בקרוב') && t1.includes('אין משימות פתוחות'), t1);
  t1 = await set({ configured: false, events: [] }, [], [{ text: 'a' }]);
  check('Home: calendar not connected is not shown as an empty day; one task reads "משימה פתוחה אחת"', t1.includes('היומן לא מחובר') && !t1.includes('אין כלום בקרוב') && t1.includes('משימה פתוחה אחת'), t1);
  check('no JS errors (home run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}

// ================================================================= SETTINGS
{
  const { ctx, page, errors, calls } = await newPage(browser);
  await goTab(page, 'settings');
  check('Settings: title "הגדרות", groups in the approved order', (await page.locator('#tab-settings .page-title').innerText()) === 'הגדרות' && JSON.stringify(await texts(page, '#tab-settings .settings-sub')) === JSON.stringify(['קול והתראות', 'מראה', 'מתקדם', 'עזרה']), JSON.stringify(await texts(page, '#tab-settings .settings-sub')));
  const heads = await page.evaluate(() => [...document.querySelectorAll('#tab-settings .settings-sub')].map(e => { const c = getComputedStyle(e); return { size: parseFloat(c.fontSize), weight: +c.fontWeight, shadow: c.textShadow !== 'none', white: c.color }; }));
  check('Settings: group titles stay big and bold with the glow (the earlier request: "clear and easy to see")', heads.every(h => h.size >= 17 && h.weight >= 700 && h.shadow), JSON.stringify(heads));
  const st = await page.locator('#tab-settings').innerText();
  check('Settings: no fake "all systems normal" card, no ◂ glyphs, no emoji anywhere', !st.includes('כל המערכות תקינות') && !st.includes('◂') && !/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B50}\u{23F0}-\u{23FF}]/u.test(st) && (await page.locator('.status-card, .conn-row').count()) === 0, st.slice(0, 200));
  check('Settings: connections row shows the real state ("מחובר")', (await page.locator('#conn-status').innerText()).includes('מחובר') && (await page.locator('#conn-status.ok').count()) === 1, await page.locator('#conn-status').innerText());
  check('Settings: every control is finger-sized (44px)', await big(page, '#tab-settings button, #tab-settings summary'));

  // mic test drawer
  check('Settings: the mic test is a closed drawer by default', (await page.locator('details.set-more').first().getAttribute('open')) === null);
  await page.locator('summary', { hasText: 'בדיקת מיקרופון' }).click();
  await page.waitForTimeout(300);
  check('Settings: opening it shows one clear start button (no emoji)', (await page.locator('#voice-diag .ds-btn').innerText()).includes('התחל בדיקה') && (await page.locator('#voice-diag .ds-btn svg').count()) === 1);
  // auto-stop switch
  const sw = () => page.locator('#autostop-row').getAttribute('aria-checked');
  const was = await sw();
  await page.locator('#autostop-row').click();
  await page.waitForTimeout(200);
  const now = await sw();
  await page.locator('#autostop-row').click();
  check('Settings: auto-stop is a real switch (aria-checked flips, and flips back)', was === 'true' && now === 'false' && (await sw()) === 'true', `${was} ${now}`);
  // push row
  check('Settings: notifications row starts with "הפעל" only', (await visible(page, '#push-enable-btn')) && !(await visible(page, '#push-test-btn')));
  await page.evaluate(() => markPushEnabledUi());
  check('Settings: once on, it shows "active" and offers a test instead', !(await visible(page, '#push-enable-btn')) && (await visible(page, '#push-test-btn')) && (await page.locator('#push-status-text').innerText()).includes('פעילות'));
  // theme
  const sws = await page.$$eval('#theme-row .theme-swatch', els => els.map(e => e.tagName + ':' + e.getAttribute('aria-pressed')));
  check('Settings: colour swatches are real buttons with a pressed state', sws.length === 6 && sws.every(s => s.startsWith('BUTTON')) && sws.filter(s => s.endsWith('true')).length === 1, JSON.stringify(sws));
  const solidCyan = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--surface-solid').trim());
  await page.locator('#theme-row .theme-swatch[aria-label="ורוד"]').click();
  await page.waitForTimeout(300);
  const solidPink = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--surface-solid').trim());
  check('Settings: cards and dock follow the chosen colour, and are fully opaque', solidCyan === 'rgb(6,26,48)' && solidPink === 'rgb(44,8,28)', `${solidCyan} / ${solidPink}`);
  await page.evaluate(() => setTheme('cyan'));
  // shortcuts drawer
  await page.locator('summary', { hasText: 'קיצורי דרך' }).click();
  await page.waitForTimeout(300);
  check('Settings: shortcuts drawer has its form (2 inputs, add button) and the iPhone-only note', (await page.locator('#shortcut-registry .ds-inp').count()) === 2 && (await page.locator('#shortcut-registry').innerText()).includes('הוסף קיצור') && (await page.locator('#shortcut-registry').innerText()).includes('באייפון'));
  await page.fill('#sc-name', 'הפעלת מוזיקה');
  await page.locator('#shortcut-registry .ds-btn', { hasText: 'הוסף קיצור' }).click();
  await page.waitForTimeout(300);
  check('Settings: a registered shortcut becomes a row with a 44px remove button', (await page.locator('#shortcut-registry .sc-row').count()) === 1 && await big(page, '#shortcut-registry .sc-del'));
  await page.locator('#shortcut-registry .sc-del').click();
  check('Settings: removing it works', (await page.locator('#shortcut-registry .sc-row').count()) === 0);
  // action rows
  await page.locator('#tab-settings .ds-btn', { hasText: 'שלח עכשיו' }).click();
  await page.waitForTimeout(400);
  check('Settings: weekly summary "שלח עכשיו" calls the server', calls.some(c => /weekly-summary\/send-now$/.test(c.url)));
  await page.locator('#tab-settings .set', { hasText: 'איפוס זיכרון שיחה' }).click();
  await page.waitForTimeout(400);
  check('Settings: reset memory calls the server and confirms in a toast', calls.some(c => /\/api\/reset$/.test(c.url)) && (await page.locator('#toast').innerText()).includes('הזיכרון אופס'));
  await page.locator('#tab-settings .set', { hasText: 'מדריך ושאלות נפוצות' }).click();
  await page.waitForTimeout(300);
  check('Settings: the guide row opens the guide', await visible(page, '#help-screen.show'));
  await page.locator('.help-head .hdr-btn').click();
  const last = await page.evaluate(() => { const b = [...document.querySelectorAll('#tab-settings > *')].pop(); return { text: b.innerText.trim(), red: getComputedStyle(b).color }; });
  check('Settings: sign-out is the last thing, in red, alone', last.text === 'התנתקות' && /255, 59, 59/.test(last.red), JSON.stringify(last));
  check('no JS errors (settings run)', errors.length === 0, errors.join(' | '));
  await ctx.close();
}
{
  const { ctx, page } = await newPage(browser, { events: { configured: false, events: [], today: '2026-09-26', tz: 'Asia/Jerusalem' } });
  await goTab(page, 'settings');
  check('Settings: a broken connection shows "התחבר" (not a fixed green tick)', (await page.locator('#conn-status .ds-btn').innerText()) === 'התחבר' && (await page.locator('#conn-status.bad').count()) === 1);
  await ctx.close();
}

// ================================================================= ORB
{
  const { ctx, page, errors } = await newPage(browser);
  const o = await page.evaluate(() => {
    const b = document.getElementById('orb-body').getBoundingClientRect(), rim = getComputedStyle(document.querySelector('.orb-rim')), stage = document.getElementById('orb-stage');
    return { w: Math.round(b.width), h: Math.round(b.height), stage: stage.className, rim: rim.borderTopColor, label: document.getElementById('orb-state-label').innerText, labelClass: document.getElementById('orb-state-label').className,
      cta: document.getElementById('orb-cta-text').innerText, ctaShown: getComputedStyle(document.getElementById('orb-cta-text')).display !== 'none', chip: document.querySelector('#orb-status-icon use').getAttribute('href') };
  });
  check('Orb: still 200px (the size the user asked to keep)', o.w === 200 && o.h === 200, JSON.stringify(o));
  check('Orb: resting state is calm cyan (not red), not "blocked"', o.stage.includes('state-ready') && !o.stage.includes('blocked') && /rgba?\(0, ?243, ?255/.test(o.rim), JSON.stringify(o));
  check('Orb: one instruction under it, none duplicated inside; the chip shows a live mic', o.label === 'לחץ על הכדור ודבר' && !o.ctaShown && o.chip === '#i-mic', JSON.stringify(o));
  await page.evaluate(() => { micPermissionState = 'denied'; updateMicNote(); });
  await page.waitForTimeout(900);   // the rim colour eases over .6s
  const bl = await page.evaluate(() => ({ stage: document.getElementById('orb-stage').className, label: document.getElementById('orb-state-label').innerText, chip: document.querySelector('#orb-status-icon use').getAttribute('href'), rim: getComputedStyle(document.querySelector('.orb-rim')).borderTopColor }));
  check('Orb: a blocked microphone turns it red, says "המיקרופון חסום", and the chip shows the slashed mic', bl.stage.includes('blocked') && bl.label === 'המיקרופון חסום' && bl.chip === '#i-micoff' && /255, ?59, ?59/.test(bl.rim), JSON.stringify(bl));
  await page.evaluate(() => { micPermissionState = 'granted'; updateMicNote(); });
  check('Orb: allowing the microphone brings the calm state back', !(await page.evaluate(() => document.getElementById('orb-stage').className)).includes('blocked'));
  await page.evaluate(() => { textOnlyMode = true; micPermissionState = 'denied'; renderOrb(); });
  const to = await page.evaluate(() => ({ stage: document.getElementById('orb-stage').className, cta: document.getElementById('orb-cta-text').innerText }));
  check('Orb: someone who chose text-only on purpose is not shown an error', !to.stage.includes('blocked') && to.cta === 'הקש כדי להקליד', JSON.stringify(to));
  await ctx.close();
  const d = await newPage(browser, {}, { denied: true });
  const dn = await d.page.evaluate(() => ({ stage: document.getElementById('orb-stage').className, note: document.getElementById('mic-note').innerText }));
  check('Orb: a browser that reports "denied" from the start opens blocked, with the explanation under it', dn.stage.includes('blocked') && dn.note.includes('חסום'), JSON.stringify(dn));
  await d.ctx.close();
}

// ================================================================= the create button steps up over a reply
{
  const { ctx, page } = await newPage(browser);
  await goTab(page, 'tasks');
  const before = await page.locator('#fab').boundingBox();
  await page.evaluate(() => showReplyBubble('תשובה ארוכה מספיק כדי לתפוס כמה שורות בבועה שנפתחת מעל שורת הפקודה, כך שיהיה ברור אם היא מכסה משהו.'));
  await page.waitForTimeout(500);
  const fab = await page.locator('#fab').boundingBox(), bub = await page.locator('#reply-bubble').boundingBox();
  check('Reply bubble: the create button moves up above it instead of hiding underneath', fab.y + fab.height <= bub.y + 1 && fab.y < before.y, JSON.stringify({ fab, bub, before }));
  await page.evaluate(() => hideReplyBubble());
  await page.waitForTimeout(500);
  const after = await page.locator('#fab').boundingBox();
  check('Reply bubble: ...and comes back down when it goes', Math.abs(after.y - before.y) < 1.5, JSON.stringify({ after, before }));
  await ctx.close();
}

await browser.close();
console.log('\n=== STEP 4: TASKS / INBOX / HOME / SETTINGS / ORB ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 500)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
