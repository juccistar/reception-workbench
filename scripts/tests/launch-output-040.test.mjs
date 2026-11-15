import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
test('authorized command fails quietly without inherited authorization', { skip: process.platform !== 'win32' }, () => {
  const env = { ...process.env };
  delete env.ACCIO_NODE_BIN;
  const result = spawnSync(process.env.ComSpec || 'cmd.exe', ['/d', '/c', path.join(root, '授权启动接待.cmd')], { env, encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 2);
  assert.equal(result.stdout, '');
  assert.ok(Buffer.byteLength(result.stderr, 'utf8') <= 512);
  assert.deepEqual(JSON.parse(result.stderr.trim()), { status: 'blocked', code: 'authorized_environment_missing' });
  assert.equal(result.stderr.trim().split(/\r?\n/).length, 1);
});
