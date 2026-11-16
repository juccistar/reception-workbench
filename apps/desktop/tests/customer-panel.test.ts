// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { CustomerPanelController } from '../src/main/whatsapp/customer-panel';

function fixture() {
  let chat = 'a@c.us', epoch = 'epoch';
  const status = () => ({ status: 'ready', account_id: 'account', connection_epoch: epoch });
  const state = () => ({ workspace_id: 'workspace', account_id: 'account', connection_epoch: epoch, connected: true, enabled: false, allowed_merchant_ids: [], model: { ready: false }, legacy: { status: 'ready' }, bindings: [], conversations: [{ chat_id: chat, human_owned: false, reception_paused: false }], default_merchant_id: 'merchant', default_revision: 0 });
  const options: any = { workspaceId: 'workspace', runtimeInstanceId: 'runtime', getStatus: status, resolveCurrentChat: vi.fn(async () => ({ chat_id: chat })), readState: vi.fn(async () => ({ profile: { workspace_id: 'workspace' }, runtime_instance_id: 'runtime', state: state(), merchants: [{ merchant_id: 'merchant', name: 'M', available: true }] })), getContext: vi.fn(async () => ({ binding_revision: 0, wa_summary: '', international_background: null })), saveBinding: vi.fn(), takeover: vi.fn(), reconcile: vi.fn(), searchCustomers: vi.fn(async () => []) };
  const controller: any = new CustomerPanelController(options);
  const messages: any[] = [];
  controller.extensionId = 'a'.repeat(32); controller.contextId = 7; controller.token = 'secret';
  controller.target = { url: () => controller.panelUrl() };
  controller.session = { send: vi.fn(async (_method: string, args: any) => { const match = args.expression?.match(/^window\.__customerPanelReceive\((.*)\)$/); if (match) messages.push(JSON.parse(JSON.parse(match[1]))); }), detach: vi.fn(async () => {}), removeAllListeners: vi.fn() };
  controller.view = { identity: { workspace_id: 'workspace', runtime_instance_id: 'runtime', account_id: 'account', connection_epoch: epoch, chat_id: chat, view_revision: 1 }, name: 'A', merchants: [{ merchant_id: 'merchant', name: 'M', available: true }], merchant_id: 'merchant', conversation_id: null, binding_revision: 0, human_owned: false, reception_paused: false, summary: '', requirements: '', context_status: 'ready' };
  const event = (action = 'search', input: any = { merchant_id: 'merchant', query: 'a' }) => ({ name: '__receptionCustomerPanel', executionContextId: 7, payload: JSON.stringify({ token: 'secret', request_id: '1', action, identity: controller.view.identity, input }) });
  return { controller, options, messages, event, setChat: (v: string) => { chat = v; }, setEpoch: (v: string) => { epoch = v; } };
}

describe('private customer panel bridge', () => {
  it('ignores other contexts, ordinary pages, oversized payloads and unknown actions', async () => {
    const f = fixture();
    await f.controller.handleBinding({ ...f.event(), executionContextId: 8 }, 0);
    f.controller.target.url = () => 'https://web.whatsapp.com/';
    await f.controller.handleBinding(f.event(), 0);
    f.controller.target.url = () => f.controller.panelUrl();
    await f.controller.handleBinding({ ...f.event(), payload: 'x'.repeat(8193) }, 0);
    await f.controller.handleBinding(f.event('invoke', { method: 'anything' }), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled();
    expect(f.messages).toEqual([expect.objectContaining({ error: expect.any(String) })]);
  });
  it.each(['chat', 'epoch'])('clears late search results after %s changes', async change => {
    const f = fixture(); let finish!: (value: any[]) => void;
    f.options.searchCustomers.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = f.controller.handleBinding(f.event(), 0);
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    if (change === 'chat') f.setChat('b@c.us'); else f.setEpoch('new-epoch');
    finish([{ conversation_id: 'private', name: 'Private' }]); await pending;
    expect(f.messages.some(m => m.kind === 'clear')).toBe(true);
    expect(JSON.stringify(f.messages)).not.toContain('Private');
  });
  it('does not report a late mutation against a different customer', async () => {
    const f = fixture(); let finish!: () => void;
    f.options.takeover.mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
    const pending = f.controller.handleBinding(f.event('takeover', { enabled: true }), 0);
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    f.setChat('b@c.us'); finish(); await pending;
    expect(f.messages.some(m => m.kind === 'clear')).toBe(true);
    expect(f.messages.some(m => m.result === true)).toBe(false);
  });
  it('does not deliver results after close', async () => {
    const f = fixture(); let finish!: (value: any[]) => void;
    f.options.searchCustomers.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = f.controller.handleBinding(f.event(), 0);
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    await f.controller.close(); finish([{ conversation_id: 'private', name: 'Private' }]); await pending;
    expect(f.messages).toEqual([{ kind: 'clear' }]); expect(f.controller.inflight).toBe(0);
  });
  it('limits concurrent requests and rejects unverified customer identifiers', async () => {
    const f = fixture(); f.controller.inflight = 4;
    await f.controller.handleBinding(f.event(), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled();
    expect(f.messages[0].error).toContain('请求过多');
    f.controller.inflight = 0;
    await f.controller.handleBinding(f.event('save', { merchant_id: 'merchant', conversation_id: 'unverified' }), 0);
    expect(f.options.saveBinding).not.toHaveBeenCalled();
  });
  it('shows only labelled ai-v1 values and limits customer search', async () => {
    const f = fixture();
    f.options.getContext.mockResolvedValue({ binding_revision: 0, wa_summary: '', international_background: { requirements: { product: { value: '收纳盒', evidence_ids: ['secret'] }, quantity: { value: null }, product_images: { value: ['media-secret'] }, budget: { value: '100 元', source_excerpts: ['private'] }, unknown: { value: 'hidden' } } } });
    f.options.searchCustomers.mockResolvedValue(Array.from({ length: 30 }, (_, i) => ({ conversation_id: String(i), name: '客户' })));
    await f.controller.poll(0);
    await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('ready'));
    const view = f.messages.filter(m => m.kind === 'view').at(-1).view;
    expect(view.reception_active).toBe(false);
    expect(view.name).toBe('当前客户'); expect(view.requirements).toBe('产品：收纳盒\n产品图片：1 张\n客户预算：100 元');
    expect(view.requirements).not.toMatch(/secret|private|unknown/);
    await f.controller.handleBinding(f.event('search', { merchant_id: 'merchant', query: 'a'.repeat(81) }), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled();
    await f.controller.handleBinding(f.event('search', { merchant_id: 'merchant', query: 'a'.repeat(80) }), 0);
    expect(f.messages.at(-1).result).toHaveLength(25);
    await f.controller.close();
  });
  it('keeps an explicitly unassigned customer separate from the default merchant for new contacts', async () => {
    const f = fixture();
    const snapshot = await f.options.readState();
    snapshot.state.enabled = true; snapshot.state.allowed_merchant_ids = ['merchant']; snapshot.state.model.ready = true;
    snapshot.state.bindings = [{ chat_id: 'a@c.us', merchant_id: null, conversation_id: null, revision: 0 }];
    f.options.readState.mockResolvedValue(snapshot);
    await f.controller.poll(0);
    const view = f.messages.find(m => m.kind === 'view').view;
    expect(view.merchant_id).toBeNull(); expect(view.reception_active).toBe(false);
    await f.controller.close();
  });
  it('shows the verified customer while registration is pending and recovers when context becomes available', async () => {
    const f = fixture(); const snapshot = await f.options.readState(); snapshot.state.conversations = [];
    f.options.readState.mockResolvedValue(snapshot);
    f.options.resolveCurrentChat.mockResolvedValue({ chat_id: 'a@c.us', name: '客户甲' });
    f.options.getContext.mockRejectedValueOnce(Object.assign(new Error('WhatsApp 联系人不存在'), { code: 'not_found' }));
    await f.controller.poll(0);
    await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('unavailable'));
    expect(f.controller.view.name).toBe('客户甲');
    for (const [action, input] of [['search', { merchant_id: 'merchant', query: 'a' }], ['save', { merchant_id: 'merchant', conversation_id: null }], ['takeover', { enabled: true }]] as const) await f.controller.handleBinding(f.event(action, input), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled(); expect(f.options.saveBinding).not.toHaveBeenCalled(); expect(f.options.takeover).not.toHaveBeenCalled();
    snapshot.state.conversations = [{ chat_id: 'a@c.us', human_owned: false, reception_paused: false }];
    clearTimeout(f.controller.timer); await f.controller.poll(0);
    await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('ready'));
    await f.controller.handleBinding(f.event(), 0); expect(f.options.searchCustomers).toHaveBeenCalledOnce();
    await f.controller.close();
  });
  it.each(['failure', 'revision'])('keeps actions unavailable on context %s', async kind => {
    const f = fixture();
    if (kind === 'failure') f.options.getContext.mockRejectedValue(new Error('failure'));
    else f.options.getContext.mockResolvedValue({ binding_revision: 1, wa_summary: '旧资料', international_background: null });
    await f.controller.poll(0); await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('unavailable'));
    await f.controller.handleBinding(f.event(), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled(); expect(JSON.stringify(f.messages)).not.toContain('旧资料');
    await f.controller.close();
  });
  it.each(['chat', 'epoch'])('does not publish delayed context after %s changes', async change => {
    const f = fixture(); let finish!: (value: any) => void;
    f.options.getContext.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await f.controller.poll(0); await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    if (change === 'chat') f.setChat('b@c.us'); else f.setEpoch('new-epoch');
    clearTimeout(f.controller.timer); await f.controller.poll(0);
    finish({ binding_revision: 0, wa_summary: '旧客户私密信息', international_background: null });
    await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('ready'));
    expect(JSON.stringify(f.messages)).not.toContain('旧客户私密信息');
    await f.controller.close();
  });
  it('keeps retrying context at a bounded rate after five failures and recovers without a snapshot change', async () => {
    const f = fixture(); const clock = vi.spyOn(Date, 'now');
    f.options.getContext.mockRejectedValue(new Error('temporary context failure'));
    try {
      for (let attempt = 0; attempt < 6; attempt++) {
        clock.mockReturnValue(1000000 + attempt * 60000);
        clearTimeout(f.controller.timer); await f.controller.poll(0);
        await vi.waitFor(() => expect(f.controller.contextPending).toBe(false));
      }
      expect(f.options.getContext).toHaveBeenCalledTimes(6);
      f.options.getContext.mockResolvedValue({ binding_revision: 0, wa_summary: '恢复后的资料', international_background: null });
      clock.mockReturnValue(2000000); clearTimeout(f.controller.timer); await f.controller.poll(0);
      await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('ready'));
      expect(f.controller.view.summary).toBe('恢复后的资料');
    } finally { clock.mockRestore(); await f.controller.close(); }
  });
  it('publishes the empty selection status again after a panel is retired and reopened', async () => {
    const f = fixture(); f.options.resolveCurrentChat.mockResolvedValue(null);
    await f.controller.poll(0);
    expect(f.messages.at(-1)).toEqual({ kind: 'clear', status: '请选择一个单聊' });
    const session = f.controller.session; f.controller.retirePanel(); f.messages.length = 0;
    f.controller.session = session; f.controller.contextId = 7; f.controller.target = { url: () => f.controller.panelUrl() };
    await f.controller.poll(f.controller.generation);
    expect(f.messages.at(-1)).toEqual({ kind: 'clear', status: '请选择一个单聊' });
    await f.controller.close();
  });
  it('keeps one context read across rapid chat changes and never publishes its late data', async () => {
    const f = fixture(); let finish!: (value: any) => void;
    f.options.getContext.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await f.controller.poll(0); await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    for (const chat of ['b@c.us', 'c@c.us', 'd@c.us']) { f.setChat(chat); clearTimeout(f.controller.timer); await f.controller.poll(0); }
    expect(f.options.getContext).toHaveBeenCalledOnce();
    expect(f.controller.view.identity.chat_id).toBe('d@c.us');
    finish({ binding_revision: 0, wa_summary: '旧客户私密信息', international_background: null });
    await vi.waitFor(() => expect(f.controller.contextPending).toBe(false));
    clearTimeout(f.controller.timer); await f.controller.poll(0);
    await vi.waitFor(() => expect(f.controller.view?.context_status).toBe('ready'));
    expect(JSON.stringify(f.messages)).not.toContain('旧客户私密信息');
    await f.controller.close();
  });
  it('restores the empty selection message after a transient snapshot error', async () => {
    const f = fixture(); f.options.resolveCurrentChat.mockResolvedValue(null);
    await f.controller.poll(0);
    f.options.readState.mockRejectedValueOnce(new Error('temporary read error'));
    clearTimeout(f.controller.timer); await f.controller.poll(0);
    expect(f.messages.at(-1)).toEqual({ kind: 'clear', status: '客户信息暂不可用' });
    clearTimeout(f.controller.timer); await f.controller.poll(0);
    expect(f.messages.at(-1)).toEqual({ kind: 'clear', status: '请选择一个单聊' });
    await f.controller.close();
  });
  it('recovers the same extension target after more than four initialization failures', async () => {
    vi.useFakeTimers(); const f = fixture(); let attempts = 0;
    const target = { url: () => f.controller.panelUrl(), createCDPSession: async () => {
      if (++attempts < 7) throw new Error('extension context still starting');
      const session: any = new EventEmitter();
      session.detach = async () => {};
      session.send = async (method: string) => {
        if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'frame', url: target.url() } } };
        if (method === 'Runtime.enable') session.emit('Runtime.executionContextCreated', { context: { id: 7, origin: `chrome-extension://${'a'.repeat(32)}`, auxData: { frameId: 'frame', isDefault: true, type: 'default' } } });
        return { result: { value: true } };
      };
      return session;
    } };
    const browser: any = new EventEmitter(); browser.targets = () => [target];
    try {
      await f.controller.attach(browser, {}, 'a'.repeat(32));
      for (let tick = 0; tick < 10 && !f.controller.getInfo().available; tick++) await vi.advanceTimersByTimeAsync(30000);
      expect(attempts).toBeGreaterThan(4); expect(f.controller.getInfo().available).toBe(true);
      expect(f.controller.session).toBeTruthy();
    } finally { await f.controller.close(); vi.useRealTimers(); }
  });
  it('waits for the extension behavior before first open and can reopen after its worker sleeps', async () => {
    const f = fixture(); const order: string[] = [];
    const browserSession = { send: vi.fn(async (method: string) => { if (method === 'Extensions.triggerAction') { order.push('open'); return {}; } return { targetInfos: [{ type: 'tab', targetId: 'tab' }, { type: 'page', targetId: 'page', parentId: 'tab' }] }; }), detach: vi.fn(async () => {}) };
    const waitForTarget = vi.fn(async () => ({ worker: async () => ({ evaluate: async () => { order.push('ready'); } }) }));
    f.controller.browser = { targets: () => [], waitForTarget, target: () => ({ createCDPSession: async () => browserSession }) };
    f.controller.page = { createCDPSession: async () => ({ send: async () => ({ targetInfo: { targetId: 'page' } }), detach: async () => {} }) };
    await f.controller.open(); await f.controller.open();
    expect(order).toEqual(['ready', 'open', 'open']); expect(waitForTarget).toHaveBeenCalledOnce();
    await f.controller.close();
  });
  it('does not trigger an already visible extension panel', async () => {
    const f = fixture(); const create = vi.fn();
    f.controller.browser = { targets: () => [{ url: () => f.controller.panelUrl() }], target: create };
    f.controller.page = {};
    await f.controller.open(); expect(create).not.toHaveBeenCalled();
  });
});
