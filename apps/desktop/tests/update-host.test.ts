// @vitest-environment node
import { EventEmitter } from 'node:events';
import { generateKeyPairSync } from 'node:crypto';
import { signReleaseMetadata } from '../../../scripts/update-signing.mjs';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UpdateHost, type UpdateEngine } from '../src/main/update-host';

const pair = generateKeyPairSync('ed25519');
const publicKey = pair.publicKey.export({ type: 'spki', format: 'pem' }).toString();
const privateKey = pair.privateKey.export({ type: 'pkcs8', format: 'pem' }).toString();
function metadata(version = '0.2.16') {
  const fields = { version, artifact: 'setup.exe', sha512: Buffer.alloc(64, 1).toString('base64'), size: 100 };
  return { version, path: fields.artifact, sha512: fields.sha512, files: [{ url: fields.artifact, sha512: fields.sha512, size: fields.size }], reception_signature: signReleaseMetadata(fields, privateKey), releaseNotes: '<b>更新</b>' };
}
const roots: string[] = [];
async function setup(extra: Partial<ConstructorParameters<typeof UpdateHost>[0]> = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'update-host-')); roots.push(root);
  const engine = new FakeEngine();
  const createEngine = vi.fn(() => engine);
  const host = new UpdateHost({ currentVersion: '0.2.15', installed: true, settingsFile: path.join(root, 'settings.json'), createEngine, trustedPublicKey: publicKey, ...extra });
  return { host, engine, createEngine, file: path.join(root, 'settings.json') };
}
function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}
class FakeEngine extends EventEmitter implements UpdateEngine {
  autoDownload = true; autoInstallOnAppQuit = true; autoRunAppAfterInstall = true;
  allowDowngrade = true; allowPrerelease = true; disableWebInstaller = false; logger: unknown = console;
  installerPath: string | null = 'C:/updates/setup.exe';
  checkForUpdates = vi.fn(async (): Promise<unknown> => { this.emit('update-available', metadata()); return {}; });
  downloadUpdate = vi.fn(async (): Promise<unknown> => []);
  quitAndInstall = vi.fn((_silent?: boolean, _force?: boolean): void => undefined);
}
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

describe('manual update host', () => {
  it('keeps the built-in GitHub source until explicitly changed or cleared', async () => {
    const repository = 'https://github.com/juccistar/reception-workbench-releases/';
    const { host, file } = await setup({ defaultFeedUrl: repository });
    expect(host.getInfo().feed_url).toBe(repository);
    await host.saveSettings({ feed_url: repository + 'releases' });
    expect(host.getInfo().feed_url).toBe(repository);
    await host.saveSettings({ feed_url: '' });
    const restored = new UpdateHost({ currentVersion: '0.2.17', installed: true, settingsFile: file, defaultFeedUrl: repository });
    expect(restored.getInfo().feed_url).toBe('');
  });
  it('does not trust GitHub metadata without the fixed release signature', async () => {
    const { host, engine } = await setup({ defaultFeedUrl: 'https://github.com/juccistar/reception-workbench-releases/' });
    engine.checkForUpdates.mockImplementationOnce(async () => { engine.emit('update-available', { version: '0.2.17', files: metadata().files }); return {}; });
    expect((await host.check()).status).toBe('error');
    expect(host.getInfo().message).toContain('签名');
    expect(engine.downloadUpdate).not.toHaveBeenCalled();
    engine.checkForUpdates.mockRejectedValueOnce(new Error('ERR_UPDATER_NO_PUBLISHED_VERSIONS private-token'));
    expect((await host.check()).message).toContain('已发布含更新文件的正式版本');
    expect(JSON.stringify(host.getInfo())).not.toContain('private-token');
  });
  it('rejects tampered metadata, unsafe files, downgrades and missing trusted keys', async () => {
    const { host, engine } = await setup();
    await host.saveSettings({ feed_url: 'https://example.com/' });
    const altered = metadata(); altered.files[0].size++;
    const unsafe = metadata(); unsafe.files[0].url = 'https://evil.example/setup.exe';
    for (const candidate of [altered, unsafe, metadata('0.2.14'), metadata('0.2.15'), { ...metadata(), version: '0.2.16+build' }, { ...metadata(), packages: {} }]) {
      engine.checkForUpdates.mockImplementationOnce(async () => { engine.emit('update-available', candidate); return {}; });
      expect((await host.check()).status).toBe('error');
      await host.download(); await host.install();
    }
    expect(engine.downloadUpdate).not.toHaveBeenCalled();
    const noKey = await setup({ trustedPublicKey: undefined, defaultFeedUrl: 'https://example.com/' });
    expect((await noKey.host.check()).status).toBe('error');
    expect(noKey.createEngine).not.toHaveBeenCalled();
  });
  it('uses the default feed only until the user explicitly clears it', async () => {
    const { host, file } = await setup({ defaultFeedUrl: 'https://EXAMPLE.com:443/releases' });
    expect(host.getInfo().feed_url).toBe('https://example.com/releases/');
    await host.saveSettings({ feed_url: '' });
    const restored = new UpdateHost({ currentVersion: '0.2.15', installed: true, settingsFile: file, defaultFeedUrl: 'https://example.com/releases' });
    expect(restored.getInfo().feed_url).toBe('');
  });
  it('refuses installation when the engine has no installer path', async () => {
    const beforeInstall = vi.fn(async () => true);
    const { host, engine } = await setup({ beforeInstall });
    await host.saveSettings({ feed_url: 'https://example.com/' }); await host.check(); await host.download();
    engine.installerPath = null;
    expect((await host.install()).status).toBe('error');
    expect(beforeInstall).not.toHaveBeenCalled(); expect(engine.quitAndInstall).not.toHaveBeenCalled();
  });
  it('persists validated HTTPS directory settings even for a portable build without creating an engine', async () => {
    const { host, createEngine, file } = await setup({ installed: false });
    for (const feed_url of ['http://example.com', 'https://user:pass@example.com/', 'https://@example.com/', 'https://example.com/?token=secret', 'https://example.com/#x', 'https://example.com\\evil']) {
      expect((await host.saveSettings({ feed_url })).message).toContain('无效');
    }
    await host.saveSettings({ feed_url: 'https://example.com/releases' });
    expect(host.getInfo()).toMatchObject({ status: 'unavailable', feed_url: 'https://example.com/releases/' });
    await host.check(); await host.download(); await host.install();
    expect(createEngine).not.toHaveBeenCalled();
    const restored = new UpdateHost({ currentVersion: '1.0.0', installed: true, settingsFile: file });
    expect(restored.getInfo().feed_url).toBe('https://example.com/releases/');
  });
  it('requires explicit check, download and install, configures safeguards, and sends plain bounded notes', async () => {
    const beforeInstall = vi.fn(async () => true);
    const { host, engine, createEngine } = await setup({ beforeInstall });
    await host.saveSettings({ feed_url: 'https://example.com/' });
    expect(createEngine).not.toHaveBeenCalled();
    await host.install(); expect(beforeInstall).not.toHaveBeenCalled();
    expect(await host.check()).toMatchObject({ status: 'available', available_version: '0.2.16', release_notes: ' 更新 ' });
    expect(engine).toMatchObject({ autoDownload: false, autoInstallOnAppQuit: false, autoRunAppAfterInstall: false, allowDowngrade: false, allowPrerelease: false, disableWebInstaller: true, logger: null });
    expect(engine.downloadUpdate).not.toHaveBeenCalled();
    await host.install(); expect(beforeInstall).not.toHaveBeenCalled();
    expect((await host.download()).status).toBe('downloaded');
    expect((await host.install()).status).toBe('installing');
    expect(beforeInstall).toHaveBeenCalledWith('0.2.16', 'C:/updates/setup.exe', expect.any(String));
    expect(engine.quitAndInstall).toHaveBeenCalledWith(true, false);
  });
  it('deduplicates concurrent operations and ignores unrelated actions while busy', async () => {
    const { host, engine } = await setup(); await host.saveSettings({ feed_url: 'https://example.com/' });
    const pending = deferred<unknown>(); engine.checkForUpdates.mockImplementation(() => pending.promise);
    const one = host.check(); const two = host.check(); await vi.waitFor(() => expect(engine.checkForUpdates).toHaveBeenCalledTimes(1));
    expect(engine.checkForUpdates).toHaveBeenCalledTimes(1);
    await host.download(); expect(engine.downloadUpdate).not.toHaveBeenCalled();
    engine.emit('update-available', metadata()); pending.resolve({});
    expect((await one).status).toBe('available'); expect((await two).status).toBe('available');
    const downloading = deferred<unknown>(); engine.downloadUpdate.mockImplementation(() => downloading.promise);
    const a = host.download(); const b = host.download(); await Promise.resolve();
    engine.emit('download-progress', { percent: 51, transferred: 51, total: 100 });
    expect(host.getInfo().progress?.percent).toBe(51);
    downloading.resolve([]); await Promise.all([a, b]); expect(engine.downloadUpdate).toHaveBeenCalledTimes(1);
  });
  it('discards late results and events after changing source', async () => {
    const { host, engine } = await setup(); await host.saveSettings({ feed_url: 'https://old.example/' });
    const pending = deferred<unknown>(); engine.checkForUpdates.mockImplementation(() => pending.promise);
    const checking = host.check(); await Promise.resolve(); await Promise.resolve();
    await host.saveSettings({ feed_url: 'https://new.example/' });
    engine.emit('update-available', metadata()); pending.resolve({}); await checking;
    engine.emit('error', new Error('private URL https://old.example/?secret=1'));
    expect(host.getInfo()).toMatchObject({ status: 'idle', available_version: null, feed_url: 'https://new.example/' });
    await host.download(); expect(engine.downloadUpdate).not.toHaveBeenCalled();
  });
  it('sanitizes network failures and permits retrying failed downloads', async () => {
    const { host, engine } = await setup(); await host.saveSettings({ feed_url: 'https://example.com/' });
    engine.checkForUpdates.mockRejectedValueOnce(new Error('private secret'));
    expect(JSON.stringify(await host.check())).not.toContain('secret');
    expect(host.getInfo().status).toBe('error');
    await host.check(); engine.downloadUpdate.mockRejectedValueOnce(new Error('secret'));
    expect((await host.download()).status).toBe('available');
    expect((await host.download()).status).toBe('downloaded');
  });
  it('cancels safely and recovers from synchronous and emitted installation errors', async () => {
    const beforeInstall = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
    const onInstallError = vi.fn(async () => undefined);
    const { host, engine } = await setup({ beforeInstall, onInstallError });
    await host.saveSettings({ feed_url: 'https://example.com/' }); await host.check(); await host.download();
    expect((await host.install()).status).toBe('downloaded'); expect(engine.quitAndInstall).not.toHaveBeenCalled();
    engine.quitAndInstall.mockImplementationOnce(() => { throw new Error('secret'); });
    expect((await host.install()).status).toBe('downloaded'); expect(onInstallError).toHaveBeenCalledTimes(1);
    engine.quitAndInstall.mockImplementationOnce(() => { engine.emit('error', new Error('secret')); });
    expect((await host.install()).status).toBe('downloaded'); expect(onInstallError).toHaveBeenCalledTimes(2);
  });
});
