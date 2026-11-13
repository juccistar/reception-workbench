import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, open, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const labelIndex = process.argv.indexOf('--label');
const label = labelIndex < 0 ? '' : process.argv[labelIndex + 1];
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const executableIndex = process.argv.indexOf('--executable');
const executable = executableIndex < 0 ? '' : assertReleasePath(root, process.argv[executableIndex + 1]);
const version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
const output = path.join(root, 'artifacts/verification/seller-images-' + label);
await mkdir(output);
await (await open(path.join(output, 'attempt.claim'), 'wx')).close();
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-seller-images-'));
const data = path.join(temporary, 'data');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared-settings');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'),
  args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000 };
const report = { status: 'failed', version, packaged: !!executable, mode: 'demo', actual_window: true, business_ipc_mocked: false,
  synthetic_sends: 0, customer_sends: 0, real_ai_calls: 0, production_data_accessed: false, windows_closed: false, renderer_errors: [] };
const until = async (check, message, timeout = 45000) => { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error(message); };
const cid = 'demo-emma';
let app, page;
const openWindow = async () => {
  app = await electron.launch(options);
  page = await app.firstWindow();
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1600, 1000));
  await page.getByRole('button', { name: /Emma Wilson/ }).click();
  assert.equal((await page.evaluate(() => window.reception.invoke('get_snapshot'))).app.version, version);
};
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = () => invoke('get_conversation', { conversation_id: cid });
const closeWindow = async () => {
  const process = app.process();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => process.exitCode !== null, 'Window did not close normally');
  assert.equal((await readFile(path.join(data, '.reception-clean-shutdown'), 'utf8')).trim(), 'reception-managed-clean-v1');
  app = null;
};
try {
  await openWindow();
  assert.equal((await invoke('get_snapshot')).app.mode, 'demo');
  await page.getByRole('textbox', { name: '回复正文', exact: true }).fill('[image]');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  let seller;
  await until(async () => { seller = (await detail()).messages.find(row => row.role === 'seller' && row.text.trim() === '[image]' && row.attachments.some(item => item.kind === 'image')); return !!seller; }, 'Synthetic seller image was not saved');
  report.synthetic_sends = 1;
  await invoke('inject_demo', { conversation_id: cid, text: '[image]' });
  let buyer;
  await until(async () => { buyer = (await detail()).messages.find(row => row.role === 'buyer' && row.text.trim() === '[image]' && row.attachments.some(item => item.kind === 'image')); return !!buyer; }, 'Synthetic buyer image was not saved');
  const sellerButton = page.getByRole('button', { name: '放大查看商家发送的照片', exact: true });
  const buyerButton = page.getByRole('button', { name: '放大查看客户发送的照片', exact: true });
  await sellerButton.waitFor({ timeout: 45000 });
  await buyerButton.waitFor({ timeout: 45000 });
  await invoke('check_ai');
  await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'Synthetic AI check did not complete');
  await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
  await until(async () => (await detail()).manual_suggestion?.status === 'ready', 'Manual suggestion did not complete');
  const current = await detail();
  const context = await invoke('get_context', { case_id: current.case.case_id });
  const roles = context.photo_context.direct_images.map(image => image.role);
  assert(roles.includes('seller') && roles.includes('buyer'), 'Both image roles must be present in direct context');
  assert(!JSON.stringify(context).includes('original_b64'));
  report.direct_context_has_both_roles = true;
  await sellerButton.click();
  await page.getByRole('dialog', { name: '商家发送的照片' }).waitFor();
  await page.screenshot({ path: path.join(output, 'seller-image-dialog.png'), animations: 'disabled' });
  await page.getByRole('button', { name: '关闭图片', exact: true }).click();
  await page.screenshot({ path: path.join(output, 'both-images.png'), animations: 'disabled' });
  report.both_previews_and_seller_dialog = true;
  await closeWindow();
  await openWindow();
  assert.equal((await invoke('get_snapshot')).app.mode, 'demo');
  for (const row of [seller, buyer]) {
    const attachment = row.attachments.find(item => item.kind === 'image');
    const cached = await invoke('get_message_image', { conversation_id: cid, message_id: row.message_id, source_version: attachment.source_version });
    assert.equal(cached.status, 'ready', 'Image cache should survive restart');
  }
  await page.getByRole('button', { name: '放大查看商家发送的照片', exact: true }).waitFor();
  await page.getByRole('button', { name: '放大查看客户发送的照片', exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, 'reopened.png'), animations: 'disabled' });
  report.image_cache_reopened = true;
  await closeWindow();
  assert.deepEqual(report.renderer_errors, []);
  report.windows_closed = true;
  report.status = 'passed';
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
  if (page) try { await page.screenshot({ path: path.join(output, 'failure.png') }); } catch {}
} finally {
  if (app) await app.close().catch(() => {});
  await writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
