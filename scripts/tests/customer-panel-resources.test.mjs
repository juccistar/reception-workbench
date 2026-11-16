import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { inspectCustomerPanel } from '../customer-panel-resources.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const source = path.join(root, 'apps/chrome-extension/customer-panel');

test('fixed native panel production files include the manifest and have no website permissions', async () => {
  const value = await inspectCustomerPanel(source, '0.4.1');
  assert.equal(value.files.length, 5);
  assert(value.files.some(file => file.path === 'manifest.json' && file.bytes > 0));
  assert(value.files.every(file => /^[a-f0-9]{64}$/.test(file.sha256)));
});
test('packaging rejects extra files and expanded extension capabilities', async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), 'arw-panel-resources-'));
  try {
    const directory = path.join(temp, 'panel');
    await cp(source, directory, { recursive: true });
    await writeFile(path.join(directory, 'account.sqlite3'), 'synthetic');
    await assert.rejects(inspectCustomerPanel(directory, '0.4.1'), /Unexpected customer panel files/);
    await rm(path.join(directory, 'account.sqlite3'));
    const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
    manifest.host_permissions = ['https://web.whatsapp.com/*'];
    await writeFile(path.join(directory, 'manifest.json'), JSON.stringify(manifest));
    await assert.rejects(inspectCustomerPanel(directory, '0.4.1'), /Unexpected customer panel capabilities/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
