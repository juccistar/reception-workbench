import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { app, WebContentsView, type BrowserWindow, type Rectangle } from 'electron';
import puppeteer, { type Frame, type HTTPResponse, type Page } from 'puppeteer';
import type { WhatsAppHostInfo, WhatsAppPageState, WhatsAppPhase } from '../../shared/whatsapp';
import { SendLedger, type FrozenIntent, type SendResult } from './ledger';
import { ViewTransport } from './transport';
import { connectionFailure, retryAfterDelay, whatsappUserAgent, whatsappPageProbe, WhatsAppConnectionError, WhatsAppStepFailure } from './connection';
import { WhatsAppDiagnostics } from './diagnostics';

// whatsapp-web.js 1.34.8, Apache-2.0. The instance-specific initialize adapter
// follows Client.initialize, replacing browser creation and guarding frame reinjection.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { Client } = require('whatsapp-web.js');

type AccountStatus = 'connecting' | 'qr' | 'authenticated' | 'ready' | 'disconnected' | 'error';
export type WhatsAppEvent =
  | ({ type: 'account'; status: AccountStatus } & WhatsAppHostInfo)
  | { type: 'messages'; account_id: string; connection_epoch: string; messages: WhatsAppMessage[] }
  | { type: 'ack'; message_id: string; ack: number; account_id: string; connection_epoch: string };
export interface WhatsAppMessage { id: string; chat_id: string; body: string; timestamp_ms: number; from_me: boolean; name: string; is_group: boolean; is_history: boolean; metadata_only?: boolean; ack?: number }
export interface WhatsAppHostOptions { window: BrowserWindow; partition: string; directory: string; diagnosticsDirectory?: string; applicationVersion?: string; sessionGeneration?: number; onEvent: (event: WhatsAppEvent) => void }
const direct = (id: string): boolean => /@(c\.us|lid)$/.test(id);
const serialized = (id: any): string => typeof id === 'string' ? id : (id?._serialized || '');
const PHASE_TIMEOUT_MS = 60_000;

export class WhatsAppHost {
  private readonly view: WebContentsView;
  private readonly ledger: SendLedger;
  private client: any;
  private generation = 0;
  private observedDocument = 0;
  private observedDocumentSerial = 0;
  private retryNotBefore = 0;
  private transport?: ViewTransport;
  private browser?: Awaited<ReturnType<typeof puppeteer.connect>>;
  private phaseTimer?: ReturnType<typeof setTimeout>;
  private pageCleanup?: () => void;
  private failure?: WhatsAppConnectionError;
  private readonly diagnostics: WhatsAppDiagnostics;
  private diagnosticId?: string;
  private stage: WhatsAppPhase = 'view_setup';
  private previousStage?: WhatsAppPhase;
  private pageState: WhatsAppPageState = 'closed';
  private documentTrusted = false;
  private documentSerial = 0;
  private postLogout = false;
  private databaseError = false;
  private webVersion: unknown;
  private nativeNavigation = '';
  private nativeProbe?: ReturnType<typeof setInterval>;
  private probeTask?: Promise<void>;
  private lastProbeSerial = -1;
  private page?: Page;
  private bindings = new Set<string>();
  private cleanupTask: Promise<void> = Promise.resolve();
  private cleanupFailed = false;
  private status: AccountStatus = 'disconnected';
  private account = '';
  private epoch = '';
  private visible = false;
  private started = false;
  private closing = false;
  private pendingSends = new Map<string, number>();
  private outgoing: Array<{ account: string; epoch: string; message: WhatsAppMessage }> = [];
  constructor(private readonly options: WhatsAppHostOptions) {
    if (!options.partition.startsWith('persist:')) throw new Error('WhatsApp requires a persistent partition');
    this.diagnostics = new WhatsAppDiagnostics(options.diagnosticsDirectory, options.applicationVersion ?? app.getVersion());
    this.diagnostics.event(0, 'view_setup', 'started');
    this.ledger = new SendLedger(join(options.directory, 'send-ledger'));
    this.view = new WebContentsView({ webPreferences: { partition: options.partition, nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false } });
    const wc = this.view.webContents;
    const agent = whatsappUserAgent(wc.getUserAgent(), app.getName(), app.getVersion());
    // This partition belongs only to WhatsApp. Workers use its session agent,
    // whereas documents use the WebContents override; both must be valid ASCII.
    wc.session.setUserAgent(agent);
    wc.setUserAgent(agent);
    wc.setWindowOpenHandler(() => ({ action: 'deny' }));
    wc.on('will-navigate', (event, url) => { if (!this.allowed(url)) event.preventDefault(); });
    wc.on('render-process-gone', () => this.pageFailed(new WhatsAppConnectionError('page_closed')));
    wc.on('destroyed', () => this.pageFailed(new WhatsAppConnectionError('page_closed')));
    wc.on('did-start-navigation', (_event, url, inPlace, mainFrame) => {
      if (!mainFrame || inPlace || this.closing) return;
      this.postLogout = this.allowed(url) && new URL(url).searchParams.get('post_logout') === '1';
      this.databaseError = false;
      this.webVersion = undefined;
      ++this.documentSerial;
      this.updateDiagnostics();
      if (this.started && this.observedDocument === this.generation && this.allowed(url)) {
        const generation = this.generation;
        this.previousStage = this.stage;
        this.stage = 'page_reload';
        this.updateDiagnostics();
        if (url.includes('post_logout=1')) this.retire(generation, 'disconnected', undefined, true);
        else {
          const failure = new WhatsAppConnectionError('page_context_lost');
          this.diagnosticId = this.diagnostics.failure(generation, this.stage, failure, failure.error_code);
          this.retire(generation, 'error', failure, true);
        }
      }
      this.nativeNavigation = url;
      this.documentTrusted = false;
      this.pageState = 'loading';
      this.update(this.status);
    });
    wc.on('did-navigate', (_event, url, code) => {
      if (this.closing || url === 'about:blank') return;
      if (!this.allowed(url)) { this.pageFailed(new WhatsAppConnectionError('unexpected_navigation')); return; }
      if (code >= 400) {
        if (!this.started) this.pageFailed(new WhatsAppConnectionError('http_error', code));
        return; // Active SDK responses preserve the exact Retry-After header.
      }
      this.documentTrusted = code >= 200 && code < 400;
      void this.probePage();
    });
    wc.on('did-finish-load', () => { void this.probePage(); });
    wc.on('did-fail-load', (_event, code, description, url, mainFrame) => {
      if (mainFrame && code !== -3 && code !== -379 && (!this.nativeNavigation || url === this.nativeNavigation))
        this.pageFailed(connectionFailure(new Error(description), 'navigation_timeout'));
    });
    wc.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    wc.session.setPermissionCheckHandler(() => false);
    this.nativeProbe = setInterval(() => { void this.probePage(); }, 1000);
    this.nativeProbe.unref?.();
    this.diagnostics.event(0, 'view_setup', 'completed');
  }
  private updateDiagnostics(): void {
    this.diagnostics.setContext({ webVersion: this.webVersion, documentGeneration: this.documentSerial, postLogout: this.postLogout, databaseError: this.databaseError, sessionGeneration: this.options.sessionGeneration ?? 0, previousPhase: this.previousStage, ...(this.options.sessionGeneration ? { recoveryStage: 'committed' as const } : {}) });
  }
  private allowed(url: string): boolean {
    try { const parsed = new URL(url); return parsed.protocol === 'https:' && parsed.hostname === 'web.whatsapp.com'; } catch { return false; }
  }
  private update(status: AccountStatus): void {
    if (this.closing && status !== 'disconnected') return;
    this.status = status;
    this.options.onEvent({ type: 'account', ...this.getStatus(), status });
  }
  private active(generation: number, account: string, epoch: string): boolean {
    return !this.closing && this.generation === generation && this.status === 'ready' && this.account === account && this.epoch === epoch;
  }
  private current(generation: number): boolean { return !this.closing && this.started && this.generation === generation; }
  private async step<T>(generation: number, stage: WhatsAppPhase, action: () => Promise<T>): Promise<T> {
    if (!this.current(generation)) throw new WhatsAppConnectionError('page_closed');
    this.stage = stage;
    this.diagnostics.event(generation, stage, 'started');
    this.update(this.status);
    const started = performance.now();
    try {
      const result = await action();
      if (!this.current(generation)) throw new WhatsAppConnectionError('page_closed');
      this.diagnostics.event(generation, stage, 'completed', performance.now() - started);
      return result;
    } catch (error) { throw error instanceof WhatsAppStepFailure ? error : new WhatsAppStepFailure(stage, error); }
  }
  private fail(generation: number, error: unknown, stage = this.stage): void {
    if (!this.current(generation)) return;
    const phase = error instanceof WhatsAppStepFailure ? error.phase : stage;
    const original = error instanceof WhatsAppStepFailure ? error.original : error;
    const failure = connectionFailure(original, phase === 'page_navigation' ? 'navigation_timeout' : 'sdk_timeout', phase);
    this.stage = phase;
    this.diagnosticId = this.diagnostics?.failure(generation, phase, original, failure.error_code, failure.http_status);
    this.retire(generation, 'error', failure, failure.error_code === 'page_database_error');
  }
  private pageFailed(error: WhatsAppConnectionError): void {
    if (this.closing) return;
    if (error.error_code === 'http_error' && error.http_status === 429)
      this.retryNotBefore = Math.max(this.retryNotBefore, Date.now() + retryAfterDelay(undefined));
    this.documentTrusted = false;
    this.pageState = error.error_code === 'page_closed' ? 'closed' : 'error';
    if (this.started) this.fail(this.generation, error, 'page_navigation');
    else {
      this.stage = 'page_navigation';
      this.failure = error;
      this.diagnosticId = this.diagnostics.failure(this.generation, this.stage, error, error.error_code, error.http_status);
      this.update('error');
    }
  }
  private probePage(): Promise<void> {
    if (this.probeTask) return this.probeTask;
    const task = this.doProbePage();
    this.probeTask = task;
    void task.finally(() => { if (this.probeTask === task) this.probeTask = undefined; });
    return task;
  }
  private async doProbePage(): Promise<void> {
    if (this.closing || !this.documentTrusted || this.view.webContents.isDestroyed()
        || !this.allowed(this.view.webContents.getURL()) || typeof this.view.webContents.executeJavaScript !== 'function') return;
    const serial = this.documentSerial;
    try {
      // Read only visibility booleans. Never copy a QR value, page text or contact.
      const result = await this.view.webContents.executeJavaScript(`(${whatsappPageProbe.toString()})()`);
      if (this.closing || this.documentSerial !== serial || !this.documentTrusted) return;
      this.lastProbeSerial = serial;
      this.databaseError = result?.databaseError === true;
      this.webVersion = result?.webVersion;
      this.updateDiagnostics();
      if (this.databaseError) {
        this.pageState = 'error';
        if (this.started) this.fail(this.generation, new WhatsAppConnectionError('page_database_error'));
        else if (this.failure?.error_code !== 'page_database_error') {
          this.failure = new WhatsAppConnectionError('page_database_error');
          this.diagnosticId = this.diagnostics.failure(this.generation, this.stage, this.failure, this.failure.error_code);
          this.update('error');
        }
        return;
      }
      if (result?.available === true && this.pageState !== 'available' && this.failure?.error_code !== 'page_database_error') {
        this.pageState = 'available';
        this.update(this.status);
      }
    } catch { /* A navigation can replace the document while this passive probe runs. */ }
  }
  private clearPhase(): void {
    if (this.phaseTimer) clearTimeout(this.phaseTimer);
    this.phaseTimer = undefined;
  }
  private phase(generation: number, code: 'navigation_timeout' | 'sdk_timeout' | 'ready_timeout'): void {
    this.clearPhase();
    this.phaseTimer = setTimeout(() => this.fail(generation, new WhatsAppConnectionError(code)), PHASE_TIMEOUT_MS);
  }
  private releaseConnection(): void {
    this.clearPhase();
    const cleanup = this.pageCleanup;
    const browser = this.browser, transport = this.transport, page = this.page;
    const bindings = [...(this.bindings ?? [])], generation = this.generation;
    this.bindings?.clear();
    this.pageCleanup = undefined;
    this.browser = undefined;
    this.transport = undefined;
    this.page = undefined;
    try { this.client?._injectAbort?.abort(); } catch { /* best effort */ }
    try { this.client?.removeAllListeners(); } catch { /* best effort */ }
    try { cleanup?.(); } catch { /* best effort */ }
    const disconnect = () => {
      try { browser?.disconnect(); } catch { /* best effort */ }
      try { transport?.close(); } catch { /* best effort */ }
    };
    if (!page || !bindings.length) { disconnect(); this.cleanupTask = Promise.resolve(); return; }
    // All operations retain the old CDP session/context. Reconnect waits for this
    // bounded cleanup; a retired Page can never issue commands through a new one.
    const release = (async () => {
      await Promise.all(bindings.map(name => page.removeExposedFunction(name)));
      await page.evaluate(names => {
        const sdk = window as any;
        for (const [owner, event, handler] of sdk._wwjsListeners || []) owner.off(event, handler);
        delete sdk._wwjsListeners;
        for (const name of names) sdk[name] = async () => undefined;
      }, bindings);
      return true;
    })().catch(() => false);
    this.cleanupTask = (async () => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const clean = await Promise.race([release, new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), 1000); })]);
      if (timer) clearTimeout(timer);
      disconnect();
      if (!clean) this.cleanupFailed = true;
      if (!clean && !this.closing && this.generation === generation) {
        this.documentTrusted = false;
        this.pageState = 'error';
        this.update(this.status);
      }
    })();
  }
  private retire(generation: number, status: 'disconnected' | 'error', failure?: WhatsAppConnectionError, preserveNavigation = false): void {
    if (this.generation !== generation || this.closing || !this.started) return;
    const keepPage = !!failure && (this.documentTrusted || failure.error_code === 'page_context_lost' && this.allowed(this.nativeNavigation))
      && ['sdk_timeout', 'ready_timeout', 'sdk_error', 'cdp_error', 'sdk_resource_missing', 'page_context_lost', 'app_module_error', 'auth_failed'].includes(failure.error_code);
    ++this.generation;
    this.started = false;
    this.failure = failure;
    this.outgoing = this.outgoing.filter(item => item.epoch !== this.epoch);
    this.pendingSends.delete(this.epoch);
    if (!keepPage && !preserveNavigation) {
      this.documentTrusted = false;
      this.pageState = failure?.error_code === 'page_closed' ? 'closed' : failure ? 'error' : 'closed';
      if (!preserveNavigation && !this.view?.webContents.isDestroyed()) this.view?.webContents.stop();
    }
    this.releaseConnection();
    this.update(status);
  }
  private bindEvents(client: any, generation: number): void {
    const current = () => !this.closing && this.generation === generation;
    client.on('qr', () => {
      if (current() && this.status !== 'authenticated' && this.status !== 'ready') {
        this.clearPhase(); this.stage = 'qr_wait'; this.update('qr'); void this.probePage();
      }
    });
    client.on('authenticated', () => {
      if (current() && this.status !== 'ready') { this.stage = 'authenticated'; this.phase(generation, 'ready_timeout'); this.update('authenticated'); }
    });
    client.on('auth_failure', () => this.fail(generation, new WhatsAppConnectionError('auth_failed')));
    client.on('disconnected', () => this.retire(generation, 'disconnected', undefined, this.documentTrusted || this.allowed(this.nativeNavigation)));
    client.on('ready', () => {
      if (!current() || this.status === 'ready') return;
      this.account = serialized(client.info?.wid);
      if (!this.account) { this.fail(generation, new WhatsAppConnectionError('auth_failed')); return; }
      this.clearPhase();
      this.stage = 'ready'; this.pageState = 'available';
      this.diagnostics?.event(generation, 'ready', 'completed');
      this.epoch = randomUUID(); this.update('ready');
      void this.readHistory(client, generation, this.account, this.epoch).catch(() => { /* no content or account in logs */ });
    });
    client.on('message_create', (message: any) => {
      const account = this.account, epoch = this.epoch;
      if (this.active(generation, account, epoch)) this.emitMessage(message, false, '', generation, account, epoch);
    });
    client.on('message_ack', (message: any, ack: number) => {
      const account = this.account, epoch = this.epoch;
      const id = serialized(message?.id);
      const chat = message?.fromMe ? message?.to : message?.from;
      if (id && direct(chat || '') && this.active(generation, account, epoch)) this.options.onEvent({ type: 'ack', message_id: id, ack, account_id: account, connection_epoch: epoch });
    });
  }
  private emitMessage(message: any, history: boolean, name: string, generation: number, account: string, epoch: string): void {
    const chat = String(message.fromMe ? message.to : message.from || '');
    const id = serialized(message.id);
    if (!direct(chat) || !id || !account || !epoch || !this.active(generation, account, epoch)) return;
    const item: WhatsAppMessage = { id, chat_id: chat, body: typeof message.body === 'string' ? message.body : '', timestamp_ms: Number(message.timestamp || 0) * 1000, from_me: !!message.fromMe, name, is_group: false, is_history: history, ack: typeof message.ack === 'number' ? message.ack : undefined };
    if (item.from_me && !history && this.pendingSends.get(epoch)) this.outgoing.push({ account, epoch, message: item });
    else this.options.onEvent({ type: 'messages', account_id: account, connection_epoch: epoch, messages: [item] });
  }
  private async readHistory(client: any, generation: number, account: string, epoch: string): Promise<void> {
    const chats = await client.getChats();
    for (const chat of chats) {
      if (!this.active(generation, account, epoch)) return;
      const chatId = serialized(chat.id);
      if (!direct(chatId) || chat.isGroup) continue;
      const name = String(chat.name || '');
      this.options.onEvent({ type: 'messages', account_id: account, connection_epoch: epoch, messages: [{ id: `metadata:${chatId}`, chat_id: chatId, body: '', timestamp_ms: 0, from_me: false, name, is_group: false, is_history: true, metadata_only: true }] });
      try { const messages = await chat.fetchMessages({ limit: 50 }); for (const msg of messages) this.emitMessage(msg, true, name, generation, account, epoch); } catch { /* retry on next connection */ }
    }
  }
  async connect(): Promise<void> {
    if (this.started) return;
    if (this.closing) throw new Error('WhatsApp host is closed');
    if (Date.now() < this.retryNotBefore) return;
    this.started = true;
    this.failure = undefined;
    this.diagnosticId = undefined;
    this.stage = 'view_setup';
    this.previousStage = undefined;
    this.pageState = 'loading';
    this.documentTrusted = false;
    const generation = ++this.generation;
    this.phase(generation, 'sdk_timeout');
    this.update('connecting');
    void (async () => {
      await this.cleanupTask;
      if (this.cleanupFailed) throw new WhatsAppConnectionError('cdp_error');
      if (!this.current(generation)) return;
      const client = this.client = await this.step(generation, 'auth_setup', async () => new Client({ userAgent: false, authTimeoutMs: PHASE_TIMEOUT_MS, webVersionCache: { type: 'none' } }));
      this.bindEvents(client, generation);
      await this.initialize(client, generation);
    })().catch(error => this.fail(generation, error));
  }
  private httpFailure(response: HTTPResponse): WhatsAppConnectionError {
    if (response.status() === 429) this.retryNotBefore = Date.now() + retryAfterDelay(response.headers?.()['retry-after']);
    return new WhatsAppConnectionError('http_error', response.status());
  }
  private observePage(page: Page, client: any, generation: number): void {
    const response = (result: HTTPResponse) => {
      if (!this.current(generation)) return;
      if (result.request().isNavigationRequest() && result.frame() === page.mainFrame() && result.status() >= 400) {
        this.documentTrusted = false;
        this.fail(generation, this.httpFailure(result), 'page_navigation');
      }
    };
    const navigated = (frame: Frame) => {
      if (frame.parentFrame() !== null || !this.current(generation) || this.observedDocument !== generation) return;
      if (!this.allowed(frame.url())) { this.fail(generation, new WhatsAppConnectionError('unexpected_navigation'), 'page_navigation'); return; }
      if (frame.url().includes('post_logout=1') || client.lastLoggedOut) { this.retire(generation, 'disconnected', undefined, true); return; }
      if (this.documentSerial !== this.observedDocumentSerial) {
        this.previousStage = this.stage;
        this.stage = 'page_reload';
        this.updateDiagnostics();
        const failure = new WhatsAppConnectionError('page_context_lost');
        this.diagnosticId = this.diagnostics.failure(generation, this.stage, failure, failure.error_code);
        this.retire(generation, 'error', failure, true);
      }
    };
    const notice = (error: unknown) => {
      if (this.current(generation)) this.diagnostics.failure(generation, this.stage, error, connectionFailure(error).error_code, undefined, false);
    };
    let restoreBindings = () => {};
    if (typeof page.exposeFunction === 'function') {
      const originalExpose = page.exposeFunction;
      const expose = originalExpose.bind(page);
      restoreBindings = () => { page.exposeFunction = originalExpose; };
      page.exposeFunction = (async (name: string, callback: (...args: any[]) => any) => {
        if (!this.current(generation)) throw new WhatsAppConnectionError('page_closed');
        this.bindings.add(name);
        await expose(name, async (...args: any[]) => {
          if (!this.current(generation)) return;
          try { return await callback(...args); }
          catch (error) { this.fail(generation, error, 'sdk_callbacks'); }
        });
      }) as Page['exposeFunction'];
    }
    page.on('response', response);
    page.on('framenavigated', navigated);
    page.on('pageerror', notice);
    this.pageCleanup = () => { page.off('response', response); page.off('framenavigated', navigated); page.off('pageerror', notice); restoreBindings(); };
  }
  private async initialize(client: any, generation: number): Promise<void> {
    const ensureCurrent = () => { if (this.closing || this.generation !== generation) throw new WhatsAppConnectionError('page_closed'); };
    // Start a fresh document for each connection; the persistent partition is retained.
    // This also discards exposed bindings/scripts belonging to a retired SDK instance.
    await this.step(generation, 'view_setup', () => this.view.webContents.loadURL('about:blank')); ensureCurrent();
    const browser = await this.step(generation, 'cdp_connect', async () => {
      const transport = this.transport = new ViewTransport(this.view.webContents);
      const connected = await puppeteer.connect({ transport, defaultViewport: null });
      if (!this.current(generation)) { connected.disconnect(); throw new WhatsAppConnectionError('page_closed'); }
      this.browser = connected;
      connected.once('disconnected', () => this.fail(generation, new WhatsAppConnectionError('cdp_error')));
      return connected;
    });
    const page = this.page = await this.step(generation, 'cdp_connect', () => browser.newPage()); ensureCurrent();
    await this.step(generation, 'auth_setup', async () => {
      await client.authStrategy.beforeBrowserInitialized(); ensureCurrent();
      client.pupBrowser = browser; client.pupPage = page;
      await client.authStrategy.afterBrowserInitialized();
    });
    await this.step(generation, 'version_cache', () => client.initWebVersionCache());
    this.observePage(page, client, generation);
    this.phase(generation, 'navigation_timeout');
    await this.step(generation, 'page_navigation', async () => {
      const response = await page.goto('https://web.whatsapp.com/', { waitUntil: 'load', timeout: PHASE_TIMEOUT_MS, referer: 'https://whatsapp.com/' });
      ensureCurrent();
      if (!response || response.status() < 200) throw new WhatsAppConnectionError('invalid_response');
      if (response.status() >= 400) throw this.httpFailure(response);
      if (!this.allowed(page.url())) throw new WhatsAppConnectionError('unexpected_navigation');
      this.documentTrusted = true;
    });
    this.observedDocument = generation;
    this.observedDocumentSerial = this.documentSerial;
    this.phase(generation, 'sdk_timeout');
    await this.probePage();
    ensureCurrent();
    if (this.lastProbeSerial !== this.documentSerial) await this.probePage();
    ensureCurrent();
    // WhatsApp loads application modules after the document load event. Give
    // that bounded bootstrap its own budget, then bound SDK authentication.
    await this.step(generation, 'app_modules', async () => {
      const readiness = await page.waitForFunction(() => {
        const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
        if (/WhatsApp\s*(?:支持|supports|works with)\s*(?:Google\s*)?Chrome\s*\d+/i.test(text)) return 'browser_unsupported';
        return (window as any).Debug?.VERSION !== undefined ? 'ready' : false;
      }, { timeout: PHASE_TIMEOUT_MS, polling: 250 });
      try { if (await readiness.jsonValue() === 'browser_unsupported') throw new WhatsAppConnectionError('browser_unsupported'); }
      finally { await readiness.dispose(); }
    });
    if (this.status !== 'connecting') return;
    this.phase(generation, 'sdk_timeout');
    await this.step(generation, 'sdk_inject', () => client.inject()); ensureCurrent();
  }
  setVisible(visible: boolean, bounds?: Rectangle): void {
    if (visible && !bounds) throw new Error('Visible WhatsApp view requires bounds');
    if (visible && !this.visible) this.options.window.contentView.addChildView(this.view);
    if (visible && bounds) this.view.setBounds(bounds);
    if (!visible && this.visible) this.options.window.contentView.removeChildView(this.view);
    this.visible = visible;
  }
  getStatus(): WhatsAppHostInfo {
    return { status: this.status, account_id: this.account, connection_epoch: this.epoch,
      page_state: this.pageState, phase: this.stage,
      ...(this.previousStage ? { previous_phase: this.previousStage } : {}),
      ...(this.diagnosticId ? { diagnostic_id: this.diagnosticId } : {}),
      ...(this.failure ? { error_code: this.failure.error_code, ...(this.failure.http_status ? { http_status: this.failure.http_status } : {}) } : {}) };
  }
  async listChats(): Promise<Array<{ chat_id: string; name: string }>> {
    if (this.status !== 'ready') return [];
    return (await this.client.getChats()).filter((chat: any) => !chat.isGroup && direct(serialized(chat.id))).map((chat: any) => ({ chat_id: serialized(chat.id), name: String(chat.name || '') }));
  }
  async getActiveChat(): Promise<null> { return null; }
  readLedger(operationId: string) { return this.ledger.read(operationId); }
  async sendFrozen(intent: FrozenIntent): Promise<SendResult> {
    if (this.status !== 'ready' || intent.account_id !== this.account || intent.connection_epoch !== this.epoch || !direct(intent.chat_id) || !intent.operation_id || !intent.body) throw new Error('Invalid or stale WhatsApp send');
    if (!this.ledger.begin(intent)) throw new Error('WhatsApp operation already recorded; outcome must be reconciled');
    const epoch = intent.connection_epoch;
    this.pendingSends.set(epoch, (this.pendingSends.get(epoch) || 0) + 1);
    try {
      const message = await this.client.sendMessage(intent.chat_id, intent.body, { sendSeen: false });
      const id = serialized(message?.id);
      if (!id) throw new Error('WhatsApp send outcome unknown');
      const result = { message_id: id, ack: typeof message.ack === 'number' ? message.ack : 0 };
      this.ledger.finish(intent.operation_id, result);
      return result;
    } finally {
      const remaining = (this.pendingSends.get(epoch) || 1) - 1;
      if (remaining) this.pendingSends.set(epoch, remaining);
      else {
        this.pendingSends.delete(epoch);
        const ready = this.outgoing.filter(item => item.epoch === epoch);
        this.outgoing = this.outgoing.filter(item => item.epoch !== epoch);
        for (const item of ready) {
          if (this.status === 'ready' && item.account === this.account && item.epoch === this.epoch && !this.closing)
            this.options.onEvent({ type: 'messages', account_id: item.account, connection_epoch: item.epoch, messages: [item.message] });
        }
      }
    }
  }
  async lookupMessage(messageId: string): Promise<SendResult | null> {
    if (this.status !== 'ready' || !messageId) return null;
    const message = await this.client.getMessageById(messageId);
    return message && serialized(message.id) === messageId ? { message_id: messageId, ack: Number(message.ack ?? 0) } : null;
  }
  async close(logout = false): Promise<void> {
    if (this.closing) {
      await this.cleanupTask;
      if (this.cleanupFailed) throw new Error('WhatsApp 旧页面绑定清理未完成。');
      return;
    }
    this.closing = true;
    this.started = false;
    ++this.generation;
    this.clearPhase();
    if (this.nativeProbe) clearInterval(this.nativeProbe);
    this.nativeProbe = undefined;
    this.client?._injectAbort?.abort();
    if (this.visible) this.setVisible(false);
    try {
      if (logout && this.status === 'ready') {
        await this.client.pupPage.evaluate(() => (window as any).require('WAWebSocketModel').Socket.logout());
        await this.client.authStrategy.logout();
      }
    } finally {
      await this.cleanupTask;
      this.releaseConnection();
      await this.cleanupTask;
      this.failure = undefined;
      this.documentTrusted = false;
      this.pageState = 'closed';
      const contents = this.view.webContents;
      if (!contents.isDestroyed()) contents.close();
      if (!contents.isDestroyed()) {
        // A replaced document may reject binding cleanup. Destroying its view
        // removes every document and preload binding before a new one exists.
        await new Promise<void>((resolve, reject) => {
          const done = () => { clearTimeout(timer); resolve(); };
          const timer = setTimeout(() => { contents.removeListener('destroyed', done); reject(new Error('WhatsApp 旧页面绑定清理未完成。')); }, 5000);
          contents.once('destroyed', done);
          if (contents.isDestroyed()) { contents.removeListener('destroyed', done); done(); }
        });
      }
      if (contents.isDestroyed()) this.cleanupFailed = false;
      this.update('disconnected');
      if (this.cleanupFailed) throw new Error('WhatsApp 旧页面绑定清理未完成。');
    }
  }
}
