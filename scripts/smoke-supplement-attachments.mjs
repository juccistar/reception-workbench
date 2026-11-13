import assert from 'node:assert/strict';
import { _electron as electron } from 'playwright';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
const label = option('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const executable = option('--executable') ? assertReleasePath(root, option('--executable')) : '';
const output = path.join(root, 'artifacts/verification', 'supplement-attachments-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-supplement-files-'));
const data = path.join(temporary, 'data');
const files = path.join(temporary, 'fixtures');
await mkdir(files);
const png = path.join(files, 'synthetic-reference.png');
const textFile = path.join(files, 'synthetic-terms.txt');
const pdf = path.join(files, 'synthetic-terms.pdf');
const text = 'Synthetic merchant supplement only: shipping is not confirmed; no sample dispatch arranged.';
await writeFile(textFile, text);
// A small, generated text PDF; no downloaded or user-owned files are opened.
const pdfText = 'Synthetic PDF terms: no sample dispatch arranged.';
const stream = `BT /F1 12 Tf 20 120 Td (${pdfText}) Tj ET`;
const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 360 160] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>', `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];
let document = '%PDF-1.4\n'; const offsets = [0];
for (const [index, body] of objects.entries()) { offsets.push(Buffer.byteLength(document)); document += `${index + 1} 0 obj\n${body}\nendobj\n`; }
const cross = Buffer.byteLength(document);
document += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(value => String(value).padStart(10, '0') + ' 00000 n ').join('\n')}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${cross}\n%%EOF\n`;
await writeFile(pdf, document);
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared-settings');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const prepare = spawnSync(python, ['-I', '-c', 'import sys; from PIL import Image; Image.new("RGB", (96, 64), "#468b76").save(sys.argv[1], format="PNG")', png], { cwd: root, env, encoding: 'utf8', timeout: 15000 });
assert.equal(prepare.status, 0, 'Synthetic image generation failed');
const report = { status: 'failed', packaged: Boolean(executable), mode: 'demo', fixture: 'synthetic', business_ipc_mocked: false, native_file_picker_mocked_only: true, real_ai_calls: 0, real_customer_sends: 0, production_data_accessed: false, completed: [], errors: [], window_closed: false };
let app, page;
async function until(check, label, timeout = 30000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('Timed out: ' + label); }
const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000 };
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
const choose = async name => { await page.getByRole('button', { name: new RegExp(name) }).click(); await page.getByRole('main', { name: `${name} 的客户接待` }).waitFor(); };
const screenshot = name => page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' });
async function launch() {
  app = await electron.launch(options); report.window_closed = false;
  assert.equal(await app.evaluate(({ app }) => app.isPackaged), Boolean(executable));
  page = await app.firstWindow(); page.on('pageerror', error => report.errors.push(error.message));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 1000));
  const snapshot = await invoke('get_snapshot'); assert.equal(snapshot.app.mode, 'demo');
  assert.equal(snapshot.app.version, JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version);
}
async function close() { const process = app.process(); await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close()); await until(() => process.exitCode !== null, 'normal close'); app = null; report.window_closed = true; }
async function add(file, count) {
  await app.evaluate(({ dialog }, target) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [target] }); }, file);
  await page.getByRole('region', { name: '业务补充', exact: true }).getByRole('button', { name: '添加附件', exact: true }).click();
  await until(async () => (await detail()).supplement.attachments.length === count, 'attachment persisted');
  await page.getByRole('region', { name: '业务补充', exact: true }).locator('li').filter({ hasText: path.basename(file) }).waitFor();
}
try {
  await launch(); await choose('Emma Wilson');
  const panel = () => page.getByRole('region', { name: '业务补充', exact: true });
  const note = '合成业务补充：附件为商家参考，客户尚未确认。';
  await page.getByLabel('业务补充内容', { exact: true }).fill(note);
  await until(async () => (await detail()).supplement.body === note, 'note saved');
  await add(png, 1); await panel().getByRole('img', { name: 'synthetic-reference.png 预览', exact: true }).waitFor();
  await add(textFile, 2);
  const txtRow = panel().locator('li').filter({ hasText: 'synthetic-terms.txt' });
  await txtRow.locator('summary').click(); await txtRow.getByText(text, { exact: true }).waitFor();
  await add(pdf, 3);
  const pdfRow = panel().locator('li').filter({ hasText: 'synthetic-terms.pdf' });
  await pdfRow.locator('summary').click(); await pdfRow.getByText(pdfText, { exact: true }).waitFor();
  const state = (await detail()).supplement;
  assert.equal(state.body, note); assert.equal(state.attachments.length, 3);
  assert(state.attachments.every(row => row.origin === 'business_supplement'));
  assert.equal(await panel().locator('img[src^="https:"]').count(), 0);
  await screenshot('attachments'); report.completed.push('native-picker-png-text-pdf-persistence-and-preview');
  await choose('Noah Brown');
  const other = await invoke('get_conversation', { conversation_id: 'demo-noah' });
  assert.deepEqual(other.supplement.attachments, []);
  assert.equal(await panel().getByText(/synthetic-reference|synthetic-terms/).count(), 0);
  await screenshot('other-customer'); report.completed.push('conversation-isolation');
  await choose('Emma Wilson');
  const revision = (await detail()).supplement.revision;
  await panel().locator('li').filter({ hasText: 'synthetic-terms.pdf' }).getByRole('button', { name: '移除', exact: true }).click();
  await until(async () => (await detail()).supplement.attachments.length === 2, 'attachment removed');
  assert((await detail()).supplement.revision > revision);
  assert.equal((await detail()).supplement.body, note);
  report.completed.push('remove-revises-note-without-changing-body');
  await invoke('check_ai');
  await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'synthetic AI check');
  await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
  await until(async () => (await detail()).manual_suggestion?.status === 'ready', 'suggestion with attachments');
  const suggestion = (await detail()).manual_suggestion;
  assert(suggestion.body);
  assert.equal((await detail()).conversation.human_owned, false);
  assert.equal(await page.getByRole('textbox', { name: '回复正文', exact: true }).inputValue(), '');
  await screenshot('suggestion'); report.completed.push('suggestion-keeps-merchant-attachments-without-takeover');
  await close(); await launch(); await choose('Emma Wilson');
  const reopened = (await detail()).supplement;
  assert.equal(reopened.body, note);
  assert.deepEqual(reopened.attachments.map(row => row.id).sort(), state.attachments.filter(row => row.name !== 'synthetic-terms.pdf').map(row => row.id).sort());
  await panel().getByRole('img', { name: 'synthetic-reference.png 预览', exact: true }).waitFor();
  await screenshot('reopened'); report.completed.push('normal-close-and-reopen');
  assert.deepEqual(report.errors, []); await close(); report.status = 'passed';
} catch (error) {
  report.error = error.message; process.exitCode = 1;
  if (page) { try { await screenshot('failure'); await writeFile(path.join(output, 'failure.txt'), (await page.locator('body').innerText()).slice(0, 16000)); } catch {} }
} finally {
  if (app) await app.close().catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
