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
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
function setup() {
  chrome.instances.length = 0; chrome.fail = false;
  const backends: Backend[] = [];
  const release = vi.fn(async () => {});
  const store = { acquire: async () => release, directory: async () => 'test', profileDirectory: async () => 'test', list: async () => [A, B].map(id => ({ id, label: id, account_tail: '', revision: 0 })) };
  const manager = new WhatsAppAccountManager({ store, broker: {}, merchants: async () => [], available: async () => true,
    service: () => { const backend = new Backend(); backends.push(backend); return backend; },
    models: async () => ({ synchronize: async () => {} }), confirm: async () => true, version: 'test' } as any);
  return { manager, backends, release };
}
describe('account manager shutdown', () => {
  it('lists without starting services and isolates two accounts on exit', async () => {
    const { manager, backends, release } = setup();
    expect((await manager.snapshot()).accounts).toHaveLength(2);
    expect(backends).toHaveLength(0);
    await Promise.all([manager.connect(A), manager.connect(B)]);
    expect(manager.isRunning(A)).toBe(true);
    expect(manager.isRunning(B)).toBe(true);
    backends[0].die();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(manager.isRunning(B)).toBe(true);
    expect(chrome.instances[1].close).not.toHaveBeenCalled();
    await manager.close();
    expect(release).toHaveBeenCalledTimes(2);
  });
  it('coalesces merchant display probes across panel polling without starting services', async () => {
    const { manager, backends } = setup();
    const options = (manager as any).options;
    options.merchants = async () => [{ record_id: A, seller_id: '10001', name: '合成商家', verified_at: 1 }];
    options.available = vi.fn(async () => true);
    await Promise.all([manager.snapshot(), manager.detail(A), manager.detail(B)]);
    expect(options.available).toHaveBeenCalledOnce();
    expect(backends).toHaveLength(0);
    await manager.close();
  });
  it('counts only unknown operations in the launcher pending label', async () => {
    const { manager, backends } = setup();
    await manager.connect(A);
    backends[0].invoke.mockImplementation(async method => (method === 'get_whatsapp_state' ? { pending_operations: [{ status: 'unknown' }, { status: 'sending' }, { status: 'prepared' }] } : undefined) as any);
    await manager.detail(A);
    expect((await manager.snapshot()).accounts[0].pending_count).toBe(1);
    await manager.close();
  });
  it('can load an offline account without opening Chrome', async () => {
    const { manager, backends } = setup();
    const detail = await manager.detail(A, true);
    expect(detail.state).not.toBeNull();
    expect(backends).toHaveLength(1);
    expect(chrome.instances).toHaveLength(0);
    expect((await manager.snapshot()).accounts[0].runtime_status).toBe('idle');
    await manager.close();
  });
  it('keeps B running when A startup fails', async () => {
    const { manager, backends, release } = setup();
    await manager.connect(B);
    const options = (manager as any).options;
    options.models = async () => ({ synchronize: async () => { throw new Error('A startup failed'); } });
    await expect(manager.connect(A)).rejects.toThrow('A startup failed');
    expect(manager.isRunning(B)).toBe(true);
    expect(chrome.instances[0].close).not.toHaveBeenCalled();
    expect(backends[1].shutdown).toHaveBeenCalled();
    expect(release).toHaveBeenCalledTimes(1);
    await manager.close();
  });
  it('passes runtime ownership into the Chrome factory and tags backend events', async () => {
    const { manager, backends } = setup();
    const options = (manager as any).options;
    const notify = vi.fn(); options.notify = notify;
    let context: any;
    options.createChrome = async (_options: unknown, value: unknown) => { context = value; return new (await import('../src/main/whatsapp/chrome-host')).ChromeWhatsAppHost({} as any); };
    const detail = await manager.connect(A);
    expect(context.workspaceId).toBe(A);
    expect(context.runtimeInstanceId).toBe(detail.runtime_instance_id);
    expect(context.backend).toBe(backends[0]);
    backends[0].emit('event', { event: 'state.changed', data: { marker: 1 } });
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ data: { marker: 1, workspace_id: A, runtime_instance_id: detail.runtime_instance_id } }));
    await manager.close();
  });
  it('attempts closing both accounts even when one Chrome fails', async () => {
    const { manager, release } = setup();
    await manager.connect(A); await manager.connect(B);
    chrome.instances[0].close.mockRejectedValue(new Error('close failed'));
    await expect(manager.close()).rejects.toThrow('未能关闭');
    expect(chrome.instances[1].close).toHaveBeenCalled();
    expect(release).toHaveBeenCalledTimes(1);
    expect(manager.isRunning(A)).toBe(true);
    expect(manager.isRunning(B)).toBe(false);
  });
  it('closes Chrome and releases the account after an unexpected exit, allowing reconnect', async () => {
    const { manager, backends, release } = setup();
    await manager.connect(A);
    backends[0].die();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(chrome.instances[0].close).toHaveBeenCalled();
    await manager.connect(A);
    expect(backends).toHaveLength(2);
    await manager.close();
    expect(release).toHaveBeenCalledTimes(2);
  });
  it('keeps the lock if owned Chrome cannot be closed', async () => {
    const { manager, backends, release } = setup();
    await manager.connect(A);
    chrome.fail = true;
    backends[0].die();
    await vi.waitFor(() => expect(chrome.instances[0].close).toHaveBeenCalled());
    await expect(manager.close()).rejects.toThrow('未能关闭');
    expect(release).not.toHaveBeenCalled();
    expect(manager.isRunning()).toBe(true);
  });
  it('cleans up after startup fails and allows a retry', async () => {
    const { manager, backends, release } = setup();
    const options = (manager as any).options;
    let fail = true;
    options.models = async () => ({ synchronize: async () => { if (fail) throw new Error('startup failed'); } });
    await expect(manager.connect(A)).rejects.toThrow('startup failed');
    expect(release).toHaveBeenCalledTimes(1);
    expect(backends[0].shutdown).toHaveBeenCalled();
    fail = false;
    await manager.connect(A);
    await manager.close();
    expect(release).toHaveBeenCalledTimes(2);
  });
  it('waits for owned Chrome to end an in-flight send after backend death before releasing ownership', async () => {
    const { manager, backends, release } = setup();
    await manager.connect(A);
    const active = (manager as any).active.get(A);
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
    await manager.connect(A);
    let finishSync!: () => void;
    (manager as any).active.get(A).models.synchronize = () => new Promise<void>(resolve => { finishSync = resolve; });
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
    await manager.connect(A);
    await manager.close();
    expect(backends[0].invoke).toHaveBeenCalledWith('set_whatsapp_reception', { enabled: false });
    expect(release).toHaveBeenCalledTimes(1);
  });
});
