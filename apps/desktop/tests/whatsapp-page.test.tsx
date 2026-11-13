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
