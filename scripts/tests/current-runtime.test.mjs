import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { currentRuntime } from '../current-runtime.mjs';

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'current-runtime-'));
  function make(name, content = '') {
    const file = path.join(root, name);
    mkdirSync(path.dirname(file), {recursive: true});
    writeFileSync(file, content);
    return file;
  }
  function delivery(executable = 'artifacts/release/ReceptionWorkbench.exe', version = '0.2.11') {
    make('artifacts/delivery.json', JSON.stringify({executable}));
    make('artifacts/release/ReceptionWorkbench.exe');
    make('artifacts/release/manifest.json', JSON.stringify({version}));
  }
  function runtime(folder, version = '0.2.11') {
    const executable = make(`artifacts/${folder}/ReceptionWorkbench.exe`);
    make(`artifacts/${folder}/manifest.json`, JSON.stringify({version}));
    return executable;
  }
  return {root, make, delivery, runtime, cleanup: () => rmSync(root, {recursive: true, force: true})};
}

test('resolves promoted version instead of source package version and prefers undated runtime', () => {
  const f = fixture(); try {
    f.make('package.json', JSON.stringify({version: '0.2.12'})); f.delivery();
    const executable = f.runtime('runtime-0.2.11'); f.runtime('runtime-0.2.11-20261107');
    assert.deepEqual(currentRuntime(f.root), {version: '0.2.11', executable});
  } finally {f.cleanup();}
});

test('resolves archived delivery while keeping runtime under artifacts', () => {
  const f = fixture(); try {
    const exe = f.make('relase/release-0.2.11/ReceptionWorkbench.exe');
    f.make('relase/release-0.2.11/manifest.json', JSON.stringify({version:'0.2.11'}));
    f.make('artifacts/delivery.json', JSON.stringify({executable:exe}));
    const executable = f.runtime('runtime-0.2.11');
    assert.equal(currentRuntime(f.root).executable, executable);
  } finally { f.cleanup(); }
});

test('uses dated fallback when current runtime is missing or has a mismatched version', () => {
  const f = fixture(); try {
    f.delivery(); const executable = f.runtime('runtime-0.2.11-20261107');
    assert.equal(currentRuntime(f.root).executable, executable);
    f.runtime('runtime-0.2.11', '0.2.12');
    assert.equal(currentRuntime(f.root).executable, executable);
  } finally {f.cleanup();}
});

test('rejects missing and mismatched runtime copies and invalid delivery metadata', () => {
  const f = fixture(); try {
    f.delivery(); assert.throws(() => currentRuntime(f.root), /runtime_copy_missing/);
    f.runtime('runtime-0.2.11', '0.2.12'); assert.throws(() => currentRuntime(f.root), /runtime_copy_missing/);
    f.make('artifacts/delivery.json', '{'); assert.throws(() => currentRuntime(f.root), /runtime_copy_missing/);
  } finally {f.cleanup();}
});

test('rejects delivery paths escaping artifacts, including directory links', () => {
  const f = fixture(); try {
    f.delivery('artifacts/../external/ReceptionWorkbench.exe');
    f.make('external/ReceptionWorkbench.exe'); f.make('external/manifest.json', JSON.stringify({version: '0.2.11'}));
    f.runtime('runtime-0.2.11'); assert.throws(() => currentRuntime(f.root), /runtime_copy_missing/);
    const external = f.make('external/ReceptionWorkbench.exe');
    f.make('external/manifest.json', JSON.stringify({version: '0.2.11'}));
    symlinkSync(path.dirname(external), path.join(f.root, 'artifacts/redirect'), process.platform === 'win32' ? 'junction' : 'dir');
    f.make('artifacts/delivery.json', JSON.stringify({executable: 'artifacts/redirect/ReceptionWorkbench.exe'}));
    assert.throws(() => currentRuntime(f.root), /runtime_copy_missing/);
  } finally {f.cleanup();}
});
