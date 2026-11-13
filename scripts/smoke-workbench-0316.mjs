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
const output = path.join(root, 'artifacts/verification', 'workbench-0316-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0316-demo-'));
const data = path.join(temporary, 'data');
const fixtureFile = path.join(temporary, 'synthetic-original.png');
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
from reception_service.repository import now_ms
s=ReceptionService(Path(sys.argv[2]),mode='demo')
try:
 s.inject_demo('demo-emma','Synthetic inquiry: blue cotton hats, quantity and destination to confirm.')
 msg=s._latest_buyer('demo-emma'); case=s.get_conversation('demo-emma')['case']
 values={'product':'轻量棉帽','product_id':'123456789','product_url':None,'product_images':[],'quantity':None,'specifications':'蓝色棉质；可刺绣','customization':None,'destination':'法国','delivery':None,'budget':None,'packaging':None,'certifications':None,'contact':'Emma Wilson（买家自述）'}
 case['requirements']={key:{'value':value,'evidence_ids':[msg['message_id']] if value else []} for key,value in values.items()}; case['requirements_format']='ai-v1'; case['requirements_revision']=1; s.repo.put('case',case['case_id'],case)
 s._sync_requirement_product(case); binding=s.repo.get('requirement_product_binding',case['case_id']); local_id=binding['local_product_id']
 images=[]
 for index,color in enumerate(['#6685a6','#7893af','#8c9f87','#ac8d72','#63747e']):
  image=Image.effect_noise((1200,900),15+index).convert('RGB'); draw=ImageDraw.Draw(image); draw.rectangle((0,0,1200,130),fill='#eef3e9'); draw.text((35,30),'SYNTHETIC ORIGINAL 0.3.16 / '+str(index+1),fill='#203b25'); draw.ellipse((250,350,1050,700),fill='#d9dfd3'); draw.pieslice((240,170,850,760),180,360,fill=color); draw.rectangle((240,450,850,590),fill=color); draw.ellipse((650,490,1080,620),fill=color)
  raw=io.BytesIO(); image.save(raw,format='PNG'); asset=decode_image(raw.getvalue(),'image/png'); s.repo.put('image_asset',asset['sha256'],asset); images.append({'status':'ready','asset_sha256':asset['sha256']})
  if index==0: image.save(sys.argv[3],format='PNG')
 s.repo.put('product_main_image',local_id,{'seller_id':s.seller_id,'local_product_id':local_id,'product_id':'123456789','status':'ready','image_group_version':1,'images':images,'asset_sha256':images[0]['asset_sha256'],'updated_at':1,'display_only':True,'selection_rule':'lowest_image_index','primary_verified':False})
 conv=s.repo.get('conversation','demo-emma'); other=s._new_case(conv,source_message_id=msg['message_id']); other['requirements_format']='ai-v1'; other['requirements_revision']=1; other['requirements']={key:{'value':None if key!='product_images' else [],'evidence_ids':[]} for key in values}; other['requirements']['product']={'value':'红色陶瓷杯','evidence_ids':[msg['message_id']]}; s.repo.put('case',other['case_id'],other)
 s.repo.put('conversation_summary','demo-emma',{'conversation_id':'demo-emma','seller_id':conv['seller_id'],'buyer_id':conv['buyer_id'],'body':'客户咨询棉帽，也有一笔独立杯子需求；数量和交期尚未明确。','revision':1,'updated_at':1})
 print(json.dumps({'local_product_id':local_id,'case_id':case['case_id'],'other_case_id':other['case_id'],'original_sha256':images[0]['asset_sha256']}))
finally:s.close()
`;
const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services/reception/src'), data, fixtureFile], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
assert.equal(prepared.status, 0, 'Synthetic seed failed: ' + prepared.stderr);
const fixture = JSON.parse(prepared.stdout.trim());
const report = { status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version, packaged: Boolean(executable), actual_window: true, synthetic_only: true, business_ipc_mocked: false, native_menu_selection_mocked: true, native_picker_source_mocked: true, clipboard_write_sink_mocked: true, external_browser_sink_mocked: true, production_clipboard_accessed: false, production_data_accessed: false, real_ai_calls: 0, real_customer_sends: 0, completed: [], screenshots: [], errors: [], window_closed: false };
let app, page;
async function until(check, message, timeout = 30000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('Timed out: ' + message); }
const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 45000 };
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = (caseId = fixture.case_id) => invoke('get_conversation', { conversation_id: 'demo-emma', case_id: caseId });
const choose = async name => { await page.getByRole('button', { name: new RegExp(name) }).click(); await page.getByRole('main', { name: name + ' 的客户接待' }).waitFor(); };
async function resize(width, height) { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size.width, size.height), { width, height }); }
async function shot(name) { await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); }
async function job(ack) { await until(async () => { const found = (await invoke('list_jobs', { include_background: true })).find(row => row.job_id === ack.job_id); if (!found || ['queued','running'].includes(found.status)) return false; assert.equal(found.status,'succeeded',JSON.stringify(found)); return true; }, 'job completed'); }
async function sinks() {
 await app.evaluate(({ Menu, clipboard, shell, nativeImage }) => {
  globalThis.__imageCopies=[]; globalThis.__openedProducts=[];
  const original=Menu.buildFromTemplate;
  Menu.buildFromTemplate=function(template){ if(template.length===1&&template[0].label==='复制图片') return {popup(options){queueMicrotask(()=>{template[0].click();options.callback();});}}; return original.call(Menu,template); };
  clipboard.write=async items=>{const blob=await items[0].getType('image/png'); const bytes=Buffer.from(await blob.arrayBuffer()); const image=nativeImage.createFromBuffer(bytes); globalThis.__imageCopies.push({...image.getSize(),bytes:bytes.length});};
  shell.openExternal=async url=>{globalThis.__openedProducts.push(url);};
 });
 await page.evaluate(()=>{ window.__urlCopies=[]; Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__urlCopies.push(text);}}}); });
}
async function launch() { app=await electron.launch(options); report.window_closed=false; assert.equal(await app.evaluate(({app})=>app.isPackaged),Boolean(executable)); page=await app.firstWindow(); page.on('pageerror',error=>report.errors.push(error.message)); await page.getByRole('textbox',{name:'回复正文',exact:true}).waitFor({timeout:45000}); await resize(1440,960); await sinks(); assert.equal((await invoke('get_snapshot')).app.version,report.version); }
async function close() { const process=app.process(); await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].close()); await until(()=>process.exitCode!==null,'normal close'); app=null; report.window_closed=true; }
async function copyCount() { return app.evaluate(()=>globalThis.__imageCopies.length); }
async function originalLoaded() { const image=page.locator('.original-image-viewport img'); return await image.count()===1 && await image.evaluate(node=>node.naturalWidth===1200); }
try {
 await launch(); await choose('Emma Wilson');
 await page.getByRole('button',{name:'放大查看商品图片 5',exact:true}).waitFor();
 assert.equal(await page.locator('.conversation-identity .avatar,.history-tool,.history-bar,.translation-toggle,.suggestion-banner,.composer-heading').count(),0);
 assert.equal(await page.locator('.conversation-summary svg').count(),2);
 await resize(1920,1080); await shot('layout-a-wide'); await resize(1440,960);
 const product=await invoke('get_product_main_image',{local_product_id:fixture.local_product_id});
 const original=await page.evaluate(async ref=>{const image=await window.reception.getImageOriginal(ref);const digest=await crypto.subtle.digest('SHA-256',image.bytes);return {width:image.width,height:image.height,size:image.bytes.length,sha:[...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join('')};},product.images[0].image_ref);
 assert.equal(original.sha,fixture.original_sha256); assert.equal(original.width,1200); assert.equal(original.height,900);
 report.original=original;
 await page.getByRole('button',{name:'放大查看商品图片 1',exact:true}).click({button:'right'}); await until(async()=>await copyCount()===1,'thumbnail original copied');
 await page.getByRole('button',{name:'放大查看商品图片 1',exact:true}).click(); await until(originalLoaded,'full-size original displayed');
 await page.getByRole('button',{name:'原始尺寸',exact:true}).click(); const frame=await page.locator('.original-image-dialog').boundingBox(), viewport=await page.locator('.original-image-viewport').boundingBox(); assert(viewport.x>=frame.x&&viewport.x+viewport.width<=frame.x+frame.width+1,'original image viewport must stay inside the card'); await shot('product-original');
 await page.locator('.original-image-viewport img').click({button:'right'}); await until(async()=>await copyCount()===2,'modal original copied');
 await page.getByRole('button',{name:'下一张商品图片',exact:true}).click(); await page.getByText('2 / 5',{exact:true}).waitFor(); await page.keyboard.press('Escape');
 const copied=await app.evaluate(()=>globalThis.__imageCopies); assert(copied.every(row=>row.width===1200&&row.height===900)); report.copied_images=copied;
 report.completed.push('real-private-chunks-original-hash-full-resolution-modal-and-thumbnail-copy');
 const url='https://www.alibaba.com/product-detail/_123456789.html';
 await page.getByRole('button',{name:url,exact:true}).click(); assert.deepEqual(await page.evaluate(()=>window.__urlCopies),[url]);
 await page.getByRole('button',{name:'在浏览器打开产品链接',exact:true}).click(); await until(async()=>await app.evaluate(()=>globalThis.__openedProducts.length)===1,'canonical URL opened'); assert.deepEqual(await app.evaluate(()=>globalThis.__openedProducts),[url]);
 report.completed.push('full-url-copy-and-separate-restricted-browser-arrow');
 const histories=(await invoke('list_jobs',{include_background:true})).filter(row=>row.kind==='fill_history'&&row.conversation_id==='demo-emma').length;
 await page.getByRole('button',{name:'补全当前客户历史',exact:true}).evaluate(button=>{button.click();button.click();});
 await until(async()=>(await invoke('list_jobs',{include_background:true})).filter(row=>row.kind==='fill_history'&&row.conversation_id==='demo-emma').some(row=>row.status==='succeeded'),'history direct task');
 assert.equal((await invoke('list_jobs',{include_background:true})).filter(row=>row.kind==='fill_history'&&row.conversation_id==='demo-emma').length,histories+1);
 await page.getByRole('button',{name:'暂停此客户自动接待',exact:true}).click(); await page.getByRole('button',{name:'恢复此客户自动接待',exact:true}).waitFor(); assert.equal((await detail()).conversation.human_owned,true);
 await page.getByRole('button',{name:'恢复此客户自动接待',exact:true}).click(); await page.getByRole('button',{name:'暂停此客户自动接待',exact:true}).waitFor(); assert.equal((await invoke('get_snapshot')).reception.running,false);
 report.completed.push('one-click-history-deduplication-and-header-customer-toggle-with-global-paused');
 const status=page.getByLabel('查看会话状态',{exact:true}), help=page.getByRole('button',{name:'附件格式与限制',exact:true});
 await help.click(); const bottom=await page.locator('.supplement-toolbar').boundingBox(); await status.click(); await until(async()=>await page.locator('.attachment-help p').count()===0,'mutual popup closure'); await page.keyboard.press('Escape'); assert.equal(await page.locator('.conversation-status').getAttribute('open'),null);
 await help.click(); await page.getByLabel('回复正文',{exact:true}).click(); assert.equal(await page.locator('.attachment-help p').count(),0); const after=await page.locator('.supplement-toolbar').boundingBox(); assert(Math.abs(bottom.y-after.y)<2);
 const split=await page.getByRole('separator',{name:'调整采购需求与业务补充高度',exact:true}).boundingBox(); await page.mouse.move(split.x+split.width/2,split.y+split.height/2); await page.mouse.down(); await page.mouse.move(split.x+split.width/2,split.y-90,{steps:8}); await page.mouse.up(); const dragged=await page.locator('.supplement-toolbar').boundingBox(); assert(Math.abs(after.y-dragged.y)<3);
 await app.evaluate(({dialog},file)=>{dialog.showOpenDialog=async()=>({canceled:false,filePaths:[file]});},fixtureFile);
 await page.getByRole('button',{name:'添加附件',exact:true}).click(); await until(async()=>(await detail()).supplement.attachments.length===1,'original attachment persisted');
 await page.locator('.attachment-preview[title="synthetic-original.png"]').click(); await until(originalLoaded,'supplement original displayed'); await shot('supplement-original'); await page.locator('.image-dialog-backdrop').click({position:{x:8,y:8}}); assert.equal(await page.locator('.original-image-dialog').count(),0);
 await shot('fixed-supplement-footer'); report.completed.push('controlled-popovers-fixed-footer-split-resize-and-centred-supplement-original');
 await invoke('check_ai'); await until(async()=>(await invoke('get_snapshot')).checks.ai.status==='demo','demo AI ready');
 await invoke('inject_demo',{conversation_id:'demo-emma',text:'I need 100 pieces'});
 await page.getByRole('combobox',{name:'当前采购事项',exact:true}).selectOption(fixture.other_case_id);
 const ack=await invoke('generate_manual_suggestion',{conversation_id:'demo-emma',case_id:fixture.case_id,client_request_id:'synthetic-0316-a'}); await job(ack);
 await until(async()=>(await detail()).case.requirements.quantity.value==='100件','explicit quantity patch persisted');
 assert.equal(await page.getByRole('combobox',{name:'当前采购事项',exact:true}).inputValue(),fixture.other_case_id);
 const saved=await detail(); const revision=saved.case.requirements_revision; const firstSuggestion=saved.manual_suggestion.suggestion_id;
 assert.equal((await detail(fixture.other_case_id)).case.requirements.product.value,'红色陶瓷杯');
 await job(await invoke('generate_manual_suggestion',{conversation_id:'demo-emma',case_id:fixture.case_id,client_request_id:'synthetic-0316-b'})); assert.equal((await detail()).case.requirements_revision,revision);
 await choose('Noah Brown'); await choose('Emma Wilson'); await page.getByRole('region',{name:'待处理回复建议',exact:true}).waitFor(); await shot('pending-suggestion-and-stable-demand');
 await page.getByRole('button',{name:'弃用',exact:true}).click(); assert.equal(await page.getByRole('region',{name:'待处理回复建议',exact:true}).count(),0);
 report.completed.push('real-demo-job-incremental-demand-no-change-revision-and-pinned-other-case');
 await resize(1440,720); await shot('low-height-footer'); const footer=await page.locator('.supplement-toolbar').boundingBox(); assert(footer.y+footer.height<=720);
 await resize(1100,820); await page.getByRole('button',{name:'采购需求',exact:true}).click(); await page.getByRole('dialog',{name:'采购需求',exact:true}).getByLabel('业务补充内容',{exact:true}).waitFor(); await shot('narrow-drawer'); await page.keyboard.press('Escape');
 await close(); await launch(); await choose('Emma Wilson'); assert.equal((await detail()).case.requirements_revision,revision); assert.equal((await detail()).supplement.attachments.length,1); assert.equal(await page.getByRole('region',{name:'待处理回复建议',exact:true}).count(),0); await shot('reopened-stable-demand');
 report.completed.push('low-height-narrow-window-normal-reopen-and-dismissal-persistence');
 assert.deepEqual(report.errors,[]); await close(); report.status='passed';
} catch(error) { report.error=error.stack; process.exitCode=1; if(page){try{await shot('failure');await writeFile(path.join(output,'failure.txt'),(await page.locator('body').innerText()).slice(0,16000));}catch{}} }
finally { if(app) await app.close().catch(()=>{}); await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n',{flag:'wx'}); }
console.log(JSON.stringify(report));
