import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
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
const output = path.join(root, 'artifacts/verification', 'workbench-0315-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0315-demo-'));
const data = path.join(temporary, 'data');
const fixtures = path.join(temporary, 'fixtures'); await mkdir(fixtures);
const png = path.join(fixtures, 'synthetic-image.png');
const txt = path.join(fixtures, 'synthetic-terms.txt');
const nativeFile = path.join(fixtures, 'native-copy.txt');
await writeFile(txt, 'Synthetic terms: price depends on quantity; shipping is not confirmed.');
await writeFile(nativeFile, 'Synthetic native clipboard file only.');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared-settings');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
if (!executable) env.RECEPTION_PYTHON = python;
const seed = `
import io,json,sys
from pathlib import Path
from PIL import Image,ImageDraw
sys.path.insert(0,sys.argv[1])
from reception_service.image_assets import decode_image
from reception_service.service import ReceptionService
s=ReceptionService(Path(sys.argv[2]),mode='demo')
try:
 s.inject_demo('demo-emma','Synthetic inquiry: blue cotton hats, quantity and destination to confirm.')
 msg=s._latest_buyer('demo-emma'); case=s.get_conversation('demo-emma')['case']
 values={'product':'轻量棉帽','product_id':'123456789','product_url':None,'product_images':[],'quantity':None,'specifications':'蓝色棉质；可刺绣','customization':None,'destination':'法国','delivery':None,'budget':None,'packaging':None,'certifications':None,'contact':'Emma Wilson（买家自述）'}
 case['requirements']={key:{'value':value,'evidence_ids':[msg['message_id']]} for key,value in values.items()}; case['requirements_format']='ai-v1'; s.repo.put('case',case['case_id'],case)
 s._sync_requirement_product(case); binding=s.repo.get('requirement_product_binding',case['case_id']); local_id=binding['local_product_id']
 images=[]
 for index,color in enumerate(['#6685a6','#7893af','#8c9f87','#ac8d72','#63747e']):
  image=Image.new('RGB',(240,180),'#f0f2eb'); draw=ImageDraw.Draw(image); draw.ellipse((45,105,209,147),fill='#d9dfd3'); draw.pieslice((52,37,179,158),180,360,fill=color); draw.rounded_rectangle((52,89,180,121),radius=8,fill=color); draw.ellipse((112,104,214,129),fill=color); raw=io.BytesIO(); image.save(raw,format='PNG'); asset=decode_image(raw.getvalue(),'image/png'); s.repo.put('image_asset',asset['sha256'],asset); images.append({'status':'ready','asset_sha256':asset['sha256']})
  if index==0: image.save(sys.argv[3],format='PNG')
 s.repo.put('product_main_image',local_id,{'seller_id':s.seller_id,'local_product_id':local_id,'product_id':'123456789','status':'ready','image_group_version':1,'images':images,'asset_sha256':images[0]['asset_sha256'],'updated_at':1,'message':'平台图组首图，仅供展示和找货参考','display_only':True,'selection_rule':'lowest_image_index','primary_verified':False})
 for cid,body in [('demo-emma','客户咨询蓝色棉帽及刺绣，数量和交期尚未明确。'),('demo-noah','客户咨询目录，当前没有采购事项。')]:
  c=s.repo.get('conversation',cid); s.repo.put('conversation_summary',cid,{'conversation_id':cid,'seller_id':c['seller_id'],'buyer_id':c['buyer_id'],'body':body,'revision':1,'updated_at':1})
 noah=s.repo.get('conversation','demo-noah'); noah['case_id']=None; s.repo.put('conversation','demo-noah',noah)
 print(json.dumps({'local_product_id':local_id,'case_id':case['case_id']}))
finally:s.close()
`;
const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services/reception/src'), data, png], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
assert.equal(prepared.status, 0, 'Synthetic seed failed: ' + prepared.stderr);
const fixture = JSON.parse(prepared.stdout.trim());
const imageB64 = (await readFile(png)).toString('base64');
const report = { status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version, packaged: Boolean(executable), mode: 'demo', synthetic_only: true, business_ipc_mocked: false, native_picker_and_clipboard_sources_mocked: true, production_clipboard_accessed: false, real_ai_calls: 0, real_customer_sends: 0, production_data_accessed: false, completed: [], screenshots: [], errors: [], window_closed: false };
let app, page;
async function until(check, message, timeout = 30000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('Timed out: ' + message); }
const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000 };
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
const count = async () => (await detail()).supplement.attachments.length;
const choose = async name => { await page.getByRole('button', { name: new RegExp(name) }).click(); await page.getByRole('main', { name: name + ' 的客户接待' }).waitFor(); };
async function resize(width, height) { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size.width, size.height), { width, height }); }
async function shot(name) { await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); }
async function launch() {
 app = await electron.launch(options); report.window_closed = false;
 assert.equal(await app.evaluate(({ app }) => app.isPackaged), Boolean(executable));
 page = await app.firstWindow(); page.on('pageerror', error => report.errors.push(error.message));
 await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
 await resize(1440, 960); const snapshot = await invoke('get_snapshot'); assert.equal(snapshot.app.mode, 'demo'); assert.equal(snapshot.app.version, report.version);
}
async function close() { const process = app.process(); await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close()); await until(() => process.exitCode !== null, 'normal close'); app = null; report.window_closed = true; }
async function browserFile(mode, name, type, b64) {
 await page.getByLabel('业务补充内容', { exact: true }).evaluate((node, args) => {
  const transfer = new DataTransfer(); transfer.items.add(new File([Uint8Array.from(atob(args.b64), char => char.charCodeAt(0))], args.name, { type: args.type }));
  if (args.mode === 'drop') node.closest('.supplement-composer').dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }));
  else node.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer }));
 }, { mode, name, type, b64 });
}
async function nativePaste(paths) {
 // Replace only OS clipboard sources; all renderer events, host IPC, file reading and backend writes are real.
 await app.evaluate(async ({ clipboard }, args) => {
  const require = (await import('node:module')).createRequire(process.cwd() + '/clipboard-probe.cjs');
  const child = require('node:child_process');
  if (!globalThis.__receptionOriginalExecFile) {
   globalThis.__receptionOriginalExecFile = child.execFile;
   child.execFile = function (executable, command, options, callback) {
    if (Array.isArray(command) && command.some(value => typeof value === 'string' && value.includes('GetFileDropList'))) { queueMicrotask(() => callback(null, JSON.stringify(globalThis.__receptionSyntheticClipboardPaths), '')); return { kill() {} }; }
    return globalThis.__receptionOriginalExecFile.apply(this, arguments);
   };
  }
  globalThis.__receptionSyntheticClipboardPaths = args.paths;
  const { Blob } = await import('node:buffer');
  clipboard.read = async () => [{ types: ['image/png'], getType: async () => new Blob([Buffer.from(args.imageB64, 'base64')], { type: 'image/png' }) }];
 }, { paths, imageB64 });
 await page.getByLabel('业务补充内容', { exact: true }).evaluate(node => node.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: new DataTransfer() })));
}
try {
 await launch(); await choose('Emma Wilson');
 await page.getByRole('button', { name: '放大查看商品图片 5', exact: true }).waitFor();
 assert.equal(await page.locator('.product-image-gallery img').count(), 5);
 assert.equal(await page.locator('.history-bar,.translation-toggle,.suggestion-banner,.composer-heading,.composer-source').count(), 0);
 assert.equal(await page.getByText('平台图组首图，仅供展示和找货参考', { exact: true }).count(), 0);
 const product = await invoke('get_product_main_image', { local_product_id: fixture.local_product_id }); assert.equal(product.images.length, 5);
 assert.deepEqual((await detail()).case.requirements.product_images.value, []);
 await page.getByRole('button', { name: '放大查看商品图片 1', exact: true }).click(); await page.getByRole('button', { name: '下一张商品图片' }).click(); await page.getByRole('dialog', { name: '商品图片' }).getByText('2 / 5', { exact: true }).waitFor(); await page.keyboard.press('Escape');
 await resize(1920, 1080); await shot('layout-a-wide'); await resize(1440, 960);
 report.completed.push('layout-a-no-duplicate-row-five-local-images-and-zoom');
 await page.getByRole('button', { name: '资料与政策', exact: true }).click();
 await page.getByLabel('类别', { exact: true }).last().selectOption('reception');
 await page.getByRole('textbox', { name: '资料名称', exact: true }).fill('0.3.15 合成接待规则');
 await page.getByRole('textbox', { name: '资料内容', exact: true }).fill('报价须依据明确数量，不擅自承诺寄样。');
 await page.getByLabel('用途', { exact: true }).selectOption('internal_guidance');
 await page.getByRole('checkbox', { name: /已审核采用/ }).check(); await page.getByRole('checkbox', { name: /启用资料/ }).check();
 await page.getByRole('button', { name: '保存资料', exact: true }).click();
 await until(async () => (await invoke('get_snapshot')).policies.some(row => row.title === '0.3.15 合成接待规则' && row.priority === 0), 'policy saved through real bridge');
 await page.getByText('已保存。引用旧版本的回复需要重新核对。', { exact: true }).waitFor(); await shot('policy-saved');
 await page.getByRole('button', { name: '客户接待', exact: true }).click(); await choose('Emma Wilson');
 const note = '合成业务补充：数量和运费需明确后报价。'; await page.getByLabel('业务补充内容', { exact: true }).fill(note);
 await until(async () => (await detail()).supplement.body === note, 'note saved');
 await app.evaluate(({ dialog }, files) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: files }); }, [png, txt]);
 await page.getByRole('button', { name: '添加附件', exact: true }).click(); await until(async () => await count() === 2, 'multi-picker upload');
 await browserFile('drop', 'dragged.txt', 'text/plain', Buffer.from('Synthetic dropped terms.').toString('base64'));
 const laterNote = note + ' 保存期间继续输入。'; await page.getByLabel('业务补充内容', { exact: true }).fill(laterNote);
 await until(async () => await count() === 3 && (await detail()).supplement.body === laterNote, 'drop preserves concurrent note');
 await browserFile('paste', 'pasted-image.png', 'image/png', imageB64); await until(async () => await count() === 4, 'paste image upload');
 await nativePaste([nativeFile]); await until(async () => await count() === 5, 'native clipboard file');
 await browserFile('paste', 'over-limit.txt', 'text/plain', Buffer.from('Synthetic over-limit.').toString('base64'));
 await page.getByRole('alert').filter({ hasText: '最多添加 5 个附件' }).waitFor(); assert.equal(await count(), 5);
 await page.getByRole('button', { name: '移除 native-copy.txt', exact: true }).click(); await until(async () => await count() === 4, 'remove native copy');
 await nativePaste([]); await until(async () => await count() === 5, 'native screenshot clipboard');
 await page.getByRole('button', { name: '移除 剪贴板图片.png', exact: true }).click(); await until(async () => await count() === 4, 'remove clipboard image');
 await shot('attachments-and-note'); report.completed.push('multi-select-drop-browser-paste-native-file-and-image-fixtures-capacity-and-concurrent-note');
 await choose('Noah Brown'); assert.equal(await page.locator('.supplement-files li').count(), 0); await choose('Emma Wilson');
 await page.getByRole('button', { name: 'synthetic-terms.txt', exact: true }).click(); await page.getByRole('dialog', { name: 'synthetic-terms.txt', exact: true }).getByText('Synthetic terms: price depends on quantity; shipping is not confirmed.', { exact: true }).waitFor(); await page.keyboard.press('Escape');
 const translation = page.getByRole('checkbox', { name: '自动中文翻译', exact: true }); await translation.check();
 await invoke('check_ai'); await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'demo AI ready');
 await page.getByRole('button', { name: '生成回复建议', exact: true }).click();
 await until(async () => !!(await detail()).manual_suggestion?.body, 'suggestion persisted');
 await page.getByRole('button', { name: '替换到输入框', exact: true }).waitFor(); assert.equal(await page.getByLabel('回复正文', { exact: true }).inputValue(), ''); assert.equal((await detail()).conversation.human_owned, false);
 await shot('suggestion-pending'); const firstSuggestion = (await detail()).manual_suggestion.suggestion_id;
 await close(); await launch(); await choose('Emma Wilson'); await page.getByRole('region', { name: '待处理回复建议', exact: true }).waitFor();
 assert.equal((await detail()).supplement.body, laterNote); assert.equal(await count(), 4); assert.equal((await invoke('get_snapshot')).policies.some(row => row.title === '0.3.15 合成接待规则'), true);
 await page.getByRole('button', { name: '弃用', exact: true }).click(); assert.equal(await page.getByRole('region', { name: '待处理回复建议', exact: true }).count(), 0);
 await close(); await launch(); await choose('Emma Wilson'); assert.equal(await page.getByRole('region', { name: '待处理回复建议', exact: true }).count(), 0);
 await invoke('check_ai'); await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'reopened demo AI ready');
 await page.getByRole('button', { name: '生成回复建议', exact: true }).click(); await until(async () => (await detail()).manual_suggestion?.suggestion_id !== firstSuggestion, 'new suggestion');
 const replacement = (await detail()).manual_suggestion.body; await page.getByRole('button', { name: '替换到输入框', exact: true }).click();
 assert.equal(await page.getByLabel('回复正文', { exact: true }).inputValue(), replacement); assert.equal(await page.getByRole('region', { name: '待处理回复建议', exact: true }).count(), 0);
 await until(async () => (await detail()).chat_draft?.body === replacement, 'adopted reply saved'); assert.equal((await invoke('get_snapshot')).outbox.some(row => ['send','send_chat'].includes(row.kind)), false);
 await page.getByRole('button', { name: '润色当前回复', exact: true }).click(); await until(async () => (await detail()).manual_suggestion?.mode === 'polish', 'polish result');
 await page.getByRole('button', { name: '弃用', exact: true }).click(); assert.equal(await page.getByLabel('回复正文', { exact: true }).inputValue(), replacement);
 await shot('adopted-and-polish-dismissed'); report.completed.push('pending-suggestion-survives-reopen-dismissal-persists-replace-does-not-send-polish-keeps-original');
 await resize(1100, 820); await page.getByRole('button', { name: '采购需求', exact: true }).click(); await page.getByRole('dialog', { name: '采购需求', exact: true }).getByLabel('业务补充内容', { exact: true }).waitFor(); await shot('narrow-drawer'); await page.keyboard.press('Escape');
 await resize(1440, 960); await shot('reopened-layout'); report.completed.push('customer-isolation-attachment-preview-policy-reopen-and-narrow-drawer');
 assert.deepEqual(report.errors, []); await close(); report.status = 'passed';
} catch (error) {
 report.error = error.stack; process.exitCode = 1;
 if (page) { try { await shot('failure'); await writeFile(path.join(output, 'failure.txt'), (await page.locator('body').innerText()).slice(0, 16000)); } catch {} }
} finally { if (app) await app.close().catch(() => {}); await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' }); }
console.log(JSON.stringify(report));
