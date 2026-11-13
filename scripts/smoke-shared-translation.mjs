import { _electron as electron } from 'playwright';
import { execFile } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { promisify } from 'node:util';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const option = name => { const i = process.argv.indexOf(name); return i < 0 ? '' : process.argv[i + 1]; };
const label = option('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const executable = option('--executable') ? assertReleasePath(root, option('--executable')) : '';
const version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
const output = path.join(root, 'artifacts/verification/shared-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-shared-demo-'));
const shared = path.join(temporary, 'shared-settings');
const secret = 'synthetic-shared-key-' + label;
const applications = [];
const report = { status: 'failed', version, mode: 'demo', packaged: !!executable,
  real_ai_calls: 0, customer_sends: 0, production_data_accessed: false, actual_windows: 2,
  business_ipc_mocked: false, renderer_errors: [] };
async function until(check, message, timeout = 45000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error(message);
}
async function launch(slot) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
  env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, slot, 'bootstrap');
  env.RECEPTION_SHARED_SETTINGS_DIR = shared;
  env.LOCALAPPDATA = path.join(temporary, slot, 'local-app-data');
  env.CHROME_LOG_FILE = path.join(output, slot + '-chromium.log');
  const data = path.join(temporary, slot, 'data');
  const app = await electron.launch({ executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'),
    args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE],
    cwd: root, env, timeout: 45000 });
  applications.push(app);
  const page = await app.firstWindow();
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  assert.equal((await invoke('get_snapshot')).app.version, version);
  await page.getByRole('button', { name: '连接与设置', exact: true }).click();
  const panel = page.getByRole('region', { name: '聊天翻译设置' });
  await panel.getByLabel('模型', { exact: true }).waitFor();
  const info = () => page.evaluate(() => window.reception.getTranslationSettings());
  return { app, page, panel, info, invoke, data };
}
async function save(client) {
  await client.panel.getByRole('button', { name: '保存翻译设置', exact: true }).click();
  await client.panel.getByText('翻译设置已保存。', { exact: true }).waitFor();
  await until(() => client.panel.getByRole('button', { name: '保存翻译设置', exact: true }).isDisabled(), 'Form did not finish saving');
}
async function close(client) {
  const process = client.app.process();
  await client.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => process.exitCode !== null, 'Window did not close normally');
  assert.equal((await readFile(path.join(client.data, '.reception-clean-shutdown'), 'utf8')).trim(), 'reception-managed-clean-v1');
  applications.splice(applications.indexOf(client.app), 1);
}
try {
  let first = await launch('merchant-a');
  if (process.argv.includes('--legacy-migration')) {
    const cipher = await first.app.evaluate(({ safeStorage }, payload) => safeStorage.encryptString(JSON.stringify(payload)).toString('base64'),
      { api_key: secret + '-legacy', data_root: first.data });
    await close(first);
    const legacyDirectory = path.join(temporary, 'merchant-a/bootstrap/translation-credentials/demo');
    await mkdir(legacyDirectory, { recursive: true });
    const ref = randomBytes(32).toString('hex');
    await writeFile(path.join(legacyDirectory, ref), Buffer.from(cipher, 'base64'), { flag: 'wx', mode: 0o600 });
    // Seed only a legacy-format control record in this closed synthetic workspace.
    const metadata = { enabled: true, base_url: 'https://api.deepseek.com/v1', model: 'legacy-demo-model', revision: 1, credential_ref: ref };
    await promisify(execFile)(path.join(root, 'services/reception/.venv/Scripts/python.exe'), ['-I', '-c',
      'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("INSERT OR REPLACE INTO entities(kind,id,data) VALUES(?,?,?)",("meta","translation_settings",sys.argv[2])); c.commit(); c.close()',
      path.join(first.data, 'demo/control.sqlite3'), JSON.stringify(metadata)], { windowsHide: true, timeout: 15000 });
    first = await launch('merchant-a');
    assert.equal((await first.info()).has_key, true);
    assert.equal((await first.info()).model, 'legacy-demo-model');
    report.legacy_profile_key_migrated = true;
  }
  await first.panel.getByLabel('模型', { exact: true }).fill('shared-demo-a');
  await first.panel.getByLabel('新 API Key', { exact: true }).fill(secret);
  await save(first);
  assert.equal(await first.panel.getByLabel('新 API Key', { exact: true }).inputValue(), '');
  const second = await launch('merchant-b');
  await until(async () => (await second.info()).model === 'shared-demo-a' && (await second.info()).has_key, 'Second client did not receive shared connection');
  assert.equal(await second.panel.getByLabel('新 API Key', { exact: true }).inputValue(), '');
  await second.panel.getByRole('checkbox', { name: '启用第三方聊天翻译', exact: true }).uncheck();
  await save(second);
  assert.equal((await first.info()).enabled, true);
  assert.equal((await second.info()).enabled, false);
  report.per_workspace_enable_independent = true;
  await first.panel.getByLabel('模型', { exact: true }).fill('shared-demo-b');
  await save(first);
  await until(() => second.panel.getByLabel('模型', { exact: true }).inputValue().then(value => value === 'shared-demo-b'), 'Open second window did not update automatically');
  assert.equal((await second.info()).enabled, false);
  report.open_window_sync = true;
  await second.panel.getByLabel('模型', { exact: true }).fill('pending-second-edit');
  await first.panel.getByLabel('模型', { exact: true }).fill('shared-demo-c');
  await save(first);
  await until(async () => (await second.info()).model === 'shared-demo-c', 'New global revision missing');
  await second.panel.getByRole('button', { name: '保存翻译设置', exact: true }).click();
  await second.panel.getByText(/翻译.*已变化，请保留输入并重新读取/).waitFor();
  assert.equal(await second.panel.getByLabel('模型', { exact: true }).inputValue(), 'pending-second-edit');
  await until(() => second.panel.getByRole('button', { name: '放弃修改', exact: true }).isEnabled(), 'Conflict refresh did not finish');
  await second.panel.getByRole('button', { name: '放弃修改', exact: true }).click();
  assert.equal(await second.panel.getByLabel('模型', { exact: true }).inputValue(), 'shared-demo-c');
  report.stale_save_rejected_without_losing_edits = true;
  await first.invoke('inject_demo', { conversation_id: 'demo-emma', text: 'isolated-' + label });
  const firstDetail = await first.invoke('get_conversation', { conversation_id: 'demo-emma' });
  const secondDetail = await second.invoke('get_conversation', { conversation_id: 'demo-emma' });
  assert(firstDetail.messages.some(message => message.text === 'isolated-' + label));
  assert(!secondDetail.messages.some(message => message.text === 'isolated-' + label));
  report.business_data_isolated = true;
  for (const client of [first, second]) assert(!JSON.stringify(await client.invoke('get_snapshot')).includes(secret));
  await second.panel.getByRole('button', { name: '清除已保存 Key', exact: true }).click();
  await save(second);
  await until(async () => !(await first.info()).has_key && !(await first.info()).enabled, 'Key clearing did not propagate');
  assert.equal((await second.info()).has_key, false);
  report.global_key_clear = true;
  await first.panel.getByLabel('服务地址', { exact: true }).fill('http://127.0.0.1:3425/v1');
  await first.panel.getByLabel('新 API Key', { exact: true }).fill(secret + '-rotated');
  await save(first);
  await until(async () => (await second.info()).base_url === 'http://127.0.0.1:3425/v1' && (await second.info()).has_key, 'Endpoint or rotated key did not synchronize');
  assert.equal((await first.info()).enabled, false);
  assert.equal((await second.info()).enabled, false);
  report.endpoint_and_key_sync = true;
  for (const [name, client] of [['merchant-a', first], ['merchant-b', second]]) {
    await client.page.bringToFront();
    await client.panel.scrollIntoViewIfNeeded();
    await client.page.screenshot({ path: path.join(output, name + '-settings.png'), animations: 'disabled' });
  }
  await close(first);
  const reopened = await launch('merchant-a');
  assert.equal((await reopened.info()).has_key, true);
  assert.equal((await reopened.info()).enabled, false);
  report.encrypted_key_reopened = true;
  for (const client of [reopened, second]) assert.equal((await client.invoke('get_snapshot')).reception.running, false);
  await close(reopened);
  await close(second);
  assert.deepEqual(report.renderer_errors, []);
  report.windows_closed = true;
  report.status = 'passed';
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
  for (const [index, app] of applications.entries()) try { await app.windows()[0]?.screenshot({ path: path.join(output, 'failure-' + index + '.png') }); } catch {}
} finally {
  await Promise.all(applications.map(app => app.close().catch(() => {})));
  await writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
