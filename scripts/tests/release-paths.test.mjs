import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { assertReleasePath, isReleasePath } from '../release-paths.mjs';
import { parsePackageArgs } from '../release-feed.mjs';

test('release paths allow new and legacy descendants and reject traversal and links', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'release-paths-'));
  try {
    mkdirSync(path.join(root, 'relase')); mkdirSync(path.join(root, 'artifacts')); mkdirSync(path.join(root, 'outside'));
    writeFileSync(path.join(root, 'package.json'), JSON.stringify({version:'0.2.20'}));
    assert.equal(parsePackageArgs([], root).output, path.join(root, 'relase', 'release-0.2.20'));
    assert.equal(assertReleasePath(root, 'artifacts/release-0.2.19'), path.join(root, 'artifacts', 'release-0.2.19'));
    for (const value of ['relase', 'relase/../artifacts/x', '../outside/x', path.join(root, 'outside/x')]) assert.equal(isReleasePath(root, value), false);
    symlinkSync(path.join(root,'outside'),path.join(root,'relase','redirect'),process.platform === 'win32' ? 'junction' : 'dir');
    assert.throws(() => assertReleasePath(root, 'relase/redirect/new'));
    assert.throws(() => parsePackageArgs(['--output',path.join(root,'relase','redirect','new')], root));
  } finally { rmSync(root, {recursive:true,force:true}); }
});
