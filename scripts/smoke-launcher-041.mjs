import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const label = process.argv[3];
assert(process.argv.length === 4 && process.argv[2] === '--label' && /^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const output = path.join(root, 'artifacts/verification', `launcher-041-${label}`);
await mkdir(output);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-launcher-041-'));
const virtualLocal = path.join(temporary, 'local');
await mkdir(virtualLocal);
const assets = path.join(root, 'artifacts/app');
const env = {};
for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|USERPROFILE|USERNAME|USERDOMAIN|COMPUTERNAME|SESSIONNAME|OS|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|ALLUSERSPROFILE|PUBLIC|HOMEDRIVE|HOMEPATH)$/i.test(key)) env[key] = value;
Object.assign(env, { LOCALAPPDATA: virtualLocal, APPDATA: path.join(temporary, 'roaming'), HARNESS_TEMP: temporary });
await mkdir(env.APPDATA);
const shim = path.join(temporary, 'known-folder.ts');
await writeFile(shim, `export async function resolveWindowsUserLocalAppData() { return ${JSON.stringify(virtualLocal)}; }\n`, { flag: 'wx' });
const entry = path.join(temporary, 'host.cjs');
await build({ entryPoints: [path.join(root, 'scripts/fixtures/launcher-host-041.ts')], outfile: entry, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron', 'whatsapp-web.js', 'puppeteer'], define: { __dirname: JSON.stringify(path.join(assets, 'main')) }, plugins: [{ name: 'isolated-known-folder', setup(builder) {
  builder.onResolve({ filter: /(?:^|\/)windows-user-directories$/ }, args => {
    // Keep real installed-Chrome discovery available. All application metadata
    // and model directories use only this fixture's empty user root.
    if (args.importer.replaceAll('\\', '/').endsWith('/whatsapp/chrome-host.ts')) return;
    return { path: shim };
  });
} }] });
const report = { status: 'failed', production_launcher_host: true, production_preload_and_renderer: true, production_account_service: true, actual_release_exe: false, known_folder_isolated: true, synthetic_merchants: true, model_calls: 0, chrome_connected: false, screenshots: [], checks: {} };
let app;
const launch = () => electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [entry], cwd: root, env, timeout: 45000 });
const screenshot = async (page, name) => { await page.screenshot({ path: path.join(output, name), animations: 'disabled' }); report.screenshots.push(name); };
try {
  app = await launch();
  let page = await app.firstWindow();
  await page.getByText('添加第一个商家').waitFor();
  await screenshot(page, '01-empty-home.png');
  await page.getByRole('button', { name: 'WhatsApp', exact: true }).click();
  await page.getByText('还没有账号环境，请先创建。').waitFor();
  await screenshot(page, '02-empty-accounts.png');
  await page.getByRole('button', { name: '新建环境', exact: true }).click();
  await page.getByLabel('环境名称').fill('隔离商务账号');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByRole('button', { name: '连接真实 Chrome', exact: true }).waitFor();
  await page.getByText('接待模型尚未就绪', { exact: false }).waitFor();
  assert.equal((await page.evaluate(() => window.launcher.getWhatsAppAccounts())).state.enabled, false);
  await screenshot(page, '03-account-without-merchant.png');
  report.checks.no_merchant_account_service = true;
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), false);
  report.checks.close_hides_to_tray = true;
  const secondProcess = spawn(path.join(root, 'node_modules/electron/dist/electron.exe'), [entry], { cwd: root, env, stdio: 'ignore', windowsHide: true });
  await Promise.race([new Promise(resolve => secondProcess.once('exit', resolve)), new Promise((_, reject) => setTimeout(() => reject(new Error('second launcher did not exit')), 15000))]);
  assert.equal(secondProcess.exitCode, 0);
  await page.waitForFunction(() => document.visibilityState === 'visible');
  report.checks.single_instance_restores_original = true;
  await page.getByRole('button', { name: '返回', exact: true }).click();
  await app.evaluate(() => globalThis.seedCompanyNames());
  await page.getByRole('radio', { name: /合成公司甲/ }).waitFor({ timeout: 20000 });
  await screenshot(page, '04-company-names.png');
  await page.getByRole('radio', { name: /合成公司乙/ }).click();
  await page.getByRole('button', { name: '商家详情', exact: true }).click();
  await page.getByRole('dialog', { name: '商家详情' }).waitFor();
  await screenshot(page, '05-long-company-details.png');
  await page.keyboard.press('Escape');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(440, 559));
  await page.waitForFunction(() => innerWidth <= 440);
  assert(await page.evaluate(() => [...document.querySelectorAll('footer button')].every(button => {
    const r = button.getBoundingClientRect();
    return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
  })), 'narrow footer must keep every action visible');
  await screenshot(page, '06-narrow-home.png');
  await page.getByRole('button', { name: 'WhatsApp', exact: true }).click();
  await page.getByRole('button', { name: '连接真实 Chrome', exact: true }).waitFor();
  await screenshot(page, '07-narrow-account.png');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && [...document.querySelectorAll('.wa-scroll')].every(node => node.scrollWidth <= node.clientWidth)), 'account management must not overflow horizontally');
  await page.locator('.wa-scroll').evaluate(node => { node.scrollTop = node.scrollHeight; });
  await screenshot(page, '08-narrow-account-controls.png');
  report.checks.company_titles_and_narrow_layout = true;
  const owned = app.process();
  const exited = new Promise(resolve => owned.once('exit', resolve));
  await app.evaluate(() => globalThis.quitProbe());
  await exited;
  assert.equal(owned.exitCode, 0);
  app = undefined;
  report.checks.normal_shutdown = true;
  report.status = 'passed';
} catch (error) { report.error = error instanceof Error ? error.stack : String(error); process.exitCode = 1; }
finally {
  if (app) { await app.evaluate(() => globalThis.quitProbe()).catch(() => {}); await app.close().catch(() => {}); }
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report));
