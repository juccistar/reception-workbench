import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { root } from './paths.mjs';
const option = name => { const index = process.argv.indexOf(name); return index >= 0 ? process.argv[index + 1] : ''; };
const label = option('--label');
assert(/^[a-z0-9][a-z0-9-]{0,60}$/.test(label || ''), 'Pass a new label');
const runtime = path.resolve(root, option('--runtime-entry') || 'artifacts/app/main/index.js');
const hostEntry = path.resolve(root, option('--host-entry') || 'artifacts/app/main/index.js');
const directory = path.join(root, 'artifacts/verification', 'whatsapp-native-' + label);
await mkdir(directory);
const temporary = await mkdtemp(path.join(os.tmpdir(), 'arw-wa-native-'));
const entry = path.join(directory, 'fixture.cjs');
await writeFile(path.join(directory, 'package.json'), JSON.stringify({ name: 'inquiry-reception-workbench', version: JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version, main: 'fixture.cjs' }) + '\n');
await build({ entryPoints: [path.join(root, 'scripts/fixtures/whatsapp-native.ts')], outfile: entry, bundle: true, format: 'cjs', platform: 'node', target: 'node22', external: ['electron', 'puppeteer', 'whatsapp-web.js'], banner: { js: 'require = require("node:module").createRequire(process.env.RECEPTION_WA_RUNTIME_ENTRY);' } });
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(ACCIO|PHOENIX|RECEPTION)_/i.test(key) || /^(ELECTRON_RUN_AS_NODE|PYTHONPATH|PYTHONHOME|GH_TOKEN|GITHUB_TOKEN)$/i.test(key) || /credential|secret|token|password|api.?key/i.test(key)) delete env[key];
env.RECEPTION_WA_RUNTIME_ENTRY = runtime;
env.RECEPTION_WA_HOST_ENTRY = hostEntry;
env.RECEPTION_WA_FIXTURE_TEMP = temporary;
env.RECEPTION_WA_SCREENSHOT_OUTPUT = path.join(directory, 'public-page-redacted.png');
if (process.argv.includes('--fault-after-qr')) env.RECEPTION_WA_SYNTHETIC_FAILURE = '1';
if (process.argv.includes('--reference-no-cdp')) {
  env.RECEPTION_WA_REFERENCE_NO_CDP = '1';
  env.RECEPTION_WA_SCREENSHOT_OUTPUT = path.join(directory, 'public-page-redacted.png');
}
env.LOCALAPPDATA = path.join(temporary, 'local-app-data');
let report = { status: 'failed', error_code: 'NO_FIXTURE_RESULT' };
try {
  const child = spawn(path.join(root, 'node_modules/electron/dist/electron.exe'), [directory], { cwd: root, env, stdio: ['ignore', 'pipe', 'ignore'] });
  let buffer = '';
  child.stdout.on('data', chunk => {
    buffer += chunk.toString('utf8');
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0, end); buffer = buffer.slice(end + 1); if (line.startsWith('WA_FIXTURE_REPORT ')) report = JSON.parse(line.slice('WA_FIXTURE_REPORT '.length)); }
    if (buffer.length > 65536) buffer = '';
  });
  const timeout = setTimeout(() => { report = { status: 'failed', error_code: 'PROCESS_TIMEOUT' }; child.kill(); }, 300000);
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); });
  clearTimeout(timeout);
  report.exit_code = code;
  report.version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
  report.runtime_entry = path.relative(root, runtime).replaceAll('\\', '/');
  report.host_entry = path.relative(root, hostEntry).replaceAll('\\', '/');
  report.packaged_host_resources = hostEntry.includes('.asar' + path.sep) || hostEntry.includes('.asar/');
  report.packaged_sdk_resources = runtime.includes('.asar' + path.sep) || runtime.includes('.asar/');
  try { await writeFile(path.join(directory, 'host-diagnostics.jsonl'), await readFile(path.join(temporary, 'diagnostics/whatsapp-host.jsonl'))); report.diagnostics_saved = true; } catch { report.diagnostics_saved = false; }
} finally {
  await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  await writeFile(path.join(directory, 'report.json'), JSON.stringify(report, null, 2) + '\n');
}
console.log(JSON.stringify(report, null, 2));
if (!(process.argv.includes('--reference-no-cdp') ? report.status === 'reference-ready' : report.status === 'passed') || report.exit_code !== 0) process.exitCode = 1;
