import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

// Build first. Add --executable <candidate-exe> to drive a packaged application.
// This script always creates a fresh Demo workspace and never runs an installer.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const labelIndex = process.argv.indexOf('--label');
const label = labelIndex < 0 ? '' : process.argv[labelIndex + 1];
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const executableIndex = process.argv.indexOf('--executable');
const packagedExecutable = executableIndex < 0 ? '' : assertReleasePath(root, process.argv[executableIndex + 1]);
const output = path.join(root, 'artifacts', 'verification', `workbench-0313-${label}`);
await mkdir(output); // Existing labels are never overwritten.
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-0313-demo-'));
const data = path.join(temp, 'data');
const python = path.join(root, 'services', 'reception', '.venv', 'Scripts', 'python.exe');
const env = { ...process.env };
for (const key of Object.keys(env)) {
  if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
}
env.RECEPTION_BOOTSTRAP_DIR = path.join(temp, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temp, 'shared-settings');
env.LOCALAPPDATA = path.join(temp, 'local-app-data');
env.RECEPTION_PYTHON = python;
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const report = { status: 'failed', mode: 'demo', fixture: 'synthetic', packaged: Boolean(packagedExecutable), completed: [], errors: [], window_closed: false, limits: ['Synthetic fixtures do not validate real platform, AI, supplier inquiries, or customer sends.', 'Screenshots require visual inspection.', 'The no-demand fixture has no current case; historical Demo cases may still exist.'] };
const seed = `
import io, json, sys
from pathlib import Path
from PIL import Image
sys.path.insert(0, sys.argv[1])
from reception_service.image_assets import decode_image
from reception_service.service import ReceptionService
s = ReceptionService(Path(sys.argv[2]), mode='demo')
try:
    s.inject_demo('demo-emma', 'Synthetic: 500 blue cotton hats for France.')
    msg = s._latest_buyer('demo-emma')
    detail = s.get_conversation('demo-emma')
    case = detail['case']
    values = {'product': '棉帽', 'product_id': '123456789', 'product_url': None, 'product_images': [], 'quantity': '500件', 'specifications': '蓝色棉质', 'customization': '刺绣', 'destination': '法国', 'delivery': '30天', 'budget': '每件2美元', 'packaging': '单独包装', 'certifications': 'CE', 'contact': '邮件'}
    case['requirements'] = {k: {'value': v, 'evidence_ids': [msg['message_id']]} for k, v in values.items()}
    case['requirements_format'] = 'ai-v1'
    s.repo.put('case', case['case_id'], case)
    for cid, text in [('demo-emma', '合成摘要：客户需要蓝色棉帽。'), ('demo-noah', '合成摘要：客户正在咨询，尚无采购需求。')]:
        conv = s.repo.get('conversation', cid)
        s.repo.put('conversation_summary', cid, {'conversation_id': cid, 'seller_id': conv['seller_id'], 'buyer_id': conv['buyer_id'], 'body': text, 'revision': 1, 'updated_at': 1})
    noah = s.repo.get('conversation', 'demo-noah')
    noah['case_id'] = None
    s.repo.put('conversation', 'demo-noah', noah)
    sync = getattr(s, '_sync_requirement_product', None)
    if sync:
        with s._lock:
            sync(case)
            binding = s.repo.get('requirement_product_binding', case['case_id'])
            local_id = binding['local_product_id']
            png = io.BytesIO()
            Image.new('RGB', (96, 72), '#7da89a').save(png, format='PNG')
            asset = decode_image(png.getvalue(), 'image/png')
            s.repo.put('image_asset', asset['sha256'], asset)
            s.repo.put('product_main_image', local_id, {'seller_id': s.seller_id, 'local_product_id': local_id, 'product_id': '123456789', 'status': 'ready', 'asset_sha256': asset['sha256'], 'updated_at': 1, 'message': '平台图组首图，仅供展示和找货参考', 'display_only': True, 'selection_rule': 'lowest_image_index', 'primary_verified': False})
    print(json.dumps({'case_id': case['case_id'], 'product_seed_available': bool(sync)}))
finally:
    s.close()
`;
let app, page;
async function until(check, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out: ${description}`);
}
async function closeWindow() {
  const owned = app.process();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => owned.exitCode !== null, 'normal window close');
  app = null;
  report.window_closed = true;
}
async function screenshot(name) {
  await page.screenshot({ path: path.join(output, `${name}.png`), animations: 'disabled' });
}
try {
  const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services', 'reception', 'src'), data], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
  assert.equal(prepared.status, 0, `Demo seed failed: ${prepared.error?.message || prepared.stderr}`);
  const fixture = JSON.parse(prepared.stdout.trim());
  if (packagedExecutable) delete env.RECEPTION_PYTHON;
  const options = { executablePath: packagedExecutable || path.join(root, 'node_modules', 'electron', 'dist', 'electron.exe'), args: [...(packagedExecutable ? [] : [path.join(root, 'artifacts', 'app', 'main', 'index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000 };
  const launch = async () => {
    app = await electron.launch(options);
    report.window_closed = false;
    const packaged = await app.evaluate(({ app }) => app.isPackaged);
    assert.equal(packaged, Boolean(packagedExecutable), 'Unexpected application distribution');
    page = await app.firstWindow();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
    const snapshot = await page.evaluate(() => window.reception.invoke('get_snapshot', {}));
    assert.equal(snapshot.app.mode, 'demo');
    assert.equal(snapshot.app.version, JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version);
  };
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const choose = async name => {
    await page.getByRole('button', { name: new RegExp(name) }).click();
    await page.getByRole('main', { name: `${name} 的客户接待` }).waitFor();
  };
  await launch();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 940));
  await choose('Emma Wilson');
  const panel = page.locator('.requirements-column .requirements');
  await page.getByLabel('会话摘要').getByText('合成摘要：客户需要蓝色棉帽。', { exact: true }).waitFor();
  await panel.getByText('500件', { exact: true }).waitFor();
  await panel.getByRole('button', { name: '放大查看商品主图' }).waitFor();
  const demand = await invoke('get_conversation', { conversation_id: 'demo-emma' });
  assert.equal(demand.case.requirements.product_url.value, 'https://www.alibaba.com/product-detail/_123456789.html');
  assert.deepEqual(demand.case.requirements.product_images.value, [], 'Main image must not be treated as a customer image');
  const mainImage = await invoke('get_product_main_image', { local_product_id: demand.case.product_main_image.local_product_id });
  assert.equal(mainImage.status, 'ready');
  assert.match(mainImage.data_url, /^data:image\/(?:png|jpeg);base64,/);
  assert.equal(Object.hasOwn(mainImage, 'source_url'), false, 'Local image RPC must not expose a source URL');
  assert.equal(Object.hasOwn(demand.case.product_main_image, 'source_url'), false, 'Case image metadata must not expose a source URL');
  assert.equal(await panel.locator('img[src^="https:"]').count(), 0, 'Remote image rendered in demand panel');
  for (const value of ['棉帽', '蓝色棉质', '法国']) assert.ok(await panel.getByText(value, { exact: true }).count(), `Missing requirement ${value}`);
  assert.equal(await panel.getByText(/待核实|存在冲突|待客户确认/).count(), 0);
  await screenshot('summary-demand');
  report.completed.push('summary-and-demand');
  await choose('Noah Brown');
  const noDemand = await invoke('get_conversation', { conversation_id: 'demo-noah' });
  assert.equal(noDemand.case, null, 'No-demand fixture must have no current case');
  await page.getByLabel('会话摘要').getByText('合成摘要：客户正在咨询，尚无采购需求。', { exact: true }).waitFor();
  await panel.getByText('当前没有采购需求', { exact: true }).waitFor();
  assert.equal(await page.getByText('当前客户事项或归属不可用').count(), 0, 'A missing demand must not be shown as an ownership failure');
  assert.equal(await page.getByText('查看暂未回复的原因').count(), 0, 'A missing demand must not create a blocker');
  assert.equal(await page.getByRole('textbox', { name: '回复正文' }).isEnabled(), true, 'Manual composer must remain available without a case');
  await screenshot('summary-no-demand');
  report.completed.push('summary-without-current-demand');
  await choose('Emma Wilson');
  const note = page.getByLabel('业务补充内容');
  await note.fill('合成补充：测试重开保存。');
  await until(async () => (await invoke('get_conversation', { conversation_id: 'demo-emma' })).supplement?.body === '合成补充：测试重开保存。', 'supplement saved');
  assert.equal(fixture.product_seed_available, true, 'Product synchronization is not implemented');
  const products = await invoke('list_products');
  assert.ok(products.items.some(item => item.product_id === '123456789'), 'Synthetic demand product missing');
  await page.getByRole('button', { name: '产品库', exact: true }).click();
  await page.getByRole('button', { name: /棉帽/ }).click();
  await page.getByRole('heading', { name: '供货方案' }).waitFor();
  await page.getByRole('button', { name: '放大查看商品主图' }).waitFor();
  await screenshot('product');
  report.completed.push('product-navigation');
  await page.getByRole('button', { name: '供应商', exact: true }).click();
  await page.getByRole('button', { name: '新增供应商', exact: true }).click();
  await page.getByLabel('名称', { exact: true }).fill('合成供应商');
  await page.getByLabel('平台', { exact: true }).fill('alibaba');
  await page.getByRole('button', { name: '保存供应商', exact: true }).click();
  await page.getByRole('button', { name: /合成供应商/ }).click();
  await page.getByRole('heading', { name: '合成供应商', exact: true }).waitFor();
  await page.getByRole('button', { name: '编辑供应商', exact: true }).click();
  await page.getByLabel('备注', { exact: true }).fill('合成备注');
  await page.getByRole('button', { name: '保存供应商', exact: true }).click();
  await page.getByText('备注：合成备注').waitFor();
  await screenshot('supplier');
  report.completed.push('supplier-create-edit-and-view');
  await page.getByRole('button', { name: '产品库', exact: true }).click();
  await page.getByRole('button', { name: /棉帽/ }).click();
  await page.getByRole('button', { name: '新增供货方案', exact: true }).click();
  await page.getByLabel('规格', { exact: true }).fill('蓝色棉质');
  await page.getByLabel('起订量', { exact: true }).fill('100');
  await page.getByLabel('单价', { exact: true }).fill('2.50');
  await page.getByLabel('币种', { exact: true }).fill('USD');
  await page.getByLabel('允许用于回复').check();
  await page.getByRole('button', { name: '保存供货方案', exact: true }).click();
  const offer = page.locator('.profile-card .handoff-card').filter({ hasText: '合成供应商' });
  await offer.waitFor();
  await offer.getByRole('button', { name: '设为默认' }).click();
  await offer.getByText(/默认方案/).waitFor();
  await offer.getByRole('button', { name: '选用于当前事项' }).click();
  await offer.getByText(/当前事项已选/).waitFor();
  await screenshot('product-offer-selected');
  report.completed.push('offer-create-default-and-case-selection');
  await page.getByRole('button', { name: '采购中心', exact: true }).click();
  await page.getByRole('heading', { name: '采购任务' }).waitFor();
  await screenshot('procurement');
  report.completed.push('procurement-navigation');
  await page.getByRole('button', { name: '客户接待', exact: true }).click();
  await choose('Emma Wilson');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(900, 800));
  const openDemand = page.getByRole('button', { name: '采购需求', exact: true });
  await openDemand.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: '采购需求' });
  const drawer = page.locator('.requirements-drawer-content .requirements');
  await drawer.waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, 'Window overflows horizontally');
  assert.equal(await drawer.evaluate(element => element.scrollWidth > element.clientWidth), false, 'Demand drawer overflows horizontally');
  await page.keyboard.press('Tab');
  assert.equal(await dialog.evaluate(element => element.contains(document.activeElement)), true, 'Keyboard focus escaped the drawer');
  await screenshot('narrow');
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(await openDemand.evaluate(element => element === document.activeElement), true, 'Focus did not return to drawer trigger');
  report.completed.push('narrow-demand-drawer-keyboard');
  await closeWindow();
  await launch();
  await choose('Emma Wilson');
  assert.equal((await invoke('get_conversation', { conversation_id: 'demo-emma' })).supplement.body, '合成补充：测试重开保存。');
  assert.ok((await invoke('list_suppliers')).items.some(item => item.name === '合成供应商' && item.notes === '合成备注'));
  const savedCase = await invoke('get_conversation', { conversation_id: 'demo-emma' });
  assert.ok(savedCase.case.selected_offer_id, 'Selected offer was not preserved');
  const savedProduct = await invoke('get_product', { local_product_id: savedCase.case.local_product_id });
  assert.equal(savedProduct.product.default_offer_id, savedCase.case.selected_offer_id, 'Default and selected offer should persist');
  assert.ok(savedProduct.offers.some(item => item.offer_id === savedCase.case.selected_offer_id && item.unit_price === '2.50'));
  await page.getByLabel('业务补充内容').waitFor();
  assert.equal(await page.getByLabel('业务补充内容').inputValue(), '合成补充：测试重开保存。');
  await screenshot('reopen');
  report.completed.push('reopen-persistence');
  await closeWindow();
  assert.deepEqual(report.errors, []);
  report.status = 'passed';
} catch (error) {
  report.failure = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
  if (page && !page.isClosed()) {
    await screenshot('failure').catch(() => {});
    await writeFile(path.join(output, 'failure.txt'), (await page.locator('body').innerText()).slice(0, 16000)).catch(() => {});
  }
} finally {
  if (app) {
    try { await closeWindow(); } catch (error) { report.errors.push(`Close failed: ${error.message}`); await app.close().catch(() => {}); }
  }
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
