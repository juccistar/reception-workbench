import { createHash } from 'node:crypto';
import type { WhatsAppContact, WhatsAppHostInfo } from '../shared/whatsapp';
import type { WhatsAppEvent } from './whatsapp/events';
import type { FrozenIntent, LedgerRecord, SendResult } from './whatsapp/ledger';

export interface WhatsAppTransportHost {
  connect(): Promise<void>;
  close(): Promise<void>;
  getStatus(): WhatsAppHostInfo;
  listChats(): Promise<WhatsAppContact[]>;
  sendFrozen(intent: FrozenIntent): Promise<SendResult>;
  readLedger(operationId: string): LedgerRecord | undefined;
  lookupMessage(messageId: string): Promise<SendResult | null>;
  downloadImage?(account: string, epoch: string, chatId: string, messageId: string, consume: (mime: string, size: number, chunks: AsyncIterable<Buffer>) => Promise<void>): Promise<void>;
}
type Invoke = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
interface Options {
  invoke: Invoke;
  createHost: (receive: (event: WhatsAppEvent) => void) => Promise<WhatsAppTransportHost>;
  notify: (event: { event: string; data: unknown }) => void;
  shutdownGraceMs?: number;
}
const disconnected: WhatsAppHostInfo = { status: 'disconnected', account_id: '', connection_epoch: '' };

/** Serializes native observations while external submissions remain independent. */
export class WhatsAppCoordinator {
  private host: WhatsAppTransportHost | null = null;
  private creating: Promise<WhatsAppTransportHost> | null = null;
  private observations: Promise<void> = Promise.resolve();
  private readonly mediaKeys = new Set<string>();
  private readonly mediaSeen = new Set<string>();
  private readonly mediaRetries = new Map<string, { attempts: number; after: number }>();
  private readonly mediaSources = new Map<string, { key: string; cancel: () => void }>();
  private readonly cancelMedia = new Set<() => void>();
  private readonly mediaQueue: Array<() => Promise<void>> = [];
  private mediaActive = 0;
  private pumpMedia(): void {
    while (this.mediaActive < 2 && this.mediaQueue.length) {
      const work = this.mediaQueue.shift()!;
      this.mediaActive++;
      void work().catch(() => {}).finally(() => { this.mediaActive--; this.pumpMedia(); });
    }
  }
  private readonly sends = new Map<string, Promise<void>>();
  private readonly reconciliations = new Map<string, Promise<void>>();
  private reconcileTimer: ReturnType<typeof setTimeout> | undefined;
  private reconcileSweepRunning = false;
  private reconcileCursor = 0;
  private reconcileEpoch = 0;
  private reconcileState?: Promise<{ pending_operations: { operation_id: string }[] }>;
  private reconcileDelay = 5000;
  private readonly unsettled = new Set<string>();
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
  receive(event: WhatsAppEvent): void {
    let accountIdentity = '';
    if (event.type === 'account') {
      // Native failures must reach the UI even while the business queue waits.
      const { type: _type, ...info } = event;
      this.options.notify({ event: 'whatsapp.host.changed', data: info });
      const status = event.status === 'ready' ? 'connected' : ['disconnected', 'error'].includes(event.status) ? 'disconnected' : 'connecting';
      accountIdentity = JSON.stringify([status, event.account_id, event.connection_epoch]);
      if (accountIdentity === this.accountProjection) return;
      this.reconcileEpoch++;
      for (const cancel of this.cancelMedia) cancel();
      this.accountProjection = accountIdentity;
    }
    void this.enqueue(async () => {
      if (event.type === 'account') {
        const status = event.status === 'ready' ? 'connected' : ['disconnected', 'error'].includes(event.status) ? 'disconnected' : 'connecting';
        try { await this.options.invoke('_wa_set_account', { account_id: event.account_id, connection_epoch: event.connection_epoch, status }); }
        catch (error) { if (this.accountProjection === accountIdentity) this.accountProjection = ''; throw error; }
        if (status === 'connected') {
          this.reconcileDelay = 5000;
          this.scheduleReconciliation(0);
        } else {
          clearTimeout(this.reconcileTimer);
          this.reconcileTimer = undefined;
        }
      } else if (event.type === 'messages') {
        await this.options.invoke('_wa_ingest', { account_id: event.account_id, connection_epoch: event.connection_epoch, messages: event.messages });
        for (const message of event.messages) {
          if (!message.has_image || message.metadata_only || message.is_group) continue;
          const host = this.host;
          if (!host?.downloadImage || this.stopping) continue;
          const identity = { account_id: event.account_id, connection_epoch: event.connection_epoch, chat_id: message.chat_id, message_id: message.id,
            source_version: createHash('sha256').update(JSON.stringify([event.account_id, event.connection_epoch, message.chat_id, message.id, String(message.timestamp_ms)])).digest('hex') };
          const key = JSON.stringify(identity);
          const source = JSON.stringify([event.account_id, event.connection_epoch, message.chat_id, message.id]);
          const previous = this.mediaSources.get(source);
          if (previous && previous.key !== key) previous.cancel();
          if (this.mediaKeys.has(key) || this.mediaSeen.has(key) || (this.mediaRetries.get(key)?.after ?? 0) > Date.now()) continue;
          this.mediaKeys.add(key);
          const backoff = () => {
            const attempts = Math.min((this.mediaRetries.get(key)?.attempts ?? 0) + 1, 5);
            this.mediaRetries.set(key, { attempts, after: Date.now() + Math.min(60000, 5000 * 2 ** (attempts - 1)) });
            if (this.mediaRetries.size > 2048) this.mediaRetries.delete(this.mediaRetries.keys().next().value!);
          };
          let transferId = '';
          let expired = false;
          const fail = () => { void this.options.invoke('_wa_media_fail', { ...identity, transfer_id: transferId, error_code: 'media_unavailable' }).catch(() => {}); };
          const deadline = Date.now() + 60_000;
          const cancel = () => { if (!expired) { expired = true; fail(); } };
          this.cancelMedia.add(cancel);
          this.mediaSources.set(source, { key, cancel });
          const timer = setTimeout(() => { if (!expired) backoff(); cancel(); }, 60_000);
          timer.unref?.();
          const check = () => {
            const status = host.getStatus();
            if (expired || Date.now() >= deadline || this.stopping || host !== this.host || status.status !== 'ready' || status.account_id !== event.account_id || status.connection_epoch !== event.connection_epoch) throw new Error('WhatsApp image source changed');
          };
          const work = async () => {
            try {
              let finished = false;
              check();
              await host.downloadImage!(event.account_id, event.connection_epoch, message.chat_id, message.id, async (mime, size, chunks) => {
                check();
                const begin = await this.options.invoke<{ transfer_id: string }>('_wa_media_begin', { ...identity, mime_type: mime });
                transferId = begin.transfer_id;
                check();
                const hash = createHash('sha256'); let offset = 0;
                for await (const chunk of chunks) {
                  check();
                  if (!chunk.length || chunk.length > 256 * 1024 || offset + chunk.length > 6 * 1024 * 1024) throw new Error('WhatsApp image chunk invalid');
                  hash.update(chunk);
                  const result = await this.options.invoke<{ next_offset: number }>('_wa_media_chunk', { ...identity, transfer_id: transferId, offset, data_base64: chunk.toString('base64') });
                  check();
                  offset += chunk.length;
                  if (result.next_offset !== offset) throw new Error('WhatsApp image offset mismatch');
                }
                check();
                await this.options.invoke('_wa_media_finish', { ...identity, transfer_id: transferId, size, sha256: hash.digest('hex') });
                check();
                finished = true;
                this.mediaRetries.delete(key);
                this.mediaSeen.add(key);
                if (this.mediaSeen.size > 2048) this.mediaSeen.delete(this.mediaSeen.values().next().value!);
              });
              if (!finished) throw new Error('WhatsApp image transfer incomplete');
            } catch {
              fail();
              if (!expired && this.mediaSources.get(source)?.cancel === cancel) {
                backoff();
              }
            }
            finally {
              clearTimeout(timer); this.cancelMedia.delete(cancel); this.mediaKeys.delete(key);
              if (this.mediaSources.get(source)?.cancel === cancel) this.mediaSources.delete(source);
            }
          };
          if (this.mediaQueue.length >= 64) { clearTimeout(timer); this.cancelMedia.delete(cancel); this.mediaKeys.delete(key); if (this.mediaSources.get(source)?.cancel === cancel) this.mediaSources.delete(source); backoff(); fail(); }
          else { this.mediaQueue.push(work); this.pumpMedia(); }
        }
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
    let transportStarted = false;
    const task = (async () => {
      await this.observations;
      const host = this.host;
      if (!host || this.stopping || host.getStatus().status !== 'ready') return;
      // The service freezes and durably marks the operation before revealing its body.
      const intent = await this.options.invoke<FrozenIntent>('_wa_prepare_send', { operation_id: operationId });
      let result: SendResult | undefined;
      transportStarted = true;
      try { result = await host.sendFrozen(intent); }
      catch { /* Unknown means reconcile this operation; never submit another key. */ }
      await this.enqueue(() => this.options.invoke('_wa_transport_result', { operation_id: intent.operation_id, account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: result ? 'submitted' : 'unknown', ...(result ?? {}) }));
      this.unsettled.delete(operationId);
    })().catch(error => {
      this.error();
      // Rejected preparation never entered Chrome. Its preserved service record
      // is sufficient for normal shutdown; receipt persistence failures differ.
      if (transportStarted) { this.unsettled.add(operationId); throw error; }
    });
    this.sends.set(operationId, task);
    void task.catch(() => {}).finally(() => { this.sends.delete(operationId); });
  }
  private scheduleReconciliation(delay: number): void {
    if (this.stopping || this.reconcileTimer) return;
    this.reconcileTimer = setTimeout(() => {
      this.reconcileTimer = undefined;
      void this.reconcileDue();
    }, delay);
    this.reconcileTimer.unref?.();
  }
  private async reconcileDue(): Promise<void> {
    if (this.stopping || this.reconcileSweepRunning || this.host?.getStatus().status !== 'ready') return;
    this.reconcileSweepRunning = true;
    const epoch = this.reconcileEpoch;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    try {
      const work = (async () => {
        if (!this.reconcileState) {
          const pending = this.options.invoke<{ pending_operations: { operation_id: string }[] }>('get_whatsapp_state');
          this.reconcileState = pending;
          void pending.finally(() => { if (this.reconcileState === pending) this.reconcileState = undefined; }).catch(() => {});
        }
        const state = await this.reconcileState;
        if (expired || epoch !== this.reconcileEpoch || this.stopping) return;
        const operations = state.pending_operations;
        if (operations.length) {
          const operation = operations[this.reconcileCursor++ % operations.length];
          this.reconcileDelay = Math.min(60000, Math.max(5000, operations.length * 5000));
          await this.reconcile(operation.operation_id);
        } else this.reconcileDelay = 30000;
      })();
      await Promise.race([work, new Promise<void>(resolve => { timer = setTimeout(resolve, 5000); })]);
    } catch { this.reconcileDelay = Math.min(60000, this.reconcileDelay * 2); }
    finally {
      expired = true;
      if (timer) clearTimeout(timer);
      this.reconcileSweepRunning = false;
      if (!this.stopping && this.host?.getStatus().status === 'ready') this.scheduleReconciliation(epoch === this.reconcileEpoch ? this.reconcileDelay : 0);
    }
  }
  async reconcile(operationId: string): Promise<void> {
    if (this.stopping) return;
    const existing = this.reconciliations.get(operationId);
    if (existing) return existing;
    const task = this.reconcileExisting(operationId);
    this.reconciliations.set(operationId, task);
    try { await task; } finally { if (this.reconciliations.get(operationId) === task) this.reconciliations.delete(operationId); }
  }
  private async reconcileExisting(operationId: string): Promise<void> {
    const host = this.host;
    if (!host) return;
    let record = host.readLedger(operationId);
    if (!record?.result?.message_id) {
      record = await this.options.invoke<LedgerRecord | undefined>('_wa_get_reconcile_record', { operation_id: operationId }).catch(() => undefined);
    }
    if (!record?.result?.message_id) return;
    const identity = host.getStatus();
    const epoch = this.reconcileEpoch;
    if (identity.status !== 'ready' || identity.account_id !== record.intent.account_id) return;
    let result: SendResult | null = null;
    try { result = await host.lookupMessage(record.result.message_id); } catch { /* Stored exact submission evidence remains usable. */ }
    if (epoch !== this.reconcileEpoch || host !== this.host || host.getStatus().status !== 'ready' || host.getStatus().account_id !== identity.account_id || host.getStatus().connection_epoch !== identity.connection_epoch) return;
    if (result && result.message_id !== record.result.message_id) throw new Error('WhatsApp 回执来源无效。');
    const evidence = result && result.ack > record.result.ack ? result : record.result;
    const intent = record.intent;
    await this.enqueue(() => this.options.invoke('_wa_transport_result', { operation_id: intent.operation_id, account_id: intent.account_id, connection_epoch: intent.connection_epoch, status: 'submitted', ...evidence }));
    this.unsettled.delete(operationId);
  }
  async stop(): Promise<void> {
    this.stopping = true;
    clearTimeout(this.reconcileTimer);
    this.reconcileTimer = undefined;
    for (const cancel of this.cancelMedia) cancel();
    if (this.creating) await this.creating.catch(() => {});
    await this.enqueue(() => this.options.invoke('set_whatsapp_reception', { enabled: false }));
    const pending = Promise.allSettled([...this.sends.values(), ...this.reconciliations.values()]);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    try {
      await Promise.race([pending, new Promise<void>(resolve => { timer = setTimeout(() => { timedOut = true; resolve(); }, this.options.shutdownGraceMs ?? 10000); })]);
    } finally { if (timer) clearTimeout(timer); }
    const host = this.host;
    if (timedOut && host) { await host.close(); }
    const settled = await pending;
    await this.observations;
    if (this.unsettled.size || settled.some(result => result.status === 'rejected')) throw new Error('WhatsApp 在途操作收尾未完成，请保留原环境。');
    if (!timedOut && host) { await host.close(); }
    await this.observations;
    this.host = null;
    ++this.generation;
  }
}
