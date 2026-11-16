import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const directory=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(directory,'../../..');
const output=path.join(directory,'previews/feedback');
const temporary=await mkdtemp(path.join(os.tmpdir(),'arw-feedback-'));
const host=path.join(temporary,'host.cjs');
await mkdir(output,{recursive:true});
await writeFile(host,`const {app,BrowserWindow}=require('electron');
app.setPath('userData',${JSON.stringify(path.join(temporary,'profile'))});
app.commandLine.appendSwitch('disable-background-networking');
app.whenReady().then(async()=>{const window=new BrowserWindow({width:1440,height:1000,useContentSize:true,frame:false,show:true,webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true,backgroundThrottling:false}});window.webContents.setWindowOpenHandler(()=>({action:'deny'}));await window.loadFile(${JSON.stringify(path.join(directory,'feedback.html'))});});
app.on('window-all-closed',()=>app.quit());`);
const env={...process.env};
for(const key of Object.keys(env))if(/^(ACCIO|PHOENIX|RECEPTION|HARNESS)_/i.test(key)||/^ELECTRON_RUN_AS_NODE$/i.test(key)||/credential|secret|token|password|api.?key/i.test(key))delete env[key];
const report={status:'failed',synthetic_only:true,external_requests:[],checks:[],screenshots:[],errors:[]};
let application;
try{
  application=await electron.launch({executablePath:path.join(root,'node_modules/electron/dist/electron.exe'),args:[host],cwd:root,env,timeout:30000});
  const page=await application.firstWindow();
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('request',request=>{if(/^https?:/i.test(request.url()))report.external_requests.push(request.url());});
  const base=new URL('index.html',`file:///${directory.replaceAll('\\','/')}/`).href;
  const open=async id=>{
    await page.goto(base+(id?`?feedback=${id}`:''));
    await page.waitForFunction(()=>!!window.ReceptionPreview&&!!window.RECEPTION_FEEDBACK_SCENARIOS);
    await page.evaluate(async()=>{await document.fonts.ready;await Promise.all([...document.images].map(image=>image.decode().catch(()=>{})));await new Promise(resolve=>requestAnimationFrame(resolve));});
  };
  const shot=async name=>{await page.screenshot({path:path.join(output,name),animations:'disabled'});report.screenshots.push(name);};
  const resize=async(width,height)=>{await application.evaluate(({BrowserWindow},size)=>BrowserWindow.getAllWindows()[0].setContentSize(size.width,size.height),{width,height});await page.waitForFunction(size=>innerWidth===size.width&&innerHeight===size.height,{width,height});};
  const withinViewport=async selector=>{const box=await page.locator(selector).boundingBox(),size=await page.evaluate(()=>({width:innerWidth,height:innerHeight}));assert(box&&box.x>=-1&&box.y>=-1&&box.x+box.width<=size.width+1&&box.y+box.height<=size.height+1,`${selector} outside viewport`);};
  const noModal=async()=>assert.equal(await page.locator('#modal[open]').count(),0);
  const runAiFailure=async()=>{await page.locator('#suggest-button').click();await page.locator('#modal[open]').waitFor();assert.equal(await page.locator('#modal-title').innerText(),'AI 不可用');};
  await open();
  assert.equal(await page.locator('[data-rf-injected]').count(),0);
  const scenarios=await page.evaluate(()=>window.RECEPTION_FEEDBACK_SCENARIOS.map(scene=>scene.id));
  assert.equal(scenarios.length,23);assert(!scenarios.includes('verify'));
  for(const id of scenarios){
    await open(id);if(id==='model')await runAiFailure();
    if(await page.locator('#modal').isVisible()){assert.equal(await page.locator('#modal-body .rf-modal-actions').count(),0);await withinViewport('#modal');}
    if(['suggestion','polish'].includes(id))assert.match(await page.locator('.suggestion-translation').innerText(),/样品|地址/);
    if(['takeover','resume','unconfirmed','unknown','evidence'].includes(id))await noModal();
    await shot(`${id}.png`);
  }
  report.checks.push('23 scenes render; suggestions and polish include matching Chinese translations; ownership, pending sends and source navigation have no confirmation dialog.');
  for(const id of ['takeover','resume']){
    await open(id);assert.equal(await page.evaluate(()=>window.ReceptionPreview.session().human),id==='takeover');
    await page.locator('[data-action="ownership"]').first().click();await noModal();
    assert.equal(await page.evaluate(()=>window.ReceptionPreview.session().human),id!=='takeover');
  }
  await open('suggestion');
  const original=await page.locator('.suggestion-body').innerText();
  await page.locator('[data-action="replace"]').click();assert.equal(await page.locator('#reply-input').inputValue(),original);
  assert(!/样品|地址/.test(await page.locator('#reply-input').inputValue()));
  await page.locator('#suggest-button').click();assert.match(await page.locator('.suggestion-translation').innerText(),/样品/);
  report.checks.push('Takeover and resume act immediately; replacing a suggestion adopts only foreign text, and polishing an adopted example keeps its translation.');
  for(const id of ['unconfirmed','unknown']){
    await open(id);await page.locator('#reply-input').fill('preserved pending draft');await page.locator('#reply-input').press('Enter');
    await noModal();assert(await page.locator('#send-button').isDisabled());assert.equal(await page.evaluate(()=>window.ReceptionPreview.session().pending),true);
    assert.equal(await page.getByRole('button',{name:/核查|核实|重发/}).count(),0);
    assert.equal(await page.locator('#reply-input').inputValue(),'preserved pending draft');
  }
  report.checks.push('Pending sends show status only, retain unresolved state and drafts, and provide no extra verification or resend action.');
  for(const id of ['translation','image-error','history-loading']){
    await open(id);assert.equal(await page.locator('.rf-panel p,.rf-panel button').count(),0);
    assert.equal(await page.locator('.rf-panel strong').count(),1);
  }
  await open('history-failed');await page.locator('#complete-history-button').click();assert(await page.locator('#complete-history-button').isDisabled());
  assert.equal(await page.locator('[data-rf-history] strong').innerText(),'正在补全历史');
  await page.waitForFunction(()=>!window.ReceptionPreview.session().historyLoading);
  assert(await page.locator('#complete-history-button').isEnabled());
  await open();await page.locator('#reply-input').fill('source navigation preserves draft');
  await page.locator('[data-action="locate-source"]').click();await page.locator('[data-message-id="alex-0"].source-located').waitFor();await noModal();
  assert.equal(await page.locator('#reply-input').inputValue(),'source navigation preserves draft');
  assert.equal(await page.evaluate(()=>window.ReceptionPreview.state.viewCase),'current');
  report.checks.push('Translation, image and history notices contain status only; history retry works from the normal button; source links directly locate the exact message without changing draft or case.');
  await open('offline');await page.locator('#reply-input').fill('offline preserved draft');await page.locator('#reply-input').press('Enter');
  assert.equal(await page.locator('#reply-input').inputValue(),'offline preserved draft');assert(await page.locator('#send-button').isDisabled());
  await page.locator('[data-customer="emma"]').click();assert.equal(await page.locator('#send-button').getAttribute('data-rf-blocked'),null);
  assert.equal(await page.evaluate(()=>window.ReceptionPreview.state.globalEnabled),false);
  await open('model');await noModal();assert(await page.locator('#suggest-button').isEnabled());
  await page.locator('#global-status').click();await page.locator('#global-status').click();
  assert.equal(await page.evaluate(()=>window.ReceptionPreview.state.globalEnabled),true);await noModal();
  await page.locator('#reply-input').fill('manual preview');await page.locator('#supplement-input').fill('preserved supplement');await runAiFailure();
  assert.equal(await page.locator('#reply-input').inputValue(),'manual preview');assert.equal(await page.locator('#supplement-input').inputValue(),'preserved supplement');
  await page.locator('[data-feedback-action="cancel-modal"]').click();await runAiFailure();
  assert.equal(await page.evaluate(()=>window.ReceptionPreview.session().aiAttempts),2);
  await page.locator('[data-feedback-action="cancel-modal"]').click();await page.locator('#send-button').click();
  await page.waitForFunction(()=>document.querySelector('#message-stream').innerText.includes('manual preview'));
  report.checks.push('AI requests run without preflight, show an alert only after failure, allow retry and retain draft/supplement; ordinary manual send still works.');
  await open('global');assert.equal(await page.evaluate(()=>window.ReceptionPreview.state.globalEnabled),true);assert.equal(await page.locator('.conversation-header+.rf-panel').count(),0);
  await resize(390,844);
  for(const id of ['suggestion','polish','takeover','resume','unconfirmed','unknown','translation','image-error','image','evidence','model','history-loading']){
    await open(id);if(id==='model')await runAiFailure();
    if(await page.locator('#modal').isVisible())await withinViewport('#modal');
    if(id==='image-error'){await page.getByRole('button',{name:'展开客户资料',exact:true}).click();await withinViewport('#customer-panel');}
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await shot(`mobile-${id}.png`);
  }
  await open();await page.getByRole('button',{name:'展开客户资料',exact:true}).click();await page.locator('[data-action="locate-source"]').click();
  assert.equal(await page.locator('#workspace').getAttribute('class'),'workspace');await noModal();
  await page.goto(new URL('feedback.html',base).href);await page.locator('#rf-list button').first().waitFor({state:'attached'});await page.locator('#rf-back').click();
  assert(await page.locator('#rf-list').isVisible());await shot('mobile-catalog.png');
  report.checks.push('Desktop and 390 px views fit; mobile source navigation closes the details drawer and jumps to chat.');
  assert.deepEqual(report.external_requests,[]);assert.deepEqual(report.errors,[]);report.status='passed';
}catch(error){report.errors.push(String(error.stack||error));process.exitCode=1;}
finally{if(application)await application.close().catch(()=>{});await writeFile(path.join(directory,'feedback-verification.json'),JSON.stringify(report,null,2)+'\n');await rm(temporary,{recursive:true,force:true,maxRetries:3,retryDelay:80});}
console.log(JSON.stringify(report,null,2));
