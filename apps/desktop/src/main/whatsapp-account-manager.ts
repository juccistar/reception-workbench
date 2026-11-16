import { createHash, randomUUID } from 'node:crypto';
import type { ServiceEvent } from '../shared/api';
import type { WhatsAppContext } from '../shared/whatsapp';
import type { WhatsAppAccountProfile, WhatsAppAccountSnapshot, WhatsAppAccountState, WhatsAppMerchantChoice, WhatsAppRenameInput, WhatsAppBindingInput, WhatsAppContextRequest, WhatsAppDefaultInput, WhatsAppLauncherSnapshot, WhatsAppReceptionInput, WhatsAppTakeoverInput, WhatsAppWritebackCapabilities } from '../shared/whatsapp-account';
import type { LauncherMerchant } from './launcher-registry';
import type { WhatsAppAccountStore } from './whatsapp-account-store';
import type { ServiceClient } from './service-client';
import type { ModelConfigHost } from './model-config-host';
import type { createWindowsWhatsAppMerchantBroker } from './whatsapp-merchant-broker';
import { launcherCompanyName } from './launcher-company-name';
import { WhatsAppCoordinator } from './whatsapp-coordinator';
import { ChromeWhatsAppHost, type ChromeWhatsAppHostOptions } from './whatsapp/chrome-host';

type Broker = Awaited<ReturnType<typeof createWindowsWhatsAppMerchantBroker>>;
interface Options {
  store: WhatsAppAccountStore;
  broker: Broker;
  merchants(): Promise<LauncherMerchant[]>;
  verifyDeclaredScope?(): Promise<void>;
  available(record: LauncherMerchant): Promise<boolean>;
  service(workspaceId: string, directory: string): ServiceClient;
  models(invoke: (method: string, params: Record<string, unknown>) => Promise<unknown>): Promise<ModelConfigHost>;
  confirm(message: string, detail: string, action?: string): Promise<boolean>;
  version: string;
  notify?(event: ServiceEvent): void;
  createChrome?(options: ChromeWhatsAppHostOptions, context: { workspaceId: string; runtimeInstanceId: string; backend: ServiceClient }): Promise<ChromeWhatsAppHost> | ChromeWhatsAppHost;
}
interface Active {
  workspaceId: string;
  runtimeInstanceId: string;
  backend: ServiceClient;
  coordinator: WhatsAppCoordinator;
  chrome: ChromeWhatsAppHost | null;
  models: ModelConfigHost;
  modelTasks: Set<Promise<void>>;
  release(): Promise<void>;
  timer: ReturnType<typeof setInterval>;
  closing: boolean;
  contexts: Map<string, Promise<void>>;
  legacyDeclaration: string | null;
  emergencyClose?: Promise<void>;
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
function serviceInput(input: { workspace_id: string }): Record<string, unknown> {
  const { workspace_id: _workspaceId, ...params } = input;
  return params;
}

/** The only launcher owner of an account process and its visible system Chrome. */
export class WhatsAppAccountManager {
  private readonly active = new Map<string, Active>();
  private readonly starting = new Map<string, Promise<Active>>();
  private readonly mutations = new Map<string, Promise<unknown>>();
  private readonly cleanup = new Map<string, Promise<void>>();
  private readonly instances = new Map<string, string>();
  private readonly errors = new Map<string, string>();
  private readonly cachedStates = new Map<string, WhatsAppAccountState>();
  private stopping = false;
  private merchantProjection?: { until: number; work: Promise<WhatsAppMerchantChoice[]> };
  constructor(private readonly options: Options) {}
  private exclusive<T>(id: string, action: () => Promise<T>): Promise<T> {
    const result = (this.mutations.get(id) ?? Promise.resolve()).then(action);
    const settled = result.catch(() => {});
    this.mutations.set(id, settled);
    void settled.finally(() => { if (this.mutations.get(id) === settled) this.mutations.delete(id); });
    return result;
  }
  private notify(value: ServiceEvent, workspaceId: string, runtimeInstanceId: string) {
    this.options.notify?.({ ...value, data: { ...(value.data && typeof value.data === 'object' ? value.data : {}), workspace_id: workspaceId, runtime_instance_id: runtimeInstanceId } });
  }
  isRunning(workspaceId?: string): boolean { return workspaceId ? this.active.has(workspaceId) || this.starting.has(workspaceId) : this.active.size > 0 || this.starting.size > 0; }
  getRuntimeIds(): string[] { return [...new Set([...this.active.keys(), ...this.starting.keys()])]; }
  private async current(workspaceId: string, start = true): Promise<Active> {
    if (!uuid(workspaceId)) throw new Error('账号环境无效。');
    if (this.stopping) throw new Error('WhatsApp 正在退出，请等待完成。');
    let active = this.active.get(workspaceId);
    if (!active && this.starting.has(workspaceId)) active = await this.starting.get(workspaceId)!;
    if (!active && start) {
      const instance = randomUUID();
      this.instances.set(workspaceId, instance);
      this.errors.delete(workspaceId);
      const work = this.start(workspaceId, instance);
      this.starting.set(workspaceId, work);
      try { active = await work; }
      catch (error) {
        this.errors.set(workspaceId, error instanceof Error ? error.message : String(error));
        if (!this.active.has(workspaceId)) this.instances.delete(workspaceId);
        throw error;
      }
      finally { if (this.starting.get(workspaceId) === work) this.starting.delete(workspaceId); }
    }
    if (!active || active.closing || active.backend.hasExited()) throw new Error('账号后台不可用，请先连接 WhatsApp。');
    return active;
  }
  private async start(workspaceId: string, runtimeInstanceId: string): Promise<Active> {
    const release = await this.options.store.acquire(workspaceId);
    let backend: ServiceClient | undefined;
    let ownedActive: Active | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    try {
      const directory = await this.options.store.directory(workspaceId);
      backend = this.options.service(workspaceId, directory);
      const service = backend;
      const models = await this.options.models((method, params) => {
        if (ownedActive?.closing) return Promise.reject(new Error('WhatsApp 正在关闭。'));
        return service.invoke(method, params);
      });
      let active: Active;
      const invoke = async <T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> => {
        if (method === '_wa_prepare_send') {
          if (active.closing) throw new Error('WhatsApp 正在关闭。');
          await this.requireLegacyDeclaration(active);
          await this.synchronizeModels(active);
          const request = await service.invoke<WhatsAppContextRequest>('_wa_get_context_request', { operation_id: params.operation_id });
          await this.supplyContext(active, request);
        }
        // After process death there is no live grant to revoke and no service to
        // receive a result. The frozen service intent and native ledger remain
        // the evidence for reconciliation on the next start.
        if (service.hasExited() && (method === 'set_whatsapp_reception' || method === '_wa_transport_result')) return undefined as T;
        try { return await service.invoke<T>(method, params); }
        catch (error) {
          if (service.hasExited() && (method === 'set_whatsapp_reception' || method === '_wa_transport_result')) return undefined as T;
          throw error;
        }
      };
      const coordinator = new WhatsAppCoordinator({ invoke,
        notify: value => this.notify(value, workspaceId, runtimeInstanceId),
        createHost: async receive => {
          const options: ChromeWhatsAppHostOptions = { profileDirectory: await this.options.store.profileDirectory(workspaceId), directory, applicationVersion: this.options.version, onEvent: receive, authorizeAccount: accountId => this.options.store.bindAccount(workspaceId, accountId) };
          const chrome = await (this.options.createChrome ? this.options.createChrome(options, { workspaceId, runtimeInstanceId, backend: service }) : new ChromeWhatsAppHost(options));
          active.chrome = chrome;
          return chrome;
        },
      });
      let syncing = false;
      timer = setInterval(() => {
        if (syncing || active.closing) return;
        syncing = true;
        void this.synchronizeModels(active).catch(async () => {
          if (!active.closing) await service.invoke('set_whatsapp_reception', { enabled: false }).catch(() => {});
        }).finally(() => { syncing = false; });
      }, 3000);
      timer.unref?.();
      active = { workspaceId, runtimeInstanceId, backend: service, coordinator, chrome: null, models, modelTasks: new Set(), release, timer, closing: false, contexts: new Map(), legacyDeclaration: null };
      ownedActive = active;
      this.active.set(workspaceId, active);
      service.on('event', (value: ServiceEvent) => {
        if (value.event === 'backend.disconnected') {
          if (!active.closing) this.errors.set(workspaceId, 'WhatsApp 账号后台已退出。');
          active.closing = true;
          clearInterval(active.timer);
          if (active.chrome && !active.emergencyClose) {
            active.emergencyClose = active.chrome.close();
            void active.emergencyClose.catch(() => {});
          }
          void this.cleanupActive(active).catch(() => {});
          this.notify(value, workspaceId, runtimeInstanceId);
          return;
        }
        if (active.closing) return;
        if (value.event === 'whatsapp.context.requested') {
          const request = value.data as WhatsAppContextRequest;
          void this.supplyContext(active, request).catch(() => {});
          return;
        }
        if (coordinator.handleServiceEvent(value)) return;
        if (!value.event.startsWith('whatsapp.') && value.event !== 'state.changed' && value.event !== 'backend.disconnected') return;
        if (value.event === 'state.changed' || value.event.startsWith('whatsapp.')) {
          void service.invoke<WhatsAppAccountState>('get_whatsapp_state').then(state => {
            if (!active.closing && this.active.get(workspaceId) === active) this.cachedStates.set(workspaceId, state);
          }).catch(() => {});
        }
        this.notify(value, workspaceId, runtimeInstanceId);
      });
      this.cachedStates.set(workspaceId, await service.invoke<WhatsAppAccountState>('get_whatsapp_state'));
      await this.synchronizeModels(active);
      if (this.stopping || active.closing || service.hasExited()) throw new Error('WhatsApp 正在退出。');
      return active;
    } catch (error) {
      if (timer) clearInterval(timer);
      if (ownedActive) {
        if (this.active.get(workspaceId) === ownedActive) await this.cleanupActive(ownedActive);
      } else {
        await backend?.shutdown();
        await release();
      }
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
        await this.synchronizeModels(active);
        const merchant = (await this.options.merchants()).find(item => item.record_id === request.merchant_id && item.verified_at > 0);
        if (!merchant || active.closing) throw new Error('merchant_unavailable');
        const context = await this.options.broker.request(merchant, request);
        if (active.closing || this.active.get(active.workspaceId) !== active) throw new Error('account_changed');
        await active.backend.invoke('_wa_set_merchant_context', { request, context });
      } catch {
        await active.backend.invoke('_wa_set_merchant_context', { request, context: null, error: 'merchant_unavailable' }).catch(() => {});
        throw new Error('接待商家暂不可用，请重新授权该商家工作台。');
      }
    })();
    active.contexts.set(request.request_id, work);
    try { await work; } finally { active.contexts.delete(request.request_id); }
  }
  private async merchantChoices(): Promise<WhatsAppMerchantChoice[]> {
    // A display projection only. Context export and mutation authorization still
    // validate the live source; each panel poll must not launch DPAPI probes.
    const cached = this.merchantProjection;
    if (cached && Date.now() < cached.until) return cached.work;
    const work = (async () => {
      const records = await this.options.merchants();
      return Promise.all(records.map(async record => {
        const available = record.verified_at > 0 && await this.options.available(record).catch(() => false);
        return { merchant_id: record.record_id, name: launcherCompanyName(record.name, record.seller_id, record.verified_at > 0), account_tail: record.seller_id.slice(-4), available, ...(available ? {} : { reason: '请从该商家的 Accio Work 重新授权启动。' }) };
      }));
    })();
    this.merchantProjection = { until: Number.POSITIVE_INFINITY, work };
    try { const value = await work; if (this.merchantProjection?.work === work) this.merchantProjection.until = Date.now() + 3000; return value; }
    catch (error) { if (this.merchantProjection?.work === work) this.merchantProjection = undefined; throw error; }
  }
  private profile(entry: { id: string; label: string; account_tail: string; revision: number }): WhatsAppAccountProfile {
    return { workspace_id: entry.id, label: entry.label, account_tail: entry.account_tail, revision: entry.revision };
  }
  async snapshot(): Promise<WhatsAppLauncherSnapshot> {
    const entries = await this.options.store.list();
    const merchants = await this.merchantChoices();
    const accounts = await Promise.all(entries.map(async entry => {
      const active = this.active.get(entry.id);
      const state = this.cachedStates.get(entry.id);
      const error = this.errors.get(entry.id);
      const runtime_status: 'idle' | 'starting' | 'running' | 'closing' | 'error' = active?.closing ? 'closing' : this.starting.has(entry.id) ? 'starting' : error ? 'error' : active?.chrome ? 'running' : 'idle';
      return { ...this.profile(entry), host: active?.coordinator.getStatus() ?? { status: 'disconnected' as const, account_id: '', connection_epoch: '' }, enabled: state?.enabled ?? false, pending_count: state?.pending_operations?.filter(operation => operation.status === 'unknown').length ?? 0, runtime_status, runtime_instance_id: this.instances.get(entry.id) ?? null, ...(error ? { error } : {}) };
    }));
    return { accounts, merchants };
  }
  async detail(workspaceId: string, startService = false): Promise<WhatsAppAccountSnapshot> {
    if (!uuid(workspaceId)) throw new Error('账号环境无效。');
    const entry = (await this.options.store.list()).find(item => item.id === workspaceId);
    if (!entry) throw new Error('账号环境不存在。');
    const active = startService ? await this.current(workspaceId) : this.active.get(workspaceId);
    let state = this.cachedStates.get(workspaceId) ?? null;
    if (active && !active.closing && !active.backend.hasExited()) {
      state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
      this.cachedStates.set(workspaceId, state);
    }
    return { profile: this.profile(entry), host: active?.coordinator.getStatus() ?? { status: 'disconnected', account_id: '', connection_epoch: '' }, state, merchants: await this.merchantChoices(), runtime_instance_id: this.instances.get(workspaceId) ?? null };
  }
  async create(label: unknown): Promise<WhatsAppAccountSnapshot> {
    if (typeof label !== 'string') throw new Error('请填写账号环境名称。');
    const entry = await this.options.store.create(label);
    return this.detail(entry.id);
  }
  rename(input: WhatsAppRenameInput): Promise<WhatsAppAccountSnapshot> {
    packet(input, ['workspace_id', 'label', 'expected_revision']);
    return this.exclusive(input.workspace_id, async () => { await this.options.store.rename(input.workspace_id, input.label, input.expected_revision); return this.detail(input.workspace_id); });
  }
  connect(workspaceId: string): Promise<WhatsAppAccountSnapshot> { return this.exclusive(workspaceId, async () => { const active = await this.current(workspaceId); try { await active.coordinator.connect(); } catch (error) { this.errors.set(workspaceId, error instanceof Error ? error.message : String(error)); await this.cleanupActive(active); throw error; } return this.detail(workspaceId); }); }
  open(workspaceId: string): Promise<void> { return this.exclusive(workspaceId, async () => { const active = await this.current(workspaceId, false); if (!active.chrome) throw new Error('请先连接 WhatsApp。'); await active.chrome.open(); }); }
  disconnect(workspaceId: string): Promise<WhatsAppAccountSnapshot> { return this.exclusive(workspaceId, async () => { const active = this.active.get(workspaceId); if (active) await this.cleanupActive(active); this.errors.delete(workspaceId); return this.detail(workspaceId); }); }
  contacts(workspaceId: string): Promise<WhatsAppAccountSnapshot> { return this.exclusive(workspaceId, async () => { const active = await this.current(workspaceId, false); if (active.coordinator.getStatus().status !== 'ready') throw new Error('请先连接 WhatsApp。'); await active.coordinator.listChats(); return this.detail(workspaceId); }); }
  private async declaredSources() {
    await this.options.verifyDeclaredScope?.();
    const records = await this.options.merchants();
    return records.map(record => ({ source_id: createHash('sha256').update(JSON.stringify([record.seller_id, record.registry_directory, record.state_directory, record.data_root])).digest('hex'), merchant_id: record.record_id, seller_id: record.seller_id, data_root: record.data_root, state_directory: record.state_directory, registry_directory: record.registry_directory })).sort((a, b) => a.source_id.localeCompare(b.source_id));
  }
  private async requireLegacyDeclaration(active: Active): Promise<void> {
    if (active.legacyDeclaration !== JSON.stringify(await this.declaredSources())) {
      await active.backend.invoke('set_whatsapp_reception', { enabled: false });
      throw new Error('已登记的旧来源需要重新核查，请先正常退出旧商家窗口，再核查旧接入记录。');
    }
  }
  verifyLegacy(workspaceId: string): Promise<WhatsAppAccountSnapshot> {
    return this.exclusive(workspaceId, async () => {
      const active = await this.current(workspaceId, false);
      const state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
      if (!state.connected || !state.account_id) throw new Error('请先连接并核验 WhatsApp 账号。');
      const sources = await this.declaredSources();
      if (!await this.options.confirm('核查并备份旧接入记录？', `将核查 ${sources.length} 个已登记商家目录并保存完整备份。请先正常退出旧商家窗口；如曾使用其他目录，请先导入对应商家登记。原消息和发送记录保留，未确认的发送继续核查。`, '开始核查')) return this.detail(workspaceId);
      const current = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
      if (!current.connected || current.account_id !== state.account_id || current.connection_epoch !== state.connection_epoch || JSON.stringify(sources) !== JSON.stringify(await this.declaredSources())) throw new Error('账号或旧来源已变化，请重新核查。');
      await active.backend.invoke('_wa_verify_legacy_sources', { account_id: state.account_id, sources });
      active.legacyDeclaration = JSON.stringify(sources);
      return this.detail(workspaceId);
    });
  }
  private async assertIdentity(active: Active, input: { expected_account_id: string; expected_connection_epoch: string }) {
    identity(input);
    const state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state');
    if (!state.connected || state.account_id !== input.expected_account_id || state.connection_epoch !== input.expected_connection_epoch) throw new Error('WhatsApp 账号已变化，请刷新后重试。');
  }
  binding(input: WhatsAppBindingInput, assertCurrent?: () => Promise<void>): Promise<WhatsAppAccountSnapshot> {
    return this.exclusive(input.workspace_id, async () => {
      packet(input, ['workspace_id', ...identityFields, 'chat_id', 'merchant_id', 'conversation_id', 'expected_revision']);
      identity(input);
      if (!text(input.chat_id) || !(input.merchant_id === null || uuid(input.merchant_id)) || !(input.conversation_id === null || text(input.conversation_id)) || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0 || input.merchant_id === null && input.conversation_id !== null) throw new Error('接待归属或客户关联无效。');
      const active = await this.current(input.workspace_id, false); await this.assertIdentity(active, input);
      if (input.merchant_id) {
        const record = (await this.options.merchants()).find(item => item.record_id === input.merchant_id && item.verified_at > 0);
        if (!record) throw new Error('请先核验目标商家。');
        await this.options.broker.request(record, { request_id: randomUUID(), account_id: input.expected_account_id, chat_id: input.chat_id, merchant_id: record.record_id, conversation_id: input.conversation_id, binding_revision: input.expected_revision + 1 });
      }
      await this.assertIdentity(active, input);
      await assertCurrent?.();
      await active.backend.invoke('save_whatsapp_binding', serviceInput(input));
      return this.detail(input.workspace_id);
    });
  }
  defaultMerchant(input: WhatsAppDefaultInput): Promise<WhatsAppAccountSnapshot> {
    return this.exclusive(input.workspace_id, async () => {
      packet(input, ['workspace_id', ...identityFields, 'merchant_id', 'expected_revision']); identity(input);
      if (!(input.merchant_id === null || uuid(input.merchant_id)) || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0) throw new Error('默认接待商家参数无效。');
      const active = await this.current(input.workspace_id, false); await this.assertIdentity(active, input);
      if (input.merchant_id && !(await this.options.merchants()).some(item => item.record_id === input.merchant_id && item.verified_at > 0)) throw new Error('请先核验目标商家。');
      await active.backend.invoke('save_whatsapp_default_merchant', serviceInput(input));
      return this.detail(input.workspace_id);
    });
  }
  reception(input: WhatsAppReceptionInput): Promise<WhatsAppAccountSnapshot> {
    return this.exclusive(input.workspace_id, async () => {
      packet(input, ['workspace_id', ...identityFields, 'enabled', 'allowed_merchant_ids']); identity(input);
      if (typeof input.enabled !== 'boolean' || !Array.isArray(input.allowed_merchant_ids) || input.allowed_merchant_ids.length > 512 || input.allowed_merchant_ids.some(id => !uuid(id)) || new Set(input.allowed_merchant_ids).size !== input.allowed_merchant_ids.length) throw new Error('接待范围无效。');
      const active = await this.current(input.workspace_id, false); await this.assertIdentity(active, input);
      if (input.enabled) {
        await this.requireLegacyDeclaration(active);
        const available = await this.snapshot();
        if (!input.allowed_merchant_ids.length || input.allowed_merchant_ids.some(id => !available.merchants.some(merchant => merchant.merchant_id === id && merchant.available))) throw new Error('请先核验并选择允许接待的商家。');
        await this.assertIdentity(active, input);
        await this.synchronizeModels(active);
      }
      await active.backend.invoke('set_whatsapp_reception', serviceInput(input));
      return this.detail(input.workspace_id);
    });
  }
  takeover(input: WhatsAppTakeoverInput): Promise<WhatsAppAccountSnapshot> {
    return this.exclusive(input.workspace_id, async () => {
      packet(input, ['workspace_id', ...identityFields, 'chat_id', 'enabled']); identity(input);
      if (!text(input.chat_id) || typeof input.enabled !== 'boolean') throw new Error('单聊接管参数无效。');
      const active = await this.current(input.workspace_id, false); await this.assertIdentity(active, input);
      await active.backend.invoke('takeover_whatsapp', serviceInput(input));
      return this.detail(input.workspace_id);
    });
  }
  reconcile(input: { workspace_id: string; operation_id: string }): Promise<WhatsAppAccountSnapshot> {
    packet(input, ['workspace_id', 'operation_id']);
    if (!text(input.operation_id)) throw new Error('发送操作无效。');
    return this.exclusive(input.workspace_id, async () => {
      const active = await this.current(input.workspace_id, false);
      await active.backend.invoke('reconcile_whatsapp_operation', { operation_id: input.operation_id });
      await active.coordinator.reconcile(input.operation_id);
      return this.detail(input.workspace_id);
    });
  }
  async saveSupplement(input: { workspace_id: string; expected_account_id: string; expected_connection_epoch: string; chat_id: string; merchant_id: string; binding_revision: number; expected_revision: number; body: string }, assertCurrent?: () => Promise<void>): Promise<{ body: string; revision: number }> {
    packet(input, ['workspace_id', ...identityFields, 'chat_id', 'merchant_id', 'binding_revision', 'expected_revision', 'body']);
    identity(input);
    if (!text(input.chat_id) || !uuid(input.merchant_id) || !Number.isSafeInteger(input.binding_revision) || input.binding_revision < 0 || !Number.isSafeInteger(input.expected_revision) || input.expected_revision < 0 || typeof input.body !== 'string' || input.body.length > 4000) throw new Error('业务补充无效。');
    return this.exclusive(input.workspace_id, async () => {
      const active = await this.current(input.workspace_id, false);
      await this.assertIdentity(active, input);
      await assertCurrent?.();
      return active.backend.invoke('_wa_save_supplement', serviceInput(input));
    });
  }
  async context(input: { workspace_id: string; chat_id: string }): Promise<WhatsAppContext> {
    packet(input, ['workspace_id', 'chat_id']);
    if (!text(input.chat_id)) throw new Error('单聊无效。');
    return (await this.current(input.workspace_id, false)).backend.invoke('get_whatsapp_context', { chat_id: input.chat_id });
  }
  async writebackCapabilities(): Promise<WhatsAppWritebackCapabilities> {
    return { format: 'wa-writeback-capabilities-v1', available: false, enabled: false, reason: 'WhatsApp 内容反写尚未开放。', destinations: ['merchant_workbench', 'international_platform'], content_kinds: ['summary', 'requirement_changes', 'note', 'message_reference'] };
  }
  private async synchronizeModels(active: Active): Promise<void> {
    if (active.closing) throw new Error('WhatsApp 正在关闭。');
    const work = active.models.synchronize();
    active.modelTasks.add(work);
    try { await work; } finally { active.modelTasks.delete(work); }
  }
  async modelsChanged(): Promise<void> {
    await Promise.all([...this.active.values()].map(async active => {
      if (active.closing) return;
      try { await this.synchronizeModels(active); }
      catch (error) {
        this.errors.set(active.workspaceId, error instanceof Error ? error.message : String(error));
        if (!active.closing) await active.backend.invoke('set_whatsapp_reception', { enabled: false }).catch(() => {});
      }
    }));
  }
  private cleanupActive(active: Active): Promise<void> {
    const existing = this.cleanup.get(active.workspaceId);
    if (existing) return existing;
    active.closing = true;
    clearInterval(active.timer);
    const work = (async () => {
      await active.coordinator.stop();
      await active.emergencyClose;
      await Promise.allSettled([...active.contexts.values(), ...active.modelTasks]);
      if (!active.backend.hasExited()) {
        const state = await active.backend.invoke<WhatsAppAccountState>('get_whatsapp_state').catch(() => null);
        if (state) this.cachedStates.set(active.workspaceId, { ...state, connected: false, enabled: false, connection_epoch: null });
      } else {
        const state = this.cachedStates.get(active.workspaceId);
        if (state) this.cachedStates.set(active.workspaceId, { ...state, connected: false, enabled: false, connection_epoch: null });
      }
      await active.backend.shutdown();
      await active.release();
      if (this.active.get(active.workspaceId) === active) this.active.delete(active.workspaceId);
      this.instances.delete(active.workspaceId);
    })();
    this.cleanup.set(active.workspaceId, work);
    void work.finally(() => { if (this.cleanup.get(active.workspaceId) === work) this.cleanup.delete(active.workspaceId); }).catch(() => {});
    return work;
  }
  async close(): Promise<void> {
    this.stopping = true;
    const ids = new Set([...this.active.keys(), ...this.starting.keys(), ...this.mutations.keys()]);
    const results = await Promise.allSettled([...ids].map(async id => {
      const starting = this.starting.get(id);
      if (starting) await starting.catch(() => {});
      const active = this.active.get(id);
      if (active) await this.cleanupActive(active);
    }));
    const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (failures.length) throw new AggregateError(failures.map(item => item.reason), `部分 WhatsApp 账号未能关闭：${failures.map(item => item.reason instanceof Error ? item.reason.message : String(item.reason)).join('；')}`);
  }
}
