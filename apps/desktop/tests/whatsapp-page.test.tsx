import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReceptionBridge } from '../src/shared/api';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';
import { WhatsAppPage } from '../src/renderer/components/WhatsAppPage';
function setup() {
  const state = { connected: true, account_id: 'self@c.us', connection_epoch: 'epoch', enabled: false, conversations: [{ chat_id: 'synthetic@lid', name: '合成单聊客户', human_owned: false, pending: true }], bindings: [{ chat_id: 'synthetic@lid', conversation_id: null, revision: 0 }], pending_operations: [] as Record<string, unknown>[] };
  const invoke = vi.fn(async (method: string, _params?: unknown): Promise<unknown> => {
    if (method === 'get_whatsapp_state') return structuredClone(state);
    if (method === 'get_whatsapp_context') return { whatsapp_messages: [], wa_summary: '合成独立摘要', knowledge: [], guidance: [], style: [], binding_revision: 0 };
    return {};
  });
  const bridge = { invoke, getWhatsAppHostInfo: vi.fn(async () => ({ status: 'ready', account_id: state.account_id, connection_epoch: state.connection_epoch })), onEvent: () => () => {}, setWhatsAppView: vi.fn(async () => {}), listWhatsAppChats: vi.fn(async () => []), disconnectWhatsApp: vi.fn(async () => {}) } as unknown as ReceptionBridge;
  const model = { snapshot: { app: { mode: 'live', account_verified: true }, checks: { ai: { status: 'passed' } }, conversations: [{ conversation_id: 'alibaba-synthetic', customer_name: '合成国际站客户' }] }, closing: false, navigate: vi.fn() } as unknown as Workbench;
  return { state, invoke, bridge, model };
}
afterEach(() => vi.restoreAllMocks());
it('enables reception for all single chats without requiring an international binding', async () => {
  const { model, bridge, invoke } = setup(); render(<WhatsAppPage model={model} bridge={bridge} />);
  const enable = await screen.findByRole('button', { name: '开启全部单聊接待' });
  await waitFor(() => expect(enable).toBeEnabled()); fireEvent.click(enable);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('set_whatsapp_reception', { expected_account_id: 'self@c.us', expected_connection_epoch: 'epoch', enabled: true }));
  expect(screen.queryByRole('textbox', { name: /回复/ })).not.toBeInTheDocument();
});
it('uses an explicit SDK contact and revision when saving optional background', async () => {
  const { model, bridge, invoke } = setup(); render(<WhatsAppPage model={model} bridge={bridge} />);
  await screen.findByRole('option', { name: '合成单聊客户' });
  fireEvent.change(screen.getByRole('combobox', { name: '选择 WhatsApp 单聊' }), { target: { value: 'synthetic@lid' } });
  fireEvent.click(screen.getByRole('button', { name: '国际站绑定' }));
  expect(await screen.findByRole('dialog', { name: '国际站背景绑定' })).toBeInTheDocument();
  await waitFor(() => expect(bridge.setWhatsAppView).toHaveBeenCalledWith({ visible: false }));
  fireEvent.change(screen.getByRole('combobox', { name: '选择国际站客户背景' }), { target: { value: 'alibaba-synthetic' } });
  fireEvent.click(screen.getByRole('button', { name: '保存背景绑定' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('save_whatsapp_binding', { expected_account_id: 'self@c.us', expected_connection_epoch: 'epoch', chat_id: 'synthetic@lid', conversation_id: 'alibaba-synthetic', expected_revision: 0 }));
});
it('hides the native surface when an outer drawer opens and when leaving the module', async () => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 184, y: 240, width: 1000, height: 600, top: 240, left: 184, right: 1184, bottom: 840, toJSON() {} });
  const { model, bridge } = setup(); const view = render(<WhatsAppPage model={model} bridge={bridge} />);
  await waitFor(() => expect(bridge.setWhatsAppView).toHaveBeenCalledWith({ visible: true, bounds: { x: 184, y: 240, width: 1000, height: 600 } }));
  view.rerender(<WhatsAppPage model={model} bridge={bridge} obscured />);
  await waitFor(() => expect(bridge.setWhatsAppView).toHaveBeenLastCalledWith({ visible: false }));
  view.unmount();
  expect(bridge.setWhatsAppView).toHaveBeenLastCalledWith({ visible: false });
  expect(bridge.disconnectWhatsApp).not.toHaveBeenCalled();
});
it('labels unknown sends as awaiting receipts and only reconciles the original operation', async () => {
  const { model, bridge, invoke, state } = setup(); state.pending_operations = [{ operation_id: 'original', chat_id: 'synthetic@lid', status: 'unknown' }];
  render(<WhatsAppPage model={model} bridge={bridge} />);
  fireEvent.click(await screen.findByRole('button', { name: '核查 合成单聊客户 的回执' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('reconcile_whatsapp_operation', { operation_id: 'original' }));
});
it('does not keep a background preview after choosing a different customer', async () => {
  const { model, bridge, invoke, state } = setup(); state.conversations.push({ chat_id: 'other@lid', name: '另一位合成客户', human_owned: false, pending: false });
  let resolve!: (value: unknown) => void;
  const original = invoke.getMockImplementation()!;
  invoke.mockImplementation((method: string, params?: unknown) => method === 'get_whatsapp_context' ? new Promise(done => { resolve = done; }) : original(method, params));
  render(<WhatsAppPage model={model} bridge={bridge} />);
  await screen.findByRole('option', { name: '合成单聊客户' });
  fireEvent.change(screen.getByRole('combobox', { name: '选择 WhatsApp 单聊' }), { target: { value: 'synthetic@lid' } });
  fireEvent.click(screen.getByRole('button', { name: '查看上下文' }));
  fireEvent.click(screen.getByRole('button', { name: '关闭WhatsApp 接待上下文' }));
  fireEvent.change(screen.getByRole('combobox', { name: '选择 WhatsApp 单聊' }), { target: { value: 'other@lid' } });
  await act(async () => resolve({ wa_summary: '旧客户摘要' }));
  expect(screen.queryByText('旧客户摘要')).not.toBeInTheDocument();
});

it('shows restricted HTTP errors and never displays arbitrary exception text', async () => {
  const { model, bridge } = setup();
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'error', account_id: '', connection_epoch: '', error_code: 'http_error', http_status: 400 });
  bridge.connectWhatsApp = vi.fn().mockRejectedValue(new Error('secret raw diagnostic'));
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('登录页请求失败（HTTP 400）')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '连接 WhatsApp' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('操作未完成');
  expect(screen.queryByText(/secret raw diagnostic/)).not.toBeInTheDocument();
});

it('shows an available page separately from SDK failure and remeasures the native view for details', async () => {
  let y = 240;
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ x: 184, y, width: 1000, height: 600, top: y, left: 184, right: 1184, bottom: y + 600, toJSON() {} }));
  const { model, bridge, state, invoke } = setup();
  state.pending_operations = [{ operation_id: 'original', chat_id: 'synthetic@lid', status: 'unknown' }];
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'error', account_id: state.account_id, connection_epoch: 'epoch', page_state: 'available', phase: 'sdk_inject', error_code: 'sdk_resource_missing', diagnostic_id: 'wa_diag_12345678-1234-1234-1234-123456789abc' });
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('原始页面可用，自动接待未就绪')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '断开连接' })).toBeEnabled();
  await waitFor(() => expect(bridge.setWhatsAppView).toHaveBeenCalledWith({ visible: true, bounds: { x: 184, y: 240, width: 1000, height: 600 } }));
  y = 300;
  fireEvent.click(screen.getByRole('button', { name: '查看失败详情' }));
  expect(screen.getByText(/阶段：注入接待组件/)).toHaveTextContent('接待组件资源缺失');
  expect(screen.getByText(/诊断编号：wa_diag_12345678-1234-1234-1234-123456789abc/)).toBeInTheDocument();
  await waitFor(() => expect(bridge.setWhatsAppView).toHaveBeenLastCalledWith({ visible: true, bounds: { x: 184, y: 300, width: 1000, height: 600 } }));
  fireEvent.click(screen.getByRole('button', { name: '核查 合成单聊客户 的回执' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('reconcile_whatsapp_operation', { operation_id: 'original' }));
});

it('shows loading degradation and filters untrusted failure details while allowing pause', async () => {
  const { model, bridge, state } = setup(); state.enabled = true;
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'error', account_id: state.account_id, connection_epoch: 'epoch', page_state: 'loading', phase: 'C:\\private\\secret' as never, error_code: 'secret error' as never, diagnostic_id: 'C:\\private\\secret' });
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('网页仍在加载，自动接待初始化失败')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '暂停 WhatsApp 接待' })).toBeEnabled();
  expect(screen.queryByText('全部单聊自动接待已开启')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看失败详情' }));
  expect(screen.getByText(/原因：连接失败，可重新连接/)).toBeInTheDocument();
  expect(screen.queryByText(/private|secret|阶段：|诊断编号：/)).not.toBeInTheDocument();
});

it('blocks authorization when the SDK is ready but the business connection is not', async () => {
  const { model, bridge, state } = setup(); state.connected = false;
  render(<WhatsAppPage model={model} bridge={bridge} />);
  await screen.findByText('已连接');
  expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeDisabled();
});

it('applies host failure events immediately during a blocked business read and ignores older host queries', async () => {
  const { model, bridge, invoke, state } = setup();
  let listener!: Parameters<ReceptionBridge['onEvent']>[0];
  bridge.onEvent = vi.fn(callback => { listener = callback; return () => {}; });
  let resolveOldHost!: (value: { status: string; account_id: string; connection_epoch: string }) => void;
  const oldHost = new Promise<{ status: string; account_id: string; connection_epoch: string }>(resolve => { resolveOldHost = resolve; });
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValueOnce({ status: 'ready', account_id: state.account_id, connection_epoch: 'epoch' }).mockReturnValueOnce(oldHost);
  const original = invoke.getMockImplementation()!;
  let blockBusiness = false;
  invoke.mockImplementation((method, params) => method === 'get_whatsapp_state' && blockBusiness ? new Promise(() => {}) : original(method, params));
  render(<WhatsAppPage model={model} bridge={bridge} />);
  await waitFor(() => expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeEnabled());
  blockBusiness = true;
  act(() => listener({ event: 'state.changed', data: {} }));
  act(() => listener({ event: 'whatsapp.host.changed', data: { status: 'error', account_id: state.account_id, connection_epoch: 'epoch', page_state: 'available', error_code: 'cdp_error' } }));
  expect(screen.getByText('原始页面可用，自动接待未就绪')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeDisabled();
  await act(async () => resolveOldHost({ status: 'ready', account_id: state.account_id, connection_epoch: 'epoch' }));
  expect(screen.getByText('原始页面可用，自动接待未就绪')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeDisabled();
});

it('shows the initial host query without waiting for business state', async () => {
  const { model, bridge, invoke } = setup();
  invoke.mockImplementation(() => new Promise(() => {}));
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'error', account_id: '', connection_epoch: '', page_state: 'loading', error_code: 'sdk_timeout' });
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('网页仍在加载，自动接待初始化失败')).toBeInTheDocument();
});

it('shows the page HTTP error even when diagnostic fields are present', async () => {
  const { model, bridge } = setup();
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'error', account_id: '', connection_epoch: '', page_state: 'error', phase: 'page_navigation', error_code: 'http_error', http_status: 503, diagnostic_id: 'not-a-wa-id' });
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('登录页请求失败（HTTP 503）')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看失败详情' }));
  expect(screen.getByText(/阶段：加载登录页/)).toBeInTheDocument();
  expect(screen.queryByText(/诊断编号：/)).not.toBeInTheDocument();
});

it('shows QR waiting and blocks new authorization during recovery while preserving receipt checks', async () => {
  const { model, bridge, state, invoke } = setup();
  model.snapshot!.executor_health = { state: 'recovering', worker_alive: true, stage: 'dispatch', job_id: null, last_progress_at_ms: 1, observed_at_ms: 2, queue_depth: 0, error_code: null };
  state.pending_operations = [{ operation_id: 'original', chat_id: 'synthetic@lid', status: 'unknown' }];
  vi.mocked(bridge.getWhatsAppHostInfo!).mockResolvedValue({ status: 'qr', account_id: '', connection_epoch: '' });
  render(<WhatsAppPage model={model} bridge={bridge} />);
  expect(await screen.findByText('请在原始页面扫码')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开启全部单聊接待' })).toBeDisabled();
  fireEvent.click(await screen.findByRole('button', { name: '核查 合成单聊客户 的回执' }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith('reconcile_whatsapp_operation', { operation_id: 'original' }));
});
