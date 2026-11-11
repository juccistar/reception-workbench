import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync, symlinkSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import os from 'node:os';
import path from 'node:path';
import { resolveMerchantProfile, merchantRegistryDirectory, merchantSupportPaths, workspacePathUnits, maintenanceEnvironment } from '../merchant-profiles.mjs';
import { readBootstrap, writeBootstrap } from '../data-location.mjs';

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'merchant-registry-'));
  const local = path.join(root, 'local'); mkdirSync(local);
  const env = { ...process.env, LOCALAPPDATA: local }; delete env.RECEPTION_MERCHANT_REGISTRY_DIR;
  return { root, env, directory: merchantRegistryDirectory(env), cleanup: () => rmSync(root, { recursive: true, force: true }) };
}
const empty = async () => ({ status: 'empty' });
const windows = { skip: process.platform !== 'win32', timeout: 60000 };

test('short roots remain isolated and path budgets count UTF-16 and SQLite sidecars', windows, async () => {
  const f = fixture(); try {
    const a = await resolveMerchantProfile('12345', f.env, { inspectOwnership: empty });
    const b = await resolveMerchantProfile('67890', f.env, { inspectOwnership: empty });
    const support = merchantSupportPaths(f.directory);
    const namespace = createHash('sha256').update(process.platform === 'win32' ? path.resolve(f.directory).toLowerCase() : path.resolve(f.directory)).digest('hex').slice(0, 16);
    assert.deepEqual(support, { dataDirectory: path.join(path.dirname(f.directory), 'ARWData', namespace), backupDirectory: path.join(path.dirname(f.directory), 'ARWBackups', namespace) });
    assert.equal(a.dataRoot, path.join(support.dataDirectory, '12345'));
    assert.notEqual(a.dataRoot, b.dataRoot);
    assert.equal(workspacePathUnits(a.dataRoot + '😀'), workspacePathUnits(a.dataRoot) + 2);
    const tooLong = path.join(f.root, 'x'.repeat(180));
    await assert.rejects(resolveMerchantProfile('999', f.env, { dataRoot: tooLong, inspectOwnership: empty }), /merchant_workspace_path_too_long/);
    assert.equal(existsSync(tooLong), false);
    assert.equal(JSON.parse(readFileSync(path.join(f.directory, 'merchants.json'))).profiles['999'], undefined);
  } finally { f.cleanup(); }
});

test('maintenance environment excludes credentials', () => {
  const result = maintenanceEnvironment({ SystemRoot: 'C:\\Windows', TEMP: 'C:\\Temp', LOCALAPPDATA: 'C:\\Local', USERPROFILE: 'C:\\User', ACCIO_GATEWAY_TOKEN: 'secret', PHOENIX_SECRET: 'secret', API_TOKEN: 'secret' });
  assert.deepEqual(result, { SystemRoot: 'C:\\Windows', TEMP: 'C:\\Temp', LOCALAPPDATA: 'C:\\Local', USERPROFILE: 'C:\\User', PYTHONIOENCODING: 'utf-8' });
});

test('only created zero-byte initialization exposes controlled recovery context', windows, async () => {
  const f = fixture(); try {
    const profile = await resolveMerchantProfile('12345', f.env, { inspectOwnership: empty });
    await assert.rejects(resolveMerchantProfile('12345', f.env, { inspectOwnership: async () => ({ status: 'unknown', reason: 'zero_byte_workspace', secret: 'secret' }) }), error => {
      assert.equal(error.message, 'merchant_initialization_incomplete');
      assert.deepEqual(error.context, { registry: f.directory, seller: '12345', dataRoot: profile.dataRoot, state: profile.stateDirectory, reason: 'zero_byte_workspace' });
      return true;
    });
    await assert.rejects(resolveMerchantProfile('12345', f.env, { inspectOwnership: async () => ({ status: 'unknown', reason: 'other' }) }), /merchant_workspace_ownership_unknown/);
  } finally { f.cleanup(); }
});

test('adopts positively owned original root, gives a different seller a fresh isolated root', windows, async () => {
  const f = fixture(); try {
    const legacy = path.join(f.root, 'existing'); mkdirSync(legacy);
    writeFileSync(path.join(legacy, 'preserved.txt'), 'ORIGINAL');
    writeBootstrap(path.join(f.directory, 'storage.json'), { version: 1, dataRoot: legacy });
    const before = readFileSync(path.join(f.directory, 'storage.json'));
    const options = { inspectOwnership: async value => value === legacy ? { status: 'owned', sellerId: '12345' } : { status: 'empty' } };
    const a = await resolveMerchantProfile('12345', f.env, options);
    const b = await resolveMerchantProfile('67890', f.env, options);
    assert.equal(a.dataRoot, legacy); assert.notEqual(b.dataRoot, legacy);
    assert.notEqual(a.stateDirectory, b.stateDirectory);
    assert.deepEqual(await resolveMerchantProfile('12345', f.env, options), a);
    assert.deepEqual(readFileSync(path.join(f.directory, 'storage.json')), before);
    assert.equal(readFileSync(path.join(legacy, 'preserved.txt'), 'utf8'), 'ORIGINAL');
  } finally { f.cleanup(); }
});

test('preserves original backup and retired-root metadata and refuses a stale first-launch directory', windows, async () => {
  const f = fixture(); try {
    const current = path.join(f.root, 'current'), retired = path.join(f.root, 'retired'), backup = path.join(f.root, 'backup');
    for (const folder of [current, retired, backup]) mkdirSync(folder);
    const original = { version: 1, dataRoot: current, previousRoot: retired, lastBackup: { path: backup, completedAt: 123 }, lastResult: { action: 'migrate', status: 'completed', message: 'saved', completedAt: 123 } };
    writeBootstrap(path.join(f.directory, 'storage.json'), original);
    const options = { inspectOwnership: async value => [current, retired].includes(value) ? { status: 'owned', sellerId: '12345' } : { status: 'empty' } };
    await assert.rejects(resolveMerchantProfile('12345', f.env, { ...options, dataRoot: retired }), /merchant_profile_root_conflict/);
    assert.equal(existsSync(path.join(f.directory, 'profiles')), false);
    const profile = await resolveMerchantProfile('12345', f.env, options);
    assert.deepEqual(readBootstrap(path.join(profile.stateDirectory, 'storage.json')), original);
  } finally { f.cleanup(); }
});

test('unknown original ownership preserves original and does not create a merchant workspace', windows, async () => {
  const f = fixture(); try {
    const legacy = path.join(f.root, 'unknown'); mkdirSync(legacy); writeFileSync(path.join(legacy, 'keep.txt'), 'KEEP');
    writeBootstrap(path.join(f.directory, 'storage.json'), { version: 1, dataRoot: legacy });
    await assert.rejects(resolveMerchantProfile('12345', f.env, { inspectOwnership: async () => ({ status: 'unknown' }) }), /merchant_legacy_ownership_unknown/);
    assert.equal(existsSync(path.join(f.directory, 'merchants.json')), false);
    assert.equal(existsSync(path.join(f.directory, 'profiles')), false);
    assert.equal(readFileSync(path.join(legacy, 'keep.txt'), 'utf8'), 'KEEP');
  } finally { f.cleanup(); }
});

test('profile config remains authoritative after migration; retired explicit root and missing saved root fail', windows, async () => {
  const f = fixture(); try {
    const a = await resolveMerchantProfile('12345', f.env, { inspectOwnership: empty });
    const moved = path.join(f.root, 'moved'); mkdirSync(moved);
    const config = path.join(a.stateDirectory, 'storage.json');
    writeBootstrap(config, { version: 1, dataRoot: moved, previousRoot: a.dataRoot }, readBootstrap(config));
    const owned = { inspectOwnership: async () => ({ status: 'owned', sellerId: '12345' }) };
    assert.equal((await resolveMerchantProfile('12345', f.env, owned)).dataRoot, moved);
    await assert.rejects(resolveMerchantProfile('12345', f.env, { ...owned, dataRoot: a.dataRoot }), /merchant_profile_root_conflict/);
    rmSync(moved, { recursive: true });
    await assert.rejects(resolveMerchantProfile('12345', f.env, owned), /merchant_workspace_missing/);
    assert.equal(existsSync(moved), false);
  } finally { f.cleanup(); }
});

test('existing registry data root remains stable and managed roots stay isolated', windows, async () => {
  const f = fixture(); try {
    const oldRoot = path.join(f.directory, 'data', '12345');
    const original = await resolveMerchantProfile('12345', f.env, { dataRoot: oldRoot, inspectOwnership: empty });
    assert.equal((await resolveMerchantProfile('12345', f.env, { inspectOwnership: empty })).dataRoot, original.dataRoot);
    const support = merchantSupportPaths(f.directory);
    await assert.rejects(resolveMerchantProfile('67890', f.env, { dataRoot: path.join(support.dataDirectory, '99999'), inspectOwnership: empty }), /merchant_path_conflict/);
    await assert.rejects(resolveMerchantProfile('67890', f.env, { dataRoot: path.join(f.directory, 'data', '99999'), inspectOwnership: empty }), /merchant_path_conflict/);
  } finally { f.cleanup(); }
});

test('rejects foreign root, managed-subtree appropriation, damaged registry and directory links', windows, async () => {
  const f = fixture(); try {
    const foreign = path.join(f.root, 'foreign'); mkdirSync(foreign);
    await assert.rejects(resolveMerchantProfile('12345', f.env, { dataRoot: foreign, inspectOwnership: async () => ({ status: 'owned', sellerId: '67890' }) }), /merchant_workspace_mismatch/);
    const futureB = path.join(f.directory, 'profiles', createHash('sha256').update('67890').digest('hex'), 'data');
    await assert.rejects(resolveMerchantProfile('12345', f.env, { dataRoot: futureB, inspectOwnership: empty }), /merchant_path_conflict/);
    const link = path.join(f.root, 'linked'); symlinkSync(foreign, link, 'junction');
    await assert.rejects(resolveMerchantProfile('12345', f.env, { dataRoot: link, inspectOwnership: empty }), /merchant_path_invalid/);
    writeFileSync(path.join(f.directory, 'merchants.json'), '{bad');
    await assert.rejects(resolveMerchantProfile('12345', f.env, { inspectOwnership: empty }));
    assert.equal(readFileSync(path.join(f.directory, 'merchants.json'), 'utf8'), '{bad');
  } finally { f.cleanup(); }
});

test('recovers a matching prepared registration without overwriting foreign state', windows, async () => {
  const f = fixture(); try {
    const home = path.join(f.directory, 'profiles', createHash('sha256').update('12345').digest('hex'));
    const stateDirectory = path.join(home, 'state'), dataRoot = path.join(f.root, 'prepared');
    mkdirSync(stateDirectory, { recursive: true }); mkdirSync(dataRoot);
    writeFileSync(path.join(stateDirectory, 'profile.json'), JSON.stringify({ format: 1, sellerId: '12345', stateDirectory, dataRoot }));
    const result = await resolveMerchantProfile('12345', f.env, { inspectOwnership: empty });
    assert.equal(result.dataRoot, dataRoot);
    assert.equal(readBootstrap(path.join(stateDirectory, 'storage.json')).dataRoot, dataRoot);
    assert.equal(JSON.parse(readFileSync(path.join(f.directory, 'merchants.json'), 'utf8')).profiles['12345'].sellerId, '12345');
  } finally { f.cleanup(); }
});

test('serializes concurrent registration across real Node processes without lost profiles', windows, async () => {
  const f = fixture(); try {
    const moduleURL = pathToFileURL(path.resolve(import.meta.dirname, '../merchant-profiles.mjs')).href;
    const run = seller => promisify(execFile)(process.execPath, ['--input-type=module', '-e', `import {resolveMerchantProfile} from ${JSON.stringify(moduleURL)}; const value=await resolveMerchantProfile(${JSON.stringify(seller)},process.env,{inspectOwnership:async()=>({status:'empty'})}); console.log(JSON.stringify(value));`], { env: f.env, timeout: 45000, encoding: 'utf8' }).then(value => JSON.parse(value.stdout));
    const results = await Promise.all([run('12345'), run('67890'), run('12345')]);
    assert.deepEqual(results[0], results[2]); assert.notEqual(results[0].dataRoot, results[1].dataRoot);
    const registry = JSON.parse(readFileSync(path.join(f.directory, 'merchants.json'), 'utf8'));
    assert.deepEqual(Object.keys(registry.profiles).sort(), ['12345', '67890']);
  } finally { f.cleanup(); }
});
