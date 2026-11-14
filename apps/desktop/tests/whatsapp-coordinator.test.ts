// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { WhatsAppCoordinator, validateWhatsAppView, type WhatsAppTransportHost } from '../src/main/whatsapp-coordinator';
const intent = { operation_id: 'operation', account_id: 'self@c.us', connection_epoch: 'epoch-old', chat_id: 'customer@lid', body: 'Frozen reply' };
function setup() {
  const invoke = vi.fn(async (method: string, _params?: Record<string, unknown>): Promise<unknown> => method === '_wa_prepare_send' ? intent : method === 'get_whatsapp_state' ? { pending_operations: [] } : {});
  const host = { connect: vi.fn(async () => {}), close: vi.fn(async () => {}), getStatus: vi.fn(() => ({ status: 'ready', account_id: intent.account_id, connection_epoch: intent.connection_epoch })), setVisible: vi.fn(), listChats: vi.fn(async () => [{ chat_id: intent.chat_id, name: 'Synthetic customer' }]), sendFrozen: vi.fn(async () => ({ message_id: 'exact-id', ack: 0 })), readLedger: vi.fn(() => undefined as ReturnType<WhatsAppTransportHost['readLedger']>), lookupMessage: vi.fn(async () => ({ message_id: 'exact-id', ack: 1 })) };
  const notify = vi.fn();
  const coordinator = new WhatsAppCoordinator({ invoke: invoke as never, createHost: async () => host, notify });
  return { coordinator, host, invoke, notify };
}
const request = { event: 'whatsapp.send.requested', data: { operation_id: intent.operation_id } };

describe('WhatsApp host/service coordination', () => {
  it('keeps new inbound observations moving while a frozen SDK send waits', async () => {
    const { coordinator, host, invoke } = setup(); await coordinator.connect();
    let finish!: (result: { message_id: string; ack: number }) => void;
    host.sendFrozen.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    expect(coordinator.handleServiceEvent(request)).toBe(true);
    await vi.waitFor(() => expect(host.sendFrozen).toHaveBeenCalledWith(intent));
    coordinator.receive({ type: 'messages', account_id: intent.account_id, connection_epoch: intent.connection_epoch, messages: [{ id: 'new-buyer-id', chat_id: intent.chat_id, body: 'More context', timestamp_ms: 1, from_me: false, name: '', is_group: false, is_history: false }] });
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('_wa_ingest', expect.objectContaining({ messages: [expect.objectContaining({ id: 'new-buyer-id' })] })));
    coordinator.handleServiceEvent(request);
    expect(host.sendFrozen).toHaveBeenCalledTimes(1);
    finish({ message_id: 'exact-id', ack: 0 });
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('_wa_transport_result', { operation_id: intent.operation_id, account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: 'submitted', message_id: 'exact-id', ack: 0 }));
  });
  it('preserves unknown submission on an SDK exception and never manufactures acceptance', async () => {
    const { coordinator, host, invoke } = setup(); await coordinator.connect();
    host.sendFrozen.mockRejectedValue(new Error('Potential SDK private diagnostics'));
    coordinator.handleServiceEvent(request);
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('_wa_transport_result', { operation_id: intent.operation_id, account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: 'unknown' }));
  });
  it('reconciles the exact recorded id using the original epoch after reconnect', async () => {
    const { coordinator, host, invoke } = setup(); await coordinator.connect();
    host.getStatus.mockReturnValue({ status: 'ready', account_id: intent.account_id, connection_epoch: 'epoch-new' });
    host.readLedger.mockReturnValue({ intent, result: { message_id: 'exact-id', ack: 0 } });
    await coordinator.reconcile(intent.operation_id);
    expect(host.lookupMessage).toHaveBeenCalledWith('exact-id');
    expect(host.sendFrozen).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith('_wa_transport_result', expect.objectContaining({ connection_epoch: 'epoch-old', message_id: 'exact-id', ack: 1 }));
    host.getStatus.mockReturnValue({ status: 'ready', account_id: 'different-account', connection_epoch: 'epoch-new' });
    await coordinator.reconcile(intent.operation_id);
    expect(host.lookupMessage).toHaveBeenCalledTimes(1);
  });
  it('revokes first, waits for a submitted result, then closes the native connection', async () => {
    const { coordinator, host, invoke } = setup(); await coordinator.connect();
    let finish!: (result: { message_id: string; ack: number }) => void;
    host.sendFrozen.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    coordinator.handleServiceEvent(request);
    await vi.waitFor(() => expect(host.sendFrozen).toHaveBeenCalled());
    const stopped = coordinator.stop();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('set_whatsapp_reception', { enabled: false }));
    expect(host.close).not.toHaveBeenCalled();
    finish({ message_id: 'exact-id', ack: 1 }); await stopped;
    expect(invoke).toHaveBeenCalledWith('_wa_transport_result', expect.objectContaining({ message_id: 'exact-id', ack: 1 }));
    expect(host.close).toHaveBeenCalledTimes(1);
  });
  it('waits for the in-flight result and closes the old host before committing a new generation', async () => {
    const { coordinator, host, invoke } = setup(); await coordinator.connect();
    let finish!: (result: { message_id: string; ack: number }) => void;
    host.sendFrozen.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    coordinator.handleServiceEvent(request);
    await vi.waitFor(() => expect(host.sendFrozen).toHaveBeenCalled());
    const commit = vi.fn(async () => { expect(host.close).toHaveBeenCalledOnce(); expect(invoke).toHaveBeenCalledWith('_wa_transport_result', expect.objectContaining({ message_id: 'exact-id' })); });
    const rebuilding = coordinator.rebuild(commit);
    await expect(coordinator.connect()).rejects.toThrow('正在重建');
    expect(commit).not.toHaveBeenCalled();
    finish({ message_id: 'exact-id', ack: 0 });
    await rebuilding;
    expect(commit).toHaveBeenCalledOnce();
  });
  it('does not commit when old host cleanup fails', async () => {
    const { coordinator, host } = setup(); await coordinator.connect();
    host.close.mockRejectedValue(new Error('cleanup failed'));
    const commit = vi.fn(async () => {});
    await expect(coordinator.rebuild(commit)).rejects.toThrow('cleanup failed');
    expect(commit).not.toHaveBeenCalled();
  });
  it('registers a chosen SDK contact as metadata without generating a new buyer message', async () => {
    const { coordinator, invoke } = setup(); await coordinator.connect(); await coordinator.listChats();
    expect(invoke).toHaveBeenCalledWith('_wa_ingest', expect.objectContaining({ messages: [expect.objectContaining({ metadata_only: true, is_history: true, body: '', chat_id: intent.chat_id })] }));
  });
  it('publishes a native SDK failure immediately while business observations are blocked', async () => {
    const { coordinator, invoke, notify } = setup(); await coordinator.connect();
    let release!: () => void;
    invoke.mockImplementation(async method => { if (method === '_wa_ingest') await new Promise<void>(done => { release = done; }); return {}; });
    coordinator.receive({ type: 'messages', account_id: intent.account_id, connection_epoch: intent.connection_epoch, messages: [] });
    await vi.waitFor(() => expect(release).toBeTypeOf('function'));
    coordinator.receive({ type: 'account', status: 'error', account_id: intent.account_id, connection_epoch: intent.connection_epoch, error_code: 'sdk_error', phase: 'sdk_callbacks', page_state: 'available', diagnostic_id: 'wa_diag_00000000-0000-4000-8000-000000000001' });
    expect(notify).toHaveBeenCalledWith({ event: 'whatsapp.host.changed', data: expect.objectContaining({ status: 'error', phase: 'sdk_callbacks', page_state: 'available' }) });
    expect(invoke).not.toHaveBeenCalledWith('_wa_set_account', expect.anything());
    release();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('_wa_set_account', { account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: 'disconnected' }));
  });
  it('does not repeat business account synchronization for native page-only progress', async () => {
    const { coordinator, invoke, notify } = setup();
    coordinator.receive({ type: 'account', status: 'connecting', account_id: '', connection_epoch: '', phase: 'page_navigation', page_state: 'loading' });
    coordinator.receive({ type: 'account', status: 'qr', account_id: '', connection_epoch: '', phase: 'qr_wait', page_state: 'available' });
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('_wa_set_account', { account_id: '', connection_epoch: '', status: 'connecting' }));
    expect(invoke.mock.calls.filter(([method]) => method === '_wa_set_account')).toHaveLength(1);
    expect(notify.mock.calls.filter(([event]) => event.event === 'whatsapp.host.changed')).toHaveLength(2);
  });
  it('clamps native bounds and rejects any attempt to cover trusted controls', () => {
    expect(validateWhatsAppView({ visible: true, bounds: { x: 184, y: 240, width: 1300, height: 1000 } }, { width: 1440, height: 960 })).toEqual({ visible: true, bounds: { x: 184, y: 240, width: 1256, height: 696 } });
    expect(() => validateWhatsAppView({ visible: true, bounds: { x: 0, y: 0, width: 1440, height: 960 } }, { width: 1440, height: 960 })).toThrow('控制区');
    expect(() => validateWhatsAppView({ visible: true, bounds: { x: 184, y: 240, width: Number.NaN, height: 600 } }, { width: 1440, height: 960 })).toThrow();
    expect(() => validateWhatsAppView({ visible: false, evaluate: 'arbitrary' }, { width: 1440, height: 960 })).toThrow();
  });
});
