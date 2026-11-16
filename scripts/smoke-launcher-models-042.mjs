import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
assert.equal(process.argv[2], '--label');
const label = process.argv[3];
assert(process.argv.length === 4 && /^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''));
const output = path.join(root, 'artifacts/verification', `launcher-models-042-${label}`);
await mkdir(output);
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-model-window-042-'));
const local = path.join(temp, 'local'); await mkdir(local);
const known = path.join(temp, 'known.ts');
const encryption = path.join(temp, 'encryption.ts');
const checker = path.join(temp, 'checker.ts');
await writeFile(known, `export async function resolveWindowsUserLocalAppData() { console.error('isolated local directory resolved'); return ${JSON.stringify(local)}; }\n`);
await writeFile(encryption, `export function createWindowsUserEncryption() { return { isEncryptionAvailable: () => true, encryptString: async (s: string) => Buffer.from(s, 'utf8'), decryptString: async (b: Buffer) => b.toString('utf8') }; }\n`);
await writeFile(checker, `export class ModelCheckHost { constructor(_options: unknown) {} check() { (globalThis as any).forbiddenModelChecks = ((globalThis as any).forbiddenModelChecks || 0) + 1; throw new Error('Isolated fixture blocks model checks'); } cancel() {} close() {} }\n`);
const host = path.join(temp, 'host.cjs');
await build({ entryPoints: [path.join(root, 'scripts/fixtures/launcher-revision-041.ts')], outfile: host, bundle: true, platform: 'node', format: 'cjs', target: 'node22', banner: { js: `require('electron').app.setPath('userData', ${JSON.stringify(path.join(temp, 'bootstrap-profile'))});` }, external: ['electron', 'whatsapp-web.js', 'puppeteer'], define: { __dirname: JSON.stringify(path.join(root, 'artifacts/app/main')) }, plugins: [{ name: 'isolated-model-window', setup(b) {
  b.onLoad({ filter: /launcher-host\.ts$/ }, async args => ({ contents: (await readFile(args.path, 'utf8')).replace('await mkdir(userData, { recursive: true });', "console.error('fixture before mkdir'); await mkdir(userData, { recursive: true }); console.error('fixture after mkdir');").replace('await app.whenReady();', "console.error('fixture before ready'); await app.whenReady(); console.error('fixture after ready');"), loader: 'ts' }));
  b.onResolve({ filter: /(?:^|\/)controlled-chrome-041$/ }, () => ({ path: path.join(root, 'scripts/fixtures/launcher-supplement-042.ts') }));
  b.onResolve({ filter: /(?:^|\/)whatsapp\/chrome-host$/ }, args => args.importer.replaceAll('\\', '/').endsWith('/launcher-supplement-042.ts') ? undefined : { path: path.join(root, 'scripts/fixtures/current-chat-registration-041.ts') });
  b.onResolve({ filter: /(?:^|\/)windows-user-directories$/ }, () => ({ path: known }));
  b.onResolve({ filter: /(?:^|\/)windows-user-encryption$/ }, () => ({ path: encryption }));
  b.onResolve({ filter: /(?:^|\/)model-check-host$/ }, () => ({ path: checker }));
} }] });
const env = {};
for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|TEMP|TMP|USERPROFILE|USERNAME|USERDOMAIN|COMPUTERNAME|SESSIONNAME|OS|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|ALLUSERSPROFILE|PUBLIC|HOMEDRIVE|HOMEPATH)$/i.test(key)) env[key] = value;
Object.assign(env, { LOCALAPPDATA: local, APPDATA: path.join(temp, 'roaming'), HARNESS_TEMP: temp, HARNESS_SDK_ENTRY: path.join(root, 'artifacts/app/main/index.js') });
await mkdir(env.APPDATA, { recursive: true });
const report = { status: 'failed', production_launcher_and_model_store: true, known_folder_isolated: true, synthetic_plaintext_encryption: true, model_checker_blocked: true, model_checks: 0, screenshots: [], checks: {} };
let app;
try {
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env, timeout: 90000 });
  const page = await app.firstWindow();
  const shot = async name => { await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); await page.screenshot({ path: path.join(output, name) }); report.screenshots.push(name); };
  await page.getByRole('button', { name: /模型设置/ }).click();
  await page.getByRole('heading', { name: '共享模型' }).waitFor();
  await shot('01-model-page-empty.png');
  await page.getByRole('button', { name: /添加 API/ }).click();
  const editor = page.getByRole('dialog', { name: '添加 API' });
  await editor.waitFor(); await shot('02-add-api.png');
  await editor.getByLabel('名称', { exact: true }).fill('合成接待 API');
  await editor.getByLabel('地址', { exact: true }).fill('https://api.example.invalid/v1');
  await editor.getByLabel('模型名', { exact: true }).fill('synthetic-model');
  await editor.locator('input[type="password"]').fill('synthetic-key-only');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  await page.getByText('配置已保存。').waitFor();
  await page.getByText('合成接待 API', { exact: true }).waitFor();
  report.checks.synthetic_api_saved_in_temporary_store = true;
  await shot('03-model-page-configured.png');
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  let edit = page.getByRole('dialog', { name: '编辑 API' }); await edit.waitFor();
  await shot('04-edit-api.png');
  await edit.locator('summary').click();
  await edit.getByRole('button', { name: '检查接待' }).click();
  let confirm = page.getByRole('dialog', { name: '模型能力检查' }); await confirm.waitFor();
  await shot('05-capability-fee-confirmation.png');
  await confirm.getByRole('button', { name: '取消' }).click();
  assert.equal(await app.evaluate(() => globalThis.forbiddenModelChecks || 0), 0);
  report.checks.capability_confirmation_cancelled_without_model_call = true;
  await edit.getByRole('button', { name: '删除 API' }).click();
  confirm = page.getByRole('dialog', { name: '删除 API' }); await confirm.waitFor();
  await shot('06-delete-confirmation.png');
  await confirm.getByRole('button', { name: '取消' }).click();
  await edit.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByLabel('接待模型').selectOption({ label: '合成接待 API · synthetic-model' });
  await page.getByRole('button', { name: '保存配置' }).click();
  confirm = page.getByRole('dialog', { name: '保存模型配置' }); await confirm.waitFor();
  await shot('07-save-impact-confirmation.png');
  await confirm.getByRole('button', { name: '取消' }).click();
  assert.equal((await page.evaluate(() => window.launcher.modelSettings())).routes.reception.profile_id, null);
  report.checks.route_impact_confirmation_cancelled = true;
  await page.getByRole('button', { name: '保存配置' }).click();
  confirm = page.getByRole('dialog', { name: '保存模型配置' });
  await confirm.getByRole('button', { name: '继续保存' }).click();
  await page.getByText('配置已保存。').waitFor();
  report.checks.synthetic_route_saved_after_confirmation = true;
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  edit = page.getByRole('dialog', { name: '编辑 API' });
  await edit.getByLabel('名称', { exact: true }).fill('合成接待 API 待编辑');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  confirm = page.getByRole('dialog', { name: '还有内容未保存' }); await confirm.waitFor();
  await shot('08-unsaved-close-confirmation.png');
  await confirm.getByRole('button', { name: '继续编辑' }).click();
  assert.equal(await edit.getByLabel('名称', { exact: true }).inputValue(), '合成接待 API 待编辑');
  assert(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()));
  report.checks.unsaved_close_retains_editor_and_input = true;
  await shot('09-continued-editing.png');
  await edit.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await app.evaluate(() => globalThis.forbiddenModelChecks || 0), 0);
  report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally {
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  if (app) {
    // The fixture owns no account, model worker, or business data. Record first,
    // then exit the isolated process instead of entering another unsaved dialog.
    await app.evaluate(({ app }) => app.exit(0)).catch(() => {});
    await app.close().catch(() => {});
  }
}
console.log(JSON.stringify(report));
