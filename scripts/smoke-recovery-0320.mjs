import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';
const option = name => { const i = process.argv.indexOf(name); return i < 0 ? '' : process.argv[i + 1]; };
const label = option('--label'), executable = option('--executable');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const output = path.join(root, 'artifacts/verification', 'recovery-0320-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0320-window-'));
const data = path.join(temporary, 'data');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
Object.assign(env, { RECEPTION_BOOTSTRAP_DIR: path.join(temporary, 'bootstrap'), RECEPTION_SHARED_SETTINGS_DIR: path.join(temporary, 'shared'), LOCALAPPDATA: path.join(temporary, 'local-app-data'), CHROME_LOG_FILE: path.join(output, 'chromium.log') });
if (!executable) env.RECEPTION_PYTHON = python;
const seed = `
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.reply_batches import freeze_reply_batch
from reception_service.repository import now_ms
s=ReceptionService(Path(sys.argv[2]),mode='demo')
try:
 cid='demo-emma'; owner=s.repo.get('conversation',cid); case=s.repo.get('case',owner['case_id']); source=s._latest_buyer(cid)
 messages=[{'body':'Noted, we will check the logo.','reply_sources':[source['message_id']]},{'body':'What is the delivery postcode?','reply_sources':[source['message_id']]}]
 draft=s._draft(case,'\\n\\n'.join(row['body'] for row in messages),[source['message_id']],s.get_context(case['case_id']),'demo',messages=messages)
 draft['status']='stale'; s.repo.put('draft',draft['draft_id'],draft)
 batch=freeze_reply_batch(messages,origin='automatic',channel='international',seller_id=s.seller_id,buyer_id=owner['buyer_id'],conversation_id=cid,case_id=case['case_id'],parent_draft_id=draft['draft_id'],parent_revision=draft['revision'],source_message_id=source['message_id'])
 batch.update(status='stopped',stopped=True,next_index=1,last_gate_reason='authorization_rejected')
 for i,part in enumerate(batch['parts']):
  oid='synthetic-window-op-'+str(i)
  part.update(status='accepted' if i==0 else 'cancelled',operation_ids=[oid],message_id='synthetic-accepted-id' if i==0 else None)
  s.repo.put('operation',oid,{'operation_id':oid,'kind':'send','purpose':'reception','seller_id':s.seller_id,'buyer_id':owner['buyer_id'],'conversation_id':cid,'case_id':case['case_id'],'source_message_id':source['message_id'],'batch_id':batch['batch_id'],'part_id':part['part_id'],'part_index':i,'intent':{'reply':part['body']},'status':'accepted' if i==0 else 'rejected','submission_started':i==0,'message_id':part['message_id'],'updated_at':now_ms()})
 s.repo.put('reply_batch',batch['batch_id'],batch)
finally:s.close()
`;
const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services/reception/src'), data], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
assert.equal(prepared.status, 0, prepared.stderr);
const report = { status: 'failed', actual_window: true, packaged_executable: executable || null, synthetic_only: true, production_data_accessed: false, real_ai_calls: 0, real_customer_sends: 0, recovery_rpc_real_demo_backend: true, waiting_projection_fixture: true, whatsapp_host_events_injected: true, native_rebuild_executed: false, screenshots: [], completed: [], errors: [], normal_close: false };
let application, page;
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
const until = async (check, message) => { const end = Date.now() + 30000; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error(message); };
const resize = (width, height) => application.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size.width, size.height), { width, height });
const shot = async name => { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); };
try {
 application = await electron.launch({ executablePath: executable ? path.resolve(root, executable) : path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 60000 });
 page = await application.firstWindow(); page.on('pageerror', error => report.errors.push(error.name));
 await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
 await page.getByRole('button', { name: /Emma Wilson/ }).first().click();
 await resize(1440, 960);
 assert.equal((await detail()).recoverable_reply_batches.length, 1);
 // Only the wait projection is injected; actual recovery below reaches Python.
 await application.evaluate(({ ipcMain, BrowserWindow }) => {
  const handlers = ipcMain._invokeHandlers;
  const original = handlers.get('reception:invoke');
  ipcMain.removeHandler('reception:invoke');
  ipcMain.handle('reception:invoke', async (event, method, params) => {
   const result = await original(event, method, params);
   if (method === 'get_conversation' && params.conversation_id === 'demo-emma') result.reply_batches = result.reply_batches.map(batch => ({ ...batch, status: 'waiting_sync', wait_reason: 'source_sync_pending', current_index: 1 }));
   return result;
  });
  globalThis.__restoreRecoverySmoke = () => { ipcMain.removeHandler('reception:invoke'); ipcMain.handle('reception:invoke', original); };
  BrowserWindow.getAllWindows()[0].webContents.send('reception:event', { event: 'state.changed', data: {} });
 });
 await page.getByText('正在同步会话，第 2 条尚未提交', { exact: true }).waitFor();
 await shot('waiting-sync-wide'); await resize(1100, 820); await shot('waiting-sync-narrow');
 await application.evaluate(({ BrowserWindow }) => { globalThis.__restoreRecoverySmoke(); delete globalThis.__restoreRecoverySmoke; BrowserWindow.getAllWindows()[0].webContents.send('reception:event', { event: 'state.changed', data: {} }); });
 await page.getByText(/后续发送已停止/).waitFor(); await resize(1440, 960);
 await page.getByRole('button', { name: '取出未发送内容', exact: true }).click();
 await until(async () => (await detail()).chat_draft?.message_parts?.[0] === 'What is the delivery postcode?', 'Recovery did not persist tail');
 const recovered = await detail();
 assert.deepEqual(recovered.chat_draft.message_parts, ['What is the delivery postcode?']);
 assert.equal(recovered.reply_batches[0].accepted, 1);
 assert.equal(recovered.reply_batches[0].status, 'stopped');
 await page.getByRole('textbox', { name: '第 1 条回复正文', exact: true }).waitFor();
 await shot('recovered-tail-wide');
 report.completed.push('waiting-progress-wide-narrow', 'real-narrow-recovery-rpc', 'accepted-prefix-excluded', 'manual-review-without-auto-send');
 await page.getByRole('button', { name: 'WhatsApp', exact: true }).first().click();
 await page.getByRole('region', { name: 'WhatsApp 工作区' }).waitFor();
 const native = value => application.evaluate(({ BrowserWindow }, data) => BrowserWindow.getAllWindows()[0].webContents.send('reception:event', { event: 'whatsapp.host.changed', data: { status: 'error', account_id: '', connection_epoch: '', ...data } }), value);
 await native({ error_code: 'page_database_error', page_state: 'error', phase: 'page_reload', previous_phase: 'app_modules' });
 await page.getByRole('button', { name: '重建 WhatsApp 登录环境', exact: true }).waitFor();
 await page.getByRole('button', { name: '查看失败详情', exact: true }).click();
 await shot('database-error-wide'); await resize(1100, 820); await shot('database-error-narrow');
 await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('reception:event', { event: 'whatsapp.recovery.changed', data: { stage: 'waiting' } }));
 await page.getByText('正在等待在途发送和旧连接收尾…', { exact: true }).waitFor(); await shot('recovery-progress-narrow');
 report.completed.push('database-error-and-reload-details', 'rebuild-entry-visible', 'recovery-progress');
 assert.deepEqual(report.errors, []);
 const child = application.process();
 await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
 await until(() => child.exitCode !== null, 'Normal close failed'); application = null;
 report.normal_close = true; report.status = 'passed';
} catch (error) { report.error = error instanceof Error ? error.message : 'RECOVERY_WINDOW_FAILED'; process.exitCode = 1; try { if (page) await shot('failure'); } catch {} }
finally { await application?.close(); await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); }
console.log(JSON.stringify(report, null, 2));
