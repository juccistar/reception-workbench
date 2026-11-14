import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { app, WebContentsView, type BrowserWindow, type Rectangle } from 'electron';
import puppeteer, { type Frame, type HTTPResponse, type Page } from 'puppeteer';
import type { WhatsAppHostInfo } from '../../shared/whatsapp';
import { SendLedger, type FrozenIntent, type SendResult } from './ledger';
import { ViewTransport } from './transport';
import { connectionFailure, whatsappUserAgent, WhatsAppConnectionError } from './connection';

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
export interface WhatsAppHostOptions { window: BrowserWindow; partition: string; directory: string; onEvent: (event: WhatsAppEvent) => void }
const direct = (id: string): boolean => /@(c\.us|lid)$/.test(id);
const serialized = (id: any): string => typeof id === 'string' ? id : (id?._serialized || '');
const PHASE_TIMEOUT_MS = 60_000;

export class WhatsAppHost {
  private readonly view: WebContentsView;
  private readonly ledger: SendLedger;
  private client: any;
  private generation = 0;
  private observedDocument = 0;
  private transport?: ViewTransport;
  private browser?: Awaited<ReturnType<typeof puppeteer.connect>>;
  private phaseTimer?: ReturnType<typeof setTimeout>;
  private pageCleanup?: () => void;
  private failure?: WhatsAppConnectionError;
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
    wc.on('render-process-gone', () => this.retire(this.generation, 'error', new WhatsAppConnectionError('page_closed')));
    wc.on('destroyed', () => this.retire(this.generation, 'error', new WhatsAppConnectionError('page_closed')));
    wc.on('did-fail-load', (_event, code, description, _url, mainFrame) => {
      if (mainFrame && code !== -3 && code !== -379 && this.started) this.retire(this.generation, 'error', connectionFailure(new Error(description)));
    });
    wc.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    wc.session.setPermissionCheckHandler(() => false);
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
  private clearPhase(): void {
    if (this.phaseTimer) clearTimeout(this.phaseTimer);
    this.phaseTimer = undefined;
  }
  private phase(generation: number, code: 'navigation_timeout' | 'sdk_timeout' | 'ready_timeout'): void {
    this.clearPhase();
    this.phaseTimer = setTimeout(() => this.retire(generation, 'error', new WhatsAppConnectionError(code)), PHASE_TIMEOUT_MS);
  }
  private releaseConnection(): void {
    this.clearPhase();
    const cleanup = this.pageCleanup;
    const browser = this.browser, transport = this.transport;
    this.pageCleanup = undefined;
    this.browser = undefined;
    this.transport = undefined;
    // Cleanup failure must not leave another callback/transport controlling this view.
    try { this.client?._injectAbort?.abort(); } catch { /* best effort */ }
    try { this.client?.removeAllListeners(); } catch { /* best effort */ }
    try { cleanup?.(); } catch { /* best effort */ }
    try { browser?.disconnect(); } catch { /* best effort */ }
    try { transport?.close(); } catch { /* best effort */ }
  }
  private retire(generation: number, status: 'disconnected' | 'error', failure?: WhatsAppConnectionError): void {
    if (this.generation !== generation || this.closing || !this.started) return;
    ++this.generation;
    this.started = false;
    this.failure = failure;
    this.outgoing = this.outgoing.filter(item => item.epoch !== this.epoch);
    this.pendingSends.delete(this.epoch);
    this.releaseConnection();
    if (!this.view?.webContents.isDestroyed()) this.view?.webContents.stop();
    this.update(status);
  }
  private bindEvents(client: any, generation: number): void {
    const current = () => !this.closing && this.generation === generation;
    client.on('qr', () => {
      if (current() && this.status !== 'authenticated' && this.status !== 'ready') { this.clearPhase(); this.update('qr'); }
    });
    client.on('authenticated', () => {
      if (current() && this.status !== 'ready') { this.phase(generation, 'ready_timeout'); this.update('authenticated'); }
    });
    client.on('auth_failure', () => this.retire(generation, 'error', new WhatsAppConnectionError('auth_failed')));
    client.on('disconnected', () => this.retire(generation, 'disconnected'));
    client.on('ready', () => {
      if (!current() || this.status === 'ready') return;
      this.account = serialized(client.info?.wid);
      if (!this.account) { this.retire(generation, 'error', new WhatsAppConnectionError('auth_failed')); return; }
      this.clearPhase();
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
    this.started = true;
    this.failure = undefined;
    const generation = ++this.generation;
    const client = this.client = new Client({ userAgent: false, authTimeoutMs: PHASE_TIMEOUT_MS, webVersionCache: { type: 'none' } });
    this.bindEvents(client, generation);
    this.phase(generation, 'sdk_timeout');
    this.update('connecting');
    void this.initialize(client, generation).catch(error => this.retire(generation, 'error', connectionFailure(error)));
  }
  private observePage(page: Page, client: any, generation: number): void {
    const fail = (error: unknown) => this.retire(generation, 'error', connectionFailure(error));
    const response = (result: HTTPResponse) => {
      if (result.request().isNavigationRequest() && result.frame() === page.mainFrame() && result.status() >= 400)
        fail(new WhatsAppConnectionError('http_error', result.status()));
    };
    const navigated = (frame: Frame) => {
      if (frame.parentFrame() !== null || this.closing || this.generation !== generation || this.observedDocument !== generation) return;
      void (async () => {
        if (!this.allowed(frame.url())) throw new WhatsAppConnectionError('unexpected_navigation');
        if (frame.url().includes('post_logout=1') || client.lastLoggedOut) { this.retire(generation, 'disconnected'); return; }
        const injected = await page.evaluate(() => typeof (window as any).WWebJS !== 'undefined');
        if (injected || this.closing || this.generation !== generation) return;
        this.phase(generation, 'sdk_timeout');
        this.update('connecting');
        await client.inject();
      })().catch(fail);
    };
    page.on('response', response);
    page.on('framenavigated', navigated);
    this.pageCleanup = () => { page.off('response', response); page.off('framenavigated', navigated); };
  }
  private async initialize(client: any, generation: number): Promise<void> {
    const ensureCurrent = () => { if (this.closing || this.generation !== generation) throw new WhatsAppConnectionError('page_closed'); };
    // Start a fresh document for each connection; the persistent partition is retained.
    // This also discards exposed bindings/scripts belonging to a retired SDK instance.
    await this.view.webContents.loadURL('about:blank'); ensureCurrent();
    const transport = this.transport = new ViewTransport(this.view.webContents);
    const browser = await puppeteer.connect({ transport, defaultViewport: null });
    if (this.closing || this.generation !== generation) { browser.disconnect(); return; }
    this.browser = browser;
    browser.once('disconnected', () => this.retire(generation, 'disconnected'));
    const page = await browser.newPage(); ensureCurrent();
    await client.authStrategy.beforeBrowserInitialized(); ensureCurrent();
    client.pupBrowser = browser; client.pupPage = page;
    await client.authStrategy.afterBrowserInitialized(); ensureCurrent();
    await client.initWebVersionCache(); ensureCurrent();
    this.observePage(page, client, generation);
    this.phase(generation, 'navigation_timeout');
    const response = await page.goto('https://web.whatsapp.com/', { waitUntil: 'load', timeout: PHASE_TIMEOUT_MS, referer: 'https://whatsapp.com/' })
      .catch(error => { throw connectionFailure(error, 'navigation_timeout'); }); ensureCurrent();
    if (!response || response.status() < 200) throw new WhatsAppConnectionError('invalid_response');
    if (response.status() >= 400) throw new WhatsAppConnectionError('http_error', response.status());
    if (!this.allowed(page.url())) throw new WhatsAppConnectionError('unexpected_navigation');
    this.observedDocument = generation;
    this.phase(generation, 'sdk_timeout');
    // WhatsApp loads application modules after the document load event. Give
    // that bounded bootstrap its own budget, then bound SDK authentication.
    const readiness = await page.waitForFunction(() => {
      const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
      if (/WhatsApp\s*(?:支持|supports|works with)\s*(?:Google\s*)?Chrome\s*\d+/i.test(text)) return 'browser_unsupported';
      return (window as any).Debug?.VERSION !== undefined ? 'ready' : false;
    }, { timeout: PHASE_TIMEOUT_MS, polling: 250 }); ensureCurrent();
    const ready = await readiness.jsonValue();
    await readiness.dispose(); ensureCurrent();
    if (ready === 'browser_unsupported') throw new WhatsAppConnectionError('browser_unsupported');
    if (this.status !== 'connecting') return;
    this.phase(generation, 'sdk_timeout');
    await client.inject(); ensureCurrent();
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
    if (this.closing) return;
    this.closing = true;
    this.started = false;
    ++this.generation;
    this.clearPhase();
    this.client?._injectAbort?.abort();
    if (this.visible) this.setVisible(false);
    try {
      if (logout && this.status === 'ready') {
        await this.client.pupPage.evaluate(() => (window as any).require('WAWebSocketModel').Socket.logout());
        await this.client.authStrategy.logout();
      }
    } finally {
      this.releaseConnection();
      this.failure = undefined;
      if (!this.view.webContents.isDestroyed()) this.view.webContents.close();
      this.update('disconnected');
    }
  }
}
