import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const label = process.argv[2];
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a unique lowercase label');
const output = path.join(root, 'artifacts/verification', `reply-progress-042-${label}`);
await mkdir(output);
const report = { status: 'failed', actual_window: false, fixture_backend: true, projected_automatic_stages: true, real_provider_requests: 0, real_customer_sends: 0, checks: {}, screenshots: [], renderer_errors: [] };
let app, temp;
const until = async (check, message) => {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(message);
};
try {
  temp = await mkdtemp(path.join(os.tmpdir(), 'arw-progress-042-'));
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|USERPROFILE|USERNAME|USERDOMAIN|COMPUTERNAME|SESSIONNAME|OS|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|ALLUSERSPROFILE|PUBLIC|HOMEDRIVE|HOMEPATH)$/i.test(key)) env[key] = value;
  }
  Object.assign(env, { HARNESS_ROOT: root, HARNESS_TEMP: temp, HARNESS_ASSETS: path.join(root, 'artifacts/app'), HARNESS_PYTHON: path.join(root, 'services/reception/.venv/Scripts/python.exe'), HARNESS_MANUAL_AI_DIRECT: '1', LOCALAPPDATA: path.join(temp, 'local'), APPDATA: path.join(temp, 'roaming'), RECEPTION_MODEL_BUDGET_DIRECTORY: path.join(temp, 'model-budget') });
  const host = path.join(temp, 'host.cjs');
  await build({ entryPoints: [path.join(root, 'scripts/fixtures/workbench-host-042.ts')], outfile: host, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] });
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env, timeout: 60000 });
  const page = await app.firstWindow();
  report.actual_window = true;
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const shot = async name => {
    await app.evaluate(({ BrowserWindow }) => { const window = BrowserWindow.getAllWindows()[0]; window.show(); window.focus(); });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const data = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage({ stayAwake: true })).toPNG().toString('base64'));
    await writeFile(path.join(output, name), Buffer.from(data, 'base64'));
    report.screenshots.push(name);
  };
  await app.evaluate(() => globalThis.__seedWindowShowcase());
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '客户接待', exact: true }).first().click();
  await page.getByRole('button', { name: /Emma Wilson/ }).first().click();
  await page.getByRole('main', { name: 'Emma Wilson 的客户接待' }).waitFor();
  await app.evaluate(() => globalThis.__blockWindowAi(true));
  await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
  await until(async () => (await app.evaluate(() => globalThis.__windowAiBlockState())).entered, 'Manual suggestion did not start');
  const suggestion = page.getByRole('region', { name: '待处理回复建议' });
  await suggestion.getByText('正在生成建议', { exact: true }).waitFor();
  assert.equal(await page.locator('.active-task').count(), 0);
  assert.equal(await page.locator('.reply-activity').count(), 0);
  assert.equal(await suggestion.locator('.tiny-spinner').evaluate(node => getComputedStyle(node).animationName), 'spin');
  await shot('01-manual-suggestion.png');
  await suggestion.getByRole('button', { name: '取消生成', exact: true }).click();
  await app.evaluate(() => globalThis.__blockWindowAi(false));
  await suggestion.waitFor({ state: 'hidden' });
  report.checks.manual_single_location_busy_and_cancel = true;

  const phases = [
    ['generating', '正在生成接待回复'], ['saving', '正在整理并保存回复…'],
    ['prepared', '回复已保存，正在准备发送'], ['sending', '正在提交第 1/2 条，等待平台回执'],
    ['unknown', '已发送 0/2 条，第 1 条发送结果待确认'], ['complete', '本轮回复已发送，共 2 条'],
  ];
  for (let index = 0; index < phases.length; index++) {
    const [phase, text] = phases[index];
    await app.evaluate((_, value) => globalThis.__setWindowReplyProgress(value), phase);
    await page.locator('.reply-activity').getByText(text, { exact: true }).waitFor();
    if (index === 0) await page.locator('.reply-activity').evaluate(node => { node.dataset.continuity = 'preserved'; });
    else assert.equal(await page.locator('.reply-activity').getAttribute('data-continuity'), 'preserved', `Progress container changed at ${phase}`);
    assert.equal(await page.locator('.active-task').count(), 0);
    await shot(`${index + 2 < 10 ? '0' : ''}${index + 2}-${phase}.png`);
  }
  assert.equal(await page.getByRole('button', { name: /核查原发送|发送记录/ }).count(), 0);
  report.checks.automatic_progress_keeps_one_container = true;
  report.checks.unknown_and_success_do_not_add_history_actions = true;
  await app.evaluate(() => globalThis.__setWindowReplyProgress('clear'));
  await app.evaluate(() => globalThis.__setWindowImageProgress('failed'));
  await page.getByRole('button', { name: '读取图片', exact: true }).click();
  await page.getByText('正在读取商品资料', { exact: true }).waitFor();
  assert.equal(await page.locator('.requirement-product-images').getAttribute('aria-busy'), 'true');
  await until(() => page.locator('.requirement-product-images .tiny-spinner').evaluateAll(nodes => nodes.some(node => node.isConnected && getComputedStyle(node).animationName === 'spin')), 'Image loading animation is missing');
  await shot('08-image-metadata.png');
  await app.evaluate(() => globalThis.__setWindowImageProgress('download'));
  await page.getByText('正在下载商品图片', { exact: true }).waitFor();
  await shot('09-image-download.png');
  await app.evaluate(() => globalThis.__setWindowImageProgress('ready'));
  await page.getByRole('button', { name: '查看需求商品原图', exact: true }).waitFor();
  await shot('10-image-ready.png');
  report.checks.image_retry_stage_spinner_and_completion = true;
  assert.deepEqual(report.renderer_errors, []);
  const child = app.process();
  await app.close(); app = undefined;
  await until(() => child.exitCode !== null, 'Window did not exit');
  report.normal_shutdown = JSON.parse(await readFile(path.join(temp, 'normal-shutdown.json'), 'utf8')).service_exited === true;
  assert(report.normal_shutdown);
  report.status = 'passed';
} catch (error) {
  report.error = error instanceof Error ? error.stack : String(error);
  if (app) { try { await app.close(); } catch {} }
  process.exitCode = 1;
} finally {
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  if (temp) await rm(temp, { recursive: true, force: true }).catch(() => {});
  console.log(JSON.stringify({ report: path.join(output, 'report.json'), status: report.status, checks: report.checks }));
}
