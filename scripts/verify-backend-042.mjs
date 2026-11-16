import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';

const args = process.argv.slice(2);
assert(args.length === 2 && args[0] === '--label' && /^[a-z0-9][a-z0-9-]{0,60}$/.test(args[1]), 'Use --label with a new label');
const destination = path.join(root, 'artifacts/verification', `backend-042-${args[1]}`);
await mkdir(destination);
const targets = ['test_message_monitor.py', 'test_reply_sync_0320.py', 'test_reply_batches_0319.py',
  'test_manual_receipts.py', 'test_native_workflow_receipts.py', 'test_whatsapp_account_generation_041.py',
  'test_model_runtime_040.py', 'test_projection_performance_042.py', 'test_performance_042.py',
  'test_product_library_0313.py', 'test_whatsapp_account_media_041.py',
  'test_feishu_open_042.py', 'test_feishu_open_adapter_042.py', 'test_feishu_configuration_042.py',
  'test_translation_retirement_042.py', 'test_model_router_040.py'];
const digest = data => createHash('sha256').update(data).digest('hex');
async function sourceInputs(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) throw new Error('Input links are not supported');
    if (entry.name === '__pycache__') continue;
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await sourceInputs(file));
    else if (/\.(py|json|txt)$/.test(entry.name)) files.push(file);
  }
  return files;
}
const inputs = [...await sourceInputs(path.join(root, 'services/reception/src')),
  ...await sourceInputs(path.join(root, 'services/reception/test_support')),
  ...targets.map(name => path.join(root, 'services/reception/tests', name))].sort();
const capture = async () => Object.fromEntries(await Promise.all(inputs.map(async file => [path.relative(root, file).replaceAll('\\', '/'), digest(await readFile(file))])));
const before = await capture();
const report = { version: '0.4.2', status: 'failed', scope: 'selected_changed_backend_files', full_suite: false,
  inputs_before: before, inputs_after: {}, changed_inputs: [], files: [], tests: 0 };
for (const file of targets) {
  const budget = await mkdtemp(path.join(os.tmpdir(), 'arw042-target-budget-'));
  let text = '', overflow = false;
  const started = Date.now();
  const child = spawn(process.execPath, [path.join(root, 'scripts/test-backend.mjs'), file], {
    cwd: root, env: { ...process.env, RECEPTION_MODEL_BUDGET_DIRECTORY: budget }, windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const collect = chunk => { if (Buffer.byteLength(text) + chunk.length <= 4 * 1024 * 1024) text += chunk.toString('utf8'); else overflow = true; };
  child.stdout.on('data', collect); child.stderr.on('data', collect);
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); });
  const count = Number(/Ran (\d+) tests? in/.exec(text)?.[1] || 0);
  const row = { file, status: code === 0 && count > 0 && !overflow ? 'passed' : 'failed', tests: count,
    exit_code: code, elapsed_ms: Date.now() - started, log_truncated: overflow };
  report.files.push(row); report.tests += count;
  await writeFile(path.join(destination, file + '.txt'), text, { flag: 'wx' });
  await rm(budget, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  console.log(JSON.stringify(row));
}
report.inputs_after = await capture();
report.changed_inputs = Object.keys(before).filter(file => before[file] !== report.inputs_after[file]);
report.status = report.files.every(row => row.status === 'passed') && !report.changed_inputs.length ? 'passed' : 'failed';
await writeFile(path.join(destination, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify({ status: report.status, files: report.files.length, tests: report.tests, changed_inputs: report.changed_inputs,
  report: path.relative(root, path.join(destination, 'report.json')) }));
if (report.status !== 'passed') process.exitCode = 1;
