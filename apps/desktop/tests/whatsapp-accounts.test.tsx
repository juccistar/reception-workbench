import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WhatsAppAccounts } from '../src/launcher/WhatsAppAccounts';
import type { WhatsAppAccountBridge, WhatsAppLauncherSnapshot } from '../src/shared/whatsapp-account';

afterEach(cleanup);
const base = (): WhatsAppLauncherSnapshot => ({ profiles: [], selected_workspace_id: null, host: { status: 'closed', account_id: '', connection_epoch: '' }, state: null, merchants: [] });
const connected = (): WhatsAppLauncherSnapshot => ({ profiles: [{ workspace_id: 'w', label: '环境一', account_tail: '1234', selected: true }, { workspace_id: 'w2', label: '环境二', account_tail: '', selected: false }], selected_workspace_id: 'w', host: { status: 'ready', account_id: 'a', connection_epoch: 'epoch' }, merchants: [{ merchant_id: 'm', name: '离线商家', account_tail: '', available: false }], state: { workspace_id: 'w', account_id: 'a', connection_epoch: 'epoch', connected: true, enabled: false, default_merchant_id: null, default_revision: 2, allowed_merchant_ids: [], conversations: [{ chat_id: 'chat', name: '联系人甲', human_owned: false, pending: true }], bindings: [{ chat_id: 'chat', merchant_id: 'm', conversation_id: null, revision: 7, segment_id: 's', requested_binding: { merchant_id: null, conversation_id: null } }], pending_operations: [{ operation_id: 'original-op', chat_id: 'chat', merchant_id: 'm', status: 'unknown', message_id: null, ack: null }], reply_batches: [], legacy: { status: 'unverified', source_count: 0, pending_count: 0, reason: '' }, model: { ready: false, status: 'missing', message: '请配置模型' } } });
function makeBridge(snapshot: WhatsAppLauncherSnapshot) {
  return { getWhatsAppAccounts: vi.fn().mockResolvedValue(snapshot), getWhatsAppWritebackCapabilities: vi.fn().mockResolvedValue({ available: false, enabled: false }), createWhatsAppAccount: vi.fn().mockResolvedValue(snapshot), selectWhatsAppAccount: vi.fn().mockResolvedValue(snapshot), saveWhatsAppAccountBinding: vi.fn().mockResolvedValue(snapshot), reconcileWhatsAppAccountOperation: vi.fn().mockResolvedValue(snapshot) } as unknown as WhatsAppAccountBridge;
}
const mount = (bridge: WhatsAppAccountBridge) => render(<WhatsAppAccounts bridge={bridge} onBack={() => {}} onModels={() => {}} />);

it('creates a named environment from the empty state', async () => {
  const bridge = makeBridge(base()); mount(bridge);
  expect(await screen.findByText('还没有账号环境，请先创建。')).toBeTruthy();
  fireEvent.click(screen.getByText('新建环境'));
  fireEvent.change(screen.getByLabelText('环境名称'), { target: { value: '  商务账号  ' } });
  fireEvent.click(screen.getByText('创建'));
  await waitFor(() => expect(bridge.createWhatsAppAccount).toHaveBeenCalledWith('商务账号'));
});

it('saves the selected contact with its account identity and binding revision', async () => {
  const bridge = makeBridge(connected()); mount(bridge);
  fireEvent.change(await screen.findByLabelText('选择联系人'), { target: { value: 'chat' } });
  expect(screen.getAllByText(/离线商家（离线）/).length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText('国际站会话 ID（可选）'), { target: { value: 'conversation-1' } });
  fireEvent.click(screen.getByText('保存联系人绑定'));
  await waitFor(() => expect(bridge.saveWhatsAppAccountBinding).toHaveBeenCalledWith({ expected_account_id: 'a', expected_connection_epoch: 'epoch', chat_id: 'chat', merchant_id: 'm', conversation_id: 'conversation-1', expected_revision: 7 }));
  expect(screen.getByText(/等待原发送结果明确后生效/)).toBeTruthy();
  expect(screen.getByText(/原操作 ID/).textContent).toContain('original-op');
  expect(screen.getByText(/状态：unknown/).textContent).toContain('离线商家');
  fireEvent.click(screen.getByText('核查原发送结果'));
  await waitFor(() => expect(bridge.reconcileWhatsAppAccountOperation).toHaveBeenCalledWith('original-op'));
});

it('can disconnect the visible QR page before account authentication completes', async () => {
  const snapshot = connected();
  snapshot.host = { status: 'qr', account_id: '', connection_epoch: '', page_state: 'available' };
  snapshot.state = { ...snapshot.state!, connected: false, account_id: null, connection_epoch: null };
  const bridge = makeBridge(snapshot);
  bridge.disconnectWhatsAppAccount = vi.fn().mockResolvedValue(base());
  mount(bridge);
  const stop = await screen.findByRole('button', { name: '断开连接' });
  expect((screen.getByRole('button', { name: '连接真实 Chrome' }) as HTMLButtonElement).disabled).toBe(true);
  expect((stop as HTMLButtonElement).disabled).toBe(false);
  expect(screen.getByText('Chrome 状态：等待扫码')).toBeTruthy();
  fireEvent.click(stop);
  await waitFor(() => expect(bridge.disconnectWhatsAppAccount).toHaveBeenCalledTimes(1));
});

it('does not let an old refresh overwrite an explicit environment selection', async () => {
  const first = connected(); let resolveRefresh!: (s: WhatsAppLauncherSnapshot) => void;
  const bridge = makeBridge(first);
  vi.mocked(bridge.getWhatsAppAccounts).mockResolvedValueOnce(first).mockImplementationOnce(() => new Promise(resolve => { resolveRefresh = resolve; }));
  const second = { ...connected(), selected_workspace_id: 'w2', state: { ...connected().state!, workspace_id: 'w2', account_id: 'b' } };
  vi.mocked(bridge.selectWhatsAppAccount).mockResolvedValue(second);
  mount(bridge);
  const selector = await screen.findByLabelText('选择环境');
  fireEvent(document, new Event('visibilitychange'));
  await waitFor(() => expect(resolveRefresh).toBeTypeOf('function'));
  fireEvent.change(selector, { target: { value: 'w2' } });
  resolveRefresh(first);
  await waitFor(() => expect(bridge.selectWhatsAppAccount).toHaveBeenCalledWith('w2'));
  await waitFor(() => expect((selector as HTMLSelectElement).value).toBe('w2'));
});
