import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Capture reference pages separately; never overwrite the checked-in design previews.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
assert.equal(process.argv[2], '--label');
const label = process.argv[3];
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || '') && process.argv.length === 4);
const output = path.join(root, 'artifacts/verification', `design-042-${label}`);
await mkdir(output);
const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-design-042-'));
const host = path.join(temp, 'host.cjs');
const design = path.join(root, 'docs/design/reception-redesign');
await writeFile(host, `const {app,BrowserWindow,session}=require('electron');\napp.setPath('userData',${JSON.stringify(path.join(temp, 'profile'))});\napp.whenReady().then(async()=>{session.defaultSession.webRequest.onBeforeRequest((d,cb)=>cb({cancel:!d.url.startsWith('file:')}));const w=new BrowserWindow({width:1440,height:960,useContentSize:true,frame:false,show:true,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});w.webContents.setWindowOpenHandler(()=>({action:'deny'}));await w.loadFile(${JSON.stringify(path.join(design, 'pages.html'))},{hash:'home'});});\napp.on('window-all-closed',()=>app.quit());`);
const report = { status: 'failed', screenshots: [], renderer_errors: [], limitations: ['Reference captures only; matching synthetic data and visual review are required.', 'Launcher, account and modal states need separate capture.'] };
let app;
try {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) if (/^(SystemRoot|WINDIR|SystemDrive|PATH|PATHEXT|ComSpec|TEMP|TMP|ProgramFiles|ProgramFiles\(x86\)|ProgramW6432|USERPROFILE|USERNAME|USERDOMAIN|COMPUTERNAME|SESSIONNAME|OS|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|ALLUSERSPROFILE|PUBLIC|HOMEDRIVE|HOMEPATH)$/i.test(key)) env[key] = value;
  app = await electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'), args: [host], cwd: root, env });
  const page = await app.firstWindow();
  page.on('pageerror', e => report.renderer_errors.push(e.message));
  const shot = async name => {
    await page.evaluate(async () => { await document.fonts.ready; await Promise.all([...document.images].map(i => i.decode().catch(() => {}))); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); });
    await page.screenshot({ path: path.join(output, name), animations: 'disabled' });
    const rects = await page.evaluate(() => ({ viewport: { width: innerWidth, height: innerHeight }, elements: [...document.querySelectorAll('nav, main, [role="dialog"], [role="region"], #suite-content, #customer-panel, .messages')].filter(el => { const r = el.getBoundingClientRect(); return r.width && r.height; }).map(el => { const r = el.getBoundingClientRect(); return { tag: el.tagName, id: el.id, role: el.getAttribute('role'), label: el.getAttribute('aria-label'), class: el.className, x: r.x, y: r.y, width: r.width, height: r.height }; }) }));
    await writeFile(path.join(output, `${name}.rects.json`), JSON.stringify(rects, null, 2) + '\n', { flag: 'wx' });
    report.screenshots.push(name);
  };
  for (const [route, name] of [['home','page-1'], ['quotes','page-2'], ['products','page-3'], ['suppliers','page-4'], ['procurement','page-5'], ['customers','page-6'], ['library','page-7'], ['settings/reception','page-8'], ['activity','page-9'], ['settings/media','settings-2'], ['settings/connection','settings-3'], ['settings/storage','settings-4'], ['settings/feishu','settings-5'], ['settings/display','settings-6'], ['settings/about','settings-7']]) {
    await page.evaluate(value => { location.hash = value; }, route);
    await page.waitForFunction(value => document.querySelector('#suite-content')?.dataset.route === value, route);
    await shot(`${name}.png`);
  }
  await page.goto(`file:///${path.join(design, 'index.html').replaceAll('\\', '/')}`);
  await page.locator('#customer-list .customer-row').first().waitFor();
  await shot('01-reception.png');
  assert.deepEqual(report.renderer_errors, []);
  report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
finally { if (app) await app.close().catch(() => {}); await rm(temp, { recursive: true, force: true }); await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' }); }
console.log(JSON.stringify(report, null, 2));
