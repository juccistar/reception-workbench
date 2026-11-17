import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from '../release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--executable', '--label'].includes(key) && value && !options.has(key), 'Use --executable PATH --label NEW_LABEL');
  options.set(key, value);
}
assert(options.size === 2 && /^[a-z0-9][a-z0-9-]{0,60}$/.test(options.get('--label') || ''), 'Use --executable PATH --label NEW_LABEL');
const executable = assertReleasePath(root, options.get('--executable'));
assert.equal(path.basename(executable).toLowerCase(), 'reception-backend.exe');
assert((await stat(executable)).isFile());
const output = path.join(root, 'artifacts', 'verification', `prompt-revision-042-${options.get('--label')}`);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-prompt-revision-042-'));
await writeFile(path.join(temporary, '.prompt-revision-042-probe'), 'synthetic\n', { flag: 'wx' });
const workspace = randomUUID();
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const fixture = path.join(root, 'scripts/probes/prompt-revision-042-fixture.py');
const env = {};
for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|USERPROFILE)$/i.test(key)) env[key] = value;
Object.assign(env, { TEMP: temporary, TMP: temporary, LOCALAPPDATA: path.join(temporary, 'local'), APPDATA: path.join(temporary, 'roaming'), RECEPTION_MODEL_BUDGET_DIRECTORY: path.join(temporary, 'budget'), PYTHONIOENCODING: 'utf-8' });
const report = { status: 'failed', executable: path.relative(root, executable), label: options.get('--label'), frozen_backend: true, synthetic_account_only: true, real_model_calls: 0, customer_sends: 0, checks: {}, source_input_sha256: {}, limitations: ['Source hashes identify current input files only; they do not establish which inputs were frozen into this executable.', 'Frozen system prompt text, prompt version and B01/B05/B06 template behavior are not verified by this probe.', 'Recovery is checked before shutdown with synthetic local account records; no real platform, model or merchant database is used.'] };
function fixtureCall(action, scenario) {
  const result = spawnSync(python, ['-I', fixture, action, temporary, workspace, scenario], { cwd: root, env, encoding: 'utf8', windowsHide: true, timeout: 30000, maxBuffer: 1024 * 1024 });
  assert.equal(result.status, 0, `fixture ${action} failed: ${result.stderr}`);
  return action === 'read' ? JSON.parse(result.stdout) : undefined;
}
function start(scenario) {
  const child = spawn(executable, ['--whatsapp-account', '--workspace-id', workspace, '--data-dir', path.join(temporary, scenario)], { cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map(); let next = 0, stderr = '';
  const exited = new Promise(resolve => child.once('close', resolve));
  const fail = error => { for (const item of pending.values()) { clearTimeout(item.timer); item.reject(error); } pending.clear(); };
  child.on('error', fail); child.stdin.on('error', fail); child.on('close', () => fail(new Error('backend exited before RPC reply')));
  child.stderr.on('data', data => { stderr = (stderr + data.toString('utf8')).slice(-16384); });
  createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', line => {
    let value; try { value = JSON.parse(line); } catch { fail(new Error('backend produced non-JSON output')); return; }
    const item = pending.get(value.id); if (!item) return;
    clearTimeout(item.timer); pending.delete(value.id);
    if (value.error) item.reject(new Error(`RPC error: ${value.error.code}`)); else item.resolve(value.result);
  });
  function call(method) { return new Promise((resolve, reject) => {
    const id = String(++next), timer = setTimeout(() => { pending.delete(id); reject(new Error(`RPC timeout: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer }); child.stdin.write(JSON.stringify({ id, method, params: {} }) + '\n');
  }); }
  return { call, async close() {
    try {
      await call('shutdown'); child.stdin.end();
      const timeout = setTimeout(() => child.kill(), 10000);
      try { await exited; } finally { clearTimeout(timeout); }
      assert.equal(child.exitCode, 0, `backend shutdown failed: ${stderr}`);
    } finally { if (child.exitCode === null && child.signalCode === null) { child.kill(); await exited; } }
  } };
}
let service;
try {
  for (const filename of ['services/reception/src/reception_service/integrations/reception_prompts.py', 'services/reception/src/reception_service/resources/builtin_policies.json', 'services/reception/src/reception_service/reception_context.py']) {
    report.source_input_sha256[filename] = createHash('sha256').update(await readFile(path.join(root, filename))).digest('hex');
  }
  report.executable_sha256 = createHash('sha256').update(await readFile(executable)).digest('hex');
  for (const scenario of ['count1', 'count3', 'unknown', 'accepted']) {
    fixtureCall('seed', scenario);
    const before = fixtureCall('read', scenario);
    assert.equal(before['operation:head'].prompt_version, undefined);
    service = start(scenario);
    const state = await service.call('get_whatsapp_state');
    assert.equal(state.enabled, false); assert.equal(state.connected, false);
    const after = fixtureCall('read', scenario);
    const attemptKey = Object.keys(before).find(key => key.startsWith('generation_attempt:'));
    const oldAttempt = before[attemptKey], attempt = after[attemptKey];
    assert.equal(attempt.count, oldAttempt.count); assert.equal(attempt.attempt_id, 'original-attempt');
    if (scenario === 'count1' || scenario === 'count3') {
      assert.equal(after['operation:head'].status, 'cancelled');
      assert.equal(after['operation:head'].submission_started, false);
      assert.equal(after['operation:head'].body, before['operation:head'].body);
      assert.equal(after['reply_batch:batch'].status, 'stopped');
      assert.equal(attempt.status, scenario === 'count1' ? 'failed' : 'completed');
    } else {
      assert.deepEqual(after['operation:head'], before['operation:head']);
      assert.equal(attempt.status, 'completed');
      if (scenario === 'accepted') assert.equal(after['operation:tail'].status, 'cancelled');
      else assert.deepEqual(after['reply_batch:batch'], before['reply_batch:batch']);
    }
    await service.close(); service = undefined;
    report.checks[scenario] = { passed: true, attempt_count: attempt.count, attempt_status: attempt.status, head_status: after['operation:head'].status, batch_status: after['reply_batch:batch'].status, normal_shutdown: true };
  }
  report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally {
  if (service) await service.close().catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
console.log(JSON.stringify({ status: report.status, report: path.relative(root, path.join(output, 'report.json')) }));
