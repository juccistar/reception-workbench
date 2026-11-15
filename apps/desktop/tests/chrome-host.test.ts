// @vitest-environment node
import { EventEmitter } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChromeWhatsAppHost } from '../src/main/whatsapp/chrome-host';

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
async function setup(fail = false, shutdownGraceMs = 10000) {
  const root = await mkdtemp(path.join(tmpdir(), 'chrome-host-test-')); roots.push(root);
  const events: any[] = [];
  const instances: MockClient[] = [];
  class MockClient extends EventEmitter {
    info = { wid: { _serialized: '123@c.us' } };
    pupPage = Object.assign(new EventEmitter(), { isClosed: () => false, bringToFront: vi.fn(async () => {}) });
    pupBrowser = Object.assign(new EventEmitter(), { close: vi.fn(async () => { this.pupBrowser.emit('disconnected'); }), process: () => null });
    getChats = vi.fn(async () => []);
    sendMessage = vi.fn(async () => ({ id: { _serialized: 'msg1' }, ack: 1 }));
    getMessageById = vi.fn(async () => ({ id: { _serialized: 'msg1' }, ack: 1 }));
    constructor(public options: any) { super(); instances.push(this); }
    async initialize() { if (fail) throw new Error('failure'); this.emit('ready'); }
  }
  const host = new ChromeWhatsAppHost({ directory: root, profileDirectory: path.join(root, 'profile'), onEvent: event => events.push(event), shutdownGraceMs, resolveExecutable: async () => 'chrome.exe', loadSdk: async () => ({ Client: MockClient }) });
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
