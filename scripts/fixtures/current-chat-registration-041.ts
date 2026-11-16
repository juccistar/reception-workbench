/** Exercise the real side panel before/after isolated backend contact metadata arrives. */
import { ChromeWhatsAppHost as ControlledHost } from './controlled-chrome-041';
export { resolveGoogleChrome } from './controlled-chrome-041';
const missingId = '10000003@c.us';
const missingName = '合成待同步客户';
const hosts = new Map<string, { host: any; options: any }>();
const workspaceKey = (value: string) => value.replaceAll('-', '');
export class ChromeWhatsAppHost extends ControlledHost {
  constructor(options: any) {
    let self: ChromeWhatsAppHost;
    super({ ...options, onBrowserReady: async (browser: any, page: any, extensionId: string) => {
      await page.waitForFunction(() => typeof (window as any).require === 'function' && typeof (window as any).require('WAWebCollections')?.Chat?.getModelsArray === 'function', { timeout: 10000 });
      const client = (self as any).client;
      const getChat = client.getChatById.bind(client);
      client.getChatById = async (id: string) => id === missingId
        ? { id: { _serialized: id }, isGroup: false, name: missingName, fetchMessages: async () => [] }
        : getChat(id);
      await page.evaluate(({ id, name }: { id: string; name: string }) => {
        const require = (window as any).require;
        const collections = require('WAWebCollections');
        const readChats = collections.Chat.getModelsArray;
        const chatStore = { ...collections.Chat, getModelsArray: () => [...readChats.call(collections.Chat), { id: { _serialized: id }, name, active: (window as any).__fixtureActiveChat === id }] };
        (window as any).require = (module: string) => module === 'WAWebCollections' ? { ...collections, Chat: chatStore } : require(module);
      }, { id: missingId, name: missingName });
      await options.onBrowserReady?.(browser, page, extensionId);
    } });
    self = this;
    hosts.set(options.profileDirectory.split(/[\\/]/).at(-1), { host: this, options });
  }
}
const entryFor = (workspace: string) => {
  const entry = hosts.get(workspaceKey(workspace));
  if (!entry) throw new Error('Isolated registration host missing');
  return entry;
};
(globalThis as any).panelRegistrationFixture = {
  async selectMissing(workspace: string) {
    const { host } = entryFor(workspace);
    await host.client.pupPage.evaluate((id: string) => { (window as any).__fixtureActiveChat = id; }, missingId);
  },
  registerMissing(workspace: string) {
    const { host, options } = entryFor(workspace), state = host.getStatus();
    if (state.status !== 'ready') throw new Error('Isolated registration account not ready');
    options.onEvent({ type: 'messages', account_id: state.account_id, connection_epoch: state.connection_epoch,
      messages: [{ id: `metadata:${missingId}`, chat_id: missingId, name: missingName, body: '', timestamp_ms: 0, from_me: false, is_group: false, is_history: true, metadata_only: true }] });
  },
  async state(workspace: string) {
    const { host } = entryFor(workspace), browser = host.client.pupBrowser;
    const target = browser.targets().find((item: any) => /^chrome-extension:\/\/[a-p]{32}\/panel\.html$/.test(item.url()));
    if (!target) throw new Error('Isolated registration panel missing');
    const session = await target.createCDPSession();
    try {
      const value = await session.send('Runtime.evaluate', { returnByValue: true, expression: `JSON.stringify({name:document.getElementById('name').textContent,visible:!document.getElementById('content').hidden,empty:!document.getElementById('empty').hidden,controlsDisabled:['merchant','query','save','takeover'].every(id=>document.getElementById(id).matches(':disabled')),controlsEnabled:['merchant','query','save','takeover'].every(id=>!document.getElementById(id).matches(':disabled')),text:document.body.innerText})` });
      if (value.exceptionDetails) throw new Error('Isolated registration panel state failed');
      return JSON.parse(value.result.value);
    } finally { await session.detach(); }
  },
};
