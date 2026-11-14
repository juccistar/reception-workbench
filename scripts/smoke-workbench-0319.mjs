import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';
const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
const label = option('--label'), executable = option('--executable');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const output = path.join(root, 'artifacts/verification', 'workbench-0319-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0319-window-'));
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.RECEPTION_PYTHON = path.join(root, 'services/reception/.venv/Scripts/python.exe');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const report = { status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version,
  actual_window: true, packaged_executable: executable || null, synthetic_only: true, production_data_accessed: false,
  real_ai_calls: 0, real_customer_sends: 0, host_event_injected: true, actual_sdk_fault_in_window: false,
  scope: 'Actual renderer/preload and Demo backend; native public-page SDK faults are checked separately',
  screenshots: [], errors: [], normal_close: false };
let application;
try {
  application = await electron.launch({ executablePath: executable ? path.resolve(root, executable) : path.join(root, 'node_modules/electron/dist/electron.exe'),
    args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', path.join(temporary, 'data'), '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 60000 });
  const page = await application.firstWindow();
  page.on('pageerror', error => report.errors.push(error.name));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  const snapshot = await page.evaluate(() => window.reception.invoke('get_snapshot'));
  assert.equal(snapshot.app.version, report.version);
  assert.equal(snapshot.executor_health.worker_alive, true);
  await page.getByRole('button', { name: 'WhatsApp', exact: true }).first().click();
  await page.getByRole('region', { name: 'WhatsApp 工作区' }).waitFor();
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 960));
  const shot = async name => { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); };
  const native = async value => application.evaluate(({ BrowserWindow }, data) => {
    BrowserWindow.getAllWindows()[0].webContents.send('reception:event', { event: 'whatsapp.host.changed', data });
  }, { status: 'error', account_id: '', connection_epoch: '', ...value });
  await shot('whatsapp-normal-wide');
  await native({ error_code: 'sdk_error', page_state: 'available', phase: 'sdk_inject', diagnostic_id: 'wa_diag_00000000-0000-4000-8000-000000000001' });
  await page.locator('.whatsapp-heading').getByText('原始页面可用，自动接待未就绪', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '开启全部单聊接待', exact: true }).isDisabled(), true);
  await shot('native-available-wide');
  await page.getByRole('button', { name: '查看失败详情' }).click();
  await page.getByText(/诊断编号：wa_diag_00000000/).waitFor();
  await shot('failure-details-wide');
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1100, 720));
  await shot('failure-details-narrow');
  await native({ error_code: 'app_module_error', page_state: 'loading', phase: 'app_modules' });
  await page.locator('.whatsapp-heading').getByText('网页仍在加载，自动接待初始化失败', { exact: true }).waitFor();
  await shot('native-loading-narrow');
  await native({ error_code: 'http_error', http_status: 403, page_state: 'error', phase: 'page_navigation' });
  await page.locator('.whatsapp-heading').getByText('登录页请求失败（HTTP 403）', { exact: true }).waitFor();
  await shot('http-failure-narrow');
  assert.deepEqual(report.errors, []);
  const child = application.process();
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  const end = Date.now() + 30000;
  while (child.exitCode === null && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 100));
  assert.notEqual(child.exitCode, null, 'Normal close timed out');
  application = null; report.normal_close = true; report.status = 'passed';
} catch (error) { report.error = error instanceof Error ? error.message : 'WINDOW_CHECK_FAILED'; process.exitCode = 1; }
finally { await application?.close(); await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); }
console.log(JSON.stringify(report, null, 2));
