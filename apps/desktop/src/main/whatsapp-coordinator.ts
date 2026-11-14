import type { WhatsAppContact, WhatsAppHostInfo, WhatsAppViewRequest } from '../shared/whatsapp';
import type { WhatsAppEvent } from './whatsapp/host';
import type { FrozenIntent, LedgerRecord, SendResult } from './whatsapp/ledger';

export interface WhatsAppTransportHost {
  connect(): Promise<void>;
  close(): Promise<void>;
  getStatus(): WhatsAppHostInfo;
  setVisible(visible: boolean, bounds?: { x: number; y: number; width: number; height: number }): void;
  listChats(): Promise<WhatsAppContact[]>;
  sendFrozen(intent: FrozenIntent): Promise<SendResult>;
  readLedger(operationId: string): LedgerRecord | undefined;
  lookupMessage(messageId: string): Promise<SendResult | null>;
}
type Invoke = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
interface Options {
  invoke: Invoke;
  createHost: (receive: (event: WhatsAppEvent) => void) => Promise<WhatsAppTransportHost>;
  notify: (event: { event: string; data: unknown }) => void;
}
const disconnected: WhatsAppHostInfo = { status: 'disconnected', account_id: '', connection_epoch: '' };

export function validateWhatsAppView(value: unknown, size: { width: number; height: number }): WhatsAppViewRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('WhatsApp 窗口范围无效。');
  const input = value as Record<string, unknown>;
  if (Object.keys(input).some(key => key !== 'visible' && key !== 'bounds') || typeof input.visible !== 'boolean') throw new Error('WhatsApp 窗口范围无效。');
  if (!input.visible) return { visible: false };
  const bounds = input.bounds as Record<string, unknown> | undefined;
  if (!bounds || typeof bounds !== 'object' || Array.isArray(bounds) || Object.keys(bounds).length !== 4 || !['x', 'y', 'width', 'height'].every(key => typeof bounds[key] === 'number' && Number.isSafeInteger(bounds[key]) && Math.abs(bounds[key] as number) <= 100000)) throw new Error('WhatsApp 窗口范围无效。');
  if ((bounds.x as number) < 56 || (bounds.y as number) < 160 || (bounds.width as number) < 120 || (bounds.height as number) < 80) throw new Error('WhatsApp 窗口不能遮挡工作台控制区。');
  const x = bounds.x as number; const y = bounds.y as number;
  const width = Math.min(bounds.width as number, size.width - x);
  const height = Math.min(bounds.height as number, size.height - y - 24);
  return width >= 120 && height >= 80 ? { visible: true, bounds: { x, y, width, height } } : { visible: false };
}

/** Serializes native observations while external submissions remain independent. */
export class WhatsAppCoordinator {
  private host: WhatsAppTransportHost | null = null;
  private creating: Promise<WhatsAppTransportHost> | null = null;
  private observations: Promise<void> = Promise.resolve();
  private readonly sends = new Map<string, Promise<void>>();
  private stopping = false;
  private generation = 0;
  private accountProjection = '';
  constructor(private readonly options: Options) {}
  getStatus(): WhatsAppHostInfo { return this.host?.getStatus() ?? { ...disconnected }; }
  private error(): void { this.options.notify({ event: 'whatsapp.host.error', data: { message: 'WhatsApp 连接或回执处理未完成，请保留原操作并检查连接。' } }); }
  private enqueue(action: () => Promise<unknown>): Promise<void> {
    const next = this.observations.then(action).then(() => {});
    this.observations = next.catch(() => { this.error(); });
    return next;
  }
  async connect(): Promise<WhatsAppHostInfo> {
    this.stopping = false;
    if (!this.host) {
      if (!this.creating) {
        const generation = ++this.generation;
        this.creating = this.options.createHost(event => { if (generation === this.generation) this.receive(event); });
      }
      try { this.host = await this.creating; } finally { this.creating = null; }
    }
    if (this.stopping) { await this.host.close(); this.host = null; throw new Error('WhatsApp 正在关闭。'); }
    await this.host.connect();
    return this.getStatus();
  }
  setView(request: WhatsAppViewRequest): void { if (this.host && !this.stopping) this.host.setVisible(request.visible, request.bounds); }
  receive(event: WhatsAppEvent): void {
    let accountIdentity = '';
    if (event.type === 'account') {
      // Native failures must reach the UI even while the business queue waits.
      const { type: _type, ...info } = event;
      this.options.notify({ event: 'whatsapp.host.changed', data: info });
      const status = event.status === 'ready' ? 'connected' : ['disconnected', 'error'].includes(event.status) ? 'disconnected' : 'connecting';
      accountIdentity = JSON.stringify([status, event.account_id, event.connection_epoch]);
      if (accountIdentity === this.accountProjection) return;
      this.accountProjection = accountIdentity;
    }
    void this.enqueue(async () => {
      if (event.type === 'account') {
        const status = event.status === 'ready' ? 'connected' : ['disconnected', 'error'].includes(event.status) ? 'disconnected' : 'connecting';
        try { await this.options.invoke('_wa_set_account', { account_id: event.account_id, connection_epoch: event.connection_epoch, status }); }
        catch (error) { if (this.accountProjection === accountIdentity) this.accountProjection = ''; throw error; }
        if (status === 'connected') {
          const state = await this.options.invoke<{ pending_operations: { operation_id: string }[] }>('get_whatsapp_state');
          for (const operation of state.pending_operations) void this.reconcile(operation.operation_id).catch(() => this.error());
        }
      } else if (event.type === 'messages') {
        await this.options.invoke('_wa_ingest', { account_id: event.account_id, connection_epoch: event.connection_epoch, messages: event.messages });
      } else {
        await this.options.invoke('_wa_ack', { account_id: event.account_id, connection_epoch: event.connection_epoch, message_id: event.message_id, ack: event.ack });
      }
    }).catch(() => {});
  }
  async listChats(): Promise<WhatsAppContact[]> {
    const host = this.host;
    if (!host || this.stopping || host.getStatus().status !== 'ready') return [];
    const identity = host.getStatus();
    const chats = await host.listChats();
    if (host !== this.host || this.stopping || JSON.stringify(host.getStatus()) !== JSON.stringify(identity)) throw new Error('WhatsApp 连接已变化，请重新读取联系人。');
    for (let start = 0; start < chats.length; start += 200) {
      const messages = chats.slice(start, start + 200).map(item => ({ id: `metadata:${item.chat_id}`, chat_id: item.chat_id, name: item.name, body: '', timestamp_ms: 0, from_me: false, is_group: false, is_history: true, metadata_only: true }));
      await this.enqueue(() => this.options.invoke('_wa_ingest', { account_id: identity.account_id, connection_epoch: identity.connection_epoch, messages }));
    }
    return chats;
  }
  handleServiceEvent(event: { event: string; data?: unknown }): boolean {
    if (event.event !== 'whatsapp.send.requested' && event.event !== 'whatsapp.reconcile.requested') return false;
    const id = (event.data as { operation_id?: unknown } | undefined)?.operation_id;
    if (typeof id !== 'string' || !id || id.length > 300) { this.error(); return true; }
    if (event.event === 'whatsapp.send.requested') this.submit(id);
    else void this.reconcile(id).catch(() => this.error());
    return true;
  }
  private submit(operationId: string): void {
    if (this.sends.has(operationId) || this.stopping) return;
    const task = (async () => {
      await this.observations;
      const host = this.host;
      if (!host || this.stopping || host.getStatus().status !== 'ready') return;
      // The service freezes and durably marks the operation before revealing its body.
      const intent = await this.options.invoke<FrozenIntent>('_wa_prepare_send', { operation_id: operationId });
      let result: SendResult | undefined;
      try { result = await host.sendFrozen(intent); }
      catch { /* Unknown means reconcile this operation; never submit another key. */ }
      await this.enqueue(() => this.options.invoke('_wa_transport_result', { operation_id: intent.operation_id, account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: result ? 'submitted' : 'unknown', ...(result ?? {}) }));
    })();
    this.sends.set(operationId, task);
    void task.catch(() => this.error()).finally(() => { this.sends.delete(operationId); });
  }
  async reconcile(operationId: string): Promise<void> {
    const host = this.host;
    if (!host) return;
    const record = host.readLedger(operationId);
    if (!record?.result?.message_id) return;
    const identity = host.getStatus();
    if (identity.status !== 'ready' || identity.account_id !== record.intent.account_id) return;
    let result: SendResult | null = null;
    try { result = await host.lookupMessage(record.result.message_id); } catch { /* Stored exact submission evidence remains usable. */ }
    if (host !== this.host || host.getStatus().account_id !== identity.account_id || host.getStatus().connection_epoch !== identity.connection_epoch) return;
    if (result && result.message_id !== record.result.message_id) throw new Error('WhatsApp 回执来源无效。');
    const evidence = result && result.ack > record.result.ack ? result : record.result;
    await this.enqueue(() => this.options.invoke('_wa_transport_result', { operation_id: record.intent.operation_id, account_id: record.intent.account_id, connection_epoch: record.intent.connection_epoch, status: 'submitted', ...evidence }));
  }
  async stop(): Promise<void> {
    this.stopping = true;
    if (this.creating) await this.creating.catch(() => {});
    await this.enqueue(() => this.options.invoke('set_whatsapp_reception', { enabled: false }));
    await Promise.allSettled([...this.sends.values()]);
    await this.observations;
    const host = this.host;
    if (host) { host.setVisible(false); await host.close(); }
    await this.observations;
    this.host = null;
    ++this.generation;
  }
}
