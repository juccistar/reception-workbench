import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--label', '--assets'].includes(key) && value && !value.startsWith('--') && !options.has(key), 'Use --label and optionally --assets once each');
  options.set(key, value);
}
const label = options.get('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const output = path.join(root, 'artifacts/verification', `workbench-040-${label}`);
await mkdir(output);
const report = { status: 'failed', actual_window: false, actual_exe_entry: false, production_main: false, fixture_host: true, production_service_client: true, real_service_rpc: true, synthetic_python_integration: true, production_renderer_and_preload: true, candidate_assets: options.has('--assets'), real_ai_calls: 0, real_customer_sends: 0, screenshots: [], checks: {}, renderer_errors: [] };
let app, temp;
try {
  temp = await mkdtemp(path.join(os.tmpdir(), 'arw-workbench-040-'));
  const assets = options.has('--assets') ? path.resolve(options.get('--assets')) : path.join(root, 'artifacts/app');
  const python = process.env.RECEPTION_PYTHON || path.join(root, 'services/reception/.venv/Scripts/python.exe');
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION|HARNESS)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
  Object.assign(env, { HARNESS_ROOT: root, HARNESS_TEMP: temp, HARNESS_ASSETS: assets, HARNESS_PYTHON: path.resolve(python), LOCALAPPDATA: path.join(temp, 'local'), APPDATA: path.join(temp, 'roaming'), HOME: path.join(temp, 'home'), USERPROFILE: path.join(temp, 'home') });
  const host = path.join(temp, 'workbench-host.cjs');
  await build({ entryPoints: [path.join(root, 'scripts/fixtures/workbench-host-040.ts')], outfile: host, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] });
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env, timeout: 45000 });
  const child = app.process();
  const page = await app.firstWindow(); report.actual_window = true;
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  const shot = async name => { await page.screenshot({ path: path.join(output, name), animations: 'disabled' }); report.screenshots.push(name); };
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
  const until = async (fn, message, timeout = 45000) => { const end = Date.now() + timeout; while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 150)); } throw new Error(message); };
  await page.getByRole('button', { name: /Emma Wilson/ }).click({ timeout: 45000 });
  await page.getByRole('main', { name: 'Emma Wilson 的客户接待' }).waitFor();
  assert.equal((await detail()).conversation.conversation_id, 'demo-emma');
  await shot('01-conversation.png'); report.checks.selection = true;
  const editor = page.getByRole('textbox', { name: '回复正文', exact: true });
  const text = 'Thank you. We will check your sample request.';
  await editor.fill(text);
  await until(async () => (await detail()).chat_draft?.body === text, 'Draft was not saved');
  await shot('02-draft.png'); report.checks.edit_and_save = true;
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await until(async () => (await detail()).chat_draft?.status === 'accepted', 'Synthetic send receipt was not accepted');
  await page.getByText('平台已受理', { exact: true }).first().waitFor();
  await shot('03-receipt.png'); report.checks.synthetic_send_accepted = true;
  await app.evaluate(() => globalThis.__injectWindowFixture());
  await until(async () => (await detail()).messages.some(message => message.text === 'I need 100 pieces'), 'Synthetic buyer message missing');
  await invoke('check_ai');
  await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'Synthetic AI check did not finish');
  await until(async () => (await editor.inputValue()) === '', 'Accepted draft did not clear');
  await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
  await until(async () => (await detail()).manual_suggestion?.status === 'ready', 'Synthetic suggestion did not finish');
  const suggestion = (await detail()).manual_suggestion;
  assert.equal(typeof suggestion.body, 'string'); assert(suggestion.body.trim());
  assert(!suggestion.reply_messages || suggestion.reply_messages.length === 1);
  await page.getByRole('region', { name: '待处理回复建议' }).getByText(suggestion.body, { exact: true }).waitFor();
  await page.getByRole('region', { name: '会话摘要' }).waitFor();
  assert.equal((await detail()).case?.requirements?.quantity?.value, '100件');
  await page.getByRole('region', { name: '采购需求' }).getByText('100件', { exact: true }).waitFor();
  await shot('04-suggestion-requirements.png'); report.checks.suggestion_summary_requirements = true;
  await page.getByRole('button', { name: '连接与设置', exact: true }).click();
  const models = page.getByRole('region', { name: '全局模型配置' });
  await models.getByText(/接待 API：未选择/).waitFor();
  await models.scrollIntoViewIfNeeded(); await shot('05-model-summary.png'); report.checks.model_summary = true;
  assert.deepEqual(report.renderer_errors, []);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => child.exitCode !== null, 'Fixture host did not exit');
  assert.equal(child.exitCode, 0); assert.equal(JSON.parse(await readFile(path.join(temp, 'normal-shutdown.json'), 'utf8')).service_exited, true);
  app = undefined; report.checks.normal_shutdown = true; report.status = 'passed';
} catch (error) {
  report.error = String(error?.stack || error); process.exitCode = 1;
  if (app) try { const page = app.windows()[0]; if (page) { await page.screenshot({ path: path.join(output, 'failure.png') }); report.screenshots.push('failure.png'); } } catch { /* Preserve the original failure. */ }
} finally {
  if (app) await app.close().catch(() => {});
  if (temp) await rm(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }).catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report, null, 2));
