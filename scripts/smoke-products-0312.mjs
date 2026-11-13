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
const output = path.join(root, 'artifacts', 'verification', `products-0312-${label}`);
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
import base64, hashlib, io, json, sys
from pathlib import Path
from PIL import Image
sys.path.insert(0, sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.procurement import merge_fact
from reception_service.repository import encoded
from reception_service.photos import photo_version
from reception_service.business_progress import apply_progress
s = ReceptionService(Path(sys.argv[2]), mode='demo')
try:
    s.inject_demo('demo-emma', 'I need 500 pcs of hats.')
    first = s._latest_buyer('demo-emma')
    s.inject_demo('demo-emma', '800 pcs of hats, please.')
    second = s._latest_buyer('demo-emma')
    first['message_id'] = 'synthetic-photo-0311'
    second['message_id'] = 'synthetic-product-0311'
    first['text'] = '[image]'
    first['attachments'] = [{'kind': 'image', 'url': 'https://example.invalid/demo.png'}]
    s.repo.put('message', encoded(['demo-emma', first['message_id']]), first)
    second['text'] += ' https://www.alibaba.com/product-detail/demo_123.html'
    second['attachments'] = [{'kind': 'native-content', 'content': {'cardType': 'product', 'fields': {
        'imageUrl': 'https://img.alicdn.com/synthetic-main.png',
        'skus': [{'skuLabel': '红色', 'imageUrl': 'https://img.alicdn.com/synthetic-sku.png'}]}}}]
    s.repo.put('message', encoded(['demo-emma', second['message_id']]), second)
    case = s.get_conversation('demo-emma')['case']
    case['facts']['product'] = {'value': 'hats', 'status': 'confirmed', 'evidence_ids': [first['message_id']]}
    case['category_label'] = '帽子'
    case['facts']['quantity'] = merge_fact({'value':'500 pcs','status':'confirmed','evidence_ids':[first['message_id']]}, {'value':'800 pcs','status':'confirmed','evidence_ids':[second['message_id']]}, {first['message_id']:first, second['message_id']:second})
    s.repo.put('case',case['case_id'],case)
    def asset(colour):
        buf = io.BytesIO()
        Image.new('RGB', (48, 48), colour).save(buf, format='PNG')
        raw = buf.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        data = base64.b64encode(raw).decode('ascii')
        s.repo.put('image_asset', digest, {'sha256': digest, 'preview_data_url': 'data:image/png;base64,' + data, 'original_b64': data})
        return digest
    owner = {key: case[key] for key in ('seller_id', 'buyer_id', 'conversation_id')}
    photo_sha = asset((35, 135, 90))
    s.repo.put('message_image', encoded(['demo-emma', first['message_id']]), {**owner, 'message_id': first['message_id'], 'source_version': photo_version(first), 'status': 'ready', 'sha256': photo_sha, 'message': '已加载'})
    ref = {'catalog_product_id':'synthetic-catalog-123', 'platform':'alibaba', 'product_id':'123', 'observed_product_id':'123', 'product_url':'https://www.alibaba.com/product-detail/demo_123.html', 'original_title':'Synthetic hat', 'product_images':[photo_sha], 'sku':{'color':'red'}, 'source_message_ids':[second['message_id']], 'inquiry_ids':[], 'suitability':'applicable', 'suitability_reason':'', 'lookup':{'status':'completed','reason':'','queried_at':1}}
    case['product_ref'] = ref
    s.repo.put('case',case['case_id'],case)
    other = s.get_conversation('demo-noah')['case']
    other['product_ref'] = {**ref, 'product_images':[], 'source_message_ids':[]}
    other['facts']['quantity'] = {'value':'100 pcs','status':'confirmed','evidence_ids':[]}
    s.repo.put('case',other['case_id'],other)
    initial_media = s.get_requirement_media(case['case_id'])
    ref['product_images'] = [item['media_id'] for item in initial_media['items'] if item['kind'] == 'customer_image']
    case['product_ref'] = ref
    s.repo.put('case', case['case_id'], case)
    for item in initial_media['items']:
        if item['kind'] in ('product_image', 'sku_image'):
            colour = (225, 150, 70) if item['kind'] == 'product_image' else (205, 65, 95)
            s.repo.put('requirement_media_cache', encoded([case['case_id'], item['media_id']]), {**owner, 'source_version': item['source_version'], 'status': 'ready', 'sha256': asset(colour)})
    def progress(cid, state, topic):
        conversation = s.get_conversation(cid)
        current_case = conversation['case']
        source = s._latest_buyer(cid)
        if state == 'waiting_customer':
            source = {**source, 'message_id': 'synthetic-seller-progress', 'role': 'seller', 'own': True, 'text': 'Synthetic reply sent earlier', 'timestamp_ms': source['timestamp_ms'] + 1, 'attachments': []}
            s.repo.put('message', encoded([cid, source['message_id']]), source)
        owner = {key: current_case[key] for key in ('seller_id', 'buyer_id', 'conversation_id')}
        apply_progress(s.repo, owner, current_case, {'recent_messages': [source]}, {'issues': [{'topic': topic, 'summary': '合成状态展示', 'state': state, 'evidence_ids': [source['message_id']]}]})
    progress('demo-emma', 'needs_input', '运费')
    progress('demo-noah', 'needs_reply', '报价')
    progress('demo-sofia', 'waiting_customer', '规格')
    print(json.dumps({'case_id':case['case_id'],'first':first['message_id'],'second':second['message_id']}, ensure_ascii=False))
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
  for (const [name, state, label] of [['Noah Brown', 'needs_reply', '待回复'], ['Sofia Martinez', 'waiting_customer', '待客户回复'], ['Emma Wilson', 'needs_input', '待你补充：运费']]) {
    await page.getByRole('button', { name: new RegExp(name) }).click();
    await page.getByRole('main', { name: name + ' 的客户接待' }).waitFor();
    await until(async () => (await page.getByLabel('此客户接待状态').innerText()).includes(label), 'fixture progress ' + state);
    await page.screenshot({ path: path.join(output, 'progress-' + state + '.png'), animations: 'disabled' });
  }
  report.completed.push('synthetic-business-progress-labels');
  const panel = page.locator('.requirements-column .requirements');
  assert.equal(await panel.getByText('摘要', { exact: true }).count(), 1);
  assert.equal(await panel.getByText('客户需求', { exact: true }).count(), 1);
  for (const text of ['本轮变化', '商家条件', '回复依据', '事项与历史']) assert.equal(await panel.getByText(text, { exact: true }).count(), 0);
  assert.equal(await page.getByText(/待你协助与跟进/).count(), 0);
  const note = page.getByLabel('业务补充内容');
  await note.fill('合成测试：到法国运费20美元');
  await until(async () => (await detail()).supplement?.body === '合成测试：到法国运费20美元', 'supplement saved');
  const progress = (await detail()).conversation.business_progress;
  assert.equal(progress?.state, 'pending_analysis', 'Saving a supplement invalidates the previous business analysis');
  await until(async () => (await page.getByLabel('此客户接待状态').innerText()).includes('状态待更新'), 'business progress visible');
  await panel.getByText('商品 ID：123').waitFor();
  await panel.getByRole('button', { name: '复制商品链接' }).waitFor();
  for (const text of ['整理与复核需求', '保存品类', '应用候选', '本次应用的资料与规则']) assert.equal(await page.getByText(text, { exact: true }).count(), 0);
  assert.equal(await panel.locator('img[src^="http"]').count(), 0);
  await until(async () => await panel.locator('.requirement-media img').count() > 0, 'local product image');
  await page.getByLabel('回复正文').fill('Demo draft preserved');
  await until(async () => (await detail()).chat_draft?.body === 'Demo draft preserved', 'draft saved');
  await page.screenshot({ path: path.join(output, 'wide.png'), animations: 'disabled' });
  await panel.locator('.requirement-media img').first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'product.png'), animations: 'disabled' });
  const emma = await detail();
  const noah = await invoke('get_conversation', { conversation_id: 'demo-noah' });
  assert.equal(emma.case.product_ref.catalog_product_id, noah.case.product_ref.catalog_product_id);
  assert.notEqual(emma.case.facts.quantity.value, noah.case.facts.quantity.value);
  report.completed.push('synthetic-product-fields-images-independent-quantities');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(900, 800));
  await page.getByRole('button', { name: '采购需求', exact: true }).click();
  const drawer = page.locator('.requirements-drawer-content .requirements');
  await drawer.waitFor();
  assert.equal(await drawer.evaluate(element => element.scrollWidth > element.clientWidth), false);
  await page.screenshot({ path: path.join(output, 'drawer.png'), animations: 'disabled' });
  await closeWindow();
  await launch();
  const reopened = await detail();
  assert.equal(reopened.supplement.body, '合成测试：到法国运费20美元');
  assert.equal(reopened.chat_draft.body, 'Demo draft preserved');
  assert.equal(reopened.case.product_ref.product_id, '123');
  report.completed.push('narrow-drawer-reopen');
  await page.screenshot({ path: path.join(output, 'reopened.png'), animations: 'disabled' });
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
