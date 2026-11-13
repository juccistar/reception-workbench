import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { stageWhatsAppRuntime, verifyWhatsAppRuntime } from '../whatsapp-runtime.mjs';

test('stages an independently loadable locked runtime and detects unexpected or modified files', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'wa-runtime-'));
  try {
    const manifest = await stageWhatsAppRuntime(dir);
    assert.ok(manifest.files.some(file => file.path.endsWith('/puppeteer-core/lib/cjs/puppeteer/generated/injected.js')));
    await verifyWhatsAppRuntime(dir);
    const require = createRequire(path.join(dir, 'entry.cjs'));
    assert.equal(typeof require('whatsapp-web.js').Client, 'function');
    assert.equal(typeof require('puppeteer').connect, 'function');
    await writeFile(path.join(dir, 'node_modules', 'unexpected.js'), 'unexpected');
    await assert.rejects(verifyWhatsAppRuntime(dir), /Unlisted/);
    await rm(path.join(dir, 'node_modules', 'unexpected.js'));
    await writeFile(path.join(dir, ...manifest.files[0].path.split('/')), 'altered');
    await assert.rejects(verifyWhatsAppRuntime(dir), /Changed/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
