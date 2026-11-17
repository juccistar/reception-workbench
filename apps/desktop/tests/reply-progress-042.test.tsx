import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { App } from '../src/renderer/App';
import { currentReplyProgress } from '../src/renderer/receptionStatus';
import type { Job, ReplyBatchProgress } from '../src/shared/api';
import { createReplyActivityFixture } from './fixtures/replyActivity';

beforeEach(() => { Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true }); });
const stamp = Date.now();
const batch = (status: string): ReplyBatchProgress => ({ batch_id: 'batch-emma', status, origin: 'automatic', channel: 'international', conversation_id: 'emma', total: 2, accepted: status === 'accepted' ? 2 : 0, current_index: status === 'accepted' ? null : 0, pending_operation_ids: [], created_at: stamp, updated_at: stamp });

it('keeps manual suggestion feedback in the lower card, including an unconfirmed acknowledgement', async () => {
  const fixture = createReplyActivityFixture();
  render(<App bridge={fixture.bridge} />);
  await screen.findByRole('textbox', { name: '回复正文' });
  fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
  await screen.findByText('正在生成建议');
  expect(screen.getByRole('region', { name: '待处理回复建议' })).toHaveAttribute('aria-busy', 'true');
  expect(document.querySelector('.suggestion-generating .tiny-spinner')).not.toBeNull();
  expect(document.querySelector('.active-task')).toBeNull();
  expect(screen.queryByRole('status', { name: 'AI 回复生成状态' })).not.toBeInTheDocument();
});

it('preserves one progress container across generation, saving, submission and receipt', async () => {
  const f = createReplyActivityFixture(); f.snapshot.reception.running = true;
  render(<App bridge={f.bridge} />);
  await screen.findByRole('textbox', { name: '回复正文' });
  act(() => f.publish(f.job('running', 'emma', { automatic: true })));
  const container = screen.getByRole('status', { name: 'AI 回复生成状态' });
  act(() => f.publish(f.job('running', 'emma', { automatic: true, stage: 'save_draft' })));
  expect(screen.getByText('正在整理并保存回复…')).toBeInTheDocument();
  f.details.emma.reply_batches = [batch('sending')];
  act(() => { f.publish(f.job('succeeded', 'emma', { automatic: true, completed_at: Date.now(), result: { draft_id: 'generated-draft' } })); f.emit({ event: 'state.changed', data: {} }); });
  await waitFor(() => expect(screen.getByRole('status', { name: '自动回复进度' })).toHaveTextContent('正在提交第 1/2 条，等待平台回执'));
  const sending = screen.getByRole('status', { name: '自动回复进度' });
  expect(sending).toBe(container);
  f.details.emma.reply_batches = [batch('waiting_receipt')];
  act(() => f.emit({ event: 'state.changed', data: {} }));
  await screen.findByText('已发送 0/2 条，第 1 条发送结果待确认');
  expect(screen.getByRole('status', { name: '自动回复进度' })).toBe(container);
  expect(container).toHaveAttribute('aria-busy', 'false');
  expect(screen.queryByRole('button', { name: /核查原发送|发送记录/ })).not.toBeInTheDocument();
});

it('only displays success from accepted receipts, and expires old completion without history', () => {
  const f = createReplyActivityFixture();
  const send = f.job('succeeded', 'emma', { job_id: 'send-1', kind: 'automatic_send', automatic: true, created_at: stamp, completed_at: stamp, result: { status: 'accepted' } });
  expect(currentReplyProgress(f.details.emma, [send], stamp, true)?.phase).toBe('complete');
  expect(currentReplyProgress(f.details.emma, [send], stamp + 5000, true)).toBeNull();
  expect(currentReplyProgress(f.details.emma, [{ ...send, result: {} }], stamp, true)).toBeNull();
  f.details.emma.reply_batches = [batch('accepted')];
  expect(currentReplyProgress(f.details.emma, [], stamp, true)?.message).toBe('本轮回复已发送，共 2 条');
  expect(currentReplyProgress(f.details.emma, [], stamp + 5000, true)).toBeNull();
});

it('shows pending sends after pause, isolates customers and omits historical case batches', () => {
  const f = createReplyActivityFixture();
  f.details.emma.reply_batches = [batch('waiting_receipt')];
  expect(currentReplyProgress(f.details.emma, [], stamp, false)?.phase).toBe('unknown');
  expect(currentReplyProgress(f.details.sofia, [], stamp, true)).toBeNull();
  f.details.emma.case = { ...f.details.emma.case!, case_id: 'older-case' };
  expect(currentReplyProgress(f.details.emma, [], stamp, true)).toBeNull();
});

it('covers queued single sends, deferred retries and terminal stops without another submission', () => {
  const f = createReplyActivityFixture();
  const send: Job = f.job('queued', 'emma', { kind: 'automatic_send', automatic: true, created_at: stamp });
  expect(currentReplyProgress(f.details.emma, [send], stamp, true)?.phase).toBe('prepared');
  expect(currentReplyProgress(f.details.emma, [{ ...send, status: 'running' }], stamp, true)?.phase).toBe('sending');
  expect(currentReplyProgress(f.details.emma, [{ ...send, status: 'deferred' }], stamp, true)?.phase).toBe('retry');
  expect(currentReplyProgress(f.details.emma, [{ ...send, status: 'failed', completed_at: stamp }], stamp, true)?.phase).toBe('stopped');
});
