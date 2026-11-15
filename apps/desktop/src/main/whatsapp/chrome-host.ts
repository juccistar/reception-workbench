import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { lstat } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { resolveWindowsUserLocalAppData } from '../windows-user-directories';
import type { WhatsAppTransportHost } from '../whatsapp-coordinator';
import type { WhatsAppHostInfo } from '../../shared/whatsapp';
import type { WhatsAppEvent, WhatsAppMessage } from './host';
import { SendLedger, type FrozenIntent, type SendResult } from './ledger';

const direct = (id: string) => /@(c\.us|lid)$/.test(id);
const serial = (id: any): string => typeof id === 'string' ? id : (id?._serialized || '');
const execFileAsync = promisify(execFile);

type Sdk = { Client: new (options: any) => any };
export interface ChromeWhatsAppHostOptions {
  profileDirectory: string;
  directory: string;
  diagnosticsDirectory?: string;
  applicationVersion?: string;
  onEvent: (event: WhatsAppEvent) => void;
  authorizeAccount?: (accountId: string) => Promise<void>;
  /** Test seams; production always resolves and validates the installed Chrome. */
  resolveExecutable?: () => Promise<string>;
  loadSdk?: () => Promise<Sdk>;
}

async function noLinks(file: string): Promise<void> {
  const resolved = path.resolve(file);
  let part = path.parse(resolved).root;
  for (const component of resolved.slice(part.length).split(path.sep).filter(Boolean)) {
    part = path.join(part, component);
    const entry = await lstat(part);
    if (entry.isSymbolicLink()) throw new Error('chrome_path_invalid');
  }
  if (!(await lstat(resolved)).isFile()) throw new Error('chrome_path_invalid');
}

/** Accept only standard installed locations and Windows version metadata. */
export async function resolveGoogleChrome(): Promise<string> {
  if (process.platform !== 'win32') throw new Error('chrome_unavailable');
  const local = await resolveWindowsUserLocalAppData();
  const drive = path.parse(process.env.SystemRoot || 'C:\\Windows').root;
  const candidates = [path.join(local, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(drive, 'Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(drive, 'Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe')];
  for (const candidate of candidates) {
    try {
      await noLinks(candidate);
      const literal = candidate.replaceAll("'", "''");
      const script = `$v=(Get-Item -LiteralPath '${literal}').VersionInfo; if ($v.CompanyName -eq 'Google LLC' -and $v.ProductName -eq 'Google Chrome' -and $v.OriginalFilename -eq 'chrome.exe') { 'ok' }`;
      const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
      const { stdout } = await execFileAsync(powershell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], { windowsHide: true, timeout: 10000 });
      if (stdout.trim() === 'ok') return candidate;
    } catch { /* Try the next standard location. */ }
  }
  throw new Error('chrome_unavailable');
}

export class ChromeWhatsAppHost implements WhatsAppTransportHost {
  private readonly ledger: SendLedger;
  private client: any;
  private generation = 0;
  private status: WhatsAppHostInfo = { status: 'disconnected', account_id: '', connection_epoch: '', page_state: 'closed' };
  private initializing?: Promise<void>;
  private connecting?: Promise<void>;
  private closing?: Promise<void>;
  private closed = false;
  private tasks = new Set<Promise<unknown>>();
  private pending = new Map<string, number>();
  private outgoing: Array<{ account: string; epoch: string; message: WhatsAppMessage }> = [];
  constructor(private readonly options: ChromeWhatsAppHostOptions) {
    this.ledger = new SendLedger(path.join(options.directory, 'send-ledger'));
  }
  private update(status: string, extra: Partial<WhatsAppHostInfo> = {}): void {
    this.status = { ...this.status, ...extra, status };
    this.options.onEvent({ type: 'account', ...this.status, status: status as any });
  }
  private current(generation: number): boolean { return !this.closed && this.generation === generation; }
  private active(generation: number, account: string, epoch: string): boolean {
    return this.current(generation) && this.status.status === 'ready' && !!account && !!epoch && this.status.account_id === account && this.status.connection_epoch === epoch;
  }
  private track<T>(work: Promise<T>): Promise<T> {
    this.tasks.add(work);
    void work.then(() => this.tasks.delete(work), () => this.tasks.delete(work));
    return work;
  }
  private retire(generation: number, status: 'error' | 'disconnected', closed = false, errorCode: WhatsAppHostInfo['error_code'] = 'sdk_error'): void {
    if (!this.current(generation)) return;
    ++this.generation;
    this.outgoing = [];
    this.update(status, { account_id: '', connection_epoch: '', page_state: closed ? 'closed' : 'available', ...(status === 'error' ? { error_code: errorCode } : { error_code: undefined }) });
  }
  private observe(client: any, generation: number): void {
    const current = () => this.current(generation);
    client.on('qr', () => { if (current() && this.status.status !== 'ready') this.update('qr', { phase: 'qr_wait', page_state: 'available' }); });
    client.on('authenticated', () => { if (current() && this.status.status !== 'ready') this.update('authenticated', { phase: 'authenticated' }); });
    client.on('auth_failure', () => this.retire(generation, 'error'));
    client.on('disconnected', () => this.retire(generation, 'disconnected'));
    client.on('ready', () => {
      if (!current() || this.status.status === 'ready') return;
      const account = serial(client.info?.wid);
      if (!account || (typeof client.pupPage?.url === 'function' && !this.allowedPage(client.pupPage.url()))) { this.retire(generation, 'error'); return; }
      void this.track((async () => {
        try { await this.options.authorizeAccount?.(account); }
        catch { this.retire(generation, 'error', false, 'auth_failed'); return; }
        if (!current()) return;
        const epoch = randomUUID();
        this.update('ready', { account_id: account, connection_epoch: epoch, page_state: 'available', phase: 'ready', error_code: undefined });
        await this.history(client, generation, account, epoch);
      })()).catch(() => {});
    });
    client.on('message_create', (message: any) => this.emitMessage(message, false, '', generation, this.status.account_id, this.status.connection_epoch));
    client.on('message_ack', (message: any, ack: number) => {
      const account = this.status.account_id, epoch = this.status.connection_epoch, id = serial(message?.id);
      if (id && direct(serial(message?.fromMe ? message?.to : message?.from)) && this.active(generation, account, epoch)) this.options.onEvent({ type: 'ack', message_id: id, ack, account_id: account, connection_epoch: epoch });
    });
  }
  private emitMessage(message: any, history: boolean, name: string, generation: number, account: string, epoch: string): void {
    const chat = serial(message?.fromMe ? message?.to : message?.from), id = serial(message?.id);
    if (!direct(chat) || !id || !this.active(generation, account, epoch)) return;
    const item: WhatsAppMessage = { id, chat_id: chat, body: typeof message.body === 'string' ? message.body : '', timestamp_ms: Number(message.timestamp || 0) * 1000, from_me: !!message.fromMe, name, is_group: false, is_history: history, ack: typeof message.ack === 'number' ? message.ack : undefined, has_image: message.type === 'image' };
    if (item.from_me && !history && this.pending.get(epoch)) this.outgoing.push({ account, epoch, message: item });
    else this.options.onEvent({ type: 'messages', account_id: account, connection_epoch: epoch, messages: [item] });
  }
  private async history(client: any, generation: number, account: string, epoch: string): Promise<void> {
    const chats = await client.getChats();
    for (const chat of chats) {
      if (!this.active(generation, account, epoch)) return;
      const id = serial(chat.id);
      if (!direct(id) || chat.isGroup) continue;
      const name = String(chat.name || '');
      this.options.onEvent({ type: 'messages', account_id: account, connection_epoch: epoch, messages: [{ id: `metadata:${id}`, chat_id: id, body: '', timestamp_ms: 0, from_me: false, name, is_group: false, is_history: true, metadata_only: true }] });
      try { const messages = await chat.fetchMessages({ limit: 50 }); for (const msg of messages) this.emitMessage(msg, true, name, generation, account, epoch); } catch { /* Later connections retry. */ }
    }
  }
  private allowedPage(url: string): boolean {
    try { const parsed = new URL(url); return parsed.protocol === 'https:' && parsed.hostname === 'web.whatsapp.com'; } catch { return false; }
  }
  private watchBrowser(client: any, generation: number): void {
    const browser = client.pupBrowser, page = client.pupPage;
    if (browser && !(browser as any).__arwWatched) {
      (browser as any).__arwWatched = true;
      browser.once('disconnected', () => this.retire(generation, 'disconnected', true));
    }
    if (page && !(page as any).__arwWatched) {
      (page as any).__arwWatched = true;
      page.once('close', () => {
        if (this.current(generation)) this.retire(generation, 'disconnected', true);
        else if (!this.closed && this.client === client && this.status.page_state !== 'closed') this.update(this.status.status, { account_id: '', connection_epoch: '', page_state: 'closed' });
      });
      page.on('framenavigated', (frame: any) => {
        if (!this.current(generation) || frame.parentFrame() !== null) return;
        const url = frame.url();
        if (url !== 'about:blank' && !this.allowedPage(url)) this.retire(generation, 'error');
      });
    }
  }
  async connect(): Promise<void> {
    if (this.connecting) return this.connecting;
    const task = this.startConnection();
    this.connecting = task;
    try { await task; } finally { if (this.connecting === task) this.connecting = undefined; }
  }
  private async startConnection(): Promise<void> {
    if (this.closed) throw new Error('WhatsApp host is closed');
    if (['connecting', 'qr', 'authenticated', 'ready'].includes(this.status.status)) return;
    if (this.client) await this.release();
    if (this.closed) throw new Error('WhatsApp host is closed');
    const generation = ++this.generation;
    this.update('connecting', { account_id: '', connection_epoch: '', error_code: undefined, page_state: 'loading', phase: 'auth_setup' });
    const task = (async () => {
      try {
        const executablePath = await (this.options.resolveExecutable ?? resolveGoogleChrome)();
        const sdk = await (this.options.loadSdk ?? (async () => ({ Client: require('whatsapp-web.js').Client })))();
        if (!this.current(generation)) return;
        const client = this.client = new sdk.Client({ userAgent: false, puppeteer: { executablePath, headless: false, pipe: true, userDataDir: this.options.profileDirectory, defaultViewport: null }, webVersionCache: { type: 'none' } });
        this.observe(client, generation);
        // Observe the assignments themselves so a close cannot slip through a polling gap.
        for (const key of ['pupBrowser', 'pupPage']) {
          let value = client[key];
          Object.defineProperty(client, key, { configurable: true, enumerable: true, get: () => value, set: next => { value = next; this.watchBrowser(client, generation); } });
        }
        this.watchBrowser(client, generation);
        // A close can race the SDK launching its browser.
        const watch = setInterval(() => {
          this.watchBrowser(client, generation);
          if (this.closed && client.pupBrowser && !client.__arwCloseStarted) {
            client.__arwCloseStarted = true;
            void client.pupBrowser.close().catch(() => {});
          }
        }, 25);
        watch.unref?.();
        try { await client.initialize(); } finally { clearInterval(watch); this.watchBrowser(client, generation); }
      } catch (error) { this.retire(generation, 'error', !this.client?.pupPage || this.client.pupPage.isClosed(), error instanceof Error && error.message === 'chrome_unavailable' ? 'browser_unsupported' : 'sdk_error'); }
    })();
    this.initializing = task;
    void task.finally(() => { if (this.initializing === task) this.initializing = undefined; });
  }
  getStatus(): WhatsAppHostInfo { return { ...this.status }; }
  setVisible(visible: boolean): void { if (visible) void this.open().catch(() => {}); }
  async open(): Promise<void> {
    const page = this.client?.pupPage;
    if (!page || page.isClosed()) throw new Error('WhatsApp page unavailable');
    await page.bringToFront();
  }
  async listChats(): Promise<Array<{ chat_id: string; name: string }>> {
    const generation = this.generation, account = this.status.account_id, epoch = this.status.connection_epoch;
    if (!this.active(generation, account, epoch)) return [];
    return this.track((async () => {
      const chats = await this.client.getChats();
      if (!this.active(generation, account, epoch)) throw new Error('WhatsApp identity changed');
      return chats.filter((chat: any) => !chat.isGroup && direct(serial(chat.id))).map((chat: any) => ({ chat_id: serial(chat.id), name: String(chat.name || '') }));
    })());
  }
  readLedger(operationId: string) { return this.ledger.read(operationId); }
  async sendFrozen(intent: FrozenIntent): Promise<SendResult> {
    const generation = this.generation, account = intent.account_id, epoch = intent.connection_epoch;
    if (!this.active(generation, account, epoch) || !direct(intent.chat_id) || !intent.operation_id || !intent.body) throw new Error('Invalid or stale WhatsApp send');
    if (!this.ledger.begin(intent)) throw new Error('WhatsApp operation already recorded; outcome must be reconciled');
    this.pending.set(epoch, (this.pending.get(epoch) || 0) + 1);
    return this.track((async () => {
      try {
        if (!this.active(generation, account, epoch)) throw new Error('WhatsApp identity changed');
        const message = await this.client.sendMessage(intent.chat_id, intent.body, { sendSeen: false });
        const id = serial(message?.id);
        if (!id) throw new Error('WhatsApp send outcome unknown');
        const result = { message_id: id, ack: typeof message.ack === 'number' ? message.ack : 0 };
        this.ledger.finish(intent.operation_id, result);
        return result;
      } finally {
        const remaining = (this.pending.get(epoch) || 1) - 1;
        if (remaining) this.pending.set(epoch, remaining);
        else {
          this.pending.delete(epoch);
          const outgoing = this.outgoing.filter(item => item.epoch === epoch);
          this.outgoing = this.outgoing.filter(item => item.epoch !== epoch);
          for (const item of outgoing) if (this.active(generation, item.account, item.epoch)) this.options.onEvent({ type: 'messages', account_id: item.account, connection_epoch: item.epoch, messages: [item.message] });
        }
      }
    })());
  }
  async lookupMessage(messageId: string): Promise<SendResult | null> {
    const generation = this.generation, account = this.status.account_id, epoch = this.status.connection_epoch;
    if (!messageId || !this.active(generation, account, epoch)) return null;
    return this.track((async () => {
      const message = await this.client.getMessageById(messageId);
      if (!this.active(generation, account, epoch)) throw new Error('WhatsApp identity changed');
      return message && serial(message.id) === messageId ? { message_id: messageId, ack: Number(message.ack ?? 0) } : null;
    })());
  }
  async downloadImage(account: string, epoch: string, chatId: string, messageId: string, consume: (mime: string, size: number, chunks: AsyncIterable<Buffer>) => Promise<void>): Promise<void> {
    const generation = this.generation, deadline = Date.now() + 60000;
    const check = () => { if (!this.active(generation, account, epoch) || Date.now() >= deadline) throw new Error('WhatsApp image source changed'); };
    const bounded = async <T>(promise: Promise<T>): Promise<T> => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try { return await Promise.race([promise, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('WhatsApp image source changed')), Math.max(0, deadline - Date.now())); timer.unref?.(); })]); }
      finally { if (timer) clearTimeout(timer); }
    };
    check();
    return this.track((async () => {
      const client = this.client;
      const message = await bounded<any>(client.getMessageById(messageId)); check();
      if (!message || serial(message.id) !== messageId || serial(message.fromMe ? message.to : message.from) !== chatId || !direct(chatId) || message.type !== 'image' || !message.hasMedia) throw new Error('WhatsApp image unavailable');
      let abandoned = false;
      const pendingHandle = client.pupPage.evaluateHandle(async (id: string) => (await (window as any).WWebJS.resolveMediaBlob(id))?.blob ?? null, messageId);
      void pendingHandle.then((late: any) => { if (abandoned) void late.dispose().catch(() => {}); }, () => {});
      let handle: any;
      try { handle = await bounded(pendingHandle); } catch (error) { abandoned = true; throw error; }
      try {
        check();
        const metadata = await bounded<{ size: number; mime: string }>(handle.evaluate((blob: Blob | null, id: string) => ({ size: blob?.size ?? 0, mime: (window as any).require('WAWebCollections').Msg.get(id)?.mimetype || 'application/octet-stream' }), messageId)); check();
        if (!Number.isSafeInteger(metadata.size) || metadata.size <= 0 || metadata.size > 6 * 1024 * 1024) throw new Error('WhatsApp image too large');
        async function* chunks() {
          for (let offset = 0; offset < metadata.size; offset += 256 * 1024) {
            check();
            const encoded = await bounded<string>(handle.evaluate(async (blob: Blob, start: number, end: number) => (window as any).WWebJS.arrayBufferToBase64Async(await blob.slice(start, end).arrayBuffer()), offset, Math.min(offset + 256 * 1024, metadata.size)));
            check(); yield Buffer.from(encoded, 'base64');
          }
        }
        await bounded(consume(metadata.mime, metadata.size, chunks())); check();
      } finally { await bounded(handle.dispose()).catch(() => {}); }
    })());
  }
  private async release(): Promise<void> {
    const client = this.client;
    if (!client) { await this.initializing; return; }
    client.removeAllListeners();
    try { client._injectAbort?.abort(); } catch { /* best effort */ }
    if (!client.pupBrowser) await this.initializing;
    const browser = client.pupBrowser;
    if (browser) {
      const process = browser.process?.();
      const exited = process && process.exitCode === null && process.signalCode === null ? new Promise<void>(resolve => process.once('exit', () => resolve())) : Promise.resolve();
      await browser.close();
      await exited;
    }
    await this.initializing;
    if (this.client === client) this.client = undefined;
  }
  async close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true; ++this.generation;
    this.update('disconnected', { account_id: '', connection_epoch: '', page_state: 'closed' });
    this.closing = (async () => {
      await Promise.allSettled([...this.tasks]);
      await this.connecting?.catch(() => {});
      await this.release();
    })();
    return this.closing;
  }
}
