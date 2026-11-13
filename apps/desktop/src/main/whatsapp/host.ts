import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { WebContentsView, type BrowserWindow, type Rectangle } from 'electron';
import puppeteer from 'puppeteer';
import { SendLedger, type FrozenIntent, type SendResult } from './ledger';
import { ViewTransport } from './transport';

// whatsapp-web.js 1.34.8, Apache-2.0. The instance-specific initialize adapter
// below follows Client.initialize, replacing only browser creation with this view.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { Client } = require('whatsapp-web.js');

type AccountStatus = 'connecting' | 'qr' | 'authenticated' | 'ready' | 'disconnected' | 'error';
export type WhatsAppEvent =
  | { type: 'account'; account_id: string; connection_epoch: string; status: AccountStatus }
  | { type: 'messages'; account_id: string; connection_epoch: string; messages: WhatsAppMessage[] }
  | { type: 'ack'; message_id: string; ack: number; account_id: string; connection_epoch: string };
export interface WhatsAppMessage { id: string; chat_id: string; body: string; timestamp_ms: number; from_me: boolean; name: string; is_group: boolean; is_history: boolean; metadata_only?: boolean; ack?: number }
export interface WhatsAppHostOptions { window: BrowserWindow; partition: string; directory: string; onEvent: (event: WhatsAppEvent) => void }
const direct = (id: string): boolean => /@(c\.us|lid)$/.test(id);
const serialized = (id: any): string => typeof id === 'string' ? id : (id?._serialized || '');

export class WhatsAppHost {
  private readonly view: WebContentsView;
  private readonly ledger: SendLedger;
  private client: any;
  private generation = 0;
  private transport?: ViewTransport;
  private browser?: Awaited<ReturnType<typeof puppeteer.connect>>;
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
    this.view = new WebContentsView({ webPreferences: { partition: options.partition, nodeIntegration: false, contextIsolation: true, sandbox: true } });
    const wc = this.view.webContents;
    wc.setWindowOpenHandler(() => ({ action: 'deny' }));
    wc.on('will-navigate', (event, url) => { if (!this.allowed(url)) event.preventDefault(); });
    wc.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    wc.session.setPermissionCheckHandler(() => false);
    this.client = undefined;
  }
  private allowed(url: string): boolean {
    try { const parsed = new URL(url); return parsed.protocol === 'https:' && parsed.hostname === 'web.whatsapp.com'; } catch { return false; }
  }
  private update(status: AccountStatus): void {
    if (this.closing && status !== 'disconnected') return;
    this.status = status;
    this.options.onEvent({ type: 'account', account_id: this.account, connection_epoch: this.epoch, status });
  }
  private active(generation: number, account: string, epoch: string): boolean {
    return !this.closing && this.generation === generation && this.status === 'ready' && this.account === account && this.epoch === epoch;
  }
  private retire(generation: number, status: 'disconnected' | 'error'): void {
    if (this.generation !== generation || this.closing) return;
    ++this.generation;
    this.started = false;
    this.outgoing = this.outgoing.filter(item => item.epoch !== this.epoch);
    this.pendingSends.delete(this.epoch);
    this.client?._injectAbort?.abort();
    this.client?.removeAllListeners();
    this.browser?.disconnect();
    this.transport?.close();
    this.browser = undefined;
    this.transport = undefined;
    this.update(status);
  }
  private bindEvents(client: any, generation: number): void {
    const current = () => !this.closing && this.generation === generation;
    client.on('qr', () => { if (current()) this.update('qr'); });
    client.on('authenticated', () => { if (current()) this.update('authenticated'); });
    client.on('auth_failure', () => this.retire(generation, 'error'));
    client.on('disconnected', () => this.retire(generation, 'disconnected'));
    client.on('ready', () => {
      if (!current()) return;
      this.account = serialized(client.info?.wid);
      if (!this.account) { this.retire(generation, 'error'); return; }
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
    const generation = ++this.generation;
    const client = this.client = new Client({ userAgent: false, webVersionCache: { type: 'none' } });
    this.bindEvents(client, generation);
    this.update('connecting');
    void this.initialize(client, generation).catch(() => this.retire(generation, 'error'));
  }
  private async initialize(client: any, generation: number): Promise<void> {
    // Puppeteer's target initialization needs an initial document and frame.
    if (!this.view.webContents.getURL()) await this.view.webContents.loadURL('about:blank');
    if (this.closing || this.generation !== generation) return;
    const transport = this.transport = new ViewTransport(this.view.webContents);
    const browser = await puppeteer.connect({ transport, defaultViewport: null });
    if (this.closing || this.generation !== generation) { browser.disconnect(); return; }
    this.browser = browser;
    browser.once('disconnected', () => this.retire(generation, 'disconnected'));
    const page = await browser.newPage();
    if (this.closing || this.generation !== generation) return;
    const ensureCurrent = () => { if (this.closing || this.generation !== generation) throw new Error('Connection superseded'); };
    await client.authStrategy.beforeBrowserInitialized(); ensureCurrent();
    client.pupBrowser = browser; client.pupPage = page;
    await client.authStrategy.afterBrowserInitialized(); ensureCurrent();
    await client.initWebVersionCache(); ensureCurrent();
    await page.goto('https://web.whatsapp.com/', { waitUntil: 'load', timeout: 0, referer: 'https://whatsapp.com/' }); ensureCurrent();
    client._registerFramenavigatedHandler();
    await client.inject();
  }
  setVisible(visible: boolean, bounds?: Rectangle): void {
    if (visible && !bounds) throw new Error('Visible WhatsApp view requires bounds');
    if (visible && !this.visible) this.options.window.contentView.addChildView(this.view);
    if (visible && bounds) this.view.setBounds(bounds);
    if (!visible && this.visible) this.options.window.contentView.removeChildView(this.view);
    this.visible = visible;
  }
  getStatus(): { status: AccountStatus; account_id: string; connection_epoch: string } { return { status: this.status, account_id: this.account, connection_epoch: this.epoch }; }
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
    this.closing = true;
    ++this.generation;
    this.client?._injectAbort?.abort();
    if (this.visible) this.setVisible(false);
    try {
      if (logout && this.status === 'ready') {
        await this.client.pupPage.evaluate(() => (window as any).require('WAWebSocketModel').Socket.logout());
        await this.client.authStrategy.logout();
      }
      this.browser?.disconnect();
    } finally {
      this.transport?.close();
      if (!this.view.webContents.isDestroyed()) this.view.webContents.close();
      this.update('disconnected');
    }
  }
}
