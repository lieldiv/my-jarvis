/* Runs every redesign suite in turn and prints one line per suite.
 *
 *   cd e2e
 *   npm run serve            # terminal 1 (serves the HUD on 5099 with a throw-away test database)
 *   npm run test:redesign    # terminal 2
 *
 * The Python unit suites need no server. Set E2E_URL to test a server somewhere else, PYTHON to pick the
 * interpreter (default "python").
 */
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
fs.mkdirSync(path.join(HERE, 'shots'), { recursive: true });
const PY = process.env.PYTHON || 'python';

const suites = [
  [PY, 'unit_events.py'],
  [PY, 'unit_web_page.py'],
  [PY, 'unit_inbox.py'],
  [PY, 'unit_capability_gap.py'],
  [PY, 'unit_tool_loop.py'],
  [PY, 'unit_confirm_honesty.py'],
  [PY, 'unit_calendar_errors.py'],
  ['node', 'test_step1_foundation.mjs'],
  ['node', 'test_step2_agenda.mjs'],
  ['node', 'test_step3_guide.mjs'],
  ['node', 'test_step4_screens.mjs'],
  ['node', 'test_smoke_regression.mjs'],
  ['node', 'test_web_page_card.mjs'],
];

let failed = 0;
for (const [cmd, file] of suites) {
  const r = spawnSync(cmd, [file], { cwd: HERE, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, timeout: 600000 });
  const out = (r.stdout || '') + (r.stderr || '');
  const m = out.match(/(\d+)\/(\d+) checks passed/);
  const ok = r.status === 0 && !!m;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${file.padEnd(28)} ${m ? m[0] : 'did not finish'}`);
  if (!ok) {
    failed++;
    console.log(out.split('\n').filter(l => /^FAIL|Error|^ {6}/.test(l)).slice(0, 12).join('\n'));
  }
}
console.log(failed ? `\n${failed} suite(s) failed` : '\nall suites passed');
process.exit(failed ? 1 : 0);
