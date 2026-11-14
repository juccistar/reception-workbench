import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { root } from './paths.mjs';
const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
const label = option('--label'), executable = option('--executable');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const output = path.join(root, 'artifacts/verification', 'replies-0319-' + label);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-0319-replies-'));
const data = path.join(temporary, 'data');
const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
if (!executable) env.RECEPTION_PYTHON = python;
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const seed = `
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from reception_service.service import ReceptionService
from reception_service.repository import now_ms
s=ReceptionService(Path(sys.argv[2]),mode='demo')
try:
 s.inject_demo('demo-emma','Please use black for the logo. The delivery postcode is still to confirm.')
 conv=s.repo.get('conversation','demo-emma'); case=s.repo.get('case',conv['case_id']); source=s._latest_buyer('demo-emma')
 values={'product':'棉帽','product_id':None,'product_url':None,'product_images':[],'quantity':'100件','specifications':'棉质，黑色Logo','customization':None,'destination':None,'delivery':None,'budget':None,'packaging':None,'certifications':None,'contact':None}
 case.update(requirements={key:{'value':value,'evidence_ids':[source['message_id']] if value else []} for key,value in values.items()},requirements_format='ai-v1',requirements_revision=1)
 s.repo.put('case',case['case_id'],case)
 messages=[{'body':'Got it, black for the logo.','reply_sources':[source['message_id']]},{'body':'What is the delivery postcode?','reply_sources':[source['message_id']]}]
 _,fingerprint=s._manual_context('demo-emma',case['case_id'])
 s.repo.put('manual_suggestion','demo-emma',{'suggestion_id':'synthetic-0319-window','conversation_id':'demo-emma','case_id':case['case_id'],'body':'\\n\\n'.join(row['body'] for row in messages),'reply_messages':messages,'reply_sources':[source['message_id']],'request_id':'synthetic-window-request','mode':'draft','input_revision':None,'input_hash':None,'context_fingerprint':fingerprint,'status':'ready','updated_at':now_ms()})
 s.repo.put('conversation_summary','demo-emma',{'conversation_id':'demo-emma','seller_id':conv['seller_id'],'buyer_id':conv['buyer_id'],'body':'客户希望棉帽使用黑色Logo，收货邮编尚待确认。','revision':1,'updated_at':now_ms()})
 # Summary is part of the frozen suggestion context.
 suggestion=s.repo.get('manual_suggestion','demo-emma'); _,suggestion['context_fingerprint']=s._manual_context('demo-emma',case['case_id']); s.repo.put('manual_suggestion','demo-emma',suggestion)
 print(json.dumps({'case_id':case['case_id'],'requirements_revision':1}))
finally:s.close()
`;
const prepared = spawnSync(python, ['-I', '-c', seed, path.join(root, 'services/reception/src'), data], { cwd: root, env, encoding: 'utf8', timeout: 30000 });
assert.equal(prepared.status, 0, 'Synthetic seed failed: ' + prepared.stderr);
const fixture = JSON.parse(prepared.stdout.trim());
const report = { status: 'failed', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version,
 actual_window: true, packaged_executable: executable || null, synthetic_only: true, production_data_accessed: false,
 real_ai_calls: 0, real_customer_sends: 0, suggestion_seeded: true, business_ipc_mocked: false,
 scope: 'Preseeded synthetic two-part suggestion; actual renderer/preload, Demo polish, saved draft, batch sends and receipts',
 screenshots: [], completed: [], errors: [], normal_close: false };
let application, page;
async function until(check, message, timeout = 30000) { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error('Timed out: ' + message); }
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
const detail = () => invoke('get_conversation', { conversation_id: 'demo-emma' });
const resize = (width, height) => application.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size.width, size.height), { width, height });
const shot = async name => { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' }); report.screenshots.push(name + '.png'); };
try {
 application = await electron.launch({ executablePath: executable ? path.resolve(root, executable) : path.join(root, 'node_modules/electron/dist/electron.exe'), args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE], cwd: root, env, timeout: 60000 });
 page = await application.firstWindow(); page.on('pageerror', error => report.errors.push(error.name));
 await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
 await page.getByRole('button', { name: /Emma Wilson/ }).first().click();
 await page.getByRole('main', { name: 'Emma Wilson 的客户接待', exact: true }).waitFor();
 await resize(1440, 960);
 assert.equal((await invoke('get_snapshot')).app.version, report.version);
 await page.getByRole('region', { name: '待处理回复建议', exact: true }).waitFor();
 assert.equal((await detail()).manual_suggestion.status, 'ready');
 await shot('two-part-suggestion-wide');
 await page.getByRole('button', { name: '替换到输入框', exact: true }).click();
 const first = page.getByRole('textbox', { name: '第 1 条回复正文', exact: true });
 const second = page.getByRole('textbox', { name: '第 2 条回复正文', exact: true });
 await first.waitFor(); assert.equal(await first.inputValue(), 'Got it, black for the logo.');
 await second.fill('Could you share the delivery postcode?');
 await until(async () => (await detail()).chat_draft?.message_parts?.[1] === 'Could you share the delivery postcode?', 'individual message persisted');
 await shot('two-part-editor-wide');
 await resize(1100, 820); await shot('two-part-editor-narrow');
 await resize(1440, 960); await second.focus();
 await invoke('check_ai');
 await until(async () => (await invoke('get_snapshot')).checks.ai.status === 'demo', 'Demo AI capability check');
 report.demo_ai_check = true;
 await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
 await until(async () => (await detail()).manual_suggestion?.mode === 'polish' && (await detail()).manual_suggestion?.input_part_index === 1, 'selected part polished');
 await page.getByRole('button', { name: '替换到输入框', exact: true }).click();
 assert.equal(await first.inputValue(), 'Got it, black for the logo.');
 await until(async () => (await detail()).chat_draft?.message_parts?.length === 2, 'polished draft saved');
 await shot('selected-part-polish-wide');
 const frozen = (await detail()).chat_draft.message_parts;
 await page.getByRole('button', { name: '发送', exact: true }).click();
 await until(async () => (await detail()).reply_batches?.some(batch => batch.status === 'accepted' && batch.total === 2 && batch.accepted === 2), 'two platform receipts recorded');
 const sent = await detail();
 assert.equal(sent.case.requirements_revision, fixture.requirements_revision);
 const seller = sent.messages.filter(message => message.role === 'seller');
 assert.equal(seller.filter(message => frozen.includes(message.text)).length, 2);
 assert.equal(new Set(seller.filter(message => frozen.includes(message.text)).map(message => message.message_id)).size, 2);
 report.sent_message_ids = seller.filter(message => frozen.includes(message.text)).map(message => message.message_id);
 await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor();
 await page.getByText('已发送 2/2 条', { exact: true }).waitFor();
 await shot('two-receipts-wide');
 report.completed.push('two-message-preview-and-adoption', 'per-part-edit-save', 'selected-part-demo-polish', 'two-separate-demo-submissions-and-receipts', 'unchanged-requirements-revision');
 await page.getByRole('textbox', { name: '回复正文', exact: true }).fill('New unsent draft after completed batch.');
 await until(async () => (await detail()).chat_draft?.body === 'New unsent draft after completed batch.', 'new editor version saved');
 assert.equal((await detail()).chat_draft.status, 'pending');
 await resize(1100, 820); await shot('new-input-after-batch-narrow');
 report.completed.push('new-editor-version-retained', 'wide-and-narrow-layouts');
 assert.deepEqual(report.errors, []);
 const child = application.process();
 await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
 await until(() => child.exitCode !== null, 'normal close'); application = null;
 report.normal_close = true; report.status = 'passed';
} catch (error) {
 report.error = error instanceof Error ? error.message : 'REPLIES_WINDOW_FAILED'; process.exitCode = 1;
 if (page) { try { await shot('failure'); } catch {} }
} finally { await application?.close(); await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); }
console.log(JSON.stringify(report, null, 2));
