import { spawn, spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from '../release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const [executableArgument, label] = process.argv.slice(2);
assert(process.argv.length === 4 && /^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass candidate backend EXE and a new label');
const executable = assertReleasePath(root, executableArgument);
assert.equal(path.basename(executable).toLowerCase(), 'reception-backend.exe');
const output = path.join(root, 'artifacts/verification', `whatsapp-account-exe-041-${label}`);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-account-exe-041-'));
const workspace = randomUUID(), account = '19990001@c.us';
const env = {};
for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|USERPROFILE|LOCALAPPDATA|APPDATA|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432)$/i.test(key)) env[key] = value;
Object.assign(env, { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1',
  LOCALAPPDATA: path.join(temporary, 'local'), APPDATA: path.join(temporary, 'roaming'),
  RECEPTION_MODEL_BUDGET_DIRECTORY: path.join(temporary, 'model-budget') });
function start(directory) {
  const child = spawn(executable, ['--whatsapp-account', '--workspace-id', workspace, '--data-dir', directory], { cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map(); let next = 0; let stderr = '';
  child.stdin.on('error', () => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error('isolated backend pipe unavailable')); } pending.clear(); });
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  const exited = new Promise(resolve => child.once('close', resolve));
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', line => {
    let value; try { value = JSON.parse(line); } catch { for (const p of pending.values()) p.reject(new Error('backend stdout was not JSON')); return; }
    const p = pending.get(value.id);
    if (p) { clearTimeout(p.timer); pending.delete(value.id); value.error ? p.reject(new Error(value.error.code)) : p.resolve(value.result); }
  });
  child.on('error', error => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(error); } pending.clear(); });
  child.once('close', () => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error('backend exited before reply')); } pending.clear(); });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = String(++next);
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('isolated RPC deadline')); }, 30000);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
  });
  return { call, async close() { try { await call('shutdown'); child.stdin.end(); await exited; assert.equal(child.exitCode, 0); assert.equal(stderr, ''); } finally { if (child.exitCode === null && child.signalCode === null) { child.kill(); await exited; } } } };
}
function maintenance(operation, source, destination) {
  const args = ['--whatsapp-maintenance', operation, operation === 'backup' ? '--source' : '--archive', source, '--destination', destination];
  const result = spawnSync(executable, args, { cwd: root, env, encoding: 'utf8', windowsHide: true, timeout: 30000 });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stderr, '');
  const lines = result.stdout.trim().split('\n'); assert.equal(lines.length, 1);
  const response = JSON.parse(lines[0]); assert.equal(response.status, 'ok'); assert.equal(response.reception_enabled, false);
  return response;
}
const report = { status: 'failed', frozen_backend: true, synthetic_private_rpc: true, actual_browser: false, real_account_authenticated: false, model_calls: 0, customer_sends: 0, checks: {} };
let service;
try {
  const directory = path.join(temporary, 'account');
  service = start(directory);
  assert.equal((await service.call('get_whatsapp_state')).enabled, false);
  await service.call('_wa_set_account', { account_id: account, connection_epoch: 'synthetic-epoch', status: 'connected' });
  const operation = { operation_id: 'synthetic-preserved-unknown', account_id: account, connection_epoch: 'old-synthetic-epoch', chat_id: '29990001@c.us', body: 'Synthetic frozen body', status: 'unknown', submission_started: true };
  await service.call('_wa_set_legacy_status', { format: 'wa-legacy-verification-v1', account_id: account, manifest_hash: 'a'.repeat(64), verified_at_ms: Date.now() - 1000, sources: [{ source_id: 'synthetic-source', status: 'verified', pending_operations: [operation] }] });
  assert.equal((await service.call('get_whatsapp_state')).legacy.pending_count, 1);
  await service.close(); service = undefined;
  const archive = path.join(temporary, 'backup'), restored = path.join(temporary, 'restored');
  maintenance('backup', directory, archive); maintenance('restore', archive, restored);
  report.checks.offline_cli_roundtrip = true;
  service = start(restored);
  let state = await service.call('get_whatsapp_state'); assert.equal(state.enabled, false); assert.equal(state.connected, false);
  await service.call('_wa_set_account', { account_id: account, connection_epoch: 'new-synthetic-epoch', status: 'connected' });
  state = await service.call('get_whatsapp_state');
  assert.equal(state.legacy.pending_count, 1);
  assert.equal(state.pending_operations.length, 1);
  assert.equal(state.pending_operations[0].operation_id, operation.operation_id);
  assert.equal(state.pending_operations[0].status, 'unknown');
  assert.equal((await service.call('get_whatsapp_writeback_capabilities')).available, false);
  report.checks.original_unknown_survives_restore = true;
  report.checks.restored_reception_disabled = true;
  report.checks.writeback_unavailable = true;
  await service.close(); service = undefined;
  report.checks.normal_process_exit = true;
  report.status = 'passed';
} catch (error) { report.error = error instanceof Error ? error.message : String(error); process.exitCode = 1; }
finally {
  if (service) await service.close().catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
}
console.log(JSON.stringify(report));
