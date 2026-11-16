import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import { extractFile } from '@electron/asar';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { lstat, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertReleasePath } from './release-paths.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--label', '--executable', '--expect-legacy', '--read-only-home', '--redirected-home', '--expect-seller-tail', '--expected-version'].includes(key) && value && !value.startsWith('--') && !options.has(key), 'Invalid arguments');
  options.set(key, value);
}
assert(!options.has('--expect-legacy') || options.get('--expect-legacy') === '1', 'Expected --expect-legacy 1');
const expectLegacy = options.has('--expect-legacy');
assert(!options.has('--read-only-home') || (options.get('--read-only-home') === '1' && expectLegacy), 'Read-only home requires --expect-legacy 1');
const readOnlyHome = options.has('--read-only-home');
const redirectedHome = options.get('--redirected-home');
const sellerTail = options.get('--expect-seller-tail');
assert(!redirectedHome && !sellerTail || (readOnlyHome && expectLegacy && redirectedHome && path.isAbsolute(redirectedHome) && !/^(?:\\\\|\/\/)/.test(redirectedHome) && /^[0-9]{4}$/.test(sellerTail || '')), 'Redirected home and seller tail require read-only legacy mode');
const label = options.get('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new --label');
const expectedVersion = options.get('--expected-version') || '0.4.0';
assert(['0.4.0', '0.4.1'].includes(expectedVersion), 'Expected version must be a supported launcher version');
assert(expectedVersion !== '0.4.1' || readOnlyHome, '0.4.1 actual entry verification requires read-only home mode');
const output = path.join(root, 'artifacts/verification', `launcher-entry-${expectedVersion.replaceAll('.', '')}-${label}`);
await mkdir(output);
const report = { status: 'failed', production_main: false, actual_exe_entry: false, no_model_calls: false, no_business_open: false, screenshots: [], checks: {}, timings_ms: {}, files: {}, limitations: ['Timings are launch-to-window observations, not OS cold-start or performance measurements.', 'No real authorization, customer session, or model quality is tested.', 'Child process and module checks are snapshots; no model-check or business-opening action is invoked.'] };
let app, temp, targets, before, legacyFiles, legacyBefore, ownedUserData;
async function readSafe(file, limit) {
  await checkParents(path.dirname(file));
  const stat = await lstat(file);
  assert(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1 && stat.size <= limit, 'Unsafe legacy metadata');
  const { open } = await import('node:fs/promises');
  const handle = await open(file, 'r');
  try {
    const actual = await handle.stat();
    assert(actual.isFile() && actual.nlink === 1 && actual.size <= limit && actual.ino === stat.ino && actual.dev === stat.dev, 'Legacy metadata changed');
    const bytes = await handle.readFile(); assert(bytes.length <= limit, 'Legacy metadata exceeds budget'); return bytes;
  } finally { await handle.close(); }
}
async function legacySnapshot(files) {
  return Promise.all(files.map(async ({ file, limit }) => createHash('sha256').update(await readSafe(file, limit)).digest('hex')));
}
async function fileState(file) {
  try {
    const stat = await lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 1024 * 1024) return { exists: true, unsafe: true };
    const data = await readFile(file);
    return { exists: true, sha256: createHash('sha256').update(data).digest('hex') };
  } catch (error) { if (error.code === 'ENOENT') return { exists: false }; throw error; }
}
async function metadataState(file) {
  await checkParents(path.dirname(file));
  try {
    const stat = await lstat(file);
    assert(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1 && stat.size <= 1024 * 1024, 'Unsafe model metadata');
    return { exists: true, dev: stat.dev, ino: stat.ino, size: stat.size, nlink: stat.nlink, mtimeMs: stat.mtimeMs, ctimeMs: stat.ctimeMs };
  } catch (error) { if (error.code === 'ENOENT') return { exists: false }; throw error; }
}
async function homeState() {
  return { settings: await metadataState(targets.settings), initialized: await metadataState(targets.initialized), registry: expectedVersion === '0.4.1' ? await fileState(targets.registry) : (await assertAbsent(targets.registry), { exists: false }) };
}
async function assertAbsent(file) {
  try { await lstat(file); throw new Error('Blocked: existing global model or launcher state; no content was read.'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
async function checkParents(directory) {
  const base = path.parse(directory).root;
  let current = base;
  for (const part of directory.slice(base.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { const s = await lstat(current); assert(s.isDirectory() && !s.isSymbolicLink(), 'Global state path contains a link or non-directory'); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
  }
}
try {
  assert.equal(process.platform, 'win32', 'Windows is required');
  temp = await mkdtemp(path.join(os.tmpdir(), 'arw-launcher-entry-040-'));
  // Execute the same KnownFolder resolver as the production store, bundled in memory.
  const bundled = await build({ entryPoints: [path.join(root, 'apps/desktop/src/main/model-settings-store.ts')], bundle: true, platform: 'node', format: 'cjs', target: 'node22', write: false });
  const resolver = path.join(temp, 'known-folder.cjs');
  await writeFile(resolver, bundled.outputFiles[0].contents, { flag: 'wx' });
  const modelDirectory = await createRequire(import.meta.url)(resolver).resolveModelSettingsDirectory();
  const registryDirectory = path.join(path.dirname(modelDirectory), 'launcher');
  await checkParents(modelDirectory); await checkParents(registryDirectory);
  targets = { settings: path.join(modelDirectory, 'settings.json'), initialized: path.join(modelDirectory, '.initialized'), registry: path.join(registryDirectory, 'merchants.json') };
  if (readOnlyHome) {
    before = await homeState();
    if (redirectedHome) assert(before.settings.exists && !before.settings.unsafe, 'Existing model settings file required');
    report.limitations.push('Model page was not opened; existing model files were checked by lstat metadata only, without reading or hashing contents.');
  } else {
    for (const file of Object.values(targets)) await assertAbsent(file);
    before = Object.fromEntries(Object.keys(targets).map(key => [key, { exists: false }]));
  }
  report.files.before = before;
  if (expectLegacy) {
    const local = path.dirname(path.dirname(modelDirectory));
    if (!redirectedHome) assert(process.env.LOCALAPPDATA && path.resolve(process.env.LOCALAPPDATA).toLowerCase() === local.toLowerCase(), 'Legacy mode requires standard LocalAppData');
    if (expectedVersion === '0.4.0') await assertAbsent(path.join(path.dirname(modelDirectory), 'launcher-activation'));
    const directories = [...new Set([path.join(local, 'AlibabaReceptionWorkbenchHost'), ...(redirectedHome ? [path.join(path.resolve(redirectedHome), 'AppData', 'Local', 'AlibabaReceptionWorkbenchHost')] : [])])];
    legacyFiles = [];
    for (const legacyDir of directories) {
      const legacyIndex = path.join(legacyDir, 'merchants.json');
      let bytes;
      try { bytes = await readSafe(legacyIndex, 1024 * 1024); }
      catch (error) { if (redirectedHome && legacyDir === directories[0] && error.code === 'ENOENT') continue; throw error; }
      const data = JSON.parse(bytes.toString('utf8'));
      assert(data?.format === 1 && data.profiles && typeof data.profiles === 'object' && !Array.isArray(data.profiles) && Object.keys(data.profiles).length <= 512, 'Invalid legacy index');
      const start = legacyFiles.length;
      legacyFiles.push({ file: legacyIndex, limit: 1024 * 1024 });
      for (const seller of Object.keys(data.profiles)) {
        assert(/^[1-9][0-9]{0,127}$/.test(seller), 'Invalid legacy profile');
        legacyFiles.push({ file: path.join(legacyDir, 'profiles', createHash('sha256').update(seller).digest('hex'), 'state', 'storage.json'), limit: 65536 });
      }
      assert(legacyFiles.length > start + 1, 'No legacy profiles');
      assert.equal(createHash('sha256').update(await readSafe(legacyIndex, 1024 * 1024)).digest('hex'), createHash('sha256').update(bytes).digest('hex'), 'Legacy index changed during snapshot');
    }
    legacyBefore = await legacySnapshot(legacyFiles);
    report.files.legacy_before = legacyBefore;
  }
  const candidate = options.has('--executable');
  const executable = candidate ? assertReleasePath(root, options.get('--executable')) : path.join(root, 'node_modules/electron/dist/electron.exe');
  assert((await lstat(executable)).isFile() && /\.exe$/i.test(executable), 'Executable must be an existing EXE');
  const archive = candidate ? path.join(path.dirname(executable), 'resources', 'app.asar') : null;
  if (archive) assertReleasePath(root, archive);
  const manifest = candidate ? JSON.parse(extractFile(archive, 'package.json').toString('utf8')) : JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  assert.equal(manifest.version, expectedVersion, 'Target package version mismatch');
  if (candidate) assert.equal(manifest.main, 'main/index.js', 'Candidate must use production main');
  const entry = path.join(root, 'artifacts/app/main/index.js');
  if (!candidate) assert((await lstat(entry)).isFile(), 'Production main build is missing');
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/accio|phoenix|reception|credential|secret|token|password|key/i.test(key) || /^(ELECTRON_RUN_AS_NODE|NODE_OPTIONS|NODE_PATH|PYTHONPATH|PYTHONHOME)$/i.test(key)) delete env[key];
  if (redirectedHome) {
    const home = path.resolve(redirectedHome);
    await checkParents(home);
    Object.assign(env, { USERPROFILE: home, LOCALAPPDATA: path.join(home, 'AppData', 'Local'), APPDATA: path.join(home, 'AppData', 'Roaming') });
    report.limitations.push('Redirected environment simulates Accio; no real Accio host connection or model readiness is tested.');
  }
  const userData = expectedVersion === '0.4.1' ? path.join(path.dirname(modelDirectory), 'launcher-host') : path.join(temp, 'electron-user-data');
  if (expectedVersion === '0.4.1') {
    await checkParents(userData);
    try { const stat = await lstat(userData); assert(stat.isDirectory() && !stat.isSymbolicLink(), 'Unsafe native launcher cache'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; await mkdir(userData, { recursive: true }); ownedUserData = userData; }
    report.limitations.push('0.4.1 uses its fixed native launcher cache namespace. Existing cache is used only by the normal application and never copied, read by this driver, removed or replaced. Existing model files are checked by lstat only; launcher routing index is hashed read-only.');
  } else await mkdir(userData);
  const launch = () => electron.launch({ executablePath: executable, args: [...(candidate ? [] : [root]), `--user-data-dir=${userData}`], cwd: root, env, timeout: 45000 });
  const inspect = async () => {
    const snapshot = () => app.evaluate(async ({ app }) => {
      await new Promise(resolve => setImmediate(resolve));
      const children = process._getActiveHandles().filter(x => x?.constructor?.name === 'ChildProcess' && x.exitCode === null && x.signalCode === null);
      return { version: app.getVersion(), userData: app.getPath('userData'), packaged: app.isPackaged, exe: process.execPath, modules: Object.keys(process.getBuiltinModule('module')._cache).filter(x => /(?:whatsapp-web\.js|puppeteer|whatsapp-runtime\.asar)/i.test(x)), children: children.length, child_programs: children.map(child => process.getBuiltinModule('path').basename(child.spawnfile || '')) };
    });
    let value = await snapshot();
    const settleUntil = Date.now() + 15000;
    while (value.children && Date.now() < settleUntil) {
      assert(value.child_programs.every(program => program.toLowerCase() === 'powershell.exe'), 'Unexpected startup child process');
      await new Promise(resolve => setTimeout(resolve, 50));
      value = await snapshot();
    }
    assert.equal(value.version, expectedVersion); assert.equal(value.packaged, candidate);
    assert.equal(path.resolve(value.exe).toLowerCase(), path.resolve(executable).toLowerCase());
    assert.equal(path.resolve(value.userData).toLowerCase(), path.resolve(userData).toLowerCase(), 'Electron cache was not isolated');
    assert.deepEqual(value.modules, [], 'WhatsApp SDK was loaded'); assert.equal(value.children, 0, `Main has active child programs: ${value.child_programs.join(', ')}`);
    return value;
  };
  for (const run of ['first', 'restart']) {
    if (readOnlyHome) assert.deepEqual(await homeState(), before, 'Home metadata changed before launch');
    else for (const file of Object.values(targets)) await assertAbsent(file);
    if (expectLegacy) {
      if (expectedVersion === '0.4.0') await assertAbsent(path.join(path.dirname(modelDirectory), 'launcher-activation'));
      assert.deepEqual(await legacySnapshot(legacyFiles), legacyBefore, 'Legacy metadata changed before launch');
    }
    const started = Date.now(); app = await launch();
    const child = app.process();
    const page = await app.firstWindow();
    if (expectLegacy) await (expectedVersion === '0.4.1' ? page.locator('.account-avatar.international') : page.getByRole('radio')).first().waitFor({ timeout: 45000 });
    else await page.getByText('添加第一个商家').waitFor({ timeout: 45000 });
    report.timings_ms[run] = Date.now() - started;
    // Wait for real launcher IPC, including the asynchronous KnownFolder resolver.
    const merchants = await page.evaluate(() => window.launcher.merchants());
    if (expectLegacy) {
      assert(merchants.length > 0 && merchants.every(m => expectedVersion === '0.4.1' ? Number.isSafeInteger(m.verified_at) && m.verified_at >= 0 && ['reauthorize', 'opened'].includes(m.status) : m.verified_at === 0 && m.status === 'reauthorize'), 'Expected valid read-only merchant routing entries');
      if (sellerTail) {
        assert(merchants.some(m => m.account_tail === sellerTail && m.verified_at === 0 && m.status === 'reauthorize'), 'Expected seller is missing');
        if (expectedVersion === '0.4.1') await page.locator('.account-card').filter({ hasText: sellerTail }).first().scrollIntoViewIfNeeded();
        else await page.getByRole('radio', { name: new RegExp(`商家 ${sellerTail}`) }).first().click();
        report.checks[`${run}_expected_seller_visible`] = true;
      }
      report.checks[`${run}_merchant_count`] = merchants.length;
      report.checks[`${run}_merchant_status`] = expectedVersion === '0.4.1' ? [...new Set(merchants.map(m => m.status))] : 'reauthorize';
      report.checks[`${run}_verified_at`] = expectedVersion === '0.4.1' ? 'original_value_preserved' : 0;
      assert.equal(await (expectedVersion === '0.4.1' ? page.locator('.account-avatar.international') : page.getByRole('radio')).count(), merchants.length);
    } else assert.deepEqual(merchants, []);
    await inspect();
    report.timings_ms[`${run}_ready`] = Date.now() - started;
    assert.deepEqual(await page.evaluate(() => ({ reception: typeof window.reception, launcher: typeof window.launcher })), { reception: 'undefined', launcher: 'object' });
    if (run === 'first') {
      report.production_main = true; report.actual_exe_entry = candidate;
      if (readOnlyHome) {
        await page.bringToFront();
        await page.screenshot({ path: path.join(output, '01-home.png'), animations: 'disabled' }); report.screenshots.push('01-home.png');
        if (expectedVersion === '0.4.1') {
          await page.locator('.account-card').filter({ has: page.locator('.account-avatar.international') }).first().getByRole('button', { name: /^编辑/ }).click();
          await page.getByRole('dialog', { name: '账号设置' }).waitFor();
        } else {
          await page.getByRole('button', { name: '商家详情', exact: true }).click();
          await page.getByRole('dialog', { name: '商家详情' }).waitFor();
        }
        await inspect();
        await page.screenshot({ path: path.join(output, '02-details.png'), animations: 'disabled' }); report.screenshots.push('02-details.png');
      } else {
        await page.screenshot({ path: path.join(output, '01-empty-launcher.png'), animations: 'disabled' }); report.screenshots.push('01-empty-launcher.png');
        await page.getByRole('button', { name: '模型配置', exact: true }).click();
        await page.getByText('还没有 API 配置，可先添加后选择用途。').waitFor();
        assert.equal(await page.getByLabel('接待 API').inputValue(), ''); assert.equal(await page.getByLabel('翻译 API').inputValue(), '');
        await inspect();
        await page.screenshot({ path: path.join(output, '02-empty-models.png'), animations: 'disabled' }); report.screenshots.push('02-empty-models.png');
      }
    }
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
    await new Promise((resolve, reject) => {
      if (child.exitCode !== null) return child.exitCode === 0 ? resolve() : reject(new Error(`App exited ${child.exitCode}`));
      const timer = setTimeout(() => reject(new Error('App did not exit normally')), 15000);
      child.once('exit', (code, signal) => { clearTimeout(timer); code === 0 && !signal ? resolve() : reject(new Error(`App exited ${code}/${signal}`)); });
    });
    report.checks[`${run}_normal_exit`] = true;
    app = undefined;
  }
  Object.assign(report.checks, { empty_launcher: !expectLegacy, legacy_launcher: expectLegacy, empty_models: !readOnlyHome, model_page_not_opened: readOnlyHome, no_reception_bridge: true, no_active_business_child: true, no_whatsapp_sdk_in_module_cache: true, normal_exit_and_restart: true, isolated_electron_user_data: expectedVersion === '0.4.0', native_launcher_cache_namespace: expectedVersion === '0.4.1' });
  report.no_model_calls = true; report.no_business_open = true; report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally {
  if (app) await app.close().catch(() => {});
  if (targets && before) {
    try {
      report.files.after = readOnlyHome ? await homeState() : Object.fromEntries(await Promise.all(Object.entries(targets).map(async ([key, file]) => [key, await fileState(file)])));
      if (JSON.stringify(before) !== JSON.stringify(report.files.after)) { report.status = 'failed'; report.error = [report.error, 'Global target files changed during verification.'].filter(Boolean).join('\n'); process.exitCode = 1; }
    } catch (error) { report.status = 'failed'; report.error = [report.error, `Unable to verify global files: ${error.message}`].filter(Boolean).join('\n'); process.exitCode = 1; }
  }
  if (legacyFiles && legacyBefore) {
    try {
      report.files.legacy_after = await legacySnapshot(legacyFiles);
      assert.deepEqual(report.files.legacy_after, legacyBefore, 'Legacy metadata changed during verification');
      if (expectedVersion === '0.4.0') await assertAbsent(path.join(path.dirname(targets.registry), '..', 'launcher-activation'));
    } catch (error) { report.status = 'failed'; report.error = [report.error, `Legacy verification failed: ${error.message}`].filter(Boolean).join('\n'); process.exitCode = 1; }
  }
  if (ownedUserData && report.status === 'passed') {
    try { await rm(ownedUserData, { recursive: true, maxRetries: 5, retryDelay: 100 }); report.checks.owned_new_launcher_cache_removed = true; }
    catch { report.status = 'failed'; report.error = 'Unable to remove the newly created launcher verification cache.'; process.exitCode = 1; }
  }
  if (temp) await rm(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }).catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
}
console.log(JSON.stringify(report, null, 2));
