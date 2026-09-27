import { chromium, devices } from 'playwright';
import fs from 'fs';
import { installMocks, enterApp, EVENTS, REMINDERS, TODAY } from './mockdata.mjs';

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
const commands = [];
await page.route('**/api/command', async route => { commands.push(JSON.parse(route.request().postData()).command); await route.fulfill({ json: { response: 'Canned reply, sir.', audio: null, speak: false, persona: 'jarvis' } }); });
let cancelBody = null;
await page.route('**/api/calendar/cancel-request', async route => { cancelBody = JSON.parse(route.request().postData()); await route.fulfill({ json: { status: 'confirmation_required', token: 'tok-cancel', kind: 'calendar_event_delete', message: 'Cancel?', details: { summary: cancelBody.summary } } }); });
let weeklyCalled = 0;
await page.route('**/api/weekly-summary/send-now', async route => { weeklyCalled++; await route.fulfill({ json: { message: 'נשלח.' } }); });
await installMocks(page);
await enterApp(page, BASE, 3000);
await page.evaluate(() => setTab('agenda'));
await page.waitForTimeout(900);

const texts = sel => page.$$eval(sel, els => els.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
const groupHeads = () => texts('#agenda-list .day-head');

// ---------- structure
check('page title', (await page.locator('#tab-agenda .page-title').innerText()) === 'היומן שלי');
check('subtitle is the long Hebrew date of the USER\'s today', (await page.locator('#agenda-sub').innerText()) === 'שבת, 26 בספטמבר', await page.locator('#agenda-sub').innerText());
const heads = await groupHeads();
check('day groups: today, tomorrow, Monday, Tuesday (the vacation reaches it), Wednesday (the recurring reminder)', heads.length === 5 && heads[0].startsWith('היום') && heads[1].startsWith('מחר') && heads[2].startsWith('יום ב׳') && heads[3].startsWith('יום ג׳') && heads[4].startsWith('יום ד׳'), JSON.stringify(heads));
check('no "Sun 03:00"-style English labels anywhere on the page', !/\b(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\b/.test(await page.locator('#tab-agenda').innerText()));
const allDay = await texts('#agenda-list .allday-chip');
check('the vacation shows "כל היום" on each of its three days', allDay.length === 3 && allDay.every(t => t === 'כל היום'), JSON.stringify(allDay));
const rowsText = await texts('#agenda-list .row-item');
check('multi-day event says which day of how many', rowsText.some(t => t.includes('יום 1 מתוך 3')) && rowsText.some(t => t.includes('יום 2 מתוך 3')) && rowsText.some(t => t.includes('יום 3 מתוך 3')), JSON.stringify(rowsText));
check('timed event shows start and end', rowsText.some(t => t.includes('20:00') && t.includes('22:00') && t.includes('ארוחת ערב משפחתית')), JSON.stringify(rowsText));
check('location is shown', rowsText.some(t => t.includes('קפה גרג')));
check('reminder sits in the timeline with its emoji and a "תזכורת" tag', rowsText.some(t => t.includes('להכין תיק לנופש') && t.includes('תזכורת') && t.includes('🎒')), JSON.stringify(rowsText));
check('recurring reminder is tagged with its weekday', rowsText.some(t => t.includes('לצאת עם הכלבה') && t.includes('כל רביעי')), JSON.stringify(rowsText));
check('the now line is present', await page.locator('#agenda-list .now-line').count() === 1);
check('one 44px action button per row, no icon-only red ✕ / emoji buttons', await page.evaluate(() => {
  const rows = [...document.querySelectorAll('#agenda-list .row-item')];
  return rows.length > 0 && rows.every(r => { const b = r.querySelector('.row-more'); const x = b.getBoundingClientRect(); return x.width >= 44 && x.height >= 44 && b.querySelector('svg'); }) && !document.querySelector('#agenda-list .cancel-x');
}));
check('the old today/week pills, reminder form and weekly-summary card are gone from the agenda', await page.evaluate(() => !document.getElementById('agenda-tab-week') && !document.getElementById('reminders-list') && !document.getElementById('tab-agenda').innerText.includes('סיכום שבועי')));
const fab = await page.evaluate(() => { const f = document.getElementById('fab'); const r = f.getBoundingClientRect(); const d = document.getElementById('dock').getBoundingClientRect(); return { shown: getComputedStyle(f).display !== 'none', label: f.innerText.trim(), above: r.bottom <= d.top, h: r.height }; });
check('create button is visible, labelled "הוסף", above the dock, 44px+', fab.shown && fab.label === 'הוסף' && fab.above && fab.h >= 44, JSON.stringify(fab));
await page.evaluate(() => setTab('home'));
check('the create button is hidden on Home', await page.evaluate(() => getComputedStyle(document.getElementById('fab')).display === 'none'));
await page.evaluate(() => setTab('agenda'));

// ---------- the now line lands in the right place
const nowIndex = async hhmm => {
  await page.evaluate(v => { window.nowHHMM = () => v; renderAgenda(); }, hhmm);
  return page.evaluate(() => [...document.querySelectorAll('#agenda-list > *')].filter(e => e.classList.contains('now-line') || e.classList.contains('row-item') || e.classList.contains('empty-card')).slice(0, 4).map(e => e.classList.contains('now-line') ? 'NOW' : e.innerText.replace(/\s+/g, ' ').slice(0, 14)));
};
let order = await nowIndex('19:00');
check('19:00 -> now line before the 20:00 dinner', order[0] === 'NOW', JSON.stringify(order));
order = await nowIndex('21:00');
check('21:00 -> between the 20:00 dinner and the 22:30 reminder', order[0].includes('20:00') && order[1] === 'NOW' && order[2].includes('22:30'), JSON.stringify(order));
order = await nowIndex('23:30');
check('23:30 -> after the last item of today', order[1].includes('22:30') && order[2] === 'NOW', JSON.stringify(order));
await page.evaluate(() => { delete window.nowHHMM; });
await page.evaluate(() => renderAgenda());

// ---------- actions
await page.locator('#agenda-list .row-item').first().click();
check('tapping a row opens its action sheet', await page.locator('#action-sheet.show').count() === 1);
check('event actions: change time / cancel event (danger)', JSON.stringify(await texts('#action-sheet-list .act-row')) === JSON.stringify(['שנה שעה', 'בטל אירוע']) && await page.locator('#action-sheet-list .act-row.danger').count() === 1);
await page.locator('#action-sheet-list .act-row.danger').click();
await page.waitForFunction(() => document.getElementById('confirm-overlay').classList.contains('show'), null, { timeout: 8000 }).catch(() => {});
check('cancelling sends the event id + title, and the confirm sheet appears (nothing is cancelled silently)', cancelBody && cancelBody.event_id === 'e2' && cancelBody.summary === 'ארוחת ערב משפחתית' && await page.locator('#confirm-overlay.show').count() === 1, JSON.stringify(cancelBody));
check('confirm sheet is the "cancel event" kind', (await page.locator('#confirm-kind-label').innerText()) === 'ביטול אירוע ביומן');
await page.locator('#confirm-btn-row-view .confirm-btn.reject').click().catch(() => {});
await page.evaluate(() => { hideConfirmModal(); pendingConfirmToken = null; });
await page.waitForTimeout(200);

// reminder row -> different actions
const remRow = page.locator('#agenda-list .row-item', { hasText: 'להכין תיק לנופש' });
await remRow.click();
check('reminder actions: edit / delete', JSON.stringify(await texts('#action-sheet-list .act-row')) === JSON.stringify(['ערוך תזכורת', 'מחק תזכורת']));
await page.locator('#action-sheet-list .act-row', { hasText: 'ערוך תזכורת' }).click();
await page.waitForTimeout(300);
check('edit opens the existing reminder-edit sheet pre-filled', await page.locator('#reminder-edit-sheet.show').count() === 1 && (await page.inputValue('#reminder-edit-text')) === 'להכין תיק לנופש');
await page.evaluate(() => closeReminderEditSheet());

// Escape closes an action sheet
await page.locator('#agenda-list .row-item').first().click();
await page.keyboard.press('Escape');
check('Escape closes the action sheet', await page.locator('#action-sheet.show').count() === 0);

// ---------- reschedule
await page.locator('#agenda-list .row-item').first().click();
await page.locator('#action-sheet-list .act-row', { hasText: 'שנה שעה' }).click();
await page.waitForTimeout(350);
check('reschedule sheet: title, no mode switch, time + day prefilled from the event', (await page.locator('#add-sheet-title').innerText()) === 'שינוי שעת פגישה' && await page.locator('#add-seg').evaluate(e => getComputedStyle(e).display === 'none') && (await page.inputValue('#add-time')) === '20:00' && (await texts('#add-day-pills .ds-pill.on'))[0] === 'היום', JSON.stringify({ t: await page.inputValue('#add-time'), on: await texts('#add-day-pills .ds-pill.on') }));
await page.fill('#add-time', '21:30');
await page.locator('#add-day-pills .ds-pill', { hasText: 'מחר' }).click();
const before = commands.length;
await page.locator('#add-submit').click();
await page.waitForTimeout(700);
check('reschedule sends the natural-language command with the new day+time', commands[before] === 'שנה את האירוע "ארוחת ערב משפחתית" ל-מחר בשעה 21:30', commands[before]);

// ---------- add a meeting
await page.locator('#fab').click();
await page.waitForTimeout(400);
check('add sheet opens on "meeting", tomorrow preselected, 30 min', (await page.locator('#add-sheet-title').innerText()) === 'הוסף ליומן' && (await texts('#add-day-pills .ds-pill.on'))[0] === 'מחר' && (await texts('#add-dur-pills .ds-pill.on'))[0] === '30 דק׳');
check('the note explains the approval step', (await page.locator('#add-note-text').innerText()).includes('יבקש את אישורך'));
check('the sheet shows seven days to pick from', await page.locator('#add-day-pills .ds-pill').count() === 7);
await page.fill('#add-title', 'בדיקה');
await page.fill('#add-time', '11:00');
await page.locator('#add-dur-pills .ds-pill', { hasText: 'שעה' }).click();
await page.locator('#add-more summary').click();
await page.fill('#add-location', 'זום');
const b2 = commands.length;
await page.locator('#add-submit').click();
await page.waitForTimeout(700);
check('meeting command carries title, day, time, duration and location', commands[b2] === 'קבע פגישה "בדיקה" מחר בשעה 11:00 למשך שעה במיקום זום', commands[b2]);
check('the sheet closes after submit', await page.locator('#add-sheet.show').count() === 0);

// ---------- add a reminder (time of day decides today vs tomorrow)
await page.evaluate(() => { window.nowHHMM = () => '20:15'; });
await page.locator('#fab').click();
await page.locator('#add-seg button[data-mode=reminder]').click();
check('reminder mode: label, no duration, recurring hint shown', (await page.locator('#add-what-label').innerText()) === 'מה להזכיר?' && await page.locator('#add-dur-block').evaluate(e => getComputedStyle(e).display === 'none') && await page.locator('#add-try').evaluate(e => getComputedStyle(e).display !== 'none'));
check('evening + 09:00 default -> tomorrow is preselected', (await texts('#add-day-pills .ds-pill.on'))[0] === 'מחר');
await page.locator('#add-submit').click();
check('an empty reminder is refused and the sheet stays open', await page.locator('#add-sheet.show').count() === 1 && (await page.locator('#toast').innerText()).includes('מה להזכיר'));
await page.fill('#add-title', 'להתקשר לדנה');
const b3 = commands.length;
await page.locator('#add-submit').click();
await page.waitForTimeout(700);
check('reminder command names the day and time', commands[b3] === 'תזכיר לי "להתקשר לדנה" מחר בשעה 09:00', commands[b3]);
await page.evaluate(() => { window.nowHHMM = () => '07:00'; });
await page.locator('#fab').click();
await page.locator('#add-seg button[data-mode=reminder]').click();
check('before 09:00 -> today is preselected', (await texts('#add-day-pills .ds-pill.on'))[0] === 'היום');
await page.locator('#add-try').click();
check('the recurring-reminder example fills the command bar', (await page.inputValue('#cmd-input')).includes('כל יום רביעי ב-15:00') && await page.locator('#add-sheet.show').count() === 0);
await page.fill('#cmd-input', '');
await page.evaluate(() => { delete window.nowHHMM; });
await page.locator('#fab').click();
await page.locator('.bsheet-overlay.show').click({ position: { x: 5, y: 5 } });
check('tapping the dimmed area closes the sheet', await page.locator('#add-sheet.show').count() === 0);

// ---------- states
const setData = async (events, reminders) => {
  await page.unroute('**/api/upcoming-events'); await page.unroute('**/api/reminders');
  await page.route('**/api/upcoming-events', r => r.fulfill({ json: events }));
  await page.route('**/api/reminders', r => r.request().method() === 'GET' ? r.fulfill({ json: reminders }) : r.continue());
  await page.evaluate(() => pollUpcomingEvents());
  await page.waitForTimeout(700);
};
await setData({ configured: true, today: TODAY, tz: 'Asia/Jerusalem', events: [] }, { reminders: [] });
const emptyText = await page.locator('#agenda-list .empty-card').innerText();
check('nothing at all: says the calendar is empty for 7 days and offers add buttons + an example', emptyText.includes('אין כלום ב-7 הימים הקרובים') && emptyText.includes('פגישה') && emptyText.includes('תזכורת') && emptyText.includes('קבע פגישה מחר ב-10'), emptyText);
await page.locator('#agenda-list .try-chip').click();
check('the example chip fills the command bar (tap-to-try)', (await page.inputValue('#cmd-input')) === 'קבע פגישה מחר ב-10');
await page.fill('#cmd-input', '');
await setData({ configured: true, today: TODAY, tz: 'Asia/Jerusalem', events: [EVENTS.events[2]] }, { reminders: [] });
check('free today but busy later: "היום פנוי", then tomorrow\'s items', (await page.locator('#agenda-list .empty-card h3').innerText()) === 'היום פנוי' && (await groupHeads())[1].startsWith('מחר'));
await setData({ configured: false, events: [] }, { reminders: REMINDERS.reminders });
const nc = await page.locator('#agenda-list .empty-card').first().innerText();
check('calendar not connected is NOT shown as "nothing scheduled"', nc.includes('היומן לא מחובר') && !nc.includes('אין כלום') && nc.includes('התחבר עם Google'), nc);
check('reminders still show when the calendar is not connected', (await texts('#agenda-list .row-item')).some(t => t.includes('להכין תיק לנופש')));
// an all-day event already under way shows under today
await setData({ configured: true, today: TODAY, tz: 'Asia/Jerusalem', events: [{ id: 'x', summary: 'חופשה שהתחילה', source: 'Google', location: '', all_day: true, day: '2026-09-25', end_day: '2026-09-27', start_iso: '', end_iso: '', time_label: '' }] }, { reminders: [] });
const ongoing = await texts('#agenda-list .row-item');
check('an all-day event that started yesterday appears today as day 2 of 3, and tomorrow as day 3', ongoing.length === 2 && ongoing[0].includes('יום 2 מתוך 3') && ongoing[1].includes('יום 3 מתוך 3'), JSON.stringify(ongoing));
await setData(EVENTS, REMINDERS);

// ---------- Home widget + greeting
await page.evaluate(() => setTab('home'));
await page.waitForTimeout(300);
await page.evaluate(() => { window.nowHHMM = () => '18:47'; renderHomeUpcoming(); });
const home = await texts('#home-upcoming-list .set');
check('Home "your day" reads in Hebrew: next event "20:00 · ארוחת ערב משפחתית" (הבא ביומן), then tasks', home[0].includes('20:00 · ארוחת ערב משפחתית') && home[0].includes('הבא ביומן') && home[1].includes('משימות'), JSON.stringify(home));
check('Home rows only navigate (each is one button to its tab; no edit/delete actions on Home)', await page.locator('#home-upcoming-list .row-more, #home-upcoming-list .act-row').count() === 0);
await page.evaluate(() => { delete window.nowHHMM; });
const greet = async (events, tasks) => page.evaluate(([ev, tk]) => { upcomingEventsCache = ev; homeTasksCache = tk; return summarizeUpcomingForGreeting(); }, [events, tasks]);
let g = await greet(EVENTS.events, [{ text: 'a' }, { text: 'b' }, { text: 'c' }]);
check('greeting counts only TODAY\'s events (the old one said "4 events today" for the next 7 days)', g.startsWith('You have one event today: ארוחת ערב משפחתית at 20:00.') && g.endsWith('You have 3 open tasks.'), g);
g = await greet(EVENTS.events.slice(1), []);
check('nothing today: says so, then what is next and when', g === 'Nothing on your calendar today. Next up: נופש, all day, tomorrow.', g);
g = await greet([], []);
check('nothing at all: clear day', g === 'Nothing on your schedule today, sir — a clear day.', g);
g = await greet([{ id: 'z', summary: 'Standup', all_day: false, day: TODAY, end_day: TODAY, start_iso: '2026-09-26T09:00:00+03:00' }, { id: 'y', summary: 'Lunch', all_day: false, day: TODAY, end_day: TODAY, start_iso: '2026-09-26T12:30:00+03:00' }], [{ text: 'a' }]);
check('several today: count + the nearest timed one + single task by name', g === 'You have 2 events today, the nearest being Standup at 09:00. You have one open task: a.', g);
await setData(EVENTS, REMINDERS);

// ---------- Settings gained the weekly summary
await page.evaluate(() => setTab('settings'));
check('weekly summary lives in Settings now', (await page.locator('#tab-settings').innerText()).includes('סיכום שבועי'));
await page.locator('#tab-settings .ds-btn', { hasText: 'שלח עכשיו' }).click();
await page.waitForTimeout(500);
check('its button still sends the summary', weeklyCalled === 1);

check('no JS errors', errors.length === 0, errors.join(' | '));
await browser.close();

console.log('\n=== STEP 2: AGENDA ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 400)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
