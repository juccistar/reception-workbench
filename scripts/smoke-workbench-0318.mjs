import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';

const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
const label = option('--label'), executable = option('--executable');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const output = path.join(root, 'artifacts/verification', 'workbench-0318-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0318-window-'));
const data = path.join(temporary, 'data');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.RECEPTION_PYTHON = path.join(root, 'services/reception/.venv/Scripts/python.exe');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const seed = `
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.repository import now_ms
s = ReceptionService(Path(sys.argv[2]))
try:
 with s._lock, s.repo.transaction():
  s.repo.put('conversation_summary','demo-emma',{'seller_id':s.seller_id,'buyer_id':s.repo.get('conversation','demo-emma')['buyer_id'],'conversation_id':'demo-emma','body':'合成会话摘要：客户咨询帆布袋数量与定制方式。','revision':1,'updated_at':now_ms()})
  s.repo.put('job','job_0318_image_coverage',{'job_id':'job_0318_image_coverage','kind':'generate_draft','conversation_id':'demo-emma','case_id':s.repo.get('conversation','demo-emma')['case_id'],'status':'succeeded','stage':'complete','message':'合成图片准备记录','created_at':now_ms(),'started_at':now_ms(),'completed_at':now_ms(),'photo_coverage':{'ready_count':0,'total_count':1,'failed_count':1,'pending_count':0,'items':[]}})
finally:
 s.close()
`;
const prepared = spawnSync(env.RECEPTION_PYTHON, ['-I', '-X', 'utf8', '-c', seed, path.join(root, 'services/reception/src'), data], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
assert.equal(prepared.status, 0, prepared.stderr);
const report = { status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version, actual_window: true, packaged_executable: executable ? path.relative(root, path.resolve(root, executable)).replaceAll('\\', '/') : null, synthetic_only: true, production_data_accessed: false, real_ai_calls: 0, real_customer_sends: 0, health_event_injected: true, backend_fault_injected: false, health_ui_scope: 'Synthetic host event over the actual preload; real backend fault/revocation is covered separately by Python tests', completed: [], screenshots: [], errors: [], normal_close: false };
let application;
const until = async (check, message, timeout = 30000) => { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('Timed out: ' + message); };
try {
  application = await electron.launch({ executablePath: executable ? path.resolve(root, executable) : path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 60000 });
  const page = await application.firstWindow();
  page.on('pageerror', error => report.errors.push(error.name));
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const shot = async name => { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); };
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  assert.equal((await invoke('get_snapshot')).app.version, report.version);
  await page.getByRole('button', { name: /Emma Wilson/ }).click();
  await page.getByRole('main', { name: 'Emma Wilson 的客户接待' }).waitFor();
  await application.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].setContentSize(1440, 960); BrowserWindow.getAllWindows()[0].focus(); });
  assert.equal(await page.locator('.photo-coverage').count(), 0);
  await page.getByRole('region', { name: '会话摘要' }).waitFor();
  const health = await invoke('get_executor_health');
  assert.equal(health.worker_alive, true);
  assert(['idle', 'running', 'waiting_external'].includes(health.state));
  const ai = await invoke('check_ai');
  await until(async () => (await invoke('list_jobs')).some(job => job.job_id === ai.job_id && job.status === 'succeeded'), 'synthetic AI check');
  await invoke('set_scope', { mode: 'incoming', conversation_ids: [], max_auto_replies: null });
  await invoke('set_reception', { enabled: true, auto_send: false });
  await page.locator('.app-header .reception-status').filter({ hasText: '自动接待中' }).waitFor();
  await shot('reception-wide');
  report.completed.push('actual-backend-health-and-synthetic-reception-ready');
  report.completed.push('photo-coverage-removed-with-summary-preserved');
  await page.getByRole('button', { name: 'WhatsApp', exact: true }).first().click();
  await page.getByRole('region', { name: 'WhatsApp 工作区' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '连接 WhatsApp', exact: true }).isDisabled(), true);
  await shot('whatsapp-wide');
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1100, 720));
  await shot('whatsapp-narrow');
  await page.getByRole('button', { name: '客户接待', exact: true }).first().click();
  await application.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows()[0];
    window.setContentSize(1440, 960);
    window.webContents.send('reception:event', { event: 'executor.health', data: { state: 'failed', worker_alive: false, stage: 'dispatch', job_id: 'job_synthetic_fault', last_progress_at_ms: Date.now(), observed_at_ms: Date.now() + 600000, queue_depth: 1, error_code: 'executor_fault' } });
  });
  await page.locator('.app-header .reception-status').filter({ hasText: '后台执行异常，自动发送已暂停' }).waitFor();
  assert.equal(await page.locator('.app-header .reception-status.running').count(), 0);
  const text = page.getByRole('textbox', { name: '回复正文', exact: true });
  await text.fill('合成草稿：执行异常时仍可编辑并保存。');
  await until(async () => (await invoke('get_conversation', { conversation_id: 'demo-emma' })).chat_draft?.body === '合成草稿：执行异常时仍可编辑并保存。', 'synthetic editor autosave');
  assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true);
  await shot('executor-failure-reception-wide');
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1100, 720));
  await shot('executor-failure-reception-narrow');
  await page.getByRole('button', { name: '连接与设置', exact: true }).first().click();
  assert.equal(await page.getByRole('button', { name: '暂停并检查 AI', exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: '授权并开始自动接待', exact: true }).isDisabled(), true);
  await shot('executor-failure-settings');
  await page.getByRole('button', { name: 'WhatsApp', exact: true }).first().click();
  await page.locator('.whatsapp-footer').filter({ hasText: '后台执行异常，自动发送已暂停' }).waitFor();
  await shot('executor-failure-whatsapp');
  report.completed.push('independent-health-event-overrides-green-state-without-blocking-editor-save');
  report.completed.push('health-failure-disables-new-checks-and-grants-in-both-channels');
  const child = application.process();
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => child.exitCode !== null, 'normal close');
  application = null;
  report.normal_close = true;
  assert.deepEqual(report.errors, []);
  report.status = 'passed';
} catch (error) {
  report.error = error instanceof Error ? error.message : 'WINDOW_CHECK_FAILED';
  process.exitCode = 1;
} finally {
  await application?.close();
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
console.log(JSON.stringify(report, null, 2));
