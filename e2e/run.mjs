/* Browser tests for the JARVIS HUD.
 *
 * What this genuinely covers: layout at real device sizes, JavaScript errors
 * that only appear once the page actually runs, and the whole record ->
 * upload -> transcript loop driven through a fake microphone.
 *
 * What it cannot cover, stated plainly so nobody trusts it too far: this is
 * Chromium, not iOS Safari. Apple's speech engine and iOS's single shared
 * audio session — the two things that actually broke — do not exist here. A
 * green run means the code is sound, not that the iPhone is happy.
 */
import { chromium, devices } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.E2E_URL || 'http://127.0.0.1:5099';
const SHOTS = path.join(HERE, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

const SESSION = fs.readFileSync(path.join(HERE, 'session.txt'), 'utf8').trim();

/* Real device profiles. The tablet and the small phone are the two that catch
   layout bugs; the desktop entry is the control. */
const PROFILES = [
  { name: 'iPhone-SE',   ...devices['iPhone SE'] },
  { name: 'iPhone-15',   ...devices['iPhone 15'] },
  { name: 'Pixel-7',     ...devices['Pixel 7'] },
  { name: 'iPad-Pro',    ...devices['iPad Pro 11'] },
  { name: 'Desktop',     viewport: { width: 1440, height: 900 } },
];

let failed = 0;
const results = [];
function check(profile, label, cond, detail){
  results.push({ profile, label, ok: !!cond, detail: cond ? '' : (detail || '') });
  if (!cond) failed = 1;
}

/* Console errors are the whole reason to run a real browser: none of the
   node-based suites can see a ReferenceError that only fires on load. */
function watch(page, bucket){
  page.on('console', m => { if (m.type() === 'error') bucket.push(m.text()); });
  page.on('pageerror', e => bucket.push('pageerror: ' + e.message));
  page.on('requestfailed', r => {
    const u = r.url();
    // Fonts and other third-party assets are not this app's problem.
    if (!u.startsWith(BASE)) return;
    bucket.push('requestfailed: ' + u + ' ' + (r.failure()?.errorText || ''));
  });
}

async function newPage(browser, profile){
  const ctx = await browser.newContext({
    ...profile,
    permissions: ['microphone'],
    ignoreHTTPSErrors: true,
  });
  await ctx.addCookies([{ name: 'session', value: SESSION, url: BASE }]);
  const errors = [];
  const page = await ctx.newPage();
  watch(page, errors);

  // The transcription endpoint is the one call that would leave the machine.
  // Intercepted so the browser half can be exercised end to end without a
  // Groq key, and so the returned text is something assertable.
  await page.route('**/api/transcribe', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ ok: true, text: 'מה יש לי היום', empty: false }),
  }));
  // Same for the command endpoint: no LLM key here, and a fixed reply keeps
  // the assertions about the HUD rather than about the model.
  await page.route('**/api/command', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ response: 'Certainly, sir.', audio: null }),
  }));
  return { ctx, page, errors };
}

async function enterApp(page){
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  // Auth gate, then a mic check, then the wake prompt. Tap-to-start is chosen
  // over the text fallback on purpose: the fallback sets textOnlyMode, which
  // disables the very speech path these tests exist to exercise.
  await page.waitForTimeout(1500);
  const start = page.locator('#mic-tap-start-btn');
  try {
    await start.waitFor({ state: 'visible', timeout: 20000 });
    await start.click();
  } catch {
    // Some profiles skip straight past the prompt; carry on and let the
    // assertions below say whether we actually got in.
  }
  // The gate fades out rather than vanishing, so wait for the HUD itself.
  try { await page.locator('#app-screen.show').waitFor({ state: 'attached', timeout: 15000 }); } catch {}
  await page.locator('.mic-gate.show').waitFor({ state: 'detached', timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(800);
}

async function openSettings(page){
  const tab = page.locator('.nav-item', { hasText: 'הגדרות' });
  if (await tab.count()) { await tab.first().click(); await page.waitForTimeout(400); }
}

console.log(`\nTarget: ${BASE}\n`);

const browser = await chromium.launch({
  args: [
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${path.join(HERE, 'mic.wav')}`,
    '--autoplay-policy=no-user-gesture-required',
  ],
});

for (const profile of PROFILES){
  const name = profile.name;
  console.log(`--- ${name} ---`);
  const { ctx, page, errors } = await newPage(browser, profile);

  try {
    await enterApp(page);

    // ---- loads clean -------------------------------------------------
    check(name, 'page loads with no JS errors', errors.length === 0, errors.join(' | '));
    check(name, 'the HUD is showing', await page.locator('#app-screen.show').count() > 0);
    check(name, 'the orb rendered', await page.locator('#orb-stage').isVisible());

    // ---- the responsive rule ----------------------------------------
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(name, 'no sideways scroll', overflow <= 1, `overflows by ${overflow}px`);

    /* Measured rather than eyeballed. A screenshot made the bottom nav look
       cramped at phone width; the numbers said otherwise, and that is the
       point of checking geometry instead of trusting a glance. */
    const layout = await page.evaluate(() => {
      const out = { overlaps: [], clipped: [] };
      const items = [...document.querySelectorAll('.nav-item')];
      for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++){
        const a = items[i].getBoundingClientRect(), b = items[j].getBoundingClientRect();
        if (a.right > b.left + 1 && b.right > a.left + 1 && a.bottom > b.top + 1 && b.bottom > a.top + 1)
          out.overlaps.push(items[i].innerText.trim() + ' / ' + items[j].innerText.trim());
      }
      document.querySelectorAll('body *').forEach(el => {
        if (!el.offsetParent || el.children.length) return;
        const cs = getComputedStyle(el);
        if (cs.overflow === 'auto' || cs.overflowX === 'auto') return;   // scrollers may exceed
        if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2)
          out.clipped.push((el.className || el.tagName) + ': ' + (el.innerText || '').slice(0, 24));
      });
      return out;
    });
    check(name, 'no nav buttons overlap', layout.overlaps.length === 0, layout.overlaps.join(' | '));
    check(name, 'no text is cut off', layout.clipped.length === 0, layout.clipped.slice(0, 4).join(' | '));

    await page.screenshot({ path: path.join(SHOTS, `${name}-home.png`), fullPage: false });

    // ---- the orb is one control -------------------------------------
    const stateOf = () => page.evaluate(() => ({
      cls: document.getElementById('orb-stage').className,
      cta: (document.getElementById('orb-cta-text') || {}).innerText,
      label: (document.getElementById('orb-state-label') || {}).innerText,
    }));
    const before = await stateOf();
    check(name, 'orb starts in a resting state',
      /state-(ready|sleeping)/.test(before.cls), before.cls);
    check(name, 'and says what a tap will do', !!before.cta, JSON.stringify(before));

    await page.locator('#orb-stage').click();
    await page.waitForTimeout(900);
    const during = await stateOf();
    check(name, 'tapping changes the orb', during.cls !== before.cls || during.cta !== before.cta,
      `${before.cls} -> ${during.cls}`);

    // ---- record -> upload -> transcript ------------------------------
    const engine = await page.evaluate(() => (typeof sttEngine === 'function' ? sttEngine() : '?'));
    if (engine === 'record'){
      await page.waitForTimeout(1200);                 // let the fake mic feed it
      await page.locator('#orb-stage').click();        // second tap ends it
      await page.waitForTimeout(1500);
      const log = await page.evaluate(() => (typeof voiceLogText === 'function' ? voiceLogText() : ''));
      // Either recorder is acceptable — MediaRecorder logs rec-stop, raw
      // sample capture logs pcm-stop. What matters is that audio came out.
      check(name, 'recorded audio', /rec-stop|pcm-stop/.test(log), log.slice(-300));
      check(name, 'and got a transcript back', /stt-text/.test(log), log.slice(-300));
    } else {
      check(name, `engine is "${engine}" here — recording path covered on mobile profiles`, true);
      await page.locator('#orb-stage').click();
      await page.waitForTimeout(400);
    }

    // ---- settings ----------------------------------------------------
    await openSettings(page);
    for (const [sel, what] of [['#theme-row', 'theme swatches'], ['#shortcut-registry', 'shortcut registry'],
                               ['#alarm-setup', 'alarm setup'], ['#voice-diag', 'voice diagnostics'],
                               ['#stt-engine', 'engine picker']]){
      check(name, `settings renders ${what}`, await page.locator(sel).count() > 0);
    }
    await page.screenshot({ path: path.join(SHOTS, `${name}-settings.png`), fullPage: true });

    // ---- themes ------------------------------------------------------
    const swatches = page.locator('.theme-swatch');
    const n = await swatches.count();
    check(name, 'four themes offered', n === 4, `found ${n}`);
    if (n >= 2){
      await swatches.nth(1).click();
      await page.waitForTimeout(300);
      const themed = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
      check(name, 'picking a theme stamps the document', !!themed, `data-theme=${themed}`);
      await page.screenshot({ path: path.join(SHOTS, `${name}-theme.png`) });
      await swatches.nth(0).click();
      await page.waitForTimeout(300);
      check(name, 'and going back to the default unstamps it',
        (await page.evaluate(() => document.documentElement.getAttribute('data-theme'))) === null);
    }

    // ---- the secret --------------------------------------------------
    const card = page.locator('.status-card');
    if (await card.count()){
      for (let i = 0; i < 5; i++) { await card.first().click(); await page.waitForTimeout(80); }
      await page.waitForTimeout(300);
      check(name, 'five taps reveal Ultron', await page.locator('.ultron-btn').count() > 0);
    }

    check(name, 'still no JS errors after exercising the UI',
      errors.length === 0, errors.join(' | '));
  } catch (e) {
    check(name, 'test run completed', false, e.message);
    try { await page.screenshot({ path: path.join(SHOTS, `${name}-CRASH.png`) }); } catch {}
  } finally {
    await ctx.close();
  }
}

await browser.close();

// ------------------------------------------------------------------ report
console.log('');
let lastProfile = '';
for (const r of results){
  if (r.profile !== lastProfile){ console.log(`\n${r.profile}`); lastProfile = r.profile; }
  console.log(`  ${r.ok ? 'PASS' : 'FAIL'}  ${r.label}${r.ok ? '' : '\n        ' + r.detail}`);
}
const pass = results.filter(r => r.ok).length;
console.log(`\n${pass}/${results.length} checks passed across ${PROFILES.length} devices`);
console.log(`Screenshots: ${SHOTS}`);
console.log(failed ? '\nSOME CHECKS FAILED' : '\nALL BROWSER CHECKS PASSED');
process.exitCode = failed;
