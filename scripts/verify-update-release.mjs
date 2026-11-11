import assert from 'node:assert/strict';
import path from 'node:path';
import { readFile, readdir, writeFile, lstat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { root } from './paths.mjs';
import { assertReleasePath } from './release-paths.mjs';
import { verifyReleaseMetadata, RELEASE_PUBLIC_KEY_SHA256, UPDATE_APP_ID } from './update-signing.mjs';
import { updateSourceFromConfig, parseUpdateSource } from './update-source.mjs';

const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
const version = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8')).version;
const release = assertReleasePath(root, option('--release') || `relase/release-${version}`);
const output = path.resolve(root, option('--output') || `artifacts/verification/update-${version}/update-release-integrity.json`);
assert(output.startsWith(path.join(root, 'artifacts/verification') + path.sep));
const digest = (bytes, algorithm = 'sha256', encoding = 'hex') => createHash(algorithm).update(bytes).digest(encoding);
const delivery = JSON.parse(await readFile(path.join(release, 'delivery.json'), 'utf8'));
const manifest = JSON.parse(await readFile(path.join(release, 'release-manifest.json'), 'utf8'));
const installerDir = path.join(release, 'installer-build/win-unpacked');
const policy = JSON.parse(await readFile(path.join(installerDir, 'resources/update-policy.json'), 'utf8'));
assert.equal(policy.format, 1); assert.equal(policy.app_id, UPDATE_APP_ID);
assert.equal(digest(Buffer.from(policy.public_key)), RELEASE_PUBLIC_KEY_SHA256, 'Published release public key changed');
const metadata = JSON.parse(await readFile(path.join(release, 'release-feed/latest.yml'), 'utf8'));
const verified = verifyReleaseMetadata(metadata, policy.public_key);
assert(verified, 'Release signature verification failed');
assert.equal(verified.version, manifest.version); assert.equal(verified.version, option('--expected-version') || version); assert.equal(manifest.release_signature, 'ed25519');
assert.equal(path.dirname(path.resolve(delivery.installer)), release);
const setup = await readFile(delivery.installer);
assert.equal(setup.length, verified.size); assert.equal(digest(setup, 'sha512', 'base64'), verified.sha512);
for (const row of manifest.files) {
  assert.equal(path.basename(row.file), row.file);
  const file = path.join(release, 'release-feed', row.file); const stat = await lstat(file);
  assert(stat.isFile() && !stat.isSymbolicLink());
  const bytes = await readFile(file);
  assert.equal(bytes.length, row.size); assert.equal(digest(bytes), row.sha256);
  assert.equal(digest(bytes, 'sha512', 'base64'), row.sha512);
}
assert.equal(digest(await readFile(path.join(release, 'release-feed', verified.artifact))), digest(setup));
const updateConfig = JSON.parse(await readFile(path.join(installerDir, 'resources/app-update.yml'), 'utf8'));
assert(updateConfig.updaterCacheDirName);
const asar = createRequire(import.meta.url)('@electron/asar');
const executableManifest = JSON.parse(asar.extractFile(path.join(installerDir, 'resources/app.asar'), 'package.json').toString('utf8'));
assert.equal(executableManifest.version, verified.version);
const launchModules = ['connect-live.mjs', 'merchant-profiles.mjs', 'merchant-mutex.mjs', 'launch-identity.mjs', 'windows-job.mjs', 'data-location.mjs', 'release-paths.mjs'];
const [major, minor, patch] = verified.version.split('.').map(Number);
const routedLaunch = major > 0 || minor > 2 || minor === 2 && patch >= 20;
if (routedLaunch) {
  for (const bundle of [installerDir, path.join(release, 'win-unpacked')]) {
    const routing = JSON.parse(await readFile(path.join(bundle, 'resources/reception-launch.json'), 'utf8'));
    assert.deepEqual(routing, { format: 1, app_id: UPDATE_APP_ID, routing: 'verified-seller-v1', version: verified.version });
    for (const module of launchModules) assert.equal(digest(await readFile(path.join(bundle, 'scripts', module))), digest(await readFile(path.join(root, 'scripts', module))), 'Launch source changed: ' + module);
  }
}
// The first-install marker is created by NSIS, not baked into a portable folder.
assert(!(await readdir(path.join(installerDir, 'resources'))).includes('reception-installed.json'));
let sourceManifest = '';
const stages = [];
for (const name of await readdir(path.join(root, 'artifacts/build'))) {
  if (/^package-input-\d+$/.test(name)) stages.push(name);
}
stages.sort((a, b) => Number(b.slice(14)) - Number(a.slice(14)));
let pythonInputs = 0;
const mainHash = digest(asar.extractFile(path.join(installerDir, 'resources/app.asar'), 'main/index.js'));
for (const name of stages) {
  const stage = path.join(root, 'artifacts/build', name);
  let input;
  try {
    if (digest(await readFile(path.join(stage, 'app/main/index.js'))) !== mainHash) continue;
    input = JSON.parse(await readFile(path.join(stage, 'source-manifest.json'), 'utf8'));
  } catch { continue; }
  for (const file of input.files) {
    const relative = path.relative(root, path.resolve(root, file.path));
    assert(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
    const bytes = await readFile(path.join(root, file.path));
    assert.equal(bytes.length, file.bytes); assert.equal(digest(bytes), file.sha256);
  }
  sourceManifest = path.relative(root, path.join(stage, 'source-manifest.json'));
  pythonInputs = input.files.length; break;
}
assert(sourceManifest && pythonInputs > 0, 'No matching current source input manifest');
const result = { status: 'passed', version: verified.version, installer_bytes: setup.length,
  installer_sha256: digest(setup), installer_sha512: verified.sha512, release_signature: 'ed25519',
  windows_signed: delivery.installer_signed === true, feed_files: manifest.files.length,
  public_key_fingerprint: digest(Buffer.from(policy.public_key)), python_inputs: pythonInputs,
  source_manifest: sourceManifest, compiled_host_matches_stage: true, installed_marker_baked_in: false,
  merchant_launch_inputs: routedLaunch ? launchModules.length : 0, merchant_launch_inputs_match_source: routedLaunch,
  update_source_preconfigured: Boolean(updateSourceFromConfig(updateConfig)),
  update_source_type: parseUpdateSource(updateSourceFromConfig(updateConfig)).kind,
  update_source_url: updateSourceFromConfig(updateConfig), actual_installer_executed: false };
await writeFile(output, JSON.stringify(result, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify(result, null, 2));
