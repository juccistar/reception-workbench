import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {getBootstrapPaths, readBootstrap, writeBootstrap, resolveDataLocation, restartArguments} from '../data-location.mjs';

function fixture(t) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'reception-location-'));
  t.after(() => rmSync(root, {recursive:true, force:true}));
  const env = {LOCALAPPDATA: root, RECEPTION_BOOTSTRAP_DIR: path.join(root, 'bootstrap')};
  return {root, env, ...getBootstrapPaths(env)};
}

test('precedence and persisted configuration are independent of app location', t => {
  const f = fixture(t);
  const selected = path.join(f.root, '客户 data');
  const legacy = path.join(f.root, 'legacy');
  mkdirSync(selected); mkdirSync(legacy);
  assert.equal(resolveDataLocation([], f.env).source, 'default');
  assert.equal(resolveDataLocation([], f.env, legacy).source, 'legacy');
  writeBootstrap(f.configFile, {version:1, dataRoot:selected});
  assert.equal(resolveDataLocation(['different-app.exe'], f.env, legacy).currentRoot, selected);
  const override = path.join(f.root, 'new override');
  assert.equal(resolveDataLocation(['--data-dir', override], f.env).currentRoot, override);
  assert.equal(resolveDataLocation(['--data-dir=' + override], f.env).source, 'argument');
  assert.equal(readBootstrap(f.configFile).dataRoot, selected);
  assert.throws(() => resolveDataLocation(['--data-dir', override, '--data-dir=' + selected], f.env));
  assert.throws(() => resolveDataLocation(['--data-dir'], f.env));
  assert.throws(() => resolveDataLocation(['--data-dir=relative'], f.env));
  assert.throws(() => resolveDataLocation(['--data-dir=' + path.parse(f.root).root], f.env));
});

test('invalid, oversized and unavailable configurations fail closed', t => {
  const f = fixture(t);
  mkdirSync(f.stateDir);
  for (const content of ['{', '{"version":2}', '{"version":1,"dataRoot":"relative"}', '{"version":1,"unexpected":true}', ' '.repeat(65537)]) {
    writeFileSync(f.configFile, content);
    assert.throws(() => resolveDataLocation([], f.env));
  }
  const missing = path.join(f.root, 'missing');
  writeFileSync(f.configFile, JSON.stringify({version:1,dataRoot:missing}));
  assert.throws(() => resolveDataLocation([], f.env), /unavailable_data_root/);
  writeFileSync(missing, 'occupied');
  assert.throws(() => resolveDataLocation([], f.env));
});

test('atomic write keeps old config on failed validation or stale expected value', t => {
  const f = fixture(t);
  const original = {version:1, lastResult:{action:'backup',status:'ok',message:'done',completedAt:1}};
  writeBootstrap(f.configFile, original, {version:1});
  const bytes = readFileSync(f.configFile, 'utf8');
  assert.throws(() => writeBootstrap(f.configFile, {version:1,dataRoot:'relative'}));
  assert.throws(() => writeBootstrap(f.configFile, {version:1}, {version:1}), /bootstrap_changed/);
  assert.equal(readFileSync(f.configFile, 'utf8'), bytes);
  assert.deepEqual(readBootstrap(f.configFile), original);
});

test('links and non-directory targets are refused', t => {
  const f = fixture(t);
  const target = path.join(f.root, 'target'); mkdirSync(target);
  const link = path.join(f.root, 'link');
  try { symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir'); }
  catch (error) { if (['EPERM','EACCES'].includes(error.code)) return t.skip('link creation unavailable'); throw error; }
  assert.throws(() => resolveDataLocation(['--data-dir', link], f.env));
  assert.throws(() => resolveDataLocation(['--data-dir', path.join(link,'child')], f.env));
  mkdirSync(f.stateDir); symlinkSync(path.join(f.root, 'missing-file'), f.configFile, 'file');
  assert.throws(() => readBootstrap(f.configFile));
});

test('a saved directory cannot be recreated by a repeated explicit restart argument', t => {
  const f = fixture(t); const saved = path.join(f.root, 'missing-custom');
  writeBootstrap(f.configFile, {version:1,dataRoot:saved});
  for (const candidate of [saved, ...(process.platform === 'win32' ? [saved.toUpperCase()] : [])]) {
    assert.throws(() => resolveDataLocation(['--data-dir', candidate], f.env), /unavailable_data_dir/);
  }
  assert.equal(resolveDataLocation(['--data-dir', path.join(f.root, 'explicit-new')], f.env).source, 'argument');
});

test('all startup sources reject overlap with fixed bootstrap storage', t => {
  const f = fixture(t);
  assert.throws(() => resolveDataLocation(['--data-dir', f.stateDir], f.env), /overlaps_bootstrap/);
  assert.throws(() => resolveDataLocation(['--data-dir', path.join(f.stateDir, 'child')], f.env), /overlaps_bootstrap/);
  assert.throws(() => resolveDataLocation(['--data-dir', f.root], f.env), /overlaps_bootstrap/);
  mkdirSync(f.stateDir);
  writeBootstrap(f.configFile, {version:1,dataRoot:f.stateDir});
  assert.throws(() => resolveDataLocation([], f.env), /overlaps_bootstrap/);
});

test('obsolete live launch commands cannot reopen the previous migrated root', t => {
  const f = fixture(t); const current = path.join(f.root, 'current'); const old = path.join(f.root, 'previous');
  mkdirSync(current); mkdirSync(old);
  writeBootstrap(f.configFile, {version:1,dataRoot:current,previousRoot:old});
  assert.throws(() => resolveDataLocation(['--connect-from-accio','--data-dir',old], f.env), /retired_data_directory/);
  assert.equal(resolveDataLocation(['--data-dir',old], f.env).source, 'argument');
  assert.equal(resolveDataLocation([], f.env).currentRoot,current);
});

test('restart replaces old directory arguments and retains other options', t => {
  const f = fixture(t);
  const selected = path.join(f.root, '选择');
  assert.deepEqual(restartArguments(['app.js','--live','--data-dir','old','--seller','123','--data-dir=other','--workctl','ctl'], selected), ['app.js','--live','--seller','123','--workctl','ctl','--data-dir',selected]);
});
