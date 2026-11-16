import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { lstat, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';

const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i], value = process.argv[i + 1];
  assert(['--release', '--elevate', '--expected-version', '--output'].includes(key) && value && !value.startsWith('--') && !options.has(key), 'Invalid arguments');
  options.set(key, value);
}
assert(options.size === 4, 'Use --release, --elevate, --expected-version and --output');
const release = assertReleasePath(root, options.get('--release'));
const output = path.resolve(root, options.get('--output'));
const outputRelative = path.relative(path.join(root, 'artifacts/verification'), output);
assert(outputRelative && !outputRelative.startsWith('..') && !path.isAbsolute(outputRelative), 'Report must be under artifacts/verification');
const portable = path.join(release, 'win-unpacked');
const installer = path.join(release, 'installer-build/win-unpacked');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const report = { status: 'failed', candidate: path.relative(root, release).replaceAll('\\', '/'), checks: {} };
async function list(directory, relative = '') {
  const result = [];
  for (const item of await readdir(path.join(directory, relative), { withFileTypes: true })) {
    const entry = path.join(relative, item.name);
    const stat = await lstat(path.join(directory, entry));
    assert(!stat.isSymbolicLink(), 'Resource links are not allowed');
    if (stat.isDirectory()) result.push(...await list(directory, entry));
    else { assert(stat.isFile() && stat.nlink === 1, 'Expected a regular resource file'); result.push(entry.replaceAll('\\', '/')); }
  }
  return result.sort();
}
try {
  const manifest = JSON.parse(await readFile(path.join(portable, 'manifest.json'), 'utf8'));
  assert.equal(manifest.version, options.get('--expected-version'));
  const resources = manifest.files.filter(file => file.path.startsWith('resources/'));
  const portableResources = await list(path.join(portable, 'resources'));
  assert.deepEqual(portableResources, resources.map(file => file.path.slice('resources/'.length)).sort());
  for (const file of resources) {
    assert(!file.path.includes('\\') && !file.path.split('/').some(part => !part || part === '..' || part === '.'), 'Invalid resource path');
    for (const directory of [portable, installer]) {
      const bytes = await readFile(path.join(directory, file.path));
      assert.equal(bytes.length, file.bytes, file.path);
      assert.equal(digest(bytes), file.sha256, file.path);
    }
  }
  const installedResources = await list(path.join(installer, 'resources'));
  const extras = installedResources.filter(file => !portableResources.includes(file));
  assert.deepEqual(extras, ['app-update.yml', 'elevate.exe', 'update-policy.json']);
  const elevateSource = path.resolve(root, options.get('--elevate'));
  assert.equal(path.basename(elevateSource), 'elevate.exe');
  assert((await lstat(elevateSource)).isFile());
  const elevateDigest = digest(await readFile(path.join(installer, 'resources/elevate.exe')));
  assert.equal(elevateDigest, digest(await readFile(elevateSource)));
  report.checks = { version: manifest.version, identical_resource_files: resources.length, installer_only_resources: extras,
    nsis_elevate_matches_builder_cache: true, nsis_elevate_sha256: elevateDigest,
    application_backend_sdk_and_extension_identical: true, no_links: true };
  report.status = 'passed';
} catch (error) { report.error = String(error?.stack || error); process.exitCode = 1; }
await writeFile(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify(report));
