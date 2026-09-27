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

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => { document.getElementById('gate-screen').style.display = 'none'; showWakeGate(); });
await page.waitForTimeout(400);
await page.locator('#wake-gate-btn').click({ force: true });
await page.waitForTimeout(1500);

// A real window.open would leave the page; record instead, and return a truthy handle so
// openPhoneLink() doesn't fall back to navigating this tab.
await page.evaluate(() => { window.__opened = []; window.open = (u) => { window.__opened.push(u); return {}; }; });

const modalShown = () => page.evaluate(() => document.getElementById('confirm-overlay').classList.contains('show'));
const toastText = () => page.evaluate(() => document.getElementById('toast').innerText);
const opened = () => page.evaluate(() => window.__opened.slice());
const fieldsText = () => page.evaluate(() => document.getElementById('confirm-read-fields').innerText);
const kindLabel = () => page.evaluate(() => document.getElementById('confirm-kind-label').innerText);
const detailTitle = () => page.evaluate(() => document.getElementById('confirm-detail-title').innerText);

const WIKI = { action: 'web', label: 'Wikipedia', host: 'en.wikipedia.org', target: 'Kendrick Lamar', query: 'Kendrick Lamar',
               url: 'https://en.wikipedia.org/wiki/Special:Search?search=Kendrick%20Lamar&go=Go' };
const show = (kind, details, message) => page.evaluate(([k, d, m]) => {
    showConfirmModal({ type: 'confirmation_required', kind: k, message: m, details: { ...d, ts: Date.now() / 1000 }, token: null });
}, [kind, details, message]);

// ---- A. card content + approve opens the exact URL
await show('web_page', WIKI, "Open Wikipedia: 'Kendrick Lamar'?");
check('A: confirm modal is shown', await modalShown());
check('A: title says "open web page"', (await kindLabel()) === 'פתיחת דף אינטרנט', await kindLabel());
check('A: headline is the topic', (await detailTitle()) === 'Kendrick Lamar', await detailTitle());
const fa = await fieldsText();
check('A: shows site name', fa.includes('Wikipedia'), fa);
check('A: shows the host being opened', fa.includes('en.wikipedia.org'), fa);
check('A: shows the topic row', fa.includes('Kendrick Lamar'), fa);
const accent = await page.evaluate(() => getComputedStyle(document.getElementById('confirm-kind-label')).color);
check('A: yellow accent like the other web kinds', accent === 'rgb(255, 200, 0)', accent);
await page.locator('#confirm-btn-row-view .confirm-btn.confirm').click();
check('A: approve opened exactly the proposed URL', JSON.stringify(await opened()) === JSON.stringify([WIKI.url]), JSON.stringify(await opened()));
check('A: toast confirms opening', (await toastText()) === 'פותח Wikipedia.', await toastText());
check('A: modal closed after approve', !(await modalShown()));

// ---- B. deny opens nothing
await show('web_page', WIKI, "Open Wikipedia: 'Kendrick Lamar'?");
await page.locator('#confirm-btn-row-view .confirm-btn.reject').click();
check('B: deny opened nothing', (await opened()).length === 1, JSON.stringify(await opened()));
check('B: deny toast', (await toastText()) === 'בסדר, לא פתחתי.', await toastText());
check('B: modal closed after deny', !(await modalShown()));

// ---- C. a spoken "yes" must NOT open a link (browsers block it; the existing rule for link kinds)
await show('web_page', WIKI, "Open Wikipedia: 'Kendrick Lamar'?");
await page.evaluate(() => handleConfirmationReply('כן'));
check('C: voice yes did not open the page', (await opened()).length === 1, JSON.stringify(await opened()));
check('C: voice yes points at the button instead', (await toastText()).includes('הקש על'), await toastText());
check('C: card still waiting for the tap', await modalShown());
await page.evaluate(() => handleConfirmationReply('לא'));
check('C: voice no dismisses it', !(await modalShown()));

// ---- D. website card: label == host, no duplicate rows
await show('web_page', { action: 'web', label: 'ynet.co.il', host: 'ynet.co.il', target: 'ynet.co.il', query: 'ynet.co.il', url: 'https://ynet.co.il' }, 'Open ynet.co.il?');
const fd = await fieldsText();
check('D: website card shows the host once', (fd.match(/ynet\.co\.il/g) || []).length === 1, fd);
await page.locator('#confirm-btn-row-view .confirm-btn.reject').click();

// ---- E. recovery poll: server payload shape (action:'web') becomes a web_page card
const flightUrl = 'https://www.google.com/travel/flights?q=Tel%20Aviv%20to%20Paris%20on%202026-10-10';
await page.route('**/api/phone-link/pending', route => route.fulfill({ json: {
    action: 'web', label: 'Google Flights', host: 'www.google.com', target: 'Tel Aviv to Paris on 2026-10-10',
    query: 'Tel Aviv to Paris on 2026-10-10', url: flightUrl, ts: Date.now() / 1000 + 60 } }));
await page.waitForFunction(() => document.getElementById('confirm-overlay').classList.contains('show'), null, { timeout: 12000 })
    .catch(() => {});
check('E: poll recovery raised the card', await modalShown());
check('E: recovered card is a web page card, not a navigation card', (await kindLabel()) === 'פתיחת דף אינטרנט', await kindLabel());
check('E: recovered card headline is the trip', (await detailTitle()).includes('Tel Aviv to Paris'), await detailTitle());
await page.unroute('**/api/phone-link/pending');
await page.locator('#confirm-btn-row-view .confirm-btn.confirm').click();
const openedE = await opened();
check('E: approve opens the flights URL', openedE[openedE.length - 1] === flightUrl, JSON.stringify(openedE));
check('E: toast names Google Flights', (await toastText()) === 'פותח Google Flights.', await toastText());

// ---- F. the branches I touched still behave for the older kinds
await show('phone_app', { action: 'navigate', label: 'Waze', target: 'Dizengoff Center', url: 'https://waze.com/ul?q=x&navigate=yes' }, 'Open Waze?');
check('F: navigation card keeps its own title', (await kindLabel()) === 'פתיחת אפליקציה', await kindLabel());
await page.locator('#confirm-btn-row-view .confirm-btn.confirm').click();
check('F: navigation approve toast unchanged', (await toastText()) === 'פותח Waze — נסיעה טובה.', await toastText());
await show('web_search', { query: 'pizza', url: 'https://www.google.com/maps/search/pizza' }, 'Search Maps?');
check('F: nearby-search card keeps its own title', (await kindLabel()) === 'הצעת חיפוש', await kindLabel());
await page.locator('#confirm-btn-row-view .confirm-btn.reject').click();
check('F: nearby-search deny toast unchanged', (await toastText()) === 'בסדר, לא חיפשתי.', await toastText());
await show('phone_app', { action: 'alarm', label: 'JARVIS Alarm', target: '7:00 AM', url: 'shortcuts://x' }, 'Alarm?');
await page.locator('#confirm-btn-row-view .confirm-btn.confirm').click();
check('F: alarm approve toast unchanged', (await toastText()).startsWith('מכוון שעון מעורר'), await toastText());

check('no JS errors during the whole run', errors.length === 0, errors.join(' | '));
await page.screenshot({ path: 'shots/web_page_card_end.png' });
await browser.close();

console.log('\n=== WEB PAGE CARD UI TEST ===');
let failed = 0;
for (const r of results) { console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n      ' + r.detail.slice(0, 300)}`); if (!r.ok) failed++; }
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
