import { randomBytes } from 'node:crypto';
import type { WhatsAppContext, WhatsAppHostInfo } from '../../shared/whatsapp';
import type { WhatsAppAccountSnapshot, WhatsAppBindingInput, WhatsAppTakeoverInput } from '../../shared/whatsapp-account';

type Customer = { conversation_id: string; name: string; buyer_id?: string; company?: string; revision?: number | string };
export interface CustomerPanelOptions {
  workspaceId: string;
  runtimeInstanceId: string;
  extensionDirectory?: string;
  getStatus(): WhatsAppHostInfo;
  resolveCurrentChat(): Promise<{ chat_id: string; name?: string } | null>;
  readState(): Promise<WhatsAppAccountSnapshot>;
  getContext(chatId: string): Promise<WhatsAppContext>;
  saveBinding(input: WhatsAppBindingInput, assertCurrent: () => Promise<void>): Promise<WhatsAppAccountSnapshot>;
  takeover(input: WhatsAppTakeoverInput): Promise<unknown>;
  saveSupplement(input: { workspace_id: string; expected_account_id: string; expected_connection_epoch: string; chat_id: string; merchant_id: string; binding_revision: number; expected_revision: number; body: string }, assertCurrent: () => Promise<void>): Promise<{ body: string; revision: number }>;
  reconcile(operationId: string): Promise<unknown>;
  searchCustomers(input: { merchant_id: string; query: string; chat_id: string; binding_revision: number }): Promise<Customer[]>;
}
type Identity = { workspace_id: string; runtime_instance_id: string; account_id: string; connection_epoch: string; chat_id: string; view_revision: number };
type View = { identity: Identity; name: string; merchants: Array<{ merchant_id: string; name: string; available: boolean }>; merchant_id: string | null; conversation_id: string | null; binding_revision: number; human_owned: boolean; reception_paused: boolean; reception_active: boolean; summary: string; requirements: string; supplement: { body: string; revision: number }; send_status: string; context_status: 'loading' | 'unavailable' | 'ready' };
const BINDING = '__receptionCustomerPanel';
const MAX_REQUEST = 20000;
const MAX_RESPONSE = 65536;
const MAX_INFLIGHT = 4;
const keys = (value: unknown, allowed: string[]): value is Record<string, any> => !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => allowed.includes(k));
const short = (s: unknown, limit = 256): s is string => typeof s === 'string' && s.length > 0 && s.length <= limit;
const byteSize = (s: string) => Buffer.byteLength(s, 'utf8');
const requirementLabels: Record<string, string> = { product: '产品', product_id: '产品ID', product_url: '产品链接', product_images: '产品图片', quantity: '数量与单位', specifications: '关键规格', customization: '定制要求', destination: '收货地', delivery: '客户期望交期', budget: '客户预算', packaging: '包装要求', certifications: '认证要求', contact: '联系方式' };
function formatRequirements(requirements: Record<string, unknown>): string {
  return Object.entries(requirementLabels).flatMap(([key, label]) => {
    const field = requirements[key];
    if (!field || typeof field !== 'object' || Array.isArray(field)) return [];
    const value = (field as { value?: unknown }).value;
    if (key === 'product_images') return Array.isArray(value) && value.length ? [`${label}：${value.length} 张`] : [];
    return typeof value === 'string' && value.trim() ? [`${label}：${value}`] : [];
  }).join('\n');
}

/** A private capability bridge attached exclusively to the installed extension's panel. */
export class CustomerPanelController {
  private browser: any;
  private page: any;
  private extensionId = '';
  private session: any;
  private target: any;
  private contextId = 0;
  private token = '';
  private timer: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;
  private revision = 0;
  private fingerprint = '';
  private emptyStatus = '';
  private observedEmptyStatus = '正在连接侧栏';
  private view: View | null = null;
  private inflight = 0;
  private candidates = new Set<string>();
  private error = '';
  private listening = false;
  private panelBehaviorReady = false;
  private contextAttempts = 0;
  private contextNextAttempt = 0;
  private contextPending = false;
  private connectAttempts = 0;
  private readonly onTarget = (target: any) => { if (target === this.target && target.url() !== this.panelUrl()) this.retirePanel(); else if (target.url() === this.panelUrl()) void this.connectPanel(target); };
  private readonly onDestroyed = (target: any) => { if (target === this.target) this.retirePanel(); };
  private readonly onDisconnected = () => { this.retirePanel(); this.browser = undefined; this.listening = false; };
  constructor(private readonly options: CustomerPanelOptions) {}
  private panelUrl() { return `chrome-extension://${this.extensionId}/panel.html`; }
  getInfo(): { available: boolean; error?: string } { return { available: !!this.browser && !this.error, ...(this.error ? { error: this.error } : {}) }; }
  async attach(browser: any, page: any, extensionId: string): Promise<void> {
    await this.close();
    if (!browser || !page || !/^[a-p]{32}$/.test(extensionId)) throw new Error('无效的扩展或浏览器');
    this.browser = browser; this.page = page; this.extensionId = extensionId; this.error = ''; this.connectAttempts = 0;
    browser.on('targetcreated', this.onTarget); browser.on('targetchanged', this.onTarget);
    browser.on('targetdestroyed', this.onDestroyed); browser.on('disconnected', this.onDisconnected); this.listening = true;
    const targets = browser.targets().filter((t: any) => t.url() === this.panelUrl());
    if (targets.length === 1) await this.connectPanel(targets[0]);
  }
  async open(): Promise<void> {
    if (!this.browser || !this.page) throw new Error('浏览器未连接');
    const existing = this.browser.targets().filter((target: any) => target.url() === this.panelUrl());
    if (existing.length === 1) { if (!this.session || this.error) await this.connectPanel(existing[0]); return; }
    const browser = this.browser;
    if (!this.panelBehaviorReady) {
      const workerTarget = await browser.waitForTarget((target: any) => target.type() === 'service_worker' && target.url() === `chrome-extension://${this.extensionId}/background.js`, { timeout: 10000 });
      const worker = await workerTarget.worker();
      if (!worker || browser !== this.browser) throw new Error('侧栏尚未就绪');
      const configured = await worker.evaluate(async () => {
        for (let attempt = 0; attempt < 100; attempt++) {
          const sidePanel = (globalThis as any).chrome?.sidePanel;
          if (typeof sidePanel?.setPanelBehavior === 'function') {
            await sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
            return true;
          }
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        return false;
      }).catch(() => false);
      if (browser !== this.browser) throw new Error('浏览器已变化');
      // The background script may already have configured the action even when
      // its transient worker execution context cannot yet expose extension APIs.
      this.panelBehaviorReady = configured === true;
    }
    const browserSession = await this.browser.target().createCDPSession();
    const pageSession = await this.page.createCDPSession();
    try {
      const info = await pageSession.send('Target.getTargetInfo');
      const all = await browserSession.send('Target.getTargets', { filter: [{ type: 'tab', exclude: false }, { type: 'page', exclude: false }] });
      const tabs = all.targetInfos.filter((v: any) => v.type === 'tab');
      const pageInfo = all.targetInfos.find((v: any) => v.targetId === info.targetInfo.targetId);
      const tab = pageInfo && (tabs.find((v: any) => v.targetId === pageInfo.parentId) || (tabs.length === 1 ? tabs[0] : undefined));
      if (!tab) throw new Error('无法确定聊天标签页');
      await browserSession.send('Extensions.triggerAction', { id: this.extensionId, targetId: tab.targetId });
    } finally { await Promise.allSettled([browserSession.detach(), pageSession.detach()]); }
  }
  async close(): Promise<void> {
    const browser = this.browser;
    if (browser && this.listening) {
      browser.off('targetcreated', this.onTarget); browser.off('targetchanged', this.onTarget);
      browser.off('targetdestroyed', this.onDestroyed); browser.off('disconnected', this.onDisconnected);
    }
    this.listening = false;
    await this.emit({ kind: 'clear' }, this.generation).catch(() => {});
    this.retirePanel(); this.browser = undefined; this.page = undefined; this.panelBehaviorReady = false;
  }
  private retirePanel() {
    this.generation++; clearTimeout(this.timer); this.timer = undefined;
    const session = this.session; this.session = undefined; this.target = undefined; this.contextId = 0; this.token = '';
    this.view = null; this.fingerprint = ''; this.emptyStatus = ''; this.observedEmptyStatus = '正在连接侧栏'; this.inflight = 0; this.candidates.clear();
    // An old read keeps its slot until it actually settles, including after a reconnect.
    this.contextAttempts = 0; this.contextNextAttempt = 0;
    if (session) { session.removeAllListeners?.(); void session.detach().catch(() => {}); }
  }
  private reconnectPanel(target: any) {
    this.retirePanel();
    if (!this.browser || target.url() !== this.panelUrl()) return;
    this.connectAttempts = Math.min(this.connectAttempts + 1, 6);
    const generation = this.generation;
    const delay = Math.min(30000, 2000 * 2 ** (this.connectAttempts - 1));
    this.timer = setTimeout(() => {
      if (generation === this.generation && this.browser && target.url() === this.panelUrl()) void this.connectPanel(target);
    }, delay);
  }
  private async connectPanel(target: any) {
    if (!this.browser || target.url() !== this.panelUrl() || target === this.target) return;
    this.retirePanel(); const generation = this.generation; this.target = target;
    try {
      const session = await target.createCDPSession();
      if (generation !== this.generation || target.url() !== this.panelUrl()) { await session.detach(); return; }
      this.session = session;
      const tree = await session.send('Page.getFrameTree');
      const frameId = tree.frameTree?.frame?.id;
      if (!frameId || tree.frameTree.frame.url !== this.panelUrl()) throw new Error('侧栏页面不匹配');
      session.on('Runtime.executionContextCreated', (event: any) => {
        const c = event.context;
        if (c.origin === `chrome-extension://${this.extensionId}` && c.auxData?.frameId === frameId && c.auxData?.isDefault && c.auxData?.type === 'default') this.contextId = c.id;
      });
      session.on('Runtime.executionContextDestroyed', (event: any) => { if (event.executionContextId === this.contextId) this.reconnectPanel(target); });
      session.on('Runtime.executionContextsCleared', () => this.reconnectPanel(target));
      session.on('Runtime.bindingCalled', (event: any) => { void this.handleBinding(event, generation); });
      await session.send('Runtime.enable');
      if (!this.contextId || generation !== this.generation || target.url() !== this.panelUrl()) throw new Error('扩展执行上下文不可用');
      await session.send('Runtime.addBinding', { name: BINDING, executionContextId: this.contextId });
      this.token = randomBytes(32).toString('hex');
      const bootstrap = await session.send('Runtime.evaluate', { contextId: this.contextId, expression: `(async()=>{for(let i=0;i<100;i++){if(typeof window.__customerPanelBootstrap==='function'){window.__customerPanelBootstrap(${JSON.stringify(this.token)});return true}await new Promise(r=>setTimeout(r,50))}throw Error('panel bootstrap timeout')})()`, awaitPromise: true, returnByValue: true });
      if (bootstrap.exceptionDetails || bootstrap.result?.value !== true) throw new Error('侧栏初始化失败');
      if (generation !== this.generation) return;
      this.error = ''; this.connectAttempts = 0; void this.poll(generation);
    } catch {
      if (generation === this.generation) {
        this.error = '侧栏连接失败'; this.reconnectPanel(target);
      }
    }
  }
  private async observe() {
    const before = this.options.getStatus();
    const snapshot = await this.options.readState();
    const chat = await this.options.resolveCurrentChat();
    const after = this.options.getStatus(); const state = snapshot.state;
    if (!state?.connected || !state.account_id || !state.connection_epoch || snapshot.runtime_instance_id !== this.options.runtimeInstanceId || snapshot.profile.workspace_id !== this.options.workspaceId || state.workspace_id !== this.options.workspaceId || before.status !== 'ready' || after.status !== 'ready' || before.account_id !== state.account_id || after.account_id !== state.account_id || before.connection_epoch !== state.connection_epoch || after.connection_epoch !== state.connection_epoch) { this.observedEmptyStatus = '正在连接侧栏'; return null; }
    this.observedEmptyStatus = '请选择一个单聊';
    if (!chat || !short(chat.chat_id) || /@g\.us$|@broadcast$/.test(chat.chat_id)) return null;
    return { chat, snapshot, state };
  }
  private async poll(generation: number) {
    try {
      const observed = await this.observe();
      if (generation !== this.generation) return;
      const binding = observed?.state.bindings.find(b => b.chat_id === observed.chat.chat_id);
      const conversation = observed?.state.conversations.find(c => c.chat_id === observed.chat.chat_id);
      const fingerprint = observed ? JSON.stringify([observed.state.account_id, observed.state.connection_epoch, observed.chat.chat_id, observed.chat.name, binding, conversation, observed.snapshot.merchants, observed.state.default_merchant_id, observed.state.default_revision, observed.state.enabled, observed.state.allowed_merchant_ids, observed.state.model.ready, observed.state.legacy.status, (observed.state.pending_operations ?? []).filter(op => op.chat_id === observed.chat.chat_id)]) : '';
      const emptyStatus = this.observedEmptyStatus;
      if (fingerprint !== this.fingerprint || (!!observed && !this.view) || (!observed && emptyStatus !== this.emptyStatus)) {
        this.emptyStatus = observed ? '' : emptyStatus;
        this.fingerprint = fingerprint; this.view = null; this.candidates.clear(); this.revision++;
        this.contextAttempts = 0; this.contextNextAttempt = 0;
        await this.emit({ kind: 'clear', status: observed ? '正在加载客户信息' : emptyStatus }, generation);
        if (observed) {
          const identity: Identity = { workspace_id: this.options.workspaceId, runtime_instance_id: this.options.runtimeInstanceId, account_id: observed.state.account_id!, connection_epoch: observed.state.connection_epoch!, chat_id: observed.chat.chat_id, view_revision: this.revision };
          const view: View = { identity, name: observed.chat.name?.trim() || '当前客户', merchants: observed.snapshot.merchants.map(m => ({ merchant_id: m.merchant_id, name: m.name, available: m.available })), merchant_id: binding ? binding.merchant_id : observed.state.default_merchant_id, conversation_id: binding?.conversation_id ?? null, binding_revision: binding?.revision ?? 0, human_owned: !!conversation?.human_owned, reception_paused: !!conversation?.reception_paused, reception_active: !!observed.state.enabled && observed.state.allowed_merchant_ids.includes((binding ? binding.merchant_id : observed.state.default_merchant_id) ?? '') && observed.state.legacy.status === 'ready', summary: '', requirements: '', supplement: { body: '', revision: 0 }, send_status: (observed.state.pending_operations ?? []).some(op => op.chat_id === observed.chat.chat_id) ? '发送结果待确认' : '', context_status: 'loading' };
          this.view = view;
          await this.emit({ kind: 'view', view }, generation);
        }
      }
      if (this.view && this.view.context_status !== 'ready' && !this.contextPending && Date.now() >= this.contextNextAttempt) void this.loadContext(this.view, generation);
    } catch { if (generation === this.generation) { this.view = null; this.fingerprint = ''; this.emptyStatus = ''; await this.emit({ kind: 'clear', status: '客户信息暂不可用' }, generation); } }
    finally { if (generation === this.generation && this.session) this.timer = setTimeout(() => void this.poll(generation), 500); }
  }
  private async loadContext(view: View, generation: number) {
    this.contextPending = true;
    this.contextAttempts = Math.min(this.contextAttempts + 1, 6);
    const retryDelay = Math.min(30000, 3000 * 2 ** (this.contextAttempts - 1));
    this.contextNextAttempt = Date.now() + retryDelay;
    try {
      const context = await this.options.getContext(view.identity.chat_id);
      await this.assertCurrent(view.identity, generation);
      if (context.binding_revision !== view.binding_revision) throw new Error('关联已更新');
      view.summary = context.wa_summary || '';
      view.supplement = context.wa_supplement || { body: '', revision: 0 };
      view.requirements = formatRequirements(context.international_background?.requirements || {});
      view.context_status = 'ready';
      await this.emit({ kind: 'view', view }, generation);
    } catch {
      if (generation !== this.generation || this.view !== view) return;
      try { await this.assertCurrent(view.identity, generation); } catch { return; }
      if (generation !== this.generation || this.view !== view) return;
      view.context_status = 'unavailable';
      this.contextNextAttempt = Date.now() + retryDelay;
      await this.emit({ kind: 'view', view }, generation).catch(() => {});
    } finally { this.contextPending = false; }
  }
  private async assertCurrent(identity: Identity, generation: number, allowBindingChange = false) {
    if (generation !== this.generation || !this.view || JSON.stringify(identity) !== JSON.stringify(this.view.identity)) throw new Error('客户已变更');
    const observed = await this.observe();
    const binding = observed?.state.bindings.find(b => b.chat_id === identity.chat_id);
    if (generation !== this.generation || !this.view || JSON.stringify(identity) !== JSON.stringify(this.view.identity)) throw new Error('客户已变更');
    if (!observed || observed.chat.chat_id !== identity.chat_id || observed.state.account_id !== identity.account_id || observed.state.connection_epoch !== identity.connection_epoch || (!allowBindingChange && (binding?.revision ?? 0) !== this.view.binding_revision)) {
      this.view = null; this.fingerprint = ''; await this.emit({ kind: 'clear' }, generation); throw new Error('客户或连接已变更');
    }
  }
  private async emit(message: unknown, generation: number) {
    if (generation !== this.generation || !this.session || !this.contextId || this.target?.url() !== this.panelUrl()) return;
    const data = JSON.stringify(message); if (byteSize(data) > MAX_RESPONSE) throw new Error('侧栏数据过大');
    await this.session.send('Runtime.evaluate', { contextId: this.contextId, expression: `window.__customerPanelReceive(${JSON.stringify(data)})` });
  }
  private async handleBinding(event: any, generation: number) {
    if (generation !== this.generation || event.name !== BINDING || event.executionContextId !== this.contextId || this.target?.url() !== this.panelUrl() || typeof event.payload !== 'string' || byteSize(event.payload) > MAX_REQUEST) return;
    let request: any;
    try { request = JSON.parse(event.payload); } catch { return; }
    if (!keys(request, ['token', 'request_id', 'action', 'identity', 'input']) || request.token !== this.token || !short(request.request_id, 80) || !short(request.action, 40) || !keys(request.identity, ['workspace_id', 'runtime_instance_id', 'account_id', 'connection_epoch', 'chat_id', 'view_revision']) || !this.view) return;
    const requestId = request.request_id;
    if (this.inflight >= MAX_INFLIGHT) { await this.emit({ kind: 'response', request_id: requestId, error: '请求过多，请稍后重试' }, generation); return; }
    this.inflight++;
    let committed = false;
    try {
      const identity = request.identity as Identity;
      await this.assertCurrent(identity, generation);
      if (this.view?.context_status !== 'ready') throw new Error('客户信息尚未就绪');
      const input = request.input; let result: unknown;
      if (request.action === 'search' && keys(input, ['merchant_id', 'query']) && short(input.merchant_id) && typeof input.query === 'string' && input.query.length <= 80 && this.view!.merchants.some(m => m.merchant_id === input.merchant_id && m.available)) {
        const rows = await this.options.searchCustomers({ merchant_id: input.merchant_id, query: input.query, chat_id: identity.chat_id, binding_revision: this.view!.binding_revision });
        const safeRows = rows.slice(0, 25).filter(r => short(r.conversation_id) && short(r.name)).map(r => ({ conversation_id: r.conversation_id, name: r.name.slice(0, 256), buyer_id: typeof r.buyer_id === 'string' ? r.buyer_id.slice(0, 120) : undefined, company: typeof r.company === 'string' ? r.company.slice(0, 256) : undefined }));
        await this.assertCurrent(identity, generation);
        this.candidates.clear();
        for (const row of safeRows) this.candidates.add(JSON.stringify([input.merchant_id, row.conversation_id]));
        result = safeRows;
      } else if (request.action === 'save' && keys(input, ['merchant_id', 'conversation_id']) && (input.merchant_id === null || (short(input.merchant_id) && this.view!.merchants.some(m => m.merchant_id === input.merchant_id && m.available))) && (input.conversation_id === null || (short(input.conversation_id) && input.merchant_id !== null && ((input.merchant_id === this.view!.merchant_id && input.conversation_id === this.view!.conversation_id) || this.candidates.has(JSON.stringify([input.merchant_id, input.conversation_id])))))) {
        await this.options.saveBinding({ workspace_id: identity.workspace_id, expected_account_id: identity.account_id, expected_connection_epoch: identity.connection_epoch, chat_id: identity.chat_id, merchant_id: input.merchant_id, conversation_id: input.conversation_id, expected_revision: this.view!.binding_revision }, () => this.assertCurrent(identity, generation)); committed = true; result = true;
      } else if (request.action === 'supplement' && keys(input, ['body', 'expected_revision']) && typeof input.body === 'string' && input.body.length <= 4000 && Number.isSafeInteger(input.expected_revision) && input.expected_revision >= 0 && this.view!.merchant_id && input.expected_revision === this.view!.supplement.revision) {
        result = await this.options.saveSupplement({ workspace_id: identity.workspace_id, expected_account_id: identity.account_id, expected_connection_epoch: identity.connection_epoch, chat_id: identity.chat_id, merchant_id: this.view!.merchant_id, binding_revision: this.view!.binding_revision, expected_revision: input.expected_revision, body: input.body }, () => this.assertCurrent(identity, generation)); committed = true;
      } else if (request.action === 'takeover' && keys(input, ['enabled']) && typeof input.enabled === 'boolean') {
        await this.options.takeover({ workspace_id: identity.workspace_id, expected_account_id: identity.account_id, expected_connection_epoch: identity.connection_epoch, chat_id: identity.chat_id, enabled: input.enabled }); result = true;
      } else throw new Error('不支持的操作');
      await this.assertCurrent(identity, generation, request.action === 'save');
      await this.emit({ kind: 'response', request_id: requestId, result }, generation);
      if (request.action !== 'search') { this.fingerprint = ''; this.view = null; }
    } catch { if (!committed) await this.emit({ kind: 'response', request_id: requestId, error: '操作未完成，请确认当前客户和连接状态' }, generation).catch(() => {}); }
    finally { if (generation === this.generation) this.inflight--; }
  }
}
