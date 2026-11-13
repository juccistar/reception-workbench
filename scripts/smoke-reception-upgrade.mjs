import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, open, readFile, writeFile } from 'node:fs/promises';
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
const version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
const output = path.join(root, 'artifacts/verification/reception-' + label);
await mkdir(output);
await (await open(path.join(output, 'attempt.claim'), 'wx')).close();
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-reception-upgrade-'));
const data = path.join(temporary, 'data');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_BOOTSTRAP_DIR = path.join(temporary, 'bootstrap');
env.RECEPTION_SHARED_SETTINGS_DIR = path.join(temporary, 'shared-settings');
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
env.CHROME_LOG_FILE = path.join(output, 'chromium.log');
const options = { executablePath: executable || path.join(root, 'node_modules/electron/dist/electron.exe'),
  args: [...(executable ? [] : [path.join(root, 'artifacts/app/main/index.js')]), '--data-dir', data, '--log-file=' + env.CHROME_LOG_FILE],
  cwd: root, env, timeout: 45000 };
const report = { status: 'failed', version, mode: 'demo', packaged: !!executable,
  real_ai_calls: 0, customer_sends: 0, synthetic_sends: 0, production_data_accessed: false,
  business_ipc_mocked: false, actual_window: true, windows_closed: false, renderer_errors: [] };
const until = async (check, message, timeout = 45000) => { const end = Date.now() + timeout; while (Date.now() < end) { if (await check()) return; await new Promise(resolve => setTimeout(resolve, 100)); } throw new Error(message); };
let app, page;
const cid = 'demo-emma';
const firstQuote = 'We can offer 15 per piece. Shipping is not included.';
const finalQuote = 'We can offer 18 per piece. Shipping is not included.';
const latestNote = '18 per piece, shipping not included. Confirm quantity before quotation.';
try {
  app = await electron.launch(options);
  page = await app.firstWindow();
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1600, 1000));
  await page.bringToFront();
  const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.reception.invoke(method, params), { method, params });
  const snapshot = () => invoke('get_snapshot');
  const detail = () => invoke('get_conversation', { conversation_id: cid });
  assert.equal((await snapshot()).app.version, version);
  assert.equal((await snapshot()).app.mode, 'demo');
  await page.getByRole('button', { name: /Emma Wilson/ }).click();
  await until(async () => (await page.locator('.merchant').innerText()).trim() === '演示商家公司', 'Company name card did not become ready');
  report.company_name_only = true;

  const note = page.getByRole('textbox', { name: '业务补充内容', exact: true });
  await note.fill('OLD_SUPPLEMENT_DO_NOT_KEEP_037');
  await until(async () => (await detail()).supplement?.body === 'OLD_SUPPLEMENT_DO_NOT_KEEP_037', 'First note did not save');
  await note.fill('Temporary current terms');
  await until(async () => (await detail()).supplement?.body === 'Temporary current terms', 'Replacement note did not save');
  await note.fill('');
  await until(async () => (await detail()).supplement?.body === '', 'Cleared note was not removed');
  await note.fill(latestNote);
  await until(async () => (await detail()).supplement?.body === latestNote, 'Latest note did not save');
  assert.equal((await detail()).conversation.human_owned, false);
  assert.equal(await page.getByRole('region', { name: '业务补充' }).getByRole('checkbox').count(), 0);
  report.supplement_latest_and_clear = true;

  await page.getByRole('checkbox', { name: '自动中文翻译', exact: true }).check();
  await invoke('check_ai');
  await until(async () => (await snapshot()).checks.ai.status === 'demo', 'Synthetic AI check did not complete');
  const editor = page.getByRole('textbox', { name: '回复正文', exact: true });
  await editor.fill(firstQuote);
  await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
  let suggestion;
  await until(async () => { suggestion = (await detail()).manual_suggestion; return suggestion?.mode === 'polish' && suggestion.status === 'ready' && suggestion.body === firstQuote; }, 'Current reply did not reach polish context');
  assert.equal(await editor.inputValue(), firstQuote);
  await page.getByRole('button', { name: '查看建议', exact: true }).click();
  await page.locator('.suggestion-preview .message-translation').getByText('每件15，不含运费。', { exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, 'polish-translation.png'), animations: 'disabled' });
  report.polish_uses_current_unsent_body = true;
  report.suggestion_translation_visible = true;
  await page.getByRole('button', { name: '采用到输入框', exact: true }).click();
  assert.equal(await editor.inputValue(), firstQuote);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await until(async () => (await detail()).messages.some(row => row.role === 'seller' && row.text === firstQuote), 'Synthetic send did not preserve original body');
  report.synthetic_sends = 1;
  report.translation_never_replaces_send_body = true;
  await until(async () => (await editor.inputValue()) === '', 'Accepted synthetic send did not clear its frozen input');

  await invoke('takeover', { conversation_id: cid, enabled: false });
  await invoke('set_scope', { mode: 'selected', conversation_ids: [cid], max_auto_replies: null });
  await invoke('set_reception', { enabled: true, auto_send: false });
  await invoke('inject_demo', { conversation_id: cid, text: '[image]' });
  await until(async () => { const value = await detail(); return value.draft?.photo_read_images?.length === 1 && value.draft.status === 'pending'; }, 'Business request did not read the original image');
  const imageDetail = await detail();
  const context = await invoke('get_context', { case_id: imageDetail.case.case_id });
  assert.equal(context.supplement.body, latestNote);
  assert.equal(context.photo_context.direct_images.length, 1);
  assert(!JSON.stringify(context).includes('original_b64'));
  const imageJob = (await snapshot()).jobs.filter(row => row.kind === 'generate_draft' && row.conversation_id === cid).sort((a, b) => b.created_at - a.created_at)[0];
  assert.equal(imageJob?.vision_calls_started ?? 0, 0);
  await page.getByRole('button', { name: '放大查看客户发送的照片', exact: true }).waitFor();
  report.original_image_in_business_request = true;
  report.separate_image_observation_calls = 0;
  report.automatic_context_has_latest_supplement = true;
  await invoke('set_reception', { enabled: false, auto_send: false });

  const widthHandle = page.getByRole('separator', { name: '调整右侧栏宽度', exact: true });
  const widthBox = await widthHandle.boundingBox();
  assert(widthBox);
  await page.mouse.move(widthBox.x + widthBox.width / 2, widthBox.y + Math.min(100, widthBox.height / 2));
  await page.mouse.down();
  await page.mouse.move(widthBox.x - 100, widthBox.y + Math.min(100, widthBox.height / 2), { steps: 8 });
  await page.mouse.up();
  await until(async () => (await page.evaluate(() => window.reception.getPreferences())).details_width >= 400, 'Right column did not resize');
  const heightHandle = page.getByRole('separator', { name: '调整采购需求与业务补充高度', exact: true });
  const heightBox = await heightHandle.boundingBox();
  assert(heightBox);
  await page.mouse.move(heightBox.x + heightBox.width / 2, heightBox.y + heightBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(heightBox.x + heightBox.width / 2, heightBox.y + 80, { steps: 8 });
  await page.mouse.up();
  await until(async () => (await page.evaluate(() => window.reception.getPreferences())).requirements_ratio > .60, 'Requirements split did not resize');
  const preferences = await page.evaluate(() => window.reception.getPreferences());
  await page.evaluate(value => window.reception.savePreferences(value), preferences);
  report.pointer_resizing_both_boundaries = true;

  await invoke('check_ai');
  await until(async () => (await snapshot()).checks.ai.status === 'demo', 'Final synthetic AI check did not complete');
  await editor.fill(finalQuote);
  await until(() => page.getByRole('button', { name: '润色当前回复', exact: true }).isEnabled(), 'Polish button did not reflect current AI check');
  await page.getByRole('button', { name: '润色当前回复', exact: true }).click();
  await until(async () => { suggestion = (await detail()).manual_suggestion; return suggestion?.body === finalQuote && suggestion.status === 'ready'; }, 'Final polish did not complete');
  if (await page.getByRole('button', { name: '查看建议', exact: true }).count()) await page.getByRole('button', { name: '查看建议', exact: true }).click();
  await page.locator('.suggestion-preview .message-translation').getByText('每件18，不含运费。', { exact: true }).waitFor();
  assert.equal(await editor.inputValue(), finalQuote);
  await page.bringToFront();
  await page.screenshot({ path: path.join(output, 'layout-and-images.png'), animations: 'disabled' });
  const item = { kind: 'manual_suggestion', source_id: suggestion.suggestion_id, source_hash: await page.evaluate(async text => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))].map(value => value.toString(16).padStart(2, '0')).join(''), finalQuote) };
  assert.equal((await invoke('get_translations', { conversation_id: cid, items: [item] })).items[0].text, '每件18，不含运费。');
  const owned = app.process();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => owned.exitCode !== null, 'First window did not close normally');
  assert.equal((await readFile(path.join(data, '.reception-clean-shutdown'), 'utf8')).trim(), 'reception-managed-clean-v1');
  app = null;

  const python = path.join(root, 'services/reception/.venv/Scripts/python.exe');
  const storageCheck = spawnSync(python, ['-I', '-c',
    "import sqlite3,sys,json; db=sqlite3.connect('file:'+sys.argv[1].replace('\\\\','/')+'?mode=ro',uri=True); rows=db.execute(\"SELECT data FROM entities WHERE kind IN ('conversation_supplement','supplement_mutation')\").fetchall(); assert not any('OLD_SUPPLEMENT_DO_NOT_KEEP_037' in row[0] for row in rows); assert db.execute(\"SELECT count(*) FROM revisions WHERE kind='conversation_supplement'\").fetchone()[0]==0; print(json.dumps({'latest_only':True})); db.close()",
    path.join(data, 'demo/workspace.sqlite3')], { cwd: root, encoding: 'utf8', env });
  assert.equal(storageCheck.status, 0, storageCheck.stderr);
  report.no_old_supplement_body_copies = true;

  app = await electron.launch(options);
  page = await app.firstWindow();
  page.on('pageerror', error => report.renderer_errors.push(error.message));
  await page.getByRole('textbox', { name: '回复正文', exact: true }).waitFor({ timeout: 45000 });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1600, 1000));
  await page.getByRole('button', { name: /Emma Wilson/ }).click();
  await until(async () => (await page.getByRole('textbox', { name: '业务补充内容', exact: true }).inputValue()) === latestNote, 'Current note did not reopen');
  assert.equal(await page.getByRole('textbox', { name: '回复正文', exact: true }).inputValue(), finalQuote);
  const reopenedPreferences = await page.evaluate(() => window.reception.getPreferences());
  assert.equal(reopenedPreferences.details_width, preferences.details_width);
  assert.equal(reopenedPreferences.requirements_ratio, preferences.requirements_ratio);
  const cached = await page.evaluate(({ cid, item }) => window.reception.invoke('get_translations', { conversation_id: cid, items: [item] }), { cid, item });
  assert.equal(cached.items[0].text, '每件18，不含运费。');
  assert.equal((await page.evaluate(() => window.reception.invoke('get_snapshot'))).reception.running, false);
  await page.getByRole('button', { name: '查看建议', exact: true }).click();
  await page.locator('.suggestion-preview .message-translation').getByText('每件18，不含运费。', { exact: true }).waitFor();
  await page.bringToFront();
  await page.screenshot({ path: path.join(output, 'reopened.png'), animations: 'disabled' });
  report.latest_note_reopened = true;
  report.unsent_input_reopened = true;
  report.suggestion_translation_cache_reopened = true;
  report.layout_preferences_reopened = true;
  report.restart_reception_paused = true;
  const reopenedProcess = app.process();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await until(() => reopenedProcess.exitCode !== null, 'Reopened window did not close normally');
  assert.equal((await readFile(path.join(data, '.reception-clean-shutdown'), 'utf8')).trim(), 'reception-managed-clean-v1');
  assert.deepEqual(report.renderer_errors, []);
  app = null;
  report.windows_closed = true;
  report.status = 'passed';
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
  if (page) try { await page.screenshot({ path: path.join(output, 'failure.png') }); await writeFile(path.join(output, 'failure.txt'), (await page.locator('body').innerText()).slice(0, 18000)); } catch {}
} finally {
  if (app) await app.close().catch(() => {});
  await writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
