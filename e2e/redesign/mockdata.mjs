// Realistic API payloads in the NEW shape (structured dates, server-side "today"), shared by the step-2+ tests.
export const TODAY = '2026-09-26';
export const EVENTS = { configured: true, today: TODAY, tz: 'Asia/Jerusalem', now_iso: '2026-09-26T18:47:00+03:00', events: [
  { id: 'e2', summary: 'ארוחת ערב משפחתית', source: 'Google', location: 'בית של סבתא', all_day: false, day: '2026-09-26', end_day: '2026-09-26', start_iso: '2026-09-26T20:00:00+03:00', end_iso: '2026-09-26T22:00:00+03:00', time_label: 'Sat 20:00' },
  { id: 'e1', summary: 'נופש', source: 'Google', location: '', all_day: true, day: '2026-09-27', end_day: '2026-09-29', start_iso: '', end_iso: '', time_label: 'Sun (all day)' },
  { id: 'e3', summary: 'פגישה עם נימרוד', source: 'Google', location: 'קפה גרג', all_day: false, day: '2026-09-27', end_day: '2026-09-27', start_iso: '2026-09-27T10:30:00+03:00', end_iso: '2026-09-27T11:30:00+03:00', time_label: 'Sun 10:30' },
  { id: 'e4', summary: 'רופא שיניים', source: 'Google', location: '', all_day: false, day: '2026-09-28', end_day: '2026-09-28', start_iso: '2026-09-28T16:00:00+03:00', end_iso: '2026-09-28T16:45:00+03:00', time_label: 'Mon 16:00' },
] };
export const REMINDERS = { reminders: [
  { id: 1, text: 'להכין תיק לנופש', label: 'שבת, 26.9 בשעה 22:30', recurring: false, day: '2026-09-26', time: '22:30', remind_at: 1790000000, weekday: null, hour: null, minute: null, emoji: '🎒' },
  { id: 2, text: 'לצאת עם הכלבה', label: 'כל רביעי בשעה 15:00', recurring: true, day: '2026-09-30', time: '15:00', remind_at: 1790400000, weekday: 2, hour: 15, minute: 0, emoji: '' },
] };
export const TASKS = { tasks: [
  { id: 1, text: 'לקנות חלב', recurring_day: null, done_this_period: false },
  { id: 2, text: 'להתאמן', recurring_day: 'Sunday,Tuesday', done_this_period: false },
  { id: 3, text: 'לשלם ארנונה', recurring_day: null, done_this_period: false },
] };
export const STATS = { today: 1, week: 6, daily: ['א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ש'].map((l, i) => ({ label: l, count: [1, 0, 2, 1, 0, 2, 0][i], is_today: i === 6 })) };
const HOUR = 3600 * 1000;
export const INBOX = { configured: true, emails: [
  { sender_name: 'דנה כהן', sender_email: 'dana@example.com', subject: 'סיכום הפגישה של אתמול', snippet: 'היי, מצרפת את הסיכום ואת המשימות שהחלטנו עליהן. תגידי לי אם משהו חסר…', date_ms: Date.now() - 2 * HOUR - 60000, source: 'Google' },
  { sender_name: 'Google Cloud', sender_email: 'noreply@google.com', subject: 'Your monthly billing summary', snippet: 'Here is your billing summary for August. Total charges: $0.00', date_ms: Date.now() - 26 * HOUR, source: 'Google' },
] };
export async function installMocks(page, over = {}) {
  await page.route('**/api/upcoming-events', r => r.fulfill({ json: over.events ?? EVENTS }));
  await page.route('**/api/reminders', r => r.request().method() === 'GET' ? r.fulfill({ json: over.reminders ?? REMINDERS }) : r.continue());
  await page.route('**/api/tasks', r => r.request().method() === 'GET' ? r.fulfill({ json: over.tasks ?? TASKS }) : r.continue());
  await page.route('**/api/tasks/stats', r => r.fulfill({ json: over.stats ?? STATS }));
  await page.route('**/api/inbox', r => r.request().method() === 'GET' ? r.fulfill({ json: over.inbox ?? INBOX }) : r.continue());
  await page.route('**/api/speak', r => r.fulfill({ json: { audio: null } }));
}
export async function enterApp(page, base, waitMs = 2500) {
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => { document.getElementById('gate-screen').style.display = 'none'; showWakeGate(); });
  await page.waitForTimeout(400);
  await page.locator('#wake-gate-btn').click({ force: true });
  await page.waitForTimeout(waitMs);
}
