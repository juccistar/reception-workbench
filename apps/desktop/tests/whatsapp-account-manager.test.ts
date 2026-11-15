// @vitest-environment node
import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import { WhatsAppAccountManager } from '../src/main/whatsapp-account-manager';

const chrome = vi.hoisted(() => ({ instances: [] as any[], fail: false }));
vi.mock('../src/main/whatsapp/chrome-host', () => ({
  ChromeWhatsAppHost: class {
    close = vi.fn(async () => { if (chrome.fail) throw new Error('close failed'); });
    connect = vi.fn(async () => {});
    getStatus = () => ({ status: 'disconnected', account_id: '', connection_epoch: '' });
    setVisible = () => {};
    constructor() { chrome.instances.push(this); }
  },
}));

class Backend extends EventEmitter {
  exited = false;
  invoke = vi.fn(async (method: string) => {
    if (this.exited) throw new Error('backend exited');
    return method === 'get_whatsapp_state' ? { pending_operations: [] } : undefined;
  });
  hasExited() { return this.exited; }
  shutdown = vi.fn(async () => { this.exited = true; });
  die() { this.exited = true; this.emit('event', { event: 'backend.disconnected' }); }
}
function setup() {
  chrome.instances.length = 0; chrome.fail = false;
  const backends: Backend[] = [];
  const release = vi.fn(async () => {});
  const store = { selected: async () => ({ id: 'account' }), acquire: async () => release, directory: async () => 'test', profileDirectory: async () => 'test', list: async () => [{ id: 'account', selected: true }] };
  const manager = new WhatsAppAccountManager({ store, broker: {}, merchants: async () => [], available: async () => true,
    service: () => { const backend = new Backend(); backends.push(backend); return backend; },
    models: async () => ({ synchronize: async () => {} }), confirm: async () => true, version: 'test' } as any);
  return { manager, backends, release };
}
describe('account manager shutdown', () => {
  it('closes Chrome and releases the account after an unexpected exit, allowing reconnect', async () => {
    const { manager, backends, release } = setup();
    await manager.connect();
    backends[0].die();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(chrome.instances[0].close).toHaveBeenCalled();
    await manager.connect();
    expect(backends).toHaveLength(2);
    await manager.close();
    expect(release).toHaveBeenCalledTimes(2);
  });
  it('keeps the lock if owned Chrome cannot be closed', async () => {
    const { manager, backends, release } = setup();
    await manager.connect();
    chrome.fail = true;
    backends[0].die();
    await vi.waitFor(() => expect(chrome.instances[0].close).toHaveBeenCalled());
    await expect(manager.close()).rejects.toThrow('close failed');
    expect(release).not.toHaveBeenCalled();
    expect(manager.isRunning()).toBe(true);
  });
  it('cleans up after startup fails and allows a retry', async () => {
    const { manager, backends, release } = setup();
    const options = (manager as any).options;
    let fail = true;
    options.models = async () => ({ synchronize: async () => { if (fail) throw new Error('startup failed'); } });
    await expect(manager.connect()).rejects.toThrow('startup failed');
    expect(release).toHaveBeenCalledTimes(1);
    expect(backends[0].shutdown).toHaveBeenCalled();
    fail = false;
    await manager.connect();
    await manager.close();
    expect(release).toHaveBeenCalledTimes(2);
  });
  it('waits for owned Chrome to end an in-flight send after backend death before releasing ownership', async () => {
    const { manager, backends, release } = setup();
    await manager.connect();
    const active = (manager as any).active;
    active.legacyDeclaration = '[]';
    // This test isolates the owner lifecycle; the context broker and durable
    // pre-submission freeze have separate integration tests.
    (manager as any).supplyContext = async () => {};
    const host = chrome.instances[0];
    host.getStatus = () => ({ status: 'ready', account_id: 'account', connection_epoch: 'epoch' });
    const intent = { operation_id: 'original', account_id: 'account', connection_epoch: 'epoch', chat_id: 'chat', body: 'frozen' };
    const originalInvoke = backends[0].invoke.getMockImplementation()!;
    backends[0].invoke.mockImplementation(async method => method === '_wa_prepare_send' ? intent : originalInvoke(method));
    let rejectSend!: (error: Error) => void;
    host.sendFrozen = vi.fn(() => new Promise((_resolve, reject) => { rejectSend = reject; }));
    let finishClose!: () => void;
    const closed = new Promise<void>(resolve => { finishClose = () => { rejectSend(new Error('owned browser exited')); resolve(); }; });
    host.close.mockImplementation(() => closed);
    backends[0].emit('event', { event: 'whatsapp.send.requested', data: { operation_id: 'original' } });
    await vi.waitFor(() => expect(host.sendFrozen).toHaveBeenCalledWith(intent));
    backends[0].die();
    await vi.waitFor(() => expect(host.close).toHaveBeenCalled());
    expect(release).not.toHaveBeenCalled();
    finishClose();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(backends[0].invoke.mock.calls.filter(([method]) => method === '_wa_prepare_send')).toHaveLength(1);
    await manager.close();
    expect(release).toHaveBeenCalledTimes(1);
  });
  it('drains model synchronization before closing the backend and releasing its lock', async () => {
    const { manager, backends, release } = setup();
    await manager.connect();
    let finishSync!: () => void;
    (manager as any).active.models.synchronize = () => new Promise<void>(resolve => { finishSync = resolve; });
    const sync = manager.modelsChanged();
    const closing = manager.close();
    await vi.waitFor(() => expect(chrome.instances[0].close).toHaveBeenCalled());
    expect(backends[0].shutdown).not.toHaveBeenCalled();
    expect(release).not.toHaveBeenCalled();
    finishSync();
    await Promise.all([sync, closing]);
    expect(backends[0].shutdown).toHaveBeenCalledTimes(1);
    expect(release).toHaveBeenCalledTimes(1);
  });
  it('revokes reception during a healthy shutdown', async () => {
    const { manager, backends, release } = setup();
    await manager.connect();
    await manager.close();
    expect(backends[0].invoke).toHaveBeenCalledWith('set_whatsapp_reception', { enabled: false });
    expect(release).toHaveBeenCalledTimes(1);
  });
});
