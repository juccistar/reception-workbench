import { createHash, randomUUID } from 'node:crypto';
import type { ServiceEvent } from '../shared/api';
import type { WhatsAppContext } from '../shared/whatsapp';
import type { WhatsAppAccountProfile, WhatsAppAccountState, WhatsAppBindingInput, WhatsAppContextRequest, WhatsAppDefaultInput, WhatsAppLauncherSnapshot, WhatsAppReceptionInput, WhatsAppTakeoverInput, WhatsAppWritebackCapabilities } from '../shared/whatsapp-account';
import type { LauncherMerchant } from './launcher-registry';
import type { WhatsAppAccountStore } from './whatsapp-account-store';
import type { ServiceClient } from './service-client';
import type { ModelConfigHost } from './model-config-host';
import type { createWindowsWhatsAppMerchantBroker } from './whatsapp-merchant-broker';
import { launcherCompanyName } from './launcher-company-name';
import { WhatsAppCoordinator } from './whatsapp-coordinator';
import { ChromeWhatsAppHost } from './whatsapp/chrome-host';

type Broker = Awaited<ReturnType<typeof createWindowsWhatsAppMerchantBroker>>;
interface Options {
  store: WhatsAppAccountStore;
  broker: Broker;
  merchants(): Promise<LauncherMerchant[]>;
  available(record: LauncherMerchant): Promise<boolean>;
  service(workspaceId: string, directory: string): ServiceClient;
  models(invoke: (method: string, params: Record<string, unknown>) => Promise<unknown>): Promise<ModelConfigHost>;
  confirm(message: string, detail: string, action?: string): Promise<boolean>;
  version: string;
  notify?(event: ServiceEvent): void;
}
interface Active {
  workspaceId: string;
  backend: ServiceClient;
  coordinator: WhatsAppCoordinator;
  chrome: ChromeWhatsAppHost | null;
  models: ModelConfigHost;
  release(): Promise<void>;
  timer: ReturnType<typeof setInterval>;
  closing: boolean;
  contexts: Map<string, Promise<void>>;
  legacyDeclaration: string | null;
}
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const text = (value: unknown): value is string => typeof value === 'string' && !!value.trim() && value.length <= 300 && !/[\x00-\x1f\x7f]/.test(value);
function packet(input: unknown, fields: string[]): asserts input is Record<string, unknown> {
  if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).sort().join(',') !== fields.sort().join(',') || Buffer.byteLength(JSON.stringify(input)) > 32768) throw new Error('WhatsApp 操作参数无效。');
}
function identity(input: { expected_account_id: unknown; expected_connection_epoch: unknown }) {
  if (!text(input.expected_account_id) || !text(input.expected_connection_epoch)) throw new Error('WhatsApp 账号已变化，请刷新后重试。');
}
const identityFields = ['expected_account_id', 'expected_connection_epoch'];

/** The only launcher owner of an account process and its visible system Chrome. */
export class WhatsAppAccountManager {
  private active: Active | null = null;
  private starting: Promise<Active> | null = null;
  private mutations: Promise<unknown> = Promise.resolve();
  private stopping = false;
  constructor(private readonly options: Options) {}
  private exclusive<T>(action: () => Promise<T>): Promise<T> {
    const result = this.mutations.then(action);
    this.mutations = result.catch(() => {});
    return result;
  }
  private notify(value: ServiceEvent) { this.options.notify?.(value); }
  isRunning(): boolean { return !!this.active || !!this.starting; }
  private async current(): Promise<Active> {
    if (this.stopping) throw new Error('WhatsApp 正在退出，请等待完成。');
    const selected = await this.options.store.selected();
    if (!selected) throw new Error('请先创建并选择 WhatsApp 账号环境。');
    if (this.active) {
      if (this.active.workspaceId !== selected.id || this.active.closing || this.active.backend.hasExited()) throw new Error('账号后台不可用，请先关闭当前连接后重试。');
      return this.active;
    }
    if (!this.starting) this.starting = this.start(selected.id);
    try { return await this.starting; } finally { this.starting = null; }
  }
  private async start(workspaceId: string): Promise<Active> {
    const release = await this.options.store.acquire(workspaceId);
    let backend: ServiceClient | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    try {
      const directory = await this.options.store.directory(workspaceId);
      backend = this.options.service(workspaceId, directory);
      const service = backend;
      const models = await this.options.models((method, params) => service.invoke(method, params));
      let active: Active;
      const invoke = async <T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> => {
        if (method === '_wa_prepare_send') {
          if (active.closing) throw new Error('WhatsApp 正在关闭。');
          await this.requireLegacyDeclaration(active);
          await models.synchronize();
          const request = await service.invoke<WhatsAppContextRequest>('_wa_get_context_request', { operation_id: params.operation_id });
          await this.supplyContext(active, request);
        }
        return service.invoke<T>(method, params);
      };
      const coordinator = new WhatsAppCoordinator({ invoke,
        notify: value => this.notify(value),
        createHost: async receive => {
          const chrome = new ChromeWhatsAppHost({ profileDirectory: await this.options.store.profileDirectory(workspaceId), directory, applicationVersion: this.options.version, onEvent: receive, authorizeAccount: accountId => this.options.store.bindAccount(workspaceId, accountId) });
          active.chrome = chrome;
          return chrome;
        },
      });
      let syncing = false;
      timer = setInterval(() => {
        if (syncing || active.closing) return;
        syncing = true;
        void models.synchronize().catch(async () => {
          await service.invoke('set_whatsapp_reception', { enabled: false }).catch(() => {});
        }).finally(() => { syncing = false; });
      }, 3000);
      timer.unref?.();
      active = { workspaceId, backend: service, coordinator, chrome: null, models, release, timer, closing: false, contexts: new Map(), legacyDeclaration: null };
      service.on('event', (value: ServiceEvent) => {
        if (value.event === 'whatsapp.context.requested') {
          const request = value.data as WhatsAppContextRequest;
          void this.supplyContext(active, request).catch(() => {});
          return;
        }
        if (coordinator.handleServiceEvent(value)) return;
        if (value.event === 'backend.disconnected') {
          void active.chrome?.close().catch(() => {});
        }
        if (!value.event.startsWith('whatsapp.') && value.event !== 'state.changed' && value.event !== 'backend.disconnected') return;
        this.notify(value);
      });
      await service.invoke('get_whatsapp_state');
      await models.synchronize();
      if (this.stopping) throw new Error('WhatsApp 正在退出。');
      this.active = active;
      return active;
    } catch (error) {
      if (timer) clearInterval(timer);
      await backend?.shutdown().catch(() => {});
      await release();
      throw error;
    }
  }
  private async supplyContext(active: Active, request: WhatsAppContextRequest): Promise<void> {
    if (!request || !text(request.request_id) || !text(request.account_id) || !text(request.chat_id) || !uuid(request.merchant_id) || !Number.isSafeInteger(request.binding_revision)) throw new Error('商家上下文请求无效。');
    const existing = active.contexts.get(request.request_id);
    if (existing) return existing;
    if (active.contexts.size >= 16 || active.closing) throw new Error('商家上下文正在处理，请稍后重试。');
    const work = (async () => {
      try {
        await this.requireLegacyDeclaration(active);
        await active.models.synchronize();
        const merchant = (await this.options.merchants()).find(item => item.record_id === request.merchant_id && item.verified_at > 0);
        if (!merchant || active.closing) throw new Error('merchant_unavailable');
        const context = await this.options.broker.request(merchant, request);
        if (active.closing || this.active && this.active !== active) throw new Error('account_changed');
        await active.backend.invoke('_wa_set_merchant_context', { request, context });
      } catch {
        await active.backend.invoke('_wa_set_merchant_context', { request, context: null, error: 'merchant_unavailable' }).catch(() => {});
        throw new Error('接待商家暂不可用，请重新授权该商家工作台。');
      }
    })();
    active.contexts.set(request.request_id, work);
    try { await work; } finally { active.contexts.delete(request.request_id); }
  }
  async snapshot(startService = true): Promise<WhatsAppLauncherSnapshot> {
    const entries = await this.options.store.list();
    const profiles: WhatsAppAccountProfile[] = entries.map(({ id, ...entry }) => ({ workspace_id: id, ...entry }));
    const selected = entries.find(item => item.selected);
    const active = selected && startService ? await this.current() : this.active;
    const records = await this.options.merchants();
    const merchants = await Promise.all(records.map(async record => {
      const available = record.verified_at > 0 && await this.options.available(record).catch(() => false);
      return { merchant_id: record.record_id, name: launcherCompanyName(record.name, record.seller_id, record.verified_at > 0), account_tail: record.seller_id.slice(-4), available, ...(available ? {} : { reason: '请从该商家的 Accio Work 重新授权启动。' }) };
    }));
    return { profiles, selected_workspace_id: selected?.id ?? null,
      host: active?.coordinator.getStatus() ?? { status: 'disconnected', account_id: '', connection_epoch: '' },
      state: active ? await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state') : null, merchants };
  }
  create(label: unknown): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      if (typeof label !== 'string') throw new Error('请填写账号环境名称。');
      const entry = await this.options.store.create(label);
      await this.stopActive();
      await this.options.store.select(entry.id);
      return this.snapshot();
    });
  }
  select(workspaceId: unknown): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      if (!uuid(workspaceId)) throw new Error('账号环境无效。');
      const selected = await this.options.store.selected();
      if (selected?.id !== workspaceId) { await this.stopActive(); await this.options.store.select(workspaceId); }
      return this.snapshot();
    });
  }
  connect(): Promise<WhatsAppLauncherSnapshot> { return this.exclusive(async () => { const active = await this.current(); await active.coordinator.connect(); return this.snapshot(); }); }
  async open(): Promise<void> { const active = await this.current(); if (!active.chrome) throw new Error('请先连接 WhatsApp。'); await active.chrome.open(); }
  disconnect(): Promise<WhatsAppLauncherSnapshot> { return this.exclusive(async () => { const active = await this.current(); await active.coordinator.stop(); active.chrome = null; return this.snapshot(); }); }
  async contacts(): Promise<WhatsAppLauncherSnapshot> { const active = await this.current(); await active.coordinator.listChats(); return this.snapshot(); }
  private async declaredSources() {
    const records = await this.options.merchants();
    return records.map(record => ({ source_id: createHash('sha256').update(JSON.stringify([record.seller_id, record.registry_directory, record.state_directory, record.data_root])).digest('hex'), merchant_id: record.record_id, seller_id: record.seller_id, data_root: record.data_root, state_directory: record.state_directory, registry_directory: record.registry_directory })).sort((a, b) => a.source_id.localeCompare(b.source_id));
  }
  private async requireLegacyDeclaration(active: Active): Promise<void> {
    if (active.legacyDeclaration !== JSON.stringify(await this.declaredSources())) {
      await active.backend.invoke('set_whatsapp_reception', { enabled: false });
      throw new Error('已登记的旧来源需要重新核查，请先正常退出旧商家窗口，再核查旧接入记录。');
    }
  }
  verifyLegacy(): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      const active = await this.current();
      const state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
      if (!state.connected || !state.account_id) throw new Error('请先连接并核验 WhatsApp 账号。');
      const sources = await this.declaredSources();
      if (!await this.options.confirm('核查并备份旧接入记录？', `将核查 ${sources.length} 个已登记商家目录并保存完整备份。请先正常退出旧商家窗口；如曾使用其他目录，请先导入对应商家登记。原消息和发送记录保留，未确认的发送继续核查。`, '开始核查')) return this.snapshot();
      const current = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
      if (!current.connected || current.account_id !== state.account_id || current.connection_epoch !== state.connection_epoch || JSON.stringify(sources) !== JSON.stringify(await this.declaredSources())) throw new Error('账号或旧来源已变化，请重新核查。');
      await active.backend.invoke('_wa_verify_legacy_sources', { account_id: state.account_id, sources });
      active.legacyDeclaration = JSON.stringify(sources);
      return this.snapshot();
    });
  }
  private async assertIdentity(active: Active, input: { expected_account_id: string; expected_connection_epoch: string }) {
    identity(input);
    const state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
    if (!state.connected || state.account_id !== input.expected_account_id || state.connection_epoch !== input.expected_connection_epoch) throw new Error('WhatsApp 账号已变化，请刷新后重试。');
  }
  binding(input: WhatsAppBindingInput): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      packet(input, [...identityFields, 'chat_id', 'merchant_id', 'conversation_id', 'expected_revision']);
      identity(input);
      if (!text(input.chat_id) || !(input.merchant_id === null || uuid(input.merchant_id)) || !(input.conversation_id === null || text(input.conversation_id)) || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0 || input.merchant_id === null && input.conversation_id !== null) throw new Error('接待归属或客户关联无效。');
      const active = await this.current(); await this.assertIdentity(active, input);
      if (input.merchant_id) {
        const record = (await this.options.merchants()).find(item => item.record_id === input.merchant_id && item.verified_at > 0);
        if (!record) throw new Error('请先核验目标商家。');
        await this.options.broker.request(record, { request_id: randomUUID(), account_id: input.expected_account_id, chat_id: input.chat_id, merchant_id: record.record_id, conversation_id: input.conversation_id, binding_revision: input.expected_revision + 1 });
      }
      await this.assertIdentity(active, input);
      await active.backend.invoke('save_whatsapp_binding', input as unknown as Record<string, unknown>);
      return this.snapshot();
    });
  }
  defaultMerchant(input: WhatsAppDefaultInput): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      packet(input, [...identityFields, 'merchant_id', 'expected_revision']); identity(input);
      if (!(input.merchant_id === null || uuid(input.merchant_id)) || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0) throw new Error('默认接待商家参数无效。');
      const active = await this.current(); await this.assertIdentity(active, input);
      if (input.merchant_id && !(await this.options.merchants()).some(item => item.record_id === input.merchant_id && item.verified_at > 0)) throw new Error('请先核验目标商家。');
      await active.backend.invoke('save_whatsapp_default_merchant', input as unknown as Record<string, unknown>);
      return this.snapshot();
    });
  }
  reception(input: WhatsAppReceptionInput): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      packet(input, [...identityFields, 'enabled', 'allowed_merchant_ids']); identity(input);
      if (typeof input.enabled !== 'boolean' || !Array.isArray(input.allowed_merchant_ids) || input.allowed_merchant_ids.length > 512 || input.allowed_merchant_ids.some(id => !uuid(id)) || new Set(input.allowed_merchant_ids).size !== input.allowed_merchant_ids.length) throw new Error('接待范围无效。');
      const active = await this.current(); await this.assertIdentity(active, input);
      if (input.enabled) {
        await this.requireLegacyDeclaration(active);
        const available = await this.snapshot();
        if (!input.allowed_merchant_ids.length || input.allowed_merchant_ids.some(id => !available.merchants.some(merchant => merchant.merchant_id === id && merchant.available))) throw new Error('请先核验并选择允许接待的商家。');
        const names = available.merchants.filter(item => input.allowed_merchant_ids.includes(item.merchant_id)).map(item => `${item.name}（${item.account_tail}）`).join('、');
        if (!await this.options.confirm('开启 WhatsApp 自动接待？', `账号尾号：${input.expected_account_id.split('@')[0].slice(-4)}\n允许商家：${names}\n仅已绑定这些商家的单聊可自动回复。历史加载不补答，未决发送只核查，不重发。`)) return this.snapshot();
        await this.assertIdentity(active, input);
        await active.models.synchronize();
      }
      await active.backend.invoke('set_whatsapp_reception', input as unknown as Record<string, unknown>);
      return this.snapshot();
    });
  }
  takeover(input: WhatsAppTakeoverInput): Promise<WhatsAppLauncherSnapshot> {
    return this.exclusive(async () => {
      packet(input, [...identityFields, 'chat_id', 'enabled']); identity(input);
      if (!text(input.chat_id) || typeof input.enabled !== 'boolean') throw new Error('单聊接管参数无效。');
      const active = await this.current(); await this.assertIdentity(active, input);
      await active.backend.invoke('takeover_whatsapp', input as unknown as Record<string, unknown>);
      return this.snapshot();
    });
  }
  async reconcile(operationId: unknown): Promise<WhatsAppLauncherSnapshot> {
    if (!text(operationId)) throw new Error('发送操作无效。');
    const active = await this.current();
    await active.backend.invoke('reconcile_whatsapp_operation', { operation_id: operationId });
    await active.coordinator.reconcile(operationId);
    return this.snapshot();
  }
  async context(chatId: unknown): Promise<WhatsAppContext> {
    if (!text(chatId)) throw new Error('单聊无效。');
    return (await this.current()).backend.invoke('get_whatsapp_context', { chat_id: chatId });
  }
  async writebackCapabilities(): Promise<WhatsAppWritebackCapabilities> {
    return { format: 'wa-writeback-capabilities-v1', available: false, enabled: false, reason: 'WhatsApp 内容反写尚未开放。', destinations: ['merchant_workbench', 'international_platform'], content_kinds: ['summary', 'requirement_changes', 'note', 'message_reference'] };
  }
  async modelsChanged(): Promise<void> {
    if (!this.active || this.active.closing) return;
    try { await this.active.models.synchronize(); }
    catch { await this.active.backend.invoke('set_whatsapp_reception', { enabled: false }).catch(() => {}); }
  }
  private async stopActive(): Promise<void> {
    if (this.starting) await this.starting;
    const active = this.active;
    if (!active) return;
    active.closing = true;
    clearInterval(active.timer);
    try {
      await active.coordinator.stop();
      await Promise.allSettled(active.contexts.values());
      await active.backend.shutdown();
      await active.release();
      this.active = null;
    } catch (error) { active.closing = false; throw error; }
  }
  async close(): Promise<void> {
    this.stopping = true;
    await this.mutations;
    await this.stopActive();
  }
}
