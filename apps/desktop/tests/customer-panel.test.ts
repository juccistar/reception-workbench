// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { CustomerPanelController } from '../src/main/whatsapp/customer-panel';

function fixture() {
  let chat = 'a@c.us', epoch = 'epoch';
  const status = () => ({ status: 'ready', account_id: 'account', connection_epoch: epoch });
  const state = () => ({ workspace_id: 'workspace', account_id: 'account', connection_epoch: epoch, connected: true, enabled: false, allowed_merchant_ids: [], model: { ready: false }, legacy: { status: 'ready' }, bindings: [], conversations: [], default_merchant_id: 'merchant', default_revision: 0 });
  const options: any = { workspaceId: 'workspace', runtimeInstanceId: 'runtime', getStatus: status, resolveCurrentChat: vi.fn(async () => ({ chat_id: chat })), readState: vi.fn(async () => ({ profile: { workspace_id: 'workspace' }, runtime_instance_id: 'runtime', state: state(), merchants: [{ merchant_id: 'merchant', name: 'M', available: true }] })), getContext: vi.fn(async () => ({ binding_revision: 0, wa_summary: '', international_background: null })), saveBinding: vi.fn(), takeover: vi.fn(), reconcile: vi.fn(), searchCustomers: vi.fn(async () => []) };
  const controller: any = new CustomerPanelController(options);
  const messages: any[] = [];
  controller.extensionId = 'a'.repeat(32); controller.contextId = 7; controller.token = 'secret';
  controller.target = { url: () => controller.panelUrl() };
  controller.session = { send: vi.fn(async (_method: string, args: any) => { const match = args.expression?.match(/^window\.__customerPanelReceive\((.*)\)$/); if (match) messages.push(JSON.parse(JSON.parse(match[1]))); }), detach: vi.fn(async () => {}), removeAllListeners: vi.fn() };
  controller.view = { identity: { workspace_id: 'workspace', runtime_instance_id: 'runtime', account_id: 'account', connection_epoch: epoch, chat_id: chat, view_revision: 1 }, name: 'A', merchants: [{ merchant_id: 'merchant', name: 'M', available: true }], merchant_id: 'merchant', conversation_id: null, binding_revision: 0, human_owned: false, reception_paused: false, summary: '', requirements: '' };
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
    const view = f.messages.find(m => m.kind === 'view').view;
    expect(view.reception_active).toBe(false);
    expect(view.name).toBe('当前客户'); expect(view.requirements).toBe('产品：收纳盒\n产品图片：1 张\n客户预算：100 元');
    expect(view.requirements).not.toMatch(/secret|private|unknown/);
    await f.controller.handleBinding(f.event('search', { merchant_id: 'merchant', query: 'a'.repeat(81) }), 0);
    expect(f.options.searchCustomers).not.toHaveBeenCalled();
    await f.controller.handleBinding(f.event('search', { merchant_id: 'merchant', query: 'a'.repeat(80) }), 0);
    expect(f.messages.at(-1).result).toHaveLength(25);
    await f.controller.close();
  });
  it('does not trigger an already visible extension panel', async () => {
    const f = fixture(); const create = vi.fn();
    f.controller.browser = { targets: () => [{ url: () => f.controller.panelUrl() }], target: create };
    f.controller.page = {};
    await f.controller.open(); expect(create).not.toHaveBeenCalled();
  });
});
