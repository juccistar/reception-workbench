import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ActivityPage, HandoffPage, SettingsPage } from '../src/renderer/components/Pages';
import type { Workbench } from '../src/renderer/hooks/useWorkbench';

vi.mock('../src/renderer/components/SharedRequirementCard', () => ({ SharedRequirementCard: ({ current }: { current: { case_id: string } }) => <div>需求 {current.case_id}</div> }));
vi.mock('../src/renderer/components/FeishuForm', () => ({ FeishuForm: () => null }));
vi.mock('../src/renderer/components/DataStorage', () => ({ DataStorage: () => null }));
vi.mock('../src/renderer/components/UpdatesPanel', () => ({ UpdatesPanel: () => null }));
vi.mock('../src/renderer/components/ModelSummary', () => ({ ModelSummary: () => null }));

it('selects a quote without navigating, saves its supplement before opening chat', async () => {
  const cases = Array.from({ length: 30 }, (_, i) => ({ case_id: `case-${i}`, conversation_id: `chat-${i}`, customer_name: `客户${i}`, state: 'waiting_quote', updated_at: 1, revision: 1 }));
  const navigate = vi.fn();
  const save = vi.fn(async () => true);
  const model = { connected: true, snapshot: { handoff_cases: cases, conversations: [] }, selectConversation: navigate, saveSupplement: save, ensureSupplement: vi.fn(async () => {}), supplementFor: () => ({ body: '现有补充', dirty: true, saving: null, attachments: [] }), editSupplementFor: vi.fn() } as unknown as Workbench;
  render(<HandoffPage model={model} />);
  const list = screen.getByRole('region', { name: '待报价事项' });
  fireEvent.click(within(list).getByRole('button', { name: /客户29/ }));
  await waitFor(() => expect(screen.getByRole('region', { name: '事项详情' })).toHaveTextContent('需求 case-29'));
  expect(navigate).not.toHaveBeenCalled();
  expect(save).toHaveBeenCalledWith('chat-0');
  fireEvent.click(screen.getByRole('button', { name: '打开聊天' }));
  await waitFor(() => expect(navigate).toHaveBeenCalledWith('chat-29', 'case-29'));
  expect(save).toHaveBeenCalledWith('chat-29');
});

it('saves a searchable selection of customers without requiring an AI check', async () => {
  const mutate = vi.fn(async () => true);
  const model = { connected: true, loading: false, busy: () => false, setFormDirty: vi.fn(), mutate, snapshot: { app: { mode: 'live' }, reception: { running: false, scope: { mode: 'all', conversation_ids: [] } }, conversations: [{ conversation_id: 'a', customer_name: 'Alice', buyer_id: '1' }, { conversation_id: 'b', customer_name: 'Bob', buyer_id: '2' }] } } as unknown as Workbench;
  render(<SettingsPage model={model} />);
  fireEvent.change(screen.getByRole('combobox', { name: '自动接待范围' }), { target: { value: 'selected' } });
  fireEvent.change(screen.getByLabelText('搜索接待客户'), { target: { value: 'Bob' } });
  fireEvent.click(screen.getByLabelText('Bob'));
  fireEvent.click(screen.getByRole('button', { name: '保存范围' }));
  await waitFor(() => expect(mutate).toHaveBeenCalledWith('set_scope', { mode: 'selected', conversation_ids: ['b'], max_auto_replies: null }, expect.any(String)));
  expect(mutate).not.toHaveBeenCalledWith('check_ai', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: '图片与上下文' }));
  expect(screen.getByText('高级处理参数')).toBeVisible();
  expect(screen.getByLabelText(/Accio workctl 程序路径/)).not.toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '平台连接' }));
  expect(screen.getByLabelText(/Accio workctl 程序路径/)).toBeVisible();
  expect(screen.getByText('高级处理参数')).not.toBeVisible();
});

it('omits send tasks and operation history from activity', () => {
  const model = { connected: true, busy: () => false, jobs: [{ job_id: 'send-1', kind: 'automatic_send', status: 'failed' }], snapshot: { conversations: [], outbox: [{ operation_id: 'private-operation', kind: 'send' }] } } as unknown as Workbench;
  render(<ActivityPage model={model} />);
  expect(screen.queryByText('private-operation')).not.toBeInTheDocument();
  expect(screen.queryByText('客户消息发送')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /核查原操作/ })).not.toBeInTheDocument();
});
