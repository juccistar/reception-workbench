import { _electron as electron } from 'playwright';
import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
assert(process.argv.length === 4 && process.argv[2] === '--label' && /^[a-z0-9][a-z0-9-]{0,60}$/.test(process.argv[3]), 'Pass a new --label');
const output = path.join(root, 'artifacts/verification', `transparent-images-041-${process.argv[3]}`);
await mkdir(output);
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-transparent-041-'));
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION|HARNESS)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
Object.assign(env, { HARNESS_ROOT: root, HARNESS_TEMP: temp, HARNESS_ASSETS: path.join(root, 'artifacts/app'), HARNESS_PYTHON: path.join(root, 'services/reception/.venv/Scripts/python.exe'), LOCALAPPDATA: path.join(temp, 'local'), APPDATA: path.join(temp, 'roaming') });
const report = { status: 'failed', production_renderer_preload: true, production_service_client_and_image_rpc: true, synthetic_host_and_merchant: true, legacy_white_preview_seeded: true, actual_exe_entry: false, actual_window: false, real_data_accessed: false, real_network_or_model_calls: 0, screenshots: [], checks: {}, errors: [] };
let app;
try {
  const host = path.join(temp, 'host.cjs');
  await build({ entryPoints: [path.join(root, 'scripts/fixtures/transparent-image-host-041.ts')], outfile: host, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] });
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env, timeout: 45000 });
  const child = app.process();
  const page = await app.firstWindow(); report.actual_window = true;
  page.on('pageerror', error => report.errors.push(error.message));
  const shot = async name => { await page.screenshot({ path: path.join(output, name), animations: 'disabled' }); report.screenshots.push(name); };
  await page.getByRole('button', { name: /Emma Wilson/ }).click({ timeout: 45000 });
  const preview = page.getByRole('button', { name: '放大查看客户发送的照片', exact: true }).last();
  await preview.waitFor({ timeout: 45000 });
  const image = preview.locator('img');
  await image.evaluate(img => img.decode());
  const pixels = await image.evaluate(img => {
    const canvas = document.createElement('canvas'); canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0);
    return { corner_alpha: ctx.getImageData(0, 0, 1, 1).data[3], white_art_alpha: ctx.getImageData(300, 48, 1, 1).data[3], background: getComputedStyle(img).backgroundImage, source: img.src.slice(0, 22) };
  });
  assert.equal(pixels.corner_alpha, 0); assert.equal(pixels.white_art_alpha, 255); assert(pixels.background.includes('conic-gradient')); assert(pixels.source.startsWith('data:image/png;base64,'));
  report.checks.thumbnail_alpha_and_grid = pixels;
  await preview.scrollIntoViewIfNeeded(); await shot('01-white-transparent-thumbnail.png');
  await preview.click();
  const dialog = page.getByRole('dialog', { name: '客户发送的照片' });
  const original = dialog.locator('img');
  await original.waitFor(); await original.evaluate(img => img.decode());
  assert((await original.evaluate(img => getComputedStyle(img).backgroundImage)).includes('conic-gradient'));
  await shot('02-original-fit.png');
  const reference = await page.evaluate(async () => {
    const detail = await window.reception.invoke('get_conversation', { conversation_id: 'demo-emma' });
    const message = detail.messages.find(row => row.message_id === 'transparent-white-artwork-041');
    const state = await window.reception.invoke('get_message_image', { conversation_id: 'demo-emma', message_id: message.message_id, source_version: message.attachments.find(item => item.kind === 'image').source_version });
    const original = await window.reception.getImageOriginal(state.image_ref, crypto.randomUUID());
    return { bytes: Array.from(original.bytes), sha256: state.sha256 };
  });
  assert.equal(createHash('sha256').update(Buffer.from(reference.bytes)).digest('hex'), reference.sha256);
  const integrity = await app.evaluate(() => globalThis.__imageIntegrity());
  assert(integrity.original_unchanged && integrity.persisted_preview_still_legacy); assert.equal(integrity.sha256, reference.sha256);
  report.checks.original_bytes_and_read_only_legacy_cache = integrity;
  await page.getByRole('button', { name: '原始尺寸', exact: true }).click();
  assert.equal(await original.evaluate(img => getComputedStyle(img).maxWidth), 'none');
  await shot('03-original-size.png');
  await page.getByRole('button', { name: '适应窗口', exact: true }).click();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(780, 650));
  await shot('04-narrow-dialog.png');
  await page.getByRole('button', { name: '关闭图片', exact: true }).click();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await new Promise((resolve, reject) => { if (child.exitCode !== null) return resolve(); const timer = setTimeout(() => reject(new Error('fixture shutdown timeout')), 30000); child.once('exit', () => { clearTimeout(timer); resolve(); }); });
  assert.equal(child.exitCode, 0); assert.equal(JSON.parse(await readFile(path.join(temp, 'normal-shutdown.json'), 'utf8')).service_exited, true);
  app = undefined; assert.deepEqual(report.errors, []); report.checks.normal_shutdown = true; report.status = 'passed';
} catch (error) {
  report.error = String(error?.stack || error); process.exitCode = 1;
  if (app) { try { await app.windows()[0]?.screenshot({ path: path.join(output, 'failure.png') }); report.screenshots.push('failure.png'); } catch {} }
} finally {
  if (app) await app.close().catch(() => {});
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  await rm(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }).catch(() => {});
}
console.log(JSON.stringify(report, null, 2));
