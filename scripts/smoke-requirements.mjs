import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const index = process.argv.indexOf('--label');
const label = index < 0 ? '' : process.argv[index + 1];
const executableIndex = process.argv.indexOf('--executable');
const executable = executableIndex < 0 ? '' : assertReleasePath(root, process.argv[executableIndex + 1]);
const version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label), 'Pass a new --label');
const output = path.join(root, 'artifacts', 'verification', `requirements-${label}`);
await mkdir(output);
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-requirements-demo-'));
const data = path.join(temp, 'data');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temp, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temp, 'shared-settings');
env.RECEPTION_PYTHON = python;
env.LOCALAPPDATA = path.join(temp, 'local-app-data');
const report = { version, packaged: !!executable, mode: 'demo', actual_window: true, real_ai_calls: 0, customer_sends: 0, production_data_accessed: false, status: 'failed', completed: [], errors: [], window_closed: false };
const seed = `
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.procurement import merge_fact
s = ReceptionService(Path(sys.argv[2]), mode='demo')
try:
    s.inject_demo('demo-emma', 'I need 500 pcs of hats.')
    first = s._latest_buyer('demo-emma')
    s.inject_demo('demo-emma', '800 pcs of hats, please.')
    second = s._latest_buyer('demo-emma')
    case = s.get_conversation('demo-emma')['case']
    case['category_label'] = '帽子'
    case['facts']['quantity'] = merge_fact({'value':'500 pcs','status':'confirmed','evidence_ids':[first['message_id']]}, {'value':'800 pcs','status':'confirmed','evidence_ids':[second['message_id']]}, {first['message_id']:first, second['message_id']:second})
    s.repo.put('case',case['case_id'],case)
    task = s._create_assistance(case, second, None, 'fact_conflict', ['quantity'], '合成任务：核实数量')
    print(json.dumps({'case_id':case['case_id'],'task_id':task['task_id'],'first':first['message_id'],'second':second['message_id']}, ensure_ascii=False))
finally:
    s.close()
`;
let app, page;
async function until(fn, label, timeout = 30000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await fn()) return; await new Promise(resolve => setTimeout(resolve, 150)); } throw new Error(`Timed out: ${label}`); }
async function closeWindow() { const process = app.process(); await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close()); await until(() => process.exitCode !== null, 'normal window close'); app = null; report.window_closed = true; }
try {
  const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services/reception/src'), data], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
  assert.equal(prepared.status, 0, `Demo seed failed: ${prepared.stderr}`);
  const fixture = JSON.parse(prepared.stdout.trim());
  const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + path.join(output, 'chromium.log')], cwd: root, env, timeout: 45000 };
  const launch = async () => { app = await electron.launch(options); page = await app.firstWindow(); page.on('pageerror', error => report.errors.push(error.message)); await page.getByRole('textbox', { name: '回复正文' }).waitFor({ timeout: 45000 }); };
  await launch();
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma', case_id: fixture.case_id });
  assert.equal((await invoke('get_snapshot')).app.mode, 'demo');
  assert.equal((await invoke('get_snapshot')).app.version, version);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 940));
  await page.getByRole('button', { name: /Emma Wilson/ }).click();
  await page.getByRole('main', { name: 'Emma Wilson 的客户接待' }).waitFor();
  await page.getByText(/待你协助与跟进/).click();
  await page.getByRole('button', { name: '开始处理' }).click();
  await until(async () => !!(await detail()).assistance_tasks.find(task => task.task_id === fixture.task_id)?.processing_started_at, 'assistance start');
  await page.getByRole('textbox', { name: '核实结果', exact: true }).fill('合成核实：数量仍待客户确认');
  await page.getByRole('button', { name: '保存核实结果' }).click();
  await until(async () => (await detail()).assistance_tasks.find(task => task.task_id === fixture.task_id)?.resolution === '合成核实：数量仍待客户确认', 'assistance save');
  report.completed.push('assistance-start-save');
  await page.screenshot({ path: path.join(output, 'assistance.png'), animations: 'disabled' });
  await page.getByText('整理与复核需求').click();
  await page.getByRole('button', { name: '整理需求', exact: true }).click();
  await until(async () => (await detail()).requirement_proposals?.some(proposal => proposal.status === 'pending'), 'requirement proposal', 45000);
  const proposal = (await detail()).requirement_proposals.find(row => row.status === 'pending');
  assert.equal(proposal.case_revision, (await detail()).case.revision);
  await page.getByText('需求候选预览').first().click();
  await page.screenshot({ path: path.join(output, 'proposal.png'), animations: 'disabled' });
  await page.getByRole('button', { name: '应用候选' }).first().click();
  await until(async () => (await detail()).requirement_proposals.some(row => row.proposal_id === proposal.proposal_id && row.status === 'applied'), 'apply proposal');
  report.completed.push('organize-preview-apply');
  const actions = page.locator('.requirement-actions');
  await actions.getByText('新建事项与纠正归属').click();
  await actions.getByRole('button', { name: '新建采购事项' }).click();
  await until(async () => (await detail()).cases.length >= 2, 'create case');
  let view = await detail();
  const target = view.cases.find(row => row.case_id !== fixture.case_id);
  const candidate = view.case.facts.quantity.candidates.find(row => row.value === '800 pcs' && row.status === 'active');
  assert.ok(candidate, 'Expected active quantity candidate');
  await actions.getByLabel('需求字段').selectOption('quantity');
  await actions.getByLabel('移动范围').selectOption(candidate.candidate_id);
  await actions.getByLabel('目标事项').selectOption(target.case_id);
  await actions.getByRole('button', { name: '纠正归属' }).click();
  await until(async () => (await detail()).cases.find(row => row.case_id === target.case_id)?.facts.quantity?.value === '800 pcs', 'move candidate');
  view = await detail();
  assert.equal(view.case.facts.quantity.value, '500 pcs');
  report.completed.push('create-case-move-candidate');
  const quantity = actions.locator('.requirement-review').filter({ hasText: '数量' });
  await quantity.getByRole('button', { name: '采用候选', exact: true }).first().click();
  await until(async () => (await detail()).case.facts.quantity.review?.status === 'confirmed', 'review quantity');
  report.completed.push('review-fact');
  const card = page.locator('.requirements .fact').filter({ hasText: '数量' });
  await card.getByRole('button', { name: '原文 1' }).click();
  await until(async () => await page.locator(`[data-message-id="${fixture.first}"].message-highlight`).count() > 0, 'source location');
  report.completed.push('locate-source');
  const selector = page.getByRole('combobox', { name: '当前采购事项' });
  await selector.selectOption(target.case_id);
  await until(async () => (await page.getByLabel('展示品类').count()) > 0, 'target case');
  await page.getByText('整理与复核需求').click();
  await page.getByLabel('展示品类').fill('帽子');
  await page.getByRole('button', { name: '保存品类' }).click();
  await until(async () => (await detail()).cases.find(row => row.case_id === target.case_id)?.category_label === '帽子', 'target category');
  await selector.selectOption(fixture.case_id);
  await until(async () => (await selector.locator('option').allTextContents()).filter(text => text.startsWith('帽子 · 事项')).length === 2, 'distinct same-category cases');
  report.completed.push('same-category-cases');
  await page.screenshot({ path: path.join(output, 'wide.png'), animations: 'disabled' });
  const resize = page.getByRole('separator', { name: '调整右侧栏宽度' });
  await resize.focus();
  for (let i = 0; i < 4; i++) await resize.press('ArrowRight');
  await until(async () => Number(await resize.getAttribute('aria-valuenow')) === 280, '280px requirements column');
  const narrowColumn = page.locator('.requirements-column .requirements');
  assert.equal(await narrowColumn.evaluate(element => element.scrollWidth > element.clientWidth), false, '280px requirements column overflows');
  await page.screenshot({ path: path.join(output, 'column-280.png'), animations: 'disabled' });
  report.completed.push('column-280-no-overflow');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(900, 800));
  await page.getByRole('button', { name: '采购需求', exact: true }).click();
  const panel = page.locator('.requirements-drawer-content .requirements');
  await panel.waitFor();
  const overflow = await panel.evaluate(element => [...element.querySelectorAll('*')].some(child => child.getBoundingClientRect().right > element.getBoundingClientRect().right + 1));
  assert.equal(overflow, false, 'Requirement drawer content overflows horizontally');
  await page.screenshot({ path: path.join(output, 'drawer.png'), animations: 'disabled' });
  report.completed.push('drawer-no-overflow');
  assert.equal((await detail()).conversation.human_owned, false);
  assert.equal((await invoke('get_snapshot')).outbox.length, 0);
  await closeWindow();
  assert.equal((await readFile(path.join(data, '.reception-clean-shutdown'), 'utf8')).trim(), 'reception-managed-clean-v1');
  await launch();
  const reopened = await page.evaluate(({ caseId }) => window.reception.invoke('get_conversation', { conversation_id: 'demo-emma', case_id: caseId }), { caseId: fixture.case_id });
  assert.equal(reopened.assistance_tasks.find(task => task.task_id === fixture.task_id).resolution, '合成核实：数量仍待客户确认');
  assert.equal(reopened.case.facts.quantity.review.status, 'confirmed');
  assert.equal(reopened.cases.find(row => row.case_id === target.case_id).facts.quantity.value, '800 pcs');
  report.completed.push('reopen-persistence');
  await page.screenshot({ path: path.join(output, 'reopened.png'), animations: 'disabled' });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 1000));
  await page.getByRole('button', { name: '连接与设置', exact: true }).click();
  await page.getByText('高级处理参数', { exact: true }).click();
  const advanced = page.locator('.processing-settings');
  for (const [name, expected] of [['每轮原图总量（MiB）', '64'], ['图片准备时限（秒）', '60'], ['同时下载图片数', '2'], ['同一来源连续生成失败上限', '3'], ['同时翻译请求数', '2'], ['翻译参考前文条数', '2'], ['翻译参考前文总量（UTF-8 字节）', '300']]) {
    assert.equal(await advanced.getByLabel(name, { exact: true }).inputValue(), expected);
  }
  assert.equal(await page.getByLabel('新消息检测（秒）', { exact: true }).inputValue(), '2');
  assert.equal(await page.getByLabel('连续消息合并（秒）', { exact: true }).inputValue(), '1');
  await advanced.getByLabel('同时下载图片数', { exact: true }).fill('3');
  await advanced.getByLabel('新增品类', { exact: true }).fill('帽子');
  await advanced.getByRole('button', { name: '添加品类条件', exact: true }).click();
  await advanced.locator('.category-template').getByRole('checkbox', { name: '数量', exact: true }).check();
  await page.getByRole('button', { name: '保存连接参数', exact: true }).click();
  await until(async () => {
    const saved = await invoke('get_snapshot');
    return saved.settings.image_parallelism === 3 && saved.settings.required_fields_by_category?.['帽子']?.includes('quantity') && saved.reception.auto_send === false;
  }, 'advanced processing settings saved');
  await advanced.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'advanced.png'), animations: 'disabled' });
  report.completed.push('advanced-defaults-and-category-template-save');
  await closeWindow();
  assert.deepEqual(report.errors, []);
  report.status = 'passed';
} catch (error) {
  report.failure = error instanceof Error ? error.message : String(error);
  if (page && !page.isClosed()) await page.screenshot({ path: path.join(output, 'failure.png'), animations: 'disabled' }).catch(() => {});
  throw error;
} finally {
  if (app) { try { await closeWindow(); } catch { await app.close().catch(() => {}); } }
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
}
