/* Drive the REAL page with a controllable speech recogniser.

   The desktop is broken and the desktop is the reference for what "working"
   looks like, so this stops reading code and reproduces the turn: tap, speak,
   see whether the command is sent and the reply is spoken. Chromium's own
   recogniser does nothing useful headless, so a fake one is installed before
   the page loads — the same events in the same order WebKit and Chrome fire.
*/
import { chromium } from 'playwright';
import fs from 'fs';

const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SESSION = fs.readFileSync('C:/Users/User/Desktop/האפליקציה הסופית/e2e/session.txt', 'utf8').trim();

const FAKE_RECOGNISER = () => {
  window.__recog = { instances: [], starts: 0, aborts: 0, stops: 0 };
  function Fake(){
    const self = this;
    this.lang = ''; this.continuous = false; this.interimResults = false;
    this.onstart = this.onaudiostart = this.onsoundstart = this.onspeechstart = null;
    this.onresult = this.onerror = this.onend = null;
    this.start = () => {
      window.__recog.starts++;
      setTimeout(() => { self.onstart && self.onstart(); self.onaudiostart && self.onaudiostart(); }, 5);
    };
    this.abort = () => { window.__recog.aborts++; setTimeout(() => { self.onerror && self.onerror({ error: 'aborted' }); self.onend && self.onend(); }, 3); };
    this.stop  = () => { window.__recog.stops++;  setTimeout(() => { self.onend && self.onend(); }, 3); };
    window.__recog.instances.push(this);
    window.__recog.last = this;
  }
  window.SpeechRecognition = Fake;
  window.webkitSpeechRecognition = Fake;
  // Speak, the way the browser would report it.
  window.__say = (text) => {
    const r = window.__recog.last;
    if (!r) return 'no recogniser';
    r.onsoundstart && r.onsoundstart();
    r.onspeechstart && r.onspeechstart();
    r.onresult && r.onresult({ results: [[{ transcript: text }]] });
    return 'said';
  };
};

const REPLY_MP3 = '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qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq';
const browser = await chromium.launch({ args: [
  '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
  '--autoplay-policy=no-user-gesture-required',
]});
const ctx = await browser.newContext({ permissions: ['microphone'], ignoreHTTPSErrors: true });
await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
await ctx.addInitScript(FAKE_RECOGNISER);
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

const commands = [];
await page.route('**/api/command', route => {
  commands.push(JSON.parse(route.request().postData() || '{}').command);
  route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ response: 'Certainly, sir.', audio: null, speak: true, persona: 'jarvis' }) });
});
await page.route('**/api/speak', route => route.fulfill({
  status: 200, contentType: 'application/json',
  body: JSON.stringify({ audio: REPLY_MP3, index: 0, total: 1, more: false }) }));

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
try {
  const start = page.locator('#mic-tap-start-btn');
  await start.waitFor({ state: 'visible', timeout: 20000 });
  await start.click();
} catch (e) { console.log('no tap-to-start button:', e.message.split('\n')[0]); }
try { await page.locator('#app-screen.show').waitFor({ state: 'attached', timeout: 15000 }); } catch {}
await page.locator('.mic-gate.show').waitFor({ state: 'detached', timeout: 15000 }).catch(() => {});
await page.waitForTimeout(900);

const snap = async (label) => {
  const s = await page.evaluate(() => ({
    isAwake: typeof isAwake !== 'undefined' ? isAwake : '?',
    micMuted: typeof micMuted !== 'undefined' ? micMuted : '?',
    voiceState: typeof voiceState !== 'undefined' ? voiceState : '?',
    recognitionActive: typeof recognitionActive !== 'undefined' ? recognitionActive : '?',
    engine: typeof sttEngine === 'function' ? sttEngine() : '?',
    starts: window.__recog.starts, aborts: window.__recog.aborts, stops: window.__recog.stops,
  }));
  console.log(`  ${label.padEnd(26)} awake=${s.isAwake} muted=${s.micMuted} state=${s.voiceState} ` +
              `recog=${s.recognitionActive} starts=${s.starts} aborts=${s.aborts} stops=${s.stops}`);
  return s;
};

console.log('=== engine and resting state ===');
await snap('after entering');

console.log('\n=== tap the orb ===');
await page.locator('#orb-stage').click();
await page.waitForTimeout(600);
await snap('after first tap');

console.log('\n=== speak ===');
console.log('  ' + await page.evaluate(() => window.__say('מה יש לי היום')));
await page.waitForTimeout(1500);
await snap('after speaking');

// ---- a SECOND turn, after the reply has actually played ----
console.log('\n=== wait for the reply to finish playing ===');
await page.waitForTimeout(3500);
await snap('after the reply');

console.log('\n=== tap again and speak again ===');
await page.locator('#orb-stage').click();
await page.waitForTimeout(900);
await snap('after the second tap');
console.log('  ' + await page.evaluate(() => window.__say('תקבע פגישה מחר')));
await page.waitForTimeout(1500);
await snap('after speaking again');

console.log('\ncommands sent to the server: ' + JSON.stringify(commands));
const log = await page.evaluate(() => (typeof voiceLogText === 'function' ? voiceLogText() : ''));
console.log('\n--- voice log ---\n' + log);
if (errors.length) console.log('\n!!! page errors:\n' + errors.join('\n'));

await browser.close();
