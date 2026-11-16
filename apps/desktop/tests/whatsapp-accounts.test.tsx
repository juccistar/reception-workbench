import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { WhatsAppAccounts } from '../src/launcher/WhatsAppAccounts';

afterEach(cleanup);
const profile = (id: string) => ({ workspace_id: id, label: `账号${id}`, account_tail: '1234', revision: 1 });
const host = { status: 'ready', account_id: 'account', connection_epoch: 'epoch' };
const state = () => ({ workspace_id: 'a', account_id: 'account', connection_epoch: 'epoch', connected: true, enabled: false, default_merchant_id: null, default_revision: 2, allowed_merchant_ids: [], conversations: [{ chat_id: 'chat', name: '联系人甲', human_owned: false, pending: true }], bindings: [{ chat_id: 'chat', merchant_id: 'm', conversation_id: null, revision: 7, segment_id: 's' }], pending_operations: [{ operation_id: 'original', chat_id: 'chat', merchant_id: 'm', status: 'unknown', message_id: null, ack: null }], reply_batches: [], legacy: { status: 'ready', source_count: 0, pending_count: 0, reason: '' }, model: { ready: true, status: 'ready', message: '' } });
const detail = (id: string, connected = true) => ({ profile: profile(id), host, state: connected ? { ...state(), workspace_id: id } : null, merchants: [{ merchant_id: 'm', name: '商家甲', account_tail: '', available: true }], runtime_instance_id: 'instance' });
const summary = (id: string, pending_count = 0) => ({ ...profile(id), host, enabled: false, pending_count, runtime_status: 'running', runtime_instance_id: 'instance' });
function setup() {
  const bridge = { getWhatsAppAccounts: vi.fn().mockResolvedValue({ accounts: [summary('a', 1), summary('b')], merchants: [] }), getWhatsAppAccount: vi.fn(async (id: string) => detail(id)), openWhatsAppAccount: vi.fn().mockResolvedValue(undefined), connectWhatsAppAccount: vi.fn(async (id: string) => detail(id)), renameWhatsAppAccount: vi.fn(async ({ workspace_id, label }: { workspace_id: string; label: string }) => ({ ...detail(workspace_id), profile: { ...profile(workspace_id), label, revision: 2 } })), saveWhatsAppDefaultMerchant: vi.fn().mockRejectedValue(new Error('保存失败')), setWhatsAppAccountReception: vi.fn().mockResolvedValue(detail('a')), reconcileWhatsAppAccountOperation: vi.fn().mockResolvedValue(detail('a')), saveWhatsAppAccountBinding: vi.fn().mockResolvedValue(detail('a')), takeoverWhatsAppContact: vi.fn().mockResolvedValue(detail('a')), readWhatsAppContacts: vi.fn().mockResolvedValue(detail('a')), verifyWhatsAppLegacySources: vi.fn().mockResolvedValue(detail('a')), renameMerchant: vi.fn().mockResolvedValue(undefined), createWhatsAppAccount: vi.fn().mockResolvedValue(detail('c')) };
  const onAuthorization = vi.fn(); const onModels = vi.fn();
  render(<WhatsAppAccounts bridge={bridge} merchants={[{ id: 'm', name: '商家甲', account_tail: '8888', status: 'reauthorize' }]} refreshMerchants={vi.fn().mockResolvedValue(undefined)} onAuthorization={onAuthorization} onModels={onModels} />);
  return { bridge, onAuthorization, onModels };
}
it('shows both channels and directs actions to the corresponding account', async () => {
  const { bridge, onAuthorization } = setup();
  const card = (await screen.findByText('账号a')).closest('article')!;
  fireEvent.click(within(card).getByText('打开窗口'));
  await waitFor(() => expect(bridge.openWhatsAppAccount).toHaveBeenCalledWith('a'));
  fireEvent.click(screen.getByText('授权')); expect(onAuthorization).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText('国际站')); expect(screen.queryByText('账号a')).toBeNull();
});
it('shows unknown only on the affected account and reconciles its original operation', async () => {
  const { bridge } = setup();
  fireEvent.click(await screen.findByText('1 条待核查'));
  fireEvent.click(await screen.findByText('核查结果'));
  await waitFor(() => expect(bridge.reconcileWhatsAppAccountOperation).toHaveBeenCalledWith({ workspace_id: 'a', operation_id: 'original' }));
  expect(screen.queryByText('重发')).toBeNull();
});
it('retains unsaved drafts and reports a partial save', async () => {
  const { bridge } = setup();
  fireEvent.click(await screen.findByLabelText('编辑账号a'));
  fireEvent.change(await screen.findByLabelText('账号名称'), { target: { value: '新名称' } });
  fireEvent.click(screen.getByLabelText('自动接待'));
  fireEvent.change(screen.getByLabelText('新联系人默认商家'), { target: { value: 'm' } });
  fireEvent.click(screen.getByText('保存'));
  expect(await screen.findByText(/已保存名称；其余未保存/)).toBeTruthy();
  expect((screen.getByLabelText('账号名称') as HTMLInputElement).value).toBe('新名称');
  expect(bridge.renameWhatsAppAccount).toHaveBeenCalledWith({ workspace_id: 'a', label: '新名称', expected_revision: 1 });
});
it('does not let a late detail replace another account', async () => {
  const { bridge } = setup(); let resolve!: (value: ReturnType<typeof detail>) => void;
  bridge.getWhatsAppAccount.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  fireEvent.click(await screen.findByLabelText('编辑账号a'));
  fireEvent.click(screen.getByLabelText('编辑账号b'));
  expect((await screen.findByLabelText('账号名称') as HTMLInputElement).value).toBe('账号b');
  resolve(detail('a'));
  await waitFor(() => expect((screen.getByLabelText('账号名称') as HTMLInputElement).value).toBe('账号b'));
});
it('can rename an offline account without starting it', async () => {
  const { bridge } = setup(); bridge.getWhatsAppAccount.mockResolvedValueOnce(detail('a', false));
  fireEvent.click(await screen.findByLabelText('编辑账号a'));
  fireEvent.change(await screen.findByLabelText('账号名称'), { target: { value: '离线改名' } });
  fireEvent.click(screen.getByText('保存'));
  await waitFor(() => expect(bridge.renameWhatsAppAccount).toHaveBeenCalled());
  expect(bridge.connectWhatsAppAccount).not.toHaveBeenCalled();
});

it('shows a native connection timeout in the status position and allows reconnecting', async () => {
  const { bridge } = setup();
  bridge.getWhatsAppAccounts.mockResolvedValue({ accounts: [{ ...summary('a'), host: Object.assign({ ...host, status: 'error' }, { error_code: 'sdk_timeout' }) }], merchants: [] });
  fireEvent.focus(window);
  const status = await screen.findByText('连接超时');
  const card = status.closest('article')!;
  expect(within(card).getAllByText('连接超时')).toHaveLength(1);
  fireEvent.click(within(card).getByText('连接'));
  await waitFor(() => expect(bridge.connectWhatsAppAccount).toHaveBeenCalledWith('a'));
});

it('keeps another account usable while one action is pending', async () => {
  const { bridge } = setup();
  let finish!: () => void;
  bridge.openWhatsAppAccount.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
  const a = (await screen.findByText('账号a')).closest('article')!;
  const b = screen.getByText('账号b').closest('article')!;
  fireEvent.click(within(a).getByText('打开窗口'));
  fireEvent.click(within(b).getByText('打开窗口'));
  await waitFor(() => expect(bridge.openWhatsAppAccount).toHaveBeenCalledWith('b'));
  finish();
});
