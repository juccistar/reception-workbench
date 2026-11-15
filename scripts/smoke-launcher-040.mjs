import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
for (let i = 2; i < process.argv.length; i += 2) assert(['--label', '--assets'].includes(process.argv[i]) && process.argv[i + 1] && !process.argv[i + 1].startsWith('--'), 'Only --label and --assets are supported; actual EXE entry is not implemented');
const index = process.argv.indexOf('--label');
const label = process.argv[index + 1];
assert(index >= 0 && /^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const output = path.join(root, 'artifacts', 'verification', `launcher-040-${label}`);
await mkdir(output);
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-launcher-040-'));
const assetsIndex = process.argv.indexOf('--assets');
const assets = assetsIndex < 0 ? path.join(root, 'artifacts/app') : path.resolve(process.argv[assetsIndex + 1]);
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION|HARNESS)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
Object.assign(env, { HARNESS_ROOT: root, HARNESS_TEMP: temp, HARNESS_ASSETS: assets, LOCALAPPDATA: path.join(temp, 'local'), APPDATA: path.join(temp, 'roaming') });
const report = { status: 'failed', actual_exe_entry: false, candidate_assets: assetsIndex >= 0, production_launcher_host: false, production_launcher_preload: true, production_renderer: true, synthetic_registry: true, synthetic_model_checker: true, synthetic_encryption: true, model_network_calls: 0, screenshots: [], checks: {} };
let app;
try {
  const fixture = path.join(temp, 'host.cjs');
  await build({ entryPoints: [path.join(root, 'scripts/fixtures/launcher-host-040.ts')], outfile: fixture, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] });
  const launch = () => electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [fixture], cwd: root, env, timeout: 45000 });
  const screenshot = async (page, name) => { await page.screenshot({ path: path.join(output, name), animations: 'disabled' }); report.screenshots.push(name); };
  app = await launch(); let page = await app.firstWindow();
  await page.getByText('添加第一个商家').waitFor(); await screenshot(page, '01-empty.png'); report.checks.empty = true;
  await app.close(); app = undefined; env.HARNESS_MERCHANTS = '1';
  app = await launch(); page = await app.firstWindow();
  await page.getByRole('radio', { name: /合成商家甲/ }).waitFor();
  await page.getByRole('radio', { name: /商家 0001/ }).waitFor();
  const legacy = (await page.evaluate(() => window.launcher.merchants())).filter(m => m.verified_at === 0);
  assert.equal(legacy.length, 1); assert.equal(legacy[0].status, 'reauthorize');
  await screenshot(page, '02-merchants.png'); report.checks.merchants = true;
  await app.evaluate(() => globalThis.addSyntheticLegacy('30001'));
  await page.waitForFunction(() => document.querySelectorAll('[role=radio]').length >= 4, null, { timeout: 16000 });
  const refreshed = (await page.evaluate(() => window.launcher.merchants())).filter(m => m.verified_at === 0);
  assert.equal(refreshed.length, 2); assert(refreshed.every(m => m.status === 'reauthorize'));
  assert.equal(await page.getByRole('radio', { name: /商家 0001/ }).count(), 2);
  await screenshot(page, '05-legacy-refresh.png');
  await page.getByRole('radio', { name: /商家 0001/ }).first().click();
  await page.getByRole('button', { name: '商家详情' }).click();
  await page.getByText('上次核验：未提供').waitFor(); await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重新授权', exact: true }).click();
  await page.getByText('合成授权说明').waitFor();
  assert.equal(await app.evaluate(() => process._getActiveHandles().filter(x => x?.constructor?.name === 'ChildProcess' && x.exitCode === null).length), 0);
  await page.keyboard.press('Escape'); report.checks.legacy_refresh_and_authorization = true;
  await page.getByRole('button', { name: '模型配置', exact: true }).click();
  async function add(name, protocol) {
    await page.getByRole('button', { name: '＋ 添加 API' }).click();
    const dialog = page.getByRole('dialog', { name: '编辑 API' });
    await dialog.getByLabel('配置名称').fill(name); await dialog.getByLabel('接口协议').selectOption(protocol);
    await dialog.getByLabel('服务地址').fill('https://synthetic.invalid/v1'); await dialog.getByLabel('模型 ID').fill('synthetic-model');
    await dialog.getByLabel('API Key').fill('synthetic-key-only'); await dialog.getByRole('button', { name: '完成' }).click();
  }
  await add('合成 API 甲', 'chat-completions'); await add('合成 API 乙', 'anthropic');
  await page.getByLabel('接待 API').selectOption({ label: '合成 API 甲' }); await page.getByLabel('翻译 API').selectOption({ label: '合成 API 乙' });
  page.once('dialog', dialog => dialog.accept()); await page.getByRole('button', { name: '保存配置' }).click();
  await page.getByText('配置已保存。').waitFor(); await screenshot(page, '03-models.png'); report.checks.multiple_profiles_and_routes = true;
  await page.getByRole('button', { name: '检查接待' }).first().click(); await page.getByRole('button', { name: '取消检查' }).click();
  await page.getByText(/模型检查已取消/).waitFor(); report.checks.cancel = true;
  await page.getByRole('button', { name: '检查翻译' }).last().click(); await page.getByText('检查已结束，请查看各项能力状态。').waitFor();
  await screenshot(page, '04-checked.png'); report.checks.synthetic_check = true;
  const stale = await page.evaluate(async () => {
    const settings = await window.launcher.modelSettings();
    const profile = settings.profiles[0];
    const pending = window.launcher.checkModel(profile.profile_id, 'reception').then(() => 'unexpected_success', () => 'rejected');
    await new Promise(resolve => setTimeout(resolve, 100));
    await window.launcher.saveModelSettings({ expected_directory_revision: settings.directory_revision, profiles: settings.profiles.map(p => ({ profile_id: p.profile_id, name: p.name, protocol: p.protocol, base_url: p.base_url, model: p.profile_id === profile.profile_id ? 'synthetic-changed' : p.model, credential_ref: p.credential_ref })), routes: { reception: settings.routes.reception.profile_id, translation: settings.routes.translation.profile_id }, credential_changes: {} });
    return pending;
  });
  assert.equal(stale, 'rejected'); report.checks.stale_check_rejected = true;
  await app.close(); app = undefined; report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally { if (app) await app.close().catch(() => {}); await writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' }); }
console.log(JSON.stringify(report));
