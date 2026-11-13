import {_electron as electron} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir, mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const option = name => {const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1];};
const label = option('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const output = path.join(root, 'artifacts/verification', 'repairs-0317-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0317-repairs-'));
const data = path.join(temporary, 'data');
const env = {...process.env};
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared-settings');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
env.RECEPTION_PYTHON = python;
const seed = `
import io,json,sys
from datetime import datetime
from pathlib import Path
from PIL import Image,ImageDraw
sys.path.insert(0,sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.image_assets import decode_image
s=ReceptionService(Path(sys.argv[2]),mode='demo')
try:
 s._requirement_media_stopped=True
 s.inject_demo('demo-emma','Synthetic product 123456789: 12 canvas tote bags with logo.')
 msg=s._latest_buyer('demo-emma'); case=s.get_conversation('demo-emma')['case']
 with s._lock,s.repo.transaction():
  msg['timestamp_ms']=int(datetime(2026,11,12,14,5,5).timestamp()*1000)
  from reception_service.repository import encoded
  s.repo.put('message',encoded(['demo-emma',msg['message_id']]),msg)
  values={'product':'合成帆布袋','product_id':'123456789','product_url':None,'product_images':[],'quantity':'12个','specifications':None,'customization':'Logo','destination':None,'delivery':None,'budget':None,'packaging':None,'certifications':None,'contact':None}
  case['requirements']={key:{'value':value,'evidence_ids':[msg['message_id']] if value else []} for key,value in values.items()};case['requirements_format']='ai-v1';case['requirements_revision']=1
  s.repo.put('case',case['case_id'],case);s._sync_requirement_product(case)
  binding=s.repo.get('requirement_product_binding',case['case_id']);identifier=binding['local_product_id']
  image=Image.new('RGBA',(640,480),(34,114,86,230));ImageDraw.Draw(image).text((30,30),'SYNTHETIC ORIGINAL / 0.3.17',fill='white')
  raw=io.BytesIO();image.save(raw,'PNG');asset=decode_image(raw.getvalue(),'image/png');s.repo.put('image_asset',asset['sha256'],asset)
  s.repo.put('product_main_image',identifier,{'seller_id':s.seller_id,'local_product_id':identifier,'product_id':'123456789','status':'ready','image_group_version':1,'asset_sha256':asset['sha256'],'images':[{'status':'ready','asset_sha256':asset['sha256']}],'updated_at':100000,'display_only':True})
 print(json.dumps({'case_id':case['case_id'],'local_product_id':identifier,'old_sha256':asset['sha256']}))
finally:s.close()
`;
const prepared = spawnSync(python, ['-I', '-X', 'utf8', '-c', seed, path.join(root, 'services/reception/src'), data], {cwd: root, env, encoding: 'utf8', timeout: 30000});
assert.equal(prepared.status, 0, prepared.stderr);
const fixture = JSON.parse(prepared.stdout.trim());
const report = {status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version, actual_window: true, synthetic_only: true, real_ai_calls: 0, real_customer_sends: 0, production_data_accessed: false, clipboard_readback_scope: 'Only synthetic product ID, derived URL and original PNG written immediately by this test', native_menu_selection_mocked: true, clipboard_write_sink_mocked: false, completed: [], screenshots: [], errors: [], window_closed: false};
let application;
const until = async (check, message, timeout = 30000) => {const end = Date.now() + timeout; while (Date.now() < end) {if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100));} throw new Error('Timed out: ' + message);};
try {
 application = await electron.launch({executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [path.join(root, 'artifacts/app/main/index.js'), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000});
 const page = await application.firstWindow();
 page.on('pageerror', error => report.errors.push(error.message));
 const invoke = (method, params = {}) => page.evaluate(({method, params}) => window.reception.invoke(method, params), {method, params});
 const shot = async name => {await page.screenshot({path: path.join(output, name + '.png'), animations: 'disabled'}); report.screenshots.push(name + '.png');};
 await page.getByRole('textbox', {name: '回复正文', exact: true}).waitFor({timeout: 45000});
 await page.getByRole('button', {name: /Emma Wilson/}).click();
 await page.getByRole('main', {name: 'Emma Wilson 的客户接待'}).waitFor();
 await application.evaluate(({BrowserWindow}) => {BrowserWindow.getAllWindows()[0].setContentSize(1440, 960);BrowserWindow.getAllWindows()[0].focus();});
 await page.getByRole('button', {name: '放大查看商品图片 1', exact: true}).waitFor();
 await page.locator('.message-meta time').filter({hasText: '2026-11-12 14:05:05'}).waitFor();
 assert.equal(await page.locator('.date-divider').count(), 0);
 await shot('complete-message-time');
 report.completed.push('visible-full-local-date-time-to-seconds-without-date-separators');
 await page.getByRole('button', {name: '复制产品ID', exact: true}).click();
 await until(async () => await application.evaluate(async ({clipboard}) => clipboard.readText()) === '123456789', 'native product ID clipboard readback');
 const url = 'https://www.alibaba.com/product-detail/_123456789.html';
 await page.getByRole('button', {name: url, exact: true}).click();
 await until(async () => await application.evaluate(async ({clipboard}) => clipboard.readText()) === url, 'native product URL clipboard readback');
 report.completed.push('actual-product-id-and-link-buttons-write-system-text-clipboard');
 await shot('product-text-copy');
 await application.evaluate(({Menu}) => {
  const build = Menu.buildFromTemplate;
  Menu.buildFromTemplate = function(template) {if (template.length === 1 && template[0].label === '复制图片') return {popup(options) {queueMicrotask(() => {template[0].click();options.callback();});}}; return build.call(Menu, template);};
 });
 await page.getByRole('button', {name: '放大查看商品图片 1', exact: true}).click({button: 'right'});
 await until(async () => await application.evaluate(async ({clipboard}) => clipboard.has('image/png')), 'native PNG clipboard');
 report.copied_image = await application.evaluate(async ({clipboard, nativeImage}) => {
  const items = await clipboard.read();const item = items.find(entry => entry.types.includes('image/png'));
  const bytes = Buffer.from(await (await item.getType('image/png')).arrayBuffer());return {...nativeImage.createFromBuffer(bytes).getSize(), bytes: bytes.length};
 });
 assert.equal(report.copied_image.width, 640);assert.equal(report.copied_image.height, 480);
 report.completed.push('full-size-original-PNG-written-to-and-read-from-system-clipboard');
 const block = page.locator('.product-main-image');
 await block.getByRole('button', {name: /重新采集|读取/}).first().click();
 await until(async () => {
  const state = await invoke('get_product_main_image', {local_product_id: fixture.local_product_id});
  return !['loading', 'not_loaded'].includes(state.status) && !state.images?.some(image => image.status === 'ready');
 }, 'Demo recapture replaces old gallery without fallback');
 await until(async () => await block.getByRole('button', {name: /放大查看商品图片/}).count() === 0, 'old preview removed');
 await assert.rejects(() => page.evaluate(ref => window.reception.getImageOriginal(ref), {kind: 'product', local_product_id: fixture.local_product_id, version: 100000, sha256: fixture.old_sha256}));
 await shot('recapture-replaces-old-gallery');
 report.completed.push('real-Demo-recapture-clears-old-gallery-and-invalidates-old-original-reference');
 await application.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].setContentSize(1100, 720));
 await shot('narrow-complete-message-time');
 const process = application.process();
 await application.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].close());
 await until(() => process.exitCode !== null, 'normal application close');
 application = null;report.window_closed = true;
 assert.deepEqual(report.errors, []);report.status = 'passed';
} catch (error) {report.error = error.stack;process.exitCode = 1;}
finally {await application?.close();await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');}
console.log(JSON.stringify(report, null, 2));
