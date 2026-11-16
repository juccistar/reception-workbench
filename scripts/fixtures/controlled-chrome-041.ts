/** Isolated GUI fixture: real Chrome and native extension, synthetic SDK and no network. */
import { EventEmitter } from 'node:events';
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { ChromeWhatsAppHost as ProductionHost } from '../../apps/desktop/src/main/whatsapp/chrome-host';
export { resolveGoogleChrome } from '../../apps/desktop/src/main/whatsapp/chrome-host';
const puppeteer = createRequire(process.env.HARNESS_SDK_ENTRY!)('puppeteer');
const clients = new Map<string, ControlledClient>();
const diagnostics = new Map<string, Record<string, unknown>>();
const keyFor = (directory: string) => directory.split(/[\\/]/).at(-1)!;
class ControlledClient extends EventEmitter {
  pupBrowser: any;
  pupPage: any;
  info: any;
  readonly workspace: string;
  constructor(private readonly options: any) {
    super();
    this.workspace = options.puppeteer.userDataDir.split(/[\\/]/).at(-1);
    const digits = BigInt('0x' + createHash('sha256').update(this.workspace).digest('hex').slice(0, 20)).toString();
    this.info = { wid: { _serialized: `${digits}@c.us` } };
    clients.set(this.workspace, this);
  }
  async initialize() {
    this.pupBrowser = await puppeteer.launch(this.options.puppeteer);
    const install = this.pupBrowser.installExtension.bind(this.pupBrowser);
    this.pupBrowser.installExtension = async (directory: string) => {
      const debug = diagnostics.get(this.workspace)!;
      try { const id = await install(directory); debug.installed_id = id; return id; }
      catch (error) { debug.install_error = String(error); throw error; }
    };
    this.pupPage = (await this.pupBrowser.pages())[0];
    await this.pupPage.setRequestInterception(true);
    this.pupPage.on('request', (request: any) => {
      const url = new URL(request.url());
      if (!['http:', 'https:'].includes(url.protocol)) { void request.continue(); return; }
      if (url.hostname !== 'web.whatsapp.com') { void request.abort(); return; }
      void request.respond({ status: 200, contentType: 'text/html', body: '<!doctype html><meta charset="utf-8"><title>隔离聊天验证</title><style>body{font:16px sans-serif;background:#eef2f1;padding:50px;color:#34433d}article{background:white;border-radius:12px;padding:28px;max-width:650px}</style><article><h1>合成客户甲</h1><p>这是隔离验证页面，不连接 WhatsApp，也不发送消息。</p></article>' });
    });
    await this.pupPage.goto('https://web.whatsapp.com/', { waitUntil: 'domcontentloaded' });
    await this.pupPage.evaluate(() => {
      const rows = [{ id: { _serialized: '10000001@c.us' }, name: '合成客户甲' }, { id: { _serialized: '10000002@c.us' }, name: '合成客户乙' }];
      (window as any).__fixtureActiveChat = rows[0].id._serialized;
      (window as any).require = (name: string) => {
        if (name === 'WAWebCollections') return { Chat: { getModelsArray: () => rows.map(row => ({ ...row, active: row.id._serialized === (window as any).__fixtureActiveChat })) } };
        if (name === 'WAWebNewsletterCollection') return { default: { getModelsArray: () => [] } };
        throw new Error('Unknown fixture module');
      };
    });
    this.emit('ready');
  }
  async getChats() { return [await this.getChatById('10000001@c.us'), await this.getChatById('10000002@c.us')]; }
  async getChatById(id: string) { return { id: { _serialized: id }, isGroup: false, name: id === '10000001@c.us' ? '合成客户甲' : '合成客户乙', fetchMessages: async () => [] }; }
  async sendMessage() { throw new Error('Fixture never sends'); }
  async getMessageById() { return null; }
}
export class ChromeWhatsAppHost extends ProductionHost {
  constructor(options: any) {
    const debug: Record<string, unknown> = {};
    diagnostics.set(keyFor(options.profileDirectory), debug);
    super({ ...options, loadSdk: async () => ({ Client: ControlledClient }),
      authorizeAccount: async (id: string) => { try { await options.authorizeAccount?.(id); debug.authorized = true; } catch (error) { debug.authorization_error = String(error); throw error; } },
      onBrowserReady: async (...args: any[]) => { debug.callback_started = true; try { await options.onBrowserReady?.(...args); debug.callback_completed = true; } catch (error) { debug.callback_error = String(error); throw error; } },
      onOpen: async () => { debug.open_requested = true; try { await options.onOpen?.(); debug.open_completed = true; } catch (error) { debug.open_error = String(error); throw error; } },
    });
  }
}
const clientFor = (workspace: string) => {
  const client = clients.get(workspace.replaceAll('-', ''));
  if (!client) throw new Error('Synthetic browser missing');
  return client;
};
const panelFor = async (workspace: string) => {
  const target = clientFor(workspace).pupBrowser.targets().find((target: any) => /^chrome-extension:\/\/[a-p]{32}\/panel\.html$/.test(target.url()));
  if (!target) throw new Error('Native panel target missing');
  return {
    async evaluate(fn: (...args: any[]) => any, value?: unknown) {
      const session = await target.createCDPSession();
      try {
        const result = await session.send('Runtime.evaluate', { expression: `(${String(fn)})(${JSON.stringify(value)})`, returnByValue: true, awaitPromise: true });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
        return result.result.value;
      } finally { await session.detach(); }
    },
    async screenshot({ path: file }: { path: string }) {
      const session = await target.createCDPSession();
      try { const result = await session.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await writeFile(file, Buffer.from(result.data, 'base64')); }
      finally { await session.detach(); }
    },
  };
};
(globalThis as any).chromeFixture = {
  diagnostics() { return [...clients].map(([id, client]) => ({ workspace: id, ...diagnostics.get(id), targets: client.pupBrowser?.targets().map((target: any) => ({ type: target.type(), url: target.url() })) })); },
  info(workspace: string) { const browser = clientFor(workspace).pupBrowser; const child = browser?.process(); return { pid: child?.pid, exited: !!child && (child.exitCode !== null || child.signalCode !== null), private_pipe: child?.spawnargs.includes('--remote-debugging-pipe'), tcp_port: child?.spawnargs.some((value: string) => value.startsWith('--remote-debugging-port')) }; },
  async panel(workspace: string) { const page = await panelFor(workspace); return page.evaluate(() => ({ visible: !(document.getElementById('content') as HTMLElement).hidden, name: document.getElementById('name')!.textContent, summary: document.getElementById('summary')!.textContent, requirements: document.getElementById('requirements')!.textContent, status: document.getElementById('reception')!.textContent, selected: document.getElementById('selected')!.textContent, results: [...document.querySelectorAll('#results button')].map(node => node.textContent), merchants: [...document.querySelectorAll('#merchant option')].map(node => ({ id: (node as HTMLOptionElement).value, disabled: (node as HTMLOptionElement).disabled })), error: document.getElementById('error')!.textContent })); },
  async selectChat(workspace: string, id: string) { await clientFor(workspace).pupPage.evaluate((id: string) => { (window as any).__fixtureActiveChat = id; }, id); },
  async interact(workspace: string, action: string, value?: string) {
    const page = await panelFor(workspace);
    await page.evaluate(({ action, value }: any) => {
      if (action === 'merchant') { const node = document.getElementById('merchant') as HTMLSelectElement; node.value = value; node.dispatchEvent(new Event('change')); }
      else if (action === 'query') { const node = document.getElementById('query') as HTMLInputElement; node.value = value; node.dispatchEvent(new Event('input')); }
      else if (action === 'result') (document.querySelector('#results button') as HTMLButtonElement)?.click();
      else (document.getElementById(action) as HTMLButtonElement)?.click();
    }, { action, value });
  },
  async screenshot(workspace: string, file: string) { await (await panelFor(workspace)).screenshot({ path: file }); },
  async websiteBinding(workspace: string) { return clientFor(workspace).pupPage.evaluate(() => typeof (window as any).__receptionCustomerPanel); },
};
