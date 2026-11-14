import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';
const option = name => { const at=process.argv.indexOf(name); return at<0?'':process.argv[at+1]; };
const label=option('--label'), executable=option('--executable');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label||''),'Pass a new label');
const output=path.join(root,'artifacts/verification/auto-0321-'+label); await mkdir(output);
const temporary=await mkdtemp(path.join(os.tmpdir(),'arw-0321-window-')),data=path.join(temporary,'data');
const python=path.join(root,'services/reception/.venv/Scripts/python.exe');
const env={...process.env};
for(const key of Object.keys(env))if(/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key)||/^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key)||/credential|secret|token|password|api.?key/i.test(key))delete env[key];
Object.assign(env,{RECEPTION_BOOTSTRAP_DIR:path.join(temporary,'bootstrap'),RECEPTION_SHARED_SETTINGS_DIR:path.join(temporary,'shared'),LOCALAPPDATA:path.join(temporary,'local'),CHROME_LOG_FILE:path.join(output,'chromium.log')});
if(!executable)env.RECEPTION_PYTHON=python;
const seed=String.raw`
import copy,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.repository import now_ms
s=ReceptionService(Path(sys.argv[2]),mode='demo')
result=[]
try:
 for count in (2,3):
  cid='demo-auto-0321-'+str(count)
  owner={'conversation_id':cid,'buyer_id':'synthetic-auto-buyer-'+str(count),'seller_id':s.seller_id,'customer_name':'Auto reply demo '+str(count)}
  row={**owner,'message_id':'synthetic-auto-source-'+str(count),'role':'buyer','text':'I need blue glasses, 500 pairs, black, ABS, 10 cm. The delivery postcode and date are still to confirm.','timestamp_ms':now_ms()-5000,'attachments':[],'own':False}
  s.integrations.messages.append(copy.deepcopy(row));s._ingest(owner,[row],has_more=False,full=True)
  conversation=s.repo.get('conversation',cid);case=s.repo.get('case',conversation['case_id']);context=s.get_context(case['case_id'])
  bodies=['Black for the logo, noted.','What is the delivery postcode?','Do you have a preferred delivery date?'][:count]
  messages=[{'body':body,'reply_sources':[row['message_id']]} for body in bodies]
  draft=s._draft(case,'\n\n'.join(bodies),[row['message_id']],context,'demo',messages=messages)
  assert draft['validation']['ready'],draft['validation']
  s.repo.put('draft',draft['draft_id'],draft,history=True)
  result.append({'conversation_id':cid,'customer_name':owner['customer_name'],'count':count,'bodies':bodies,'draft_id':draft['draft_id']})
 print(json.dumps(result))
finally:s.close()
`;
const prepared=spawnSync(python,['-X','utf8','-I','-c',seed,path.join(root,'services/reception/src'),data],{cwd:root,env,encoding:'utf8',timeout:30000});
assert.equal(prepared.status,0,'Synthetic seed failed: '+prepared.stderr);
const fixtures=JSON.parse(prepared.stdout.trim());
const report={status:'failed',version:JSON.parse(await readFile(path.join(root,'package.json'),'utf8')).version,actual_window:true,packaged_executable:executable||null,synthetic_only:true,production_data_accessed:false,real_ai_calls:0,real_customer_sends:0,ai_reply_seeded:true,automatic_sends_use_real_demo_backend:true,intermediate_status_screenshots_use_projection_fixture:true,manual_operations_use_real_demo_backend:true,screenshots:[],completed:[],errors:[],normal_close:false};
let application,page,current=fixtures[0].conversation_id;
const until=async(check,message,timeout=30000)=>{const end=Date.now()+timeout;while(Date.now()<end){if(await check())return;await new Promise(resolve=>setTimeout(resolve,75));}throw new Error('Timed out: '+message);};
const invoke=(method,params={})=>page.evaluate(({method,params})=>window.reception.invoke(method,params),{method,params});
const detail=()=>invoke('get_conversation',{conversation_id:current});
const resize=(width,height)=>application.evaluate(({BrowserWindow},size)=>BrowserWindow.getAllWindows()[0].setContentSize(size.width,size.height),{width,height});
const shot=async name=>{await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));await page.screenshot({path:path.join(output,name+'.png'),animations:'disabled'});report.screenshots.push(name+'.png');};
async function projection(status){
 await application.evaluate(({ipcMain,BrowserWindow},{cid,status})=>{
  if(!globalThis.__auto321Original){globalThis.__auto321Original=ipcMain._invokeHandlers.get('reception:invoke');ipcMain.removeHandler('reception:invoke');ipcMain.handle('reception:invoke',async(event,method,params)=>{const value=await globalThis.__auto321Original(event,method,params);const fixture=globalThis.__auto321Projection;if(fixture&&method==='get_conversation'&&params.conversation_id===fixture.cid){const batch=value.reply_batches?.find(row=>row.origin==='automatic');if(batch){batch.status=fixture.status;batch.accepted=1;batch.current_index=1;batch.updated_at=Date.now();batch.wait_reason=fixture.status==='waiting_sync'?'source_sync_pending':null;batch.last_gate_reason=fixture.status==='stopped'?'conversation_taken_over':null;batch.message=fixture.status==='stopped'?'当前会话已由人工接管，未提交尾部已停止':null;batch.pending_operation_ids=fixture.status==='waiting_receipt'?['synthetic-ui-only-unknown']:[];batch.parts=batch.parts.map((part,index)=>({...part,status:index===0?'accepted':index===1&&fixture.status==='waiting_receipt'?'unknown':fixture.status==='stopped'?'cancelled':'planned',message_id:index===0?part.message_id:null}));}}return value;});}
  globalThis.__auto321Projection=status?{cid,status}:null;BrowserWindow.getAllWindows()[0].webContents.send('reception:event',{event:'state.changed',data:{conversation_id:cid}});
 },{cid:current,status});
}
try{
 application=await electron.launch({executablePath:executable?assertReleasePath(root,executable):path.join(root,'node_modules/electron/dist/electron.exe'),args:[...(executable?[]:[path.join(root,'artifacts/app/main/index.js')]),'--data-dir',data,'--log-file='+env.CHROME_LOG_FILE],cwd:root,env,timeout:60000});
 page=await application.firstWindow();page.on('pageerror',error=>report.errors.push(error.name));
 await page.getByRole('textbox',{name:'回复正文',exact:true}).waitFor({timeout:45000});await resize(1440,960);
 assert.equal((await invoke('get_snapshot')).app.version,report.version);
 await invoke('check_ai');await until(async()=>(await invoke('get_snapshot')).checks.ai.status==='demo','Demo AI check');
 for(const fixture of fixtures){
  current=fixture.conversation_id;
  await page.getByRole('button',{name:new RegExp(fixture.customer_name)}).first().click();
  await invoke('set_scope',{mode:'selected',conversation_ids:[current],max_auto_replies:null});
  await invoke('set_reception',{enabled:true,auto_send:true});
  await until(async()=>(await detail()).reply_batches?.some(batch=>batch.origin==='automatic'&&batch.total===fixture.count&&batch.accepted===fixture.count&&batch.status==='accepted'),'automatic '+fixture.count+' distinct receipts');
  const value=await detail(),batch=value.reply_batches.find(row=>row.total===fixture.count&&row.status==='accepted');
  assert.equal(new Set(batch.parts.map(part=>part.message_id)).size,fixture.count);
  assert(batch.parts.every(part=>part.status==='accepted'&&part.message_id));
  const sent=value.messages.filter(message=>batch.parts.some(part=>part.message_id===message.message_id));
  assert.deepEqual(sent.map(row=>row.text),fixture.bodies);
  assert.equal(await page.getByRole('textbox',{name:'回复正文',exact:true}).inputValue(),'');
  await invoke('set_reception',{enabled:false,auto_send:false});
  const history=page.locator('summary').filter({hasText:'发送记录'}).first();await history.waitFor();
  if(!(await history.evaluate(node=>node.parentElement.open)))await history.click();
  await shot('automatic-'+fixture.count+'-receipts-wide');
  report.completed.push('automatic-'+fixture.count+'-ordered-independent-demo-receipts');
 }
 await projection('waiting_sync');await page.getByRole('region',{name:'自动回复进度',exact:true}).waitFor();await shot('automatic-sync-wide');
 await resize(1100,820);await projection('waiting_receipt');await until(async()=>(await page.getByRole('region',{name:'自动回复进度',exact:true}).innerText()).includes('确认'),'unknown projection');await shot('automatic-receipt-narrow');
 await projection('stopped');await until(async()=>(await page.getByRole('region',{name:'自动回复进度',exact:true}).innerText()).includes('停止'),'stopped projection');await shot('automatic-stopped-narrow');
 await projection(null);await resize(1440,960);
 await page.getByRole('button',{name:'生成回复建议',exact:true}).click();await page.getByRole('region',{name:'待处理回复建议',exact:true}).waitFor();
 assert.equal((await detail()).manual_suggestion.reply_messages.length,1);await shot('single-manual-suggestion-wide');
 await page.getByRole('button',{name:'替换到输入框',exact:true}).click();
 const editor=page.getByRole('textbox',{name:'回复正文',exact:true});await until(async()=>!!(await editor.inputValue()),'single suggestion adopted');
 assert.equal(await page.getByRole('textbox',{name:/第 \d+ 条回复正文/}).count(),0);
 assert.equal(await page.getByRole('button',{name:'合为一条',exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'取出未发送内容',exact:true}).count(),0);
 await until(async()=>(await detail()).chat_draft?.body===(await editor.inputValue()),'single draft saved');
 await page.getByRole('button',{name:'润色当前回复',exact:true}).click();await until(async()=>(await detail()).manual_suggestion?.mode==='polish','whole body polish');
 await resize(1100,820);await shot('whole-body-polish-narrow');
 await page.getByRole('button',{name:'替换到输入框',exact:true}).click();await until(async()=>!(await page.getByRole('region',{name:'待处理回复建议',exact:true}).count()),'polish applied');
 await until(async()=>{const value=await detail();return value.chat_draft?.body===await editor.inputValue();},'polish saved');
 await editor.press('Control+Enter');await until(async()=>(await detail()).chat_draft?.status==='accepted','single manual Ctrl+Enter receipt');
 await editor.fill('A new unsent manual message.');await until(async()=>(await detail()).chat_draft?.body==='A new unsent manual message.','new input preserved');
 await resize(1440,960);await shot('plain-manual-new-input-wide');
 report.completed.push('separate-current-progress-and-history','explicit-projection-wait-receipt-stop-layouts','strict-single-suggestion','whole-body-polish','single-manual-ctrl-enter','new-input-preserved','no-human-multipart-controls');
 assert.deepEqual(report.errors,[]);
 const child=application.process();await application.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].close());await until(()=>child.exitCode!==null,'normal close');application=null;report.normal_close=true;report.status='passed';
}catch(error){report.error=error instanceof Error?error.message:'AUTO_WINDOW_FAILED';process.exitCode=1;try{if(page)await shot('failure');}catch{}}
finally{await application?.close();await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n');await rm(temporary,{recursive:true,force:true,maxRetries:5,retryDelay:100});}
console.log(JSON.stringify(report,null,2));
