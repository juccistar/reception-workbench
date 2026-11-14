import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ReplyBatchRecords } from '../src/renderer/components/ReplyBatchRecords';

const batch = (overrides: Record<string, unknown> = {}) => ({
  batch_id: 'batch', status: 'sending', total: 3, accepted: 1, current_index: 1,
  pending_operation_ids: [] as string[], origin: 'automatic', ...overrides,
});

afterEach(() => { vi.useRealTimers(); });

it('shows active progress and puts an older stopped partial batch in history', () => {
  render(<ReplyBatchRecords channel="international" ownerId="c1" batches={[
    batch({ batch_id: 'old', status: 'stopped', conversation_id: 'c1', parts: [{ part_index: 0, status: 'accepted' }, { part_index: 1, status: 'stopped' }], last_gate_reason: 'source_changed' }),
    batch({ batch_id: 'current', status: 'waiting_sync', conversation_id: 'c1' }),
  ]} />);
  expect(screen.getByRole('status')).toHaveTextContent('已发送 1/3 条，正在同步后继续');
  expect(screen.getByText('发送记录（1）')).toBeInTheDocument();
  fireEvent.click(screen.getByText('发送记录（1）'));
  fireEvent.click(screen.getByText('1/3 条已受理 · 已停止'));
  expect(screen.getByText('第 1 条：平台已受理')).toBeVisible();
  expect(screen.getByText('第 2 条：已停止')).toBeVisible();
  expect(screen.getByText('原因：会话来源已变化')).toBeVisible();
});

it('reconciles the original unknown operation and respects disabled state', () => {
  const reconcile = vi.fn();
  const unknown = batch({ status: 'unknown', pending_operation_ids: ['original-operation'] });
  const view = render(<ReplyBatchRecords channel="international" batches={[unknown]} onReconcile={reconcile} />);
  expect(screen.getByRole('status')).toHaveTextContent('已发送 1/3 条，正在确认第 2 条发送结果');
  fireEvent.click(screen.getByRole('button', { name: '核查原发送' }));
  expect(reconcile).toHaveBeenCalledExactlyOnceWith('original-operation');
  view.rerender(<ReplyBatchRecords channel="international" batches={[unknown]} onReconcile={reconcile} reconcileDisabled />);
  expect(screen.getByRole('button', { name: '核查原发送' })).toBeDisabled();
});

it('moves a newly completed batch into history after about three seconds', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
  const running = batch();
  const view = render(<ReplyBatchRecords channel="international" batches={[running]} />);
  view.rerender(<ReplyBatchRecords channel="international" batches={[batch({ status: 'completed', accepted: 3, updated_at: Date.now() })]} />);
  expect(screen.getByRole('status')).toHaveTextContent('自动回复已发送，共 3 条');
  act(() => vi.advanceTimersByTime(2000));
  expect(screen.getByRole('status')).toHaveTextContent('自动回复已发送，共 3 条');
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.getByText('发送记录（1）')).toBeInTheDocument();
});

it('groups WhatsApp activity by contact and keeps manual records in history', () => {
  render(<ReplyBatchRecords channel="whatsapp" contactName={id => ({ a: '客户甲', b: '客户乙' }[id] || id)} batches={[
    batch({ batch_id: 'a-auto', chat_id: 'a', channel: 'whatsapp' }),
    batch({ batch_id: 'b-auto', chat_id: 'b', channel: 'whatsapp', status: 'waiting_sync' }),
    batch({ batch_id: 'a-manual', chat_id: 'a', channel: 'whatsapp', origin: 'manual', status: 'stopped' }),
    batch({ batch_id: 'other-channel', channel: 'international', status: 'sending' }),
  ]} />);
  const statuses = screen.getAllByRole('status');
  expect(statuses).toHaveLength(2);
  expect(within(statuses[0]).getByText('客户甲')).toBeInTheDocument();
  expect(within(statuses[1]).getByText('客户乙')).toBeInTheDocument();
  fireEvent.click(screen.getByText('发送记录（1）'));
  expect(screen.getByText('WhatsApp · 人工 · 客户甲')).toBeVisible();
});

it('filters international batches by conversation and labels automatic and manual history', () => {
  render(<ReplyBatchRecords channel="international" ownerId="c1" batches={[
    batch({ batch_id: 'active', conversation_id: 'c1' }),
    batch({ batch_id: 'auto-old', conversation_id: 'c1', status: 'stopped', phase: 'supplement' }),
    batch({ batch_id: 'manual', conversation_id: 'c1', origin: 'manual', status: 'stopped' }),
    batch({ batch_id: 'other', conversation_id: 'c2' }),
  ]} />);
  expect(screen.getAllByRole('status')).toHaveLength(1);
  fireEvent.click(screen.getByText('发送记录（2）'));
  expect(screen.getByText('国际站 · 自动 · 补充续答')).toBeVisible();
  expect(screen.getByText('国际站 · 人工')).toBeVisible();
});
