// @vitest-environment node
import { EventEmitter } from 'node:events';
import { EventEmitter as PuppeteerEventEmitter } from 'puppeteer';
import { mkdtemp, rm, mkdir, writeFile, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChromeWhatsAppHost } from '../src/main/whatsapp/chrome-host';

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
async function setup(fail = false, shutdownGraceMs = 10000, extra: Record<string, any> = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'chrome-host-test-')); roots.push(root);
  const events: any[] = [];
  const instances: MockClient[] = [];
  class MockClient extends EventEmitter {
    info = { wid: { _serialized: extra.account ?? '123@c.us' } };
    getContactLidAndPhone = extra.getContactLidAndPhone;
    pupPage = Object.assign(new PuppeteerEventEmitter() as any, { isClosed: () => false, bringToFront: vi.fn(async () => {}) });
    pupBrowser = Object.assign(new PuppeteerEventEmitter() as any, { close: vi.fn(async () => { this.pupBrowser.emit('disconnected'); }), process: () => null, installExtension: extra.installExtension ?? vi.fn(async () => 'a'.repeat(32)) });
    getChats = vi.fn(async () => []);
    sendMessage = vi.fn(async () => ({ id: { _serialized: 'msg1' }, ack: 1 }));
    getMessageById = vi.fn(async () => ({ id: { _serialized: 'msg1' }, ack: 1 }));
    constructor(public options: any) { super(); instances.push(this); }
    async initialize() { if (fail) throw new Error('failure'); this.emit('ready'); }
  }
  const host = new ChromeWhatsAppHost({ directory: root, profileDirectory: path.join(root, 'profile'), onEvent: event => events.push(event), shutdownGraceMs, resolveExecutable: async () => 'chrome.exe', loadSdk: async () => ({ Client: MockClient }), ...extra });
  await host.connect(); await tick();
  return { host, events, instances };
}
describe('Chrome WhatsApp host', () => {
  it('uses one visible Chrome profile and sends with a durable exact receipt', async () => {
    const { host, instances } = await setup(); const client = instances[0];
    expect(client.options).toMatchObject({ userAgent: false, puppeteer: { executablePath: 'chrome.exe', headless: false, pipe: true } });
    expect(client.options.puppeteer.userDataDir).toContain('profile');
    const info = host.getStatus(); expect(info.status).toBe('ready');
    const intent = { operation_id: 'one', account_id: info.account_id, connection_epoch: info.connection_epoch, chat_id: '4@c.us', body: 'hi' };
    expect(await host.sendFrozen(intent)).toEqual({ message_id: 'msg1', ack: 1 });
    expect(host.readLedger('one')?.result?.message_id).toBe('msg1');
    await expect(host.sendFrozen(intent)).rejects.toThrow('already recorded');
    expect(client.sendMessage).toHaveBeenCalledTimes(1);
    await host.open(); expect(client.pupPage.bringToFront).toHaveBeenCalledOnce();
    await host.close(); expect(client.pupBrowser.close).toHaveBeenCalledOnce();
  });
  it('removes Puppeteer listeners through off without assuming Node removeListener', async () => {
    const { host, instances } = await setup();
    const client = instances[0];
    expect(client.pupBrowser.removeListener).toBeUndefined();
    expect(client.pupPage.removeListener).toBeUndefined();
    await host.close();
    expect(client.pupBrowser.listenerCount('disconnected')).toBe(0);
    expect(client.pupPage.listenerCount('close')).toBe(0);
    expect(client.pupPage.listenerCount('framenavigated')).toBe(0);
  });
  it('preserves the visible page on initialization failure and closes it before retry', async () => {
    const { host, instances } = await setup(true); const first = instances[0];
    expect(host.getStatus().status).toBe('error'); expect(first.pupBrowser.close).not.toHaveBeenCalled();
    await host.open(); await host.connect(); await tick();
    expect(first.pupBrowser.close).toHaveBeenCalledOnce(); expect(instances).toHaveLength(2);
    await host.close();
  });
  it('retires immediately on page close and ignores late callbacks', async () => {
    const { host, instances, events } = await setup(); const client = instances[0];
    client.pupPage.emit('close'); expect(host.getStatus().status).toBe('disconnected');
    const count = events.length; client.emit('ready'); client.emit('message_create', { id: 'x', from: '4@c.us' });
    expect(events).toHaveLength(count); await host.close();
  });
  it('waits for an already submitted send before closing the browser', async () => {
    const { host, instances } = await setup(); const client = instances[0], info = host.getStatus();
    let settle!: (value: any) => void;
    client.sendMessage.mockImplementationOnce(() => new Promise(resolve => { settle = resolve; }));
    const send = host.sendFrozen({ operation_id: 'pending', account_id: info.account_id, connection_epoch: info.connection_epoch, chat_id: '4@c.us', body: 'hi' });
    const closing = host.close(); await tick();
    expect(client.pupBrowser.close).not.toHaveBeenCalled();
    settle({ id: { _serialized: 'msg2' }, ack: 0 });
    expect((await send).ack).toBe(0); await closing;
    expect(host.readLedger('pending')?.result?.message_id).toBe('msg2');
    expect(client.pupBrowser.close).toHaveBeenCalledOnce();
  });
  it('waits for the owned process exit before rejecting a hanging send and ignores its late result', async () => {
    const { host, instances } = await setup(false, 5); const client = instances[0], info = host.getStatus();
    const child = Object.assign(new EventEmitter(), { exitCode: null as number | null, signalCode: null as string | null });
    Object.assign(client.pupBrowser, { process: () => child, close: vi.fn(async () => {}) });
    let finish!: (value: any) => void;
    client.sendMessage.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const send = host.sendFrozen({ operation_id: 'hung', account_id: info.account_id, connection_epoch: info.connection_epoch, chat_id: '4@c.us', body: 'hi' });
    const rejected = expect(send).rejects.toThrow('transport closed');
    let closed = false;
    const closing = host.close().then(() => { closed = true; });
    await tick(); expect(client.pupBrowser.close).toHaveBeenCalledOnce(); expect(closed).toBe(false);
    child.exitCode = 0; child.emit('exit'); await closing; await rejected;
    finish({ id: { _serialized: 'late' }, ack: 1 }); await tick();
    expect(host.readLedger('hung')?.result).toBeUndefined();
    expect(child.listenerCount('exit')).toBe(0);
  });
  it('releases hanging lookup and history after the browser exits', async () => {
    const { host, instances } = await setup(false, 5); const client = instances[0];
    client.getMessageById.mockImplementationOnce(() => new Promise(() => {}));
    client.getChats.mockImplementationOnce(() => new Promise(() => {}));
    const lookup = expect(host.lookupMessage('msg1')).rejects.toThrow('transport closed');
    const chats = expect(host.listChats()).rejects.toThrow('transport closed');
    await host.close(); await Promise.all([lookup, chats]);
  });
  it('retains ownership across a delayed launch and does not wait for hung initialization after exit', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-host-launch-')); roots.push(root);
    let client!: MockClient;
    class MockClient extends EventEmitter {
      pupBrowser: any; pupPage: any;
      constructor(_options: any) { super(); client = this; }
      initialize() { return new Promise<void>(() => {}); }
    }
    const host = new ChromeWhatsAppHost({ directory: root, profileDirectory: path.join(root, 'profile'), onEvent: () => {}, shutdownGraceMs: 5, resolveExecutable: async () => 'chrome.exe', loadSdk: async () => ({ Client: MockClient }) });
    await host.connect();
    let closed = false;
    const closing = host.close().then(() => { closed = true; });
    await tick(); expect(closed).toBe(false);
    const browser = Object.assign(new EventEmitter(), { process: () => null, close: vi.fn(async () => {}) });
    client.pupBrowser = browser;
    await closing; expect(browser.close).toHaveBeenCalledOnce();
  });
  it('installs the fixed extension through the owned browser and closes it before the browser', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root);
    await writeFile(path.join(root, 'manifest.json'), '{}');
    const order: string[] = [];
    const ready = vi.fn(async () => { order.push('ready'); });
    const closed = vi.fn(async () => { order.push('panel-closed'); });
    const opened = vi.fn(async () => { order.push('open'); });
    const { host, instances } = await setup(false, 10000, { extensionDirectory: root, onBrowserReady: ready, onBrowserClosed: closed, onOpen: opened });
    const client = instances[0];
    await vi.waitFor(() => expect(ready).toHaveBeenCalledWith(client.pupBrowser, client.pupPage, 'a'.repeat(32)));
    expect(client.options.puppeteer).toMatchObject({ enableExtensions: true, headless: false, pipe: true });
    expect(client.pupBrowser.installExtension).toHaveBeenCalledWith(root);
    await host.open(); expect(opened).toHaveBeenCalledOnce();
    client.pupBrowser.close.mockImplementation(async () => { order.push('browser-closed'); });
    await host.close(); expect(order).toEqual(['ready', 'open', 'panel-closed', 'browser-closed']);
  });
  it('opens the sidebar after a user opens the window while installation is pending', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root); await writeFile(path.join(root, 'manifest.json'), '{}');
    let finish!: (id: string) => void;
    const installExtension = vi.fn(() => new Promise<string>(resolve => { finish = resolve; }));
    const opened = vi.fn(async () => {}), ready = vi.fn(async () => {});
    const { host } = await setup(false, 5, { extensionDirectory: root, installExtension, onBrowserReady: ready, onOpen: opened });
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    await host.open(); expect(opened).not.toHaveBeenCalled(); finish('a'.repeat(32));
    await vi.waitFor(() => expect(opened).toHaveBeenCalledOnce()); expect(ready).toHaveBeenCalledOnce(); await host.close();
  });
  it('does not attach a late extension to a closed runtime', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root);
    await writeFile(path.join(root, 'manifest.json'), '{}');
    let finish!: (id: string) => void;
    const installExtension = vi.fn(() => new Promise<string>(resolve => { finish = resolve; }));
    const ready = vi.fn(async () => {}), closed = vi.fn(async () => {});
    const { host } = await setup(false, 5, { extensionDirectory: root, installExtension, onBrowserReady: ready, onBrowserClosed: closed });
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    await host.close(); finish('a'.repeat(32)); await tick();
    expect(ready).not.toHaveBeenCalled(); expect(closed).not.toHaveBeenCalled();
  });
  it('does not attach an old installation after reconnecting', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root); await writeFile(path.join(root, 'manifest.json'), '{}');
    let finish!: (id: string) => void;
    const installExtension = vi.fn().mockImplementationOnce(() => new Promise<string>(resolve => { finish = resolve; })).mockResolvedValue('b'.repeat(32));
    const ready = vi.fn(async () => {});
    const { host, instances } = await setup(false, 5, { extensionDirectory: root, installExtension, onBrowserReady: ready });
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    instances[0].pupPage.emit('close'); await host.connect();
    await vi.waitFor(() => expect(ready).toHaveBeenCalledTimes(1));
    expect(ready).toHaveBeenCalledWith(instances[1].pupBrowser, instances[1].pupPage, 'b'.repeat(32));
    finish('a'.repeat(32)); await tick(); expect(ready).toHaveBeenCalledTimes(1); await host.close();
  });
  it('waits for an attaching sidebar to be destroyed before closing Chrome', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root); await writeFile(path.join(root, 'manifest.json'), '{}');
    let finish!: () => void; const closed = vi.fn(async () => {});
    const ready = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const { host, instances } = await setup(false, 5, { extensionDirectory: root, onBrowserReady: ready, onBrowserClosed: closed });
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    const closing = host.close(); await tick(); expect(instances[0].pupBrowser.close).not.toHaveBeenCalled();
    finish(); await closing; expect(closed).toHaveBeenCalledOnce(); expect(instances[0].pupBrowser.close).toHaveBeenCalledOnce();
  });
  it('leaves WhatsApp connected when extension installation fails', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root);
    const ready = vi.fn(async () => {});
    const { host, instances } = await setup(false, 10000, { extensionDirectory: root, onBrowserReady: ready });
    expect(host.getStatus().status).toBe('ready');
    expect(instances[0].pupBrowser.installExtension).not.toHaveBeenCalled(); expect(ready).not.toHaveBeenCalled();
    await host.close();
  });
  it('rejects a linked extension directory', async () => {
    const root = await mkdtemp(path.join(tmpdir(), 'chrome-extension-')); roots.push(root);
    const actual = path.join(root, 'actual'), alias = path.join(root, 'alias'); await mkdir(actual); await writeFile(path.join(actual, 'manifest.json'), '{}');
    await symlink(actual, alias, 'junction');
    const ready = vi.fn(async () => {});
    const { host, instances } = await setup(false, 10000, { extensionDirectory: alias, onBrowserReady: ready });
    expect(host.getStatus().status).toBe('ready'); expect(instances[0].pupBrowser.installExtension).not.toHaveBeenCalled(); expect(ready).not.toHaveBeenCalled(); await host.close();
  });
  it('authorizes LID only with exact SDK mapping while retaining the raw account', async () => {
    const authorize = vi.fn(async () => {});
    const getContactLidAndPhone = vi.fn(async () => [{ lid: '123@lid', pn: '456@c.us' }]);
    const { host } = await setup(false, 10000, { account: '123@lid', authorizeAccount: authorize, getContactLidAndPhone });
    expect(getContactLidAndPhone).toHaveBeenCalledWith(['123@lid']); expect(authorize).toHaveBeenCalledWith('456@c.us'); expect(host.getStatus()).toMatchObject({ status: 'ready', account_id: '123@lid' }); await host.close();
  });
  it.each([undefined, [{ lid: 'other@lid', pn: '456@c.us' }], [{ lid: '123@lid', pn: 'bad@c.us' }]])('fails closed without a proven LID mapping %#', async mapping => {
    const authorize = vi.fn(async () => {});
    const { host } = await setup(false, 10000, { account: '123@lid', authorizeAccount: authorize, getContactLidAndPhone: mapping && vi.fn(async () => mapping) });
    expect(host.getStatus()).toMatchObject({ status: 'error', error_code: 'auth_failed' }); expect(authorize).not.toHaveBeenCalled(); await host.close();
  });
  it('identifies a single active chat only while both reads and identity agree', async () => {
    const { host, instances } = await setup(); const client = instances[0];
    const chats: any[] = [{ active: true, id: { _serialized: '456@c.us' } }], channels: any[] = [];
    const oldWindow = (globalThis as any).window;
    const collections: any = { Chat: { getModelsArray: () => chats }, WAWebNewsletterCollection: { getModelsArray: () => channels } };
    let legacy: any;
    (globalThis as any).window = { require: (name: string) => {
      if (name === 'WAWebCollections') return collections;
      if (name === 'WAWebNewsletterCollection' && legacy) return legacy;
      throw new Error(`Unknown module: ${name}`);
    } };
    Object.assign(client.pupPage, { url: () => 'https://web.whatsapp.com/', evaluate: async (fn: () => unknown) => fn() });
    (client as any).getChatById = vi.fn(async () => ({ id: { _serialized: '456@c.us' }, isGroup: false, name: 'Customer' }));
    try {
      expect(await host.resolveCurrentChat()).toEqual({ chat_id: '456@c.us', name: 'Customer' });
      chats[0].id = { $1: '456@c.us' };
      (client as any).getChatById.mockResolvedValueOnce({ id: { $1: '456@c.us' }, isGroup: false });
      expect(await host.resolveCurrentChat()).toEqual({ chat_id: '456@c.us' });
      chats[0].id = { _serialized: '456@c.us', $1: '789@c.us' }; expect(await host.resolveCurrentChat()).toBeNull();
      chats[0].id = { _serialized: '456@c.us' };
      delete collections.WAWebNewsletterCollection;
      expect(await host.resolveCurrentChat()).toBeNull();
      legacy = { default: { getModelsArray: () => channels } };
      expect(await host.resolveCurrentChat()).toEqual({ chat_id: '456@c.us', name: 'Customer' });
      collections.WAWebNewsletterCollection = null;
      expect(await host.resolveCurrentChat()).toBeNull();
      collections.WAWebNewsletterCollection = { getModelsArray: () => null };
      expect(await host.resolveCurrentChat()).toBeNull();
      collections.WAWebNewsletterCollection = { getModelsArray: () => { throw new Error('unreadable'); } };
      expect(await host.resolveCurrentChat()).toBeNull();
      collections.WAWebNewsletterCollection = { getModelsArray: () => channels };
      collections.Chat = { getModelsArray: () => ({}) }; expect(await host.resolveCurrentChat()).toBeNull();
      collections.Chat = { getModelsArray: () => { throw new Error('unreadable'); } }; expect(await host.resolveCurrentChat()).toBeNull();
      collections.Chat = { getModelsArray: () => chats };
      chats[0].active = false; expect(await host.resolveCurrentChat()).toBeNull(); chats[0].active = true;
      chats[0].id = { _serialized: '456@g.us' }; expect(await host.resolveCurrentChat()).toBeNull();
      chats[0].id = { _serialized: '456@c.us' };
      chats.push({ active: true, id: { _serialized: '789@c.us' } }); expect(await host.resolveCurrentChat()).toBeNull(); chats.pop();
      channels.push({ active: true }); expect(await host.resolveCurrentChat()).toBeNull(); channels.pop();
      (client as any).getChatById.mockImplementationOnce(async () => { chats[0].id._serialized = '789@c.us'; return { id: '456@c.us', isGroup: false }; });
      expect(await host.resolveCurrentChat()).toBeNull();
      chats[0].id._serialized = '456@c.us'; (client as any).getChatById.mockResolvedValueOnce({ id: '456@c.us', isGroup: true }); expect(await host.resolveCurrentChat()).toBeNull();
      (client as any).getChatById.mockResolvedValueOnce({ id: { _serialized: '456@c.us', $1: '789@c.us' }, isGroup: false }); expect(await host.resolveCurrentChat()).toBeNull();
      const epoch = (host as any).status.connection_epoch;
      (client as any).getChatById.mockImplementationOnce(async () => { (host as any).status.connection_epoch = 'changed'; return { id: '456@c.us', isGroup: false }; });
      expect(await host.resolveCurrentChat()).toBeNull(); (host as any).status.connection_epoch = epoch;
      (client as any).getChatById.mockImplementationOnce(async () => { client.pupPage.emit('close'); return { id: '456@c.us', isGroup: false }; }); expect(await host.resolveCurrentChat()).toBeNull();
    } finally { (globalThis as any).window = oldWindow; await host.close(); }
  });
  it('rejects stale identities and never resends an unknown submission', async () => {
    const { host, instances } = await setup(); const client = instances[0], info = host.getStatus();
    const intent = { operation_id: 'unknown', account_id: info.account_id, connection_epoch: info.connection_epoch, chat_id: '4@c.us', body: 'hi' };
    await expect(host.sendFrozen({ ...intent, connection_epoch: 'old' })).rejects.toThrow('stale');
    client.sendMessage.mockRejectedValueOnce(new Error('uncertain'));
    await expect(host.sendFrozen(intent)).rejects.toThrow('uncertain');
    await expect(host.sendFrozen(intent)).rejects.toThrow('already recorded');
    expect(host.readLedger('unknown')?.result).toBeUndefined(); expect(client.sendMessage).toHaveBeenCalledOnce();
    await host.close();
  });
});
