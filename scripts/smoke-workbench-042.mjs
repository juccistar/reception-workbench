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
  assert(['--label', '--assets'].includes(key) && value && !value.startsWith('--') && !options.has(key), 'Use --label LABEL and optionally --assets RESOURCES');
  options.set(key, value);
}
const label = options.get('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a unique lowercase --label');
const output = path.join(root, 'artifacts/verification', `workbench-042-${label}`);
await mkdir(output);
const report = {
  status: 'failed', coverage: 'synthetic_production_window', actual_window: false, actual_exe_entry: false,
  fixture_host: true, production_renderer_and_preload: true, production_service_client: true,
  synthetic_python_integration: true, candidate_assets: options.has('--assets'), real_ai_calls: 0,
  real_customer_sends: 0, screenshots: [], checks: {}, renderer_errors: [],
  not_covered: ['real account and paid model validation', 'actual release executable entry'],
};
let app, temp;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const until = async (check, message, timeout = 30000) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await pause(150); }
  throw new Error(message);
};
try {
  temp = await mkdtemp(path.join(os.tmpdir(), 'arw-workbench-042-'));
  const assets = options.has('--assets') ? path.join(path.resolve(options.get('--assets')), 'app.asar') : path.join(root, 'artifacts/app');
  const python = path.resolve(process.env.RECEPTION_PYTHON || path.join(root, 'services/reception/.venv/Scripts/python.exe'));
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|USERPROFILE|USERNAME|USERDOMAIN|COMPUTERNAME|SESSIONNAME|OS|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|ALLUSERSPROFILE|PUBLIC|HOMEDRIVE|HOMEPATH)$/i.test(key)) env[key] = value;
  }
  Object.assign(env, { HARNESS_ROOT: root, HARNESS_TEMP: temp, HARNESS_ASSETS: assets, HARNESS_PYTHON: python, HARNESS_MANUAL_AI_DIRECT: '1', LOCALAPPDATA: path.join(temp, 'local'), APPDATA: path.join(temp, 'roaming'), RECEPTION_MODEL_BUDGET_DIRECTORY: path.join(temp, 'model-budget') });
  const host = path.join(temp, 'host.cjs');
  await build({ entryPoints: [path.join(root, 'scripts/fixtures/workbench-host-042.ts')], outfile: host, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] });
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env, timeout: 60000 });
  const child = app.process(), page = await app.firstWindow();
  report.actual_window = true;
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
  const shot = async name => {
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].show());
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const encoded = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage({ stayAwake: true })).toPNG().toString('base64'));
    await writeFile(path.join(output, name), Buffer.from(encoded, 'base64')); report.screenshots.push(name);
  };
  const nav = name => page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name, exact: true }).first();
  await nav('客户接待').click({ timeout: 45000 });
  await page.getByRole('button', { name: /Emma Wilson/ }).first().click({ timeout: 45000 });
  await page.getByRole('main', { name: 'Emma Wilson 的客户接待' }).waitFor();
  const list = page.getByRole('region', { name: '客户列表' });
  assert.equal(await list.locator('img, [class*="avatar"]').count(), 0, 'Customer list must not contain avatars');
  const messages = page.locator('.messages article');
  await until(async () => await messages.count() > 0, 'Synthetic messages are missing');
  for (const text of await messages.locator('time').allTextContents()) assert.match(text.trim(), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  report.checks.customer_list_and_message_times = true;
  await shot('01-reception.png');
  const snapshot = await invoke('get_snapshot');
  assert.equal(snapshot.app.mode, 'live'); assert.equal(snapshot.checks.ai.status, 'not_checked');
  const start = page.getByRole('button', { name: '开启自动接待', exact: true });
  await start.click();
  await page.getByRole('button', { name: '关闭自动接待', exact: true }).waitFor();
  await page.getByRole('button', { name: '关闭自动接待', exact: true }).click();
  await start.waitFor();
  assert.equal((await app.evaluate(() => globalThis.__getWindowAiStats())).checks, 0);
  report.checks.top_level_start_stop_without_precheck = true;
  const editor = page.getByRole('textbox', { name: '回复正文', exact: true });
  const translateToggle = page.getByRole('checkbox', { name: '自动中文翻译' });
  if (await translateToggle.isChecked()) await translateToggle.uncheck();
  const injected = await app.evaluate(() => globalThis.__injectWindowFixture());
  await until(async () => (await detail()).messages.some(item => item.message_id === injected.message_id), 'Injected source message did not arrive');
  await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
  await until(async () => (await detail()).manual_suggestion?.status === 'ready', 'Suggestion did not finish');
  const suggestion = page.getByRole('region', { name: '待处理回复建议' });
  await suggestion.waitFor();
  await suggestion.getByText('合成建议中文译文').waitFor();
  assert.equal(await translateToggle.isChecked(), false);
  assert.equal(await page.locator('.messages .message-translation').count(), 0);
  await shot('02-suggestion.png'); report.checks.direct_suggestion_without_ai_check = true;
  report.checks.suggestion_translation_with_chat_translation_off = true;
  const source = page.locator('.canonical-field').filter({ hasText: '数量与单位' }).getByRole('button', { name: '来源', exact: true });
  await source.first().click();
  await until(async () => await page.locator('[data-message-id]').evaluateAll((nodes, id) => nodes.some(node => node.getAttribute('data-message-id') === id && node.classList.contains('message-highlight')), injected.message_id), 'Source message was not highlighted');
  report.checks.source_navigates_to_injected_message = true;
  const body = 'Please keep this draft while the model is unavailable.';
  await editor.fill(body);
  await until(async () => (await detail()).chat_draft?.body === body, 'Draft was not saved');
  await app.evaluate(() => globalThis.__setWindowAiFailure(true));
  await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'AI不可用' });
  await dialog.waitFor(); assert.equal(await editor.inputValue(), body);
  assert.equal(await dialog.getByRole('button', { name: '查看模型设置', exact: true }).count(), 1);
  await shot('03-ai-unavailable.png');
  await dialog.getByRole('button', { name: '知道了', exact: true }).click();
  await app.evaluate(() => globalThis.__repeatWindowAiFailure()); await pause(600);
  assert.equal(await dialog.count(), 0); assert.equal(await editor.inputValue(), body);
  await app.evaluate(() => globalThis.__setWindowAiFailure(false));
  await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
  await until(async () => { const value = (await detail()).manual_suggestion; return value?.status === 'ready' && value.mode === 'polish'; }, 'Polish retry did not finish');
  assert.equal(await editor.inputValue(), body);
  await suggestion.getByText('合成建议中文译文').waitFor();
  assert.equal(await translateToggle.isChecked(), false);
  await shot('04-polish-retry.png'); report.checks.failure_popup_deduplication_and_retry = true;
  report.checks.polish_translation_with_chat_translation_off = true;
  await app.evaluate(() => globalThis.__blockWindowAi(true));
  await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
  await until(async () => (await app.evaluate(() => globalThis.__windowAiBlockState())).entered, 'Synthetic AI request did not block');
  const blockedJob = (await app.evaluate(() => globalThis.__windowAiBlockState())).jobs.find(job => job.status === 'running');
  assert(blockedJob, 'Blocked manual generation must have an owned running job');
  await page.getByRole('button', { name: '取消生成', exact: true }).click();
  await app.evaluate(() => globalThis.__blockWindowAi(false));
  await until(async () => {
    const state = await app.evaluate(() => globalThis.__windowAiBlockState());
    const job = state.jobs.find(job => job.job_id === blockedJob.job_id);
    return state.cancel_observed && job && ['cancelled', 'stale', 'failed'].includes(job.status);
  }, 'Cancelled AI worker or owned job did not settle');
  await pause(500);
  assert.equal(await dialog.count(), 0);
  assert.equal(await editor.inputValue(), body);
  report.checks.cancelled_generation_late_failure_no_popup = true;
  await app.evaluate(() => globalThis.__blockWindowHistory(true));
  await page.getByRole('button', { name: '补全当前客户历史' }).click();
  await until(async () => (await app.evaluate(() => globalThis.__windowHistoryState())).entered, 'Synthetic history read did not block');
  await page.locator('.active-task').getByText('正在补全历史', { exact: true }).waitFor();
  await shot('07-history-running.png');
  await app.evaluate(() => globalThis.__blockWindowHistory(false));
  await until(async () => (await invoke('get_snapshot')).jobs.some(job => job.kind === 'fill_history' && job.conversation_id === 'demo-emma' && job.status === 'succeeded' && /历史补全完成/.test(job.message)), 'History did not complete');
  await until(async () => await page.locator('.active-task').filter({ hasText: '正在补全历史' }).count() === 0, 'Completed history remained active');
  report.checks.history_running_and_service_completion = true;
  report.ai_stats = await app.evaluate(() => globalThis.__getWindowAiStats());
  assert.equal(report.ai_stats.checks, 0);
  const acceptedBody = 'Synthetic ordinary single-body send.';
  await editor.fill(acceptedBody);
  await until(async () => (await detail()).chat_draft?.body === acceptedBody, 'Ordinary draft did not save');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await until(async () => (await detail()).chat_draft?.status === 'accepted', 'Ordinary send was not accepted');
  assert.equal(await page.getByRole('dialog').count(), 0);
  await shot('08-ordinary-accepted.png'); report.checks.ordinary_single_send_without_confirmation = true;
  await app.evaluate(() => globalThis.__setWindowSendMode('unknown'));
  const frozenBody = 'Synthetic frozen message awaiting its original receipt.';
  const newerBody = 'Keep this newer input after the unknown result.';
  await editor.fill(frozenBody);
  await until(async () => (await detail()).chat_draft?.body === frozenBody, 'Unknown test draft did not save');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await until(async () => (await app.evaluate(() => globalThis.__windowSendStats())).entered, 'Synthetic transport did not enter');
  await editor.fill(newerBody);
  await until(async () => (await detail()).chat_draft?.body === newerBody, 'Newer input did not save during send');
  await app.evaluate(() => globalThis.__releaseWindowSend());
  await until(async () => (await invoke('list_outbox')).some(row => row.kind === 'send' && row.status === 'unknown'), 'Original operation did not retain unknown');
  await until(async () => await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), 'Unknown did not fence a second send');
  assert.equal(await editor.inputValue(), newerBody);
  assert.equal(await page.getByRole('button', { name: /发送记录|核查原操作|核查发送/ }).count(), 0);
  assert.equal((await app.evaluate(() => globalThis.__windowSendStats())).attempts, 2);
  await shot('09-unknown-preserves-input.png'); report.checks.unknown_original_operation_fence_preserves_input = true;
  await app.evaluate(() => globalThis.__seedWindowQuotes());
  await nav('待我报价').click();
  const quoteList = page.getByRole('region', { name: '待报价事项' });
  const quoteDetail = page.getByRole('region', { name: '事项详情' });
  await until(async () => await quoteList.getByRole('button').count() === 28, 'Multiple quote cases were not projected');
  assert(await quoteList.evaluate(node => node.scrollHeight > node.clientHeight));
  assert(await quoteDetail.evaluate(node => node.scrollHeight > node.clientHeight));
  await quoteList.evaluate(node => { node.scrollTop = 240; });
  const listPosition = await quoteList.evaluate(node => node.scrollTop);
  const detailPosition = await quoteDetail.evaluate(node => node.scrollTop);
  await quoteDetail.evaluate(node => { node.scrollTop = 300; });
  assert.equal(await quoteList.evaluate(node => node.scrollTop), listPosition);
  assert.notEqual(await quoteDetail.evaluate(node => node.scrollTop), detailPosition);
  const quoteBody = 'Synthetic shared supplement saved before changing the quote.';
  await page.getByRole('textbox', { name: '报价业务补充', exact: true }).fill(quoteBody);
  await quoteList.getByRole('button').nth(8).click();
  await until(async () => /active/.test(await quoteList.getByRole('button').nth(8).getAttribute('class') || ''), 'Quote selection did not stay in its page');
  assert.equal((await detail()).supplement.body, quoteBody);
  assert.equal(await nav('待我报价').getAttribute('aria-current'), 'page');
  await shot('10-quotes-independent-scroll.png'); report.checks.quote_scroll_and_shared_supplement_saved_switch = true;
  for (const name of ['开始', '待我报价', '产品库', '供应商', '采购中心', '客户资料', '资料与政策', '连接与设置', '运行记录']) {
    await nav(name).click();
    await until(async () => await nav(name).getAttribute('aria-current') === 'page', `Navigation failed: ${name}`);
    await shot(`page-${['开始', '待我报价', '产品库', '供应商', '采购中心', '客户资料', '资料与政策', '连接与设置', '运行记录'].indexOf(name) + 1}.png`);
    if (name === '产品库') {
      const directory = page.getByRole('region', { name: '产品目录' });
      await directory.getByRole('button', { name: /合成报价产品 1/ }).click();
      const productDialog = page.getByRole('dialog', { name: '产品与供货方案', exact: true });
      await productDialog.getByText('合成供货商', { exact: true }).waitFor();
      assert(await directory.locator('.product-042-thumbnail').evaluateAll(nodes => nodes.length === 3 && nodes.every(node => node.getBoundingClientRect().width <= 64)));
      await shot('11-populated-product-detail.png');
      await productDialog.getByRole('button', { name: '设为默认', exact: true }).click();
      await until(async () => (await invoke('list_products')).items.some(product => product.name === '合成报价产品 1' && product.default_offer_id), 'Product default offer did not save');
      await productDialog.getByRole('button', { name: '关闭产品与供货方案', exact: true }).click();
      report.checks.populated_compact_products_and_default_offer = true;
    }
    if (name === '供应商') {
      await page.getByRole('region', { name: '供应商目录' }).getByRole('button', { name: /合成供货商/ }).click();
      const supplierDialog = page.getByRole('dialog', { name: '供应商详情', exact: true });
      await supplierDialog.getByRole('heading', { name: '合成供货商', exact: true }).waitFor();
      await shot('12-populated-supplier-detail.png');
      await supplierDialog.getByRole('button', { name: '编辑供应商', exact: true }).click();
      await supplierDialog.getByLabel('联系方式', { exact: true }).fill('更新后的隔离窗口联系信息');
      await supplierDialog.getByRole('button', { name: '保存供应商', exact: true }).click();
      await supplierDialog.getByText('联系方式：更新后的隔离窗口联系信息', { exact: true }).waitFor();
      await shot('13-supplier-edit-saved.png');
      await supplierDialog.getByRole('button', { name: '关闭供应商详情', exact: true }).click();
      report.checks.populated_supplier_and_real_save = true;
    }
    if (name === '资料与政策') {
      const save = page.getByRole('button', { name: /保存资料/ }).first();
      await save.scrollIntoViewIfNeeded();
      await shot('14-policy-form-actions.png');
    }
    if (name === '连接与设置') {
      const settings = page.getByRole('navigation', { name: '设置分类' });
      for (const section of ['自动接待', '聊天翻译', '图片与上下文', '平台连接', '数据与备份', '飞书登记', '界面布局', '版本与更新']) {
        await settings.getByRole('button', { name: section, exact: true }).click();
        assert.match(await settings.getByRole('button', { name: section, exact: true }).getAttribute('class'), /active/);
        await shot(`settings-${['自动接待', '聊天翻译', '图片与上下文', '平台连接', '数据与备份', '飞书登记', '界面布局', '版本与更新'].indexOf(section) + 1}.png`);
      }
      report.checks.eight_settings_sections = true;
    }
  }
  report.checks.page_navigation = true;
  await nav('客户接待').click();
  const resize = async (name, dx, dy) => {
    const handle = page.getByRole('separator', { name, exact: true });
    const before = Number(await handle.getAttribute('aria-valuenow'));
    const box = await handle.boundingBox(); assert(box);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down(); await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2 + dy, { steps: 5 }); await page.mouse.up();
    await until(async () => Number(await handle.getAttribute('aria-valuenow')) !== before, `Resize did not move: ${name}`);
  };
  await resize('调整客户列表宽度', 30, 0);
  await resize('调整右侧栏宽度', -30, 0);
  await resize('调整采购需求与业务补充高度', 0, -30);
  await shot('05-resized.png'); report.checks.drag_horizontal_and_vertical = true;
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(800, 700));
  await until(async () => await page.locator('.app.layout-narrow').count() === 1, 'Narrow layout not active');
  await shot('06-narrow.png'); report.checks.narrow_layout = true;
  await page.getByRole('button', { name: '打开客户列表', exact: true }).click();
  const customersDrawer = page.getByRole('dialog', { name: '客户列表', exact: true });
  await customersDrawer.waitFor();
  assert.equal(await customersDrawer.locator('img, [class*="avatar"]').count(), 0);
  await shot('15-narrow-customers.png');
  await customersDrawer.getByRole('button', { name: '关闭客户列表', exact: true }).first().click();
  await page.getByRole('button', { name: '采购需求', exact: true }).click();
  const requirementsDrawer = page.getByRole('dialog', { name: '采购需求', exact: true });
  await requirementsDrawer.getByRole('textbox', { name: '业务补充内容', exact: true }).scrollIntoViewIfNeeded();
  await shot('16-narrow-requirements.png');
  await requirementsDrawer.getByRole('button', { name: '关闭采购需求', exact: true }).click();
  await nav('待我报价').click();
  await page.getByRole('region', { name: '事项详情' }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await shot('17-narrow-quotes.png');
  await nav('连接与设置').click();
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: '自动接待', exact: true }).click();
  await page.getByRole('combobox', { name: '自动接待范围', exact: true }).waitFor();
  await page.getByRole('button', { name: '保存范围', exact: true }).scrollIntoViewIfNeeded();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await shot('18-narrow-settings-actions.png');
  await nav('资料与政策').click();
  await page.getByRole('button', { name: /保存资料/ }).first().scrollIntoViewIfNeeded();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await shot('19-narrow-policy-actions.png');
  report.checks.narrow_customer_requirements_quote_and_form_access = true;
  assert.deepEqual(report.renderer_errors, []);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => child.exitCode !== null, 'Fixture host did not exit');
  assert.equal(child.exitCode, 0);
  assert.equal(JSON.parse(await readFile(path.join(temp, 'normal-shutdown.json'), 'utf8')).service_exited, true);
  app = undefined; report.checks.normal_shutdown = true;
  report.status = 'passed';
} catch (error) {
  report.error = String(error?.stack || error); process.exitCode = 1;
  if (app) try { const page = app.windows()[0]; if (page) { await page.screenshot({ path: path.join(output, 'failure.png') }); report.screenshots.push('failure.png'); } } catch { /* Preserve the original failure. */ }
} finally {
  if (app) await app.close().catch(() => {});
  if (temp) await rm(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }).catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report, null, 2));
