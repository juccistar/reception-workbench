import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFile, readdir, lstat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';
const require = createRequire(import.meta.url);
const { extractFile, listPackage } = require('@electron/asar');
const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
assert(option('--delivery') && option('--staging') && option('--output'), '--delivery, --staging and --output are required');
const deliveryPath = path.resolve(root, option('--delivery'));
const staging = path.resolve(root, option('--staging'));
const stageRelative = path.relative(path.join(root, 'artifacts/build'), staging);
assert(stageRelative && !stageRelative.startsWith('..') && !path.isAbsolute(stageRelative));
assert(/^package-input-\d+$/.test(path.basename(staging)));
const delivery = JSON.parse(await readFile(deliveryPath, 'utf8'));
const bundle = assertReleasePath(root, path.dirname(delivery.executable));
const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
async function filesAt(folder, prefix = '') {
  const result = [];
  for (const entry of await readdir(path.join(folder, prefix), { withFileTypes: true })) {
    const name = prefix + entry.name;
    const file = path.join(folder, name);
    assert(!(await lstat(file)).isSymbolicLink(), 'Source links are not allowed');
    if (entry.isDirectory()) result.push(...await filesAt(folder, name + '/')); else result.push(name);
  }
  return result;
}
const manifest = JSON.parse(await readFile(path.join(staging, 'source-manifest.json'), 'utf8'));
assert.equal(manifest.format, 1);
assert(manifest.files.length > 0);
for (const row of manifest.files) {
  assert(/^services\/reception\/(?:src\/.*\.(?:py|txt)|run\.py)$/.test(row.path) || row.path === 'services/reception/src/reception_service/resources/builtin_policies.json');
  assert(!row.path.split('/').includes('..'));
  const current = await readFile(path.join(root, row.path));
  assert.equal(current.length, row.bytes, row.path);
  assert.equal(hash(current), row.sha256, row.path);
  const staged = row.path === 'services/reception/run.py' ? path.join(staging, 'python/run.py') : path.join(staging, 'python/src', row.path.slice('services/reception/src/'.length));
  assert.equal(hash(await readFile(staged)), row.sha256, row.path + ' staged');
}
const currentPython = (await filesAt(path.join(root, 'services/reception/src'))).filter(name => (name.endsWith('.py') || name.endsWith('/SOURCE.txt') || name === 'reception_service/resources/builtin_policies.json') && !name.split('/').includes('__pycache__') && name !== 'reception_service/integrations/demo.py').map(name => 'services/reception/src/' + name);
assert(!manifest.files.some(row => row.path === 'services/reception/src/reception_service/integrations/demo.py'), 'Retired Demo shim must not enter package input');
assert.deepEqual([...manifest.files.map(row => row.path)].sort(), [...currentPython, 'services/reception/run.py'].sort(), 'Python source manifest coverage');
const frozen = path.join(staging, 'python-dist/reception-backend');
const frozenFiles = await filesAt(frozen);
const policyResource = 'reception_service/resources/builtin_policies.json';
assert.equal(hash(await readFile(path.join(frozen, '_internal', policyResource))), hash(await readFile(path.join(root, 'services/reception/src', policyResource))), 'Frozen built-in policy resource');
for (const name of frozenFiles) assert.equal(hash(await readFile(path.join(frozen, name))), hash(await readFile(path.join(bundle, 'resources/backend', name))), 'Frozen backend ' + name);
const stagedApp = path.join(staging, 'app');
const asar = path.join(bundle, 'resources/app.asar');
const stagedFiles = await filesAt(stagedApp);
for (const name of stagedFiles) {
  const bytes = await readFile(path.join(stagedApp, name));
  assert.equal(hash(extractFile(asar, path.normalize(name))), hash(bytes), 'ASAR ' + name);
  if (name !== 'package.json') assert.equal(hash(await readFile(path.join(root, 'artifacts/app', name))), hash(bytes), 'Current compiled app ' + name);
}
const entries = listPackage(asar).map(name => name.replaceAll('\\', '/').replace(/^\//, ''));
const fileEntries = entries.filter(name => stagedFiles.includes(name));
assert.equal(fileEntries.length, stagedFiles.length);
assert.equal(JSON.parse(extractFile(asar, 'package.json').toString('utf8')).version, pkg.version);
const launchModules = ['connect-live.mjs', 'merchant-profiles.mjs', 'merchant-mutex.mjs', 'launch-identity.mjs', 'windows-job.mjs', 'data-location.mjs', 'release-paths.mjs'];
for (const name of launchModules) assert.equal(hash(await readFile(path.join(bundle, 'scripts', name))), hash(await readFile(path.join(root, 'scripts', name))), 'External launch module ' + name);
assert.equal(hash(await readFile(path.join(bundle, '授权启动接待.cmd'))), hash(await readFile(path.join(root, '授权启动接待.cmd'))));
assert.equal(hash(await readFile(path.join(bundle, 'resources/backend/SOURCE.txt'))), hash(await readFile(path.join(root, 'services/reception/src/reception_service/integrations/accio_native/vendor/SOURCE.txt'))));
const result = { status: 'passed', version: pkg.version, delivery: deliveryPath, staging, python_inputs: manifest.files.length,
  frozen_backend_files: frozenFiles.length, compiled_app_files: stagedFiles.length, external_launch_modules: launchModules.length,
  asar_sha256: hash(await readFile(asar)), source_manifest_sha256: hash(await readFile(path.join(staging, 'source-manifest.json'))),
  production_workspace_accessed: false };
await writeFile(path.resolve(root, option('--output')), JSON.stringify(result, null, 2), { flag: 'wx' });
console.log(JSON.stringify(result));
