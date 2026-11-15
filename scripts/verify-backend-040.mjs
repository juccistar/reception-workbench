import { spawn } from 'node:child_process';
import { mkdir, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { root } from './paths.mjs';

const args = process.argv.slice(2);
assert(args.length === 2 && args[0] === '--label' && /^[a-z0-9][a-z0-9-]{0,60}$/.test(args[1]), 'Use --label with a new verification label');
const destination = path.join(root, 'artifacts/verification', args[1]);
await mkdir(destination);
const tests = path.join(root, 'services/reception/tests');
// The 0.4.0 feature tests plus the existing workflows affected by this release.
// This is deliberately a selected regression set, not unittest discovery over all files.
const targets = [
  ...(await readdir(tests)).filter(name => /^test_.*_040\.py$/.test(name)).sort(),
  'test_manual_reception.py', 'test_photo_reception.py', 'test_message_monitor.py',
  'test_whatsapp_0317.py', 'test_whatsapp_media_lifetime.py',
  'test_conversation_assistance_031.py', 'test_reply_batches_0319.py',
  'test_requirement_changes_0316.py', 'test_storage_maintenance.py',
  'test_reception_prompts_0314.py', 'test_demo_incoming.py',
  'test_execution_0310.py', 'test_procurement_0310.py', 'test_product_catalog_0312.py',
  'test_supplement_attachments.py', 'test_cached_media.py',
];
const report = { version: '0.4.0', status: 'running', scope: 'selected_backend_regressions', full_suite: false, files: [], tests: 0 };
for (const file of targets) {
  const started = Date.now();
  let text = '';
  const child = spawn(process.execPath, [path.join(root, 'scripts/test-backend.mjs'), file], { cwd: root, env: process.env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let overflow = false;
  const collect = chunk => { if (text.length + chunk.length <= 4 * 1024 * 1024) text += chunk.toString('utf8'); else overflow = true; };
  child.stdout.on('data', collect); child.stderr.on('data', collect);
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); });
  const count = Number(/Ran (\d+) tests? in/.exec(text)?.[1] || 0);
  const status = code === 0 && count > 0 && !overflow ? 'passed' : 'failed';
  const row = { file, status, tests: count, exit_code: code, elapsed_ms: Date.now() - started, log_truncated: overflow };
  report.files.push(row); report.tests += count;
  await writeFile(path.join(destination, file + '.txt'), text, { flag: 'wx' });
  console.log(JSON.stringify(row));
}
report.status = report.files.every(row => row.status === 'passed') ? 'passed' : 'failed';
await writeFile(path.join(destination, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify({ status: report.status, files: report.files.length, tests: report.tests, report: path.relative(root, path.join(destination, 'report.json')) }));
if (report.status !== 'passed') process.exitCode = 1;
