import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { extractFile } from '@electron/asar';
import { assertReleasePath } from '../release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--executable', '--label', '--expected-version'].includes(key) && value && !options.has(key), 'Use --executable, --label and --expected-version');
  options.set(key, value);
}
const label = options.get('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Invalid label');
assert.equal(options.get('--expected-version'), '0.4.2', 'Expected version must be 0.4.2');
assert(options.has('--executable'), 'Missing executable');
const executable = assertReleasePath(root, options.get('--executable'));
assert.equal(path.basename(executable).toLowerCase(), 'reception-backend.exe');
const output = path.join(root, 'artifacts', 'verification', `account-images-042-${label}`);
await mkdir(output);
const report = { status: 'failed', expected_version: '0.4.2', frozen_backend: true, synthetic_private_rpc: true, real_account: false, network_requests: 'not initiated by probe', model_calls: 0, customer_sends: 0, checks: {}, not_covered: ['original image readback and full SHA comparison: no account private read RPC', 'JPEG and WebP', 'exact 20 MiB limit and over-limit rejection', 'real account and platform'] };
let temporary, service;
try {
  temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-account-images-042-'));
  const archive = path.join(path.dirname(path.dirname(executable)), 'app.asar');
  const version = JSON.parse(extractFile(archive, 'package.json').toString('utf8')).version;
  report.packaged_version = version;
  assert.equal(version, '0.4.2');
  report.checks.app_asar_version = true;
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432)$/i.test(key)) env[key] = value;
  }
  Object.assign(env, { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1', TEMP: temporary, TMP: temporary, USERPROFILE: temporary, LOCALAPPDATA: path.join(temporary, 'local'), APPDATA: path.join(temporary, 'roaming'), RECEPTION_MODEL_BUDGET_DIRECTORY: path.join(temporary, 'budget') });
  const python = path.join(root, 'services', 'reception', '.venv', 'Scripts', 'python.exe');
  const generator = `import os, sys\nfrom PIL import Image\nout = sys.argv[1]\nImage.frombytes('RGB', (1600, 1500), os.urandom(1600 * 1500 * 3)).save(os.path.join(out, 'large.png'), 'PNG')\nImage.new('RGB', (12, 12), 'red').save(os.path.join(out, 'static.gif'), 'GIF')\na = Image.new('RGB', (12, 12), 'red')\nb = Image.new('RGB', (12, 12), 'blue')\na.save(os.path.join(out, 'animated.gif'), 'GIF', save_all=True, append_images=[b], duration=100, loop=0)\n`;
  const generated = spawnSync(python, ['-I', '-c', generator, temporary], { cwd: root, env, encoding: 'utf8', windowsHide: true, timeout: 60000 });
  assert.equal(generated.status, 0, `Pillow generator failed: ${generated.error?.message || generated.stderr}`);
  const images = await Promise.all(['large.png', 'static.gif', 'animated.gif'].map(name => readFile(path.join(temporary, name))));
  assert(images[0].length > 6 * 1024 * 1024 && images[0].length < 20 * 1024 * 1024, 'PNG encoded size outside intended range');
  report.checks.synthetic_large_png_bytes = images[0].length;
  const workspace = randomUUID(), account = '19990001@c.us', epoch = 'synthetic-epoch', chat = '29990001@c.us';
  service = start(executable, ['--whatsapp-account', '--workspace-id', workspace, '--data-dir', path.join(temporary, 'account')], env);
  assert.equal((await service.call('get_whatsapp_state')).enabled, false);
  await service.call('_wa_set_account', { account_id: account, connection_epoch: epoch, status: 'connected' });
  async function transfer(data, message, mime) {
    const timestamp = 123;
    await service.call('_wa_ingest', { account_id: account, connection_epoch: epoch, messages: [{ id: message, chat_id: chat, body: '', timestamp_ms: timestamp, from_me: false, is_group: false, is_history: true, has_image: true }] });
    const sourceVersion = createHash('sha256').update(JSON.stringify([account, epoch, chat, message, String(timestamp)])).digest('hex');
    const identity = { account_id: account, connection_epoch: epoch, chat_id: chat, message_id: message, source_version: sourceVersion };
    const { transfer_id } = await service.call('_wa_media_begin', { ...identity, mime_type: mime });
    for (let offset = 0; offset < data.length; offset += 256 * 1024) {
      const chunk = data.subarray(offset, offset + 256 * 1024);
      const result = await service.call('_wa_media_chunk', { ...identity, transfer_id, offset, data_base64: chunk.toString('base64') });
      assert.equal(result.next_offset, offset + chunk.length);
    }
    const sha256 = createHash('sha256').update(data).digest('hex');
    try {
      return { sha256, result: await service.call('_wa_media_finish', { ...identity, transfer_id, size: data.length, sha256 }) };
    } catch (error) { error.transferStage = 'finish'; throw error; }
  }
  for (const [data, message, mime, check] of [[images[0], 'large-png', 'image/png', 'large_png_saved_and_decoded'], [images[1], 'static-gif', 'image/gif', 'static_gif_saved_and_decoded']]) {
    const { result, sha256 } = await transfer(data, message, mime);
    assert.equal(result.status, 'ready');
    assert.equal(result.sha256, sha256);
    report.checks[check] = { status: result.status, bytes: data.length, input_sha256: sha256, returned_sha256: result.sha256 };
  }
  // A generic transport or contract error does not establish animation rejection.
  try {
    await transfer(images[2], 'animated-gif', 'image/gif');
    assert.fail('Animated GIF was accepted');
  } catch (error) {
    if (error.transferStage === 'finish' && error.rpcCode === 'invalid_media') report.checks.animated_gif_rejected = { code: error.rpcCode };
    else throw error;
  }
  await service.close(); service = undefined;
  report.checks.normal_process_exit = true;
  report.status = 'passed';
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  if (service) await service.close().catch(error => { report.cleanup_error = error.message; process.exitCode = 1; report.status = 'failed'; });
  if (temporary) await rm(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }).catch(error => { report.cleanup_error = error.message; process.exitCode = 1; report.status = 'failed'; });
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));

function start(executable, args, env) {
  const child = spawn(executable, args, { cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map(); let next = 0, stderr = '';
  const rejectAll = error => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(error); } pending.clear(); };
  child.stdin.on('error', rejectAll);
  child.stderr.on('data', data => { stderr += data.toString('utf8'); });
  child.on('error', rejectAll);
  const exited = new Promise(resolve => child.once('close', code => { rejectAll(new Error('backend exited before reply')); resolve(code); }));
  createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', line => {
    let value;
    try { value = JSON.parse(line); } catch { rejectAll(new Error('backend stdout was not JSON')); return; }
    const p = pending.get(value.id);
    if (!p) return;
    clearTimeout(p.timer); pending.delete(value.id);
    if (value.error) { const error = new Error(`RPC ${value.error.code || 'unknown error'}`); error.rpcCode = value.error.code; p.reject(error); }
    else p.resolve(value.result);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = String(++next);
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`RPC deadline: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
  });
  return { call, async close() {
    try {
      await call('shutdown'); child.stdin.end();
      const code = await Promise.race([exited, new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('backend exit deadline')), 30000); timer.unref(); })]);
      assert.equal(code, 0); assert.equal(stderr, '');
    } finally {
      if (child.exitCode === null && child.signalCode === null) { child.kill(); await exited; }
    }
  } };
}
