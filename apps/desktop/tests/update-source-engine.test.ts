// @vitest-environment node
import { mkdtemp, rm } from 'node:fs/promises';
import { generateKeyPairSync } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { signReleaseMetadata } from '../../../scripts/update-signing.mjs';
import { UpdateHost } from '../src/main/update-host';

const observed = vi.hoisted(() => ({ options: null as any, listener: null as any, candidate: null as any }));
vi.mock('electron-updater', async () => {
  const { EventEmitter } = await import('node:events');
  return { NsisUpdater: class extends EventEmitter {
    netSession = { webRequest: { onBeforeRequest: (listener: any) => { observed.listener = listener; } } };
    constructor(options: unknown) { super(); observed.options = options; }
    async checkForUpdates() { this.emit('update-available', observed.candidate); return {}; }
    async downloadUpdate() { return []; }
  } };
});
const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

async function host(feed: string) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'update-provider-')); roots.push(root);
  const pair = generateKeyPairSync('ed25519');
  const fields = { version: '0.2.18', artifact: 'Setup-0.2.18.exe', sha512: Buffer.alloc(64, 1).toString('base64'), size: 100 };
  observed.candidate = { ...fields, path: fields.artifact, files: [{ url: fields.artifact, sha512: fields.sha512, size: fields.size }],
    reception_signature: signReleaseMetadata(fields, pair.privateKey.export({ type: 'pkcs8', format: 'pem' })) };
  const result = new UpdateHost({ currentVersion: '0.2.17', installed: true, settingsFile: path.join(root, 'settings.json'),
    defaultFeedUrl: feed, trustedPublicKey: pair.publicKey.export({ type: 'spki', format: 'pem' }).toString() });
  expect((await result.check()).status).toBe('available');
  return result;
}
function allowed(url: string) {
  const callback = vi.fn(); observed.listener({ url }, callback);
  return callback.mock.calls[0][0].cancel === false;
}

describe('actual update engine factory wiring', () => {
  it('selects the public GitHub provider and installs the restricted download policy', async () => {
    await host('https://github.com/juccistar/reception-workbench-releases/');
    expect(observed.options).toEqual({ provider: 'github', owner: 'juccistar', repo: 'reception-workbench-releases', private: false });
    expect(allowed('https://github.com/juccistar/reception-workbench-releases/releases.atom')).toBe(true);
    expect(allowed('https://release-assets.githubusercontent.com/fixture?sig=synthetic')).toBe(true);
    expect(allowed('https://github.com/other/repo/releases.atom')).toBe(false);
    expect(allowed('http://release-assets.githubusercontent.com/fixture')).toBe(false);
    expect(allowed('https://release-assets.githubusercontent.com.evil.example/fixture')).toBe(false);
  });
  it('preserves strict same-origin downloads for a generic source', async () => {
    await host('https://updates.example/stable/');
    expect(observed.options).toEqual({ provider: 'generic', url: 'https://updates.example/stable/' });
    expect(allowed('https://updates.example/stable/latest.yml')).toBe(true);
    expect(allowed('https://release-assets.githubusercontent.com/fixture')).toBe(false);
  });
});
