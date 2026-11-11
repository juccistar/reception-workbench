import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/renderer/App';
import type { Job, Outbox } from '../src/shared/api';
import { registrationSummary, receptionLabel } from '../src/renderer/receptionStatus';
import { createReplyActivityFixture } from './fixtures/replyActivity';

const activity = () => screen.queryByRole('status', { name: 'AI 回复生成状态' });
async function open(fixture = createReplyActivityFixture()) {
  render(<App bridge={fixture.bridge} />);
  await screen.findByRole('textbox', { name: '回复正文' });
  return fixture;
}
beforeEach(() => { Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true }); });

describe('seller-side reply activity', () => {
  it('shows a real queued/running job without inventing chat messages or sending', async () => {
    const f = await open();
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '生成回复建议' })).toHaveAttribute('aria-busy', 'true'));
    expect(activity()).not.toBeInTheDocument();
    act(() => f.publish(f.job('running', 'emma', { kind: 'manual_suggestion' })));
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeDisabled();
    const history = screen.getByLabelText('原始聊天记录');
    expect(history.querySelectorAll('[data-message-id]')).toHaveLength(1);
    expect(history.querySelectorAll('.seller-message')).toHaveLength(0);
    expect(screen.getByRole('textbox', { name: '回复正文' })).toHaveValue('');
    expect(f.calls.filter(call => /send|check_ai/.test(call.method))).toHaveLength(0);
  });

  it.each(['succeeded', 'failed', 'cancelled', 'stale', 'unknown', 'deferred'] as Job['status'][])('ends activity on %s and ignores a late running event', async status => {
    const f = await open();
    act(() => f.publish(f.job()));
    expect(activity()).toBeInTheDocument();
    act(() => f.publish(f.job(status)));
    expect(activity()).not.toBeInTheDocument();
    act(() => f.emit({ event: 'job.updated', data: f.job('running') }));
    expect(activity()).not.toBeInTheDocument();
    expect(screen.queryByText('回复已发送')).not.toBeInTheDocument();
  });

  it('isolates customers and cases and restores activity when returning to its customer', async () => {
    const f = await open();
    act(() => f.publish(f.job('running', 'sofia')));
    expect(activity()).not.toBeInTheDocument();
    act(() => f.publish(f.job('running', 'emma', { job_id: 'other-case', case_id: 'old-case' })));
    expect(activity()).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Sofia Martin/ }));
    await screen.findByRole('main', { name: 'Sofia Martin 的客户接待' });
    expect(activity()).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Emma Wilson/ }));
    await screen.findByRole('main', { name: 'Emma Wilson 的客户接待' });
    expect(activity()).not.toBeInTheDocument();
  });

  it('keeps another customer usable while the first generation RPC is pending', async () => {
    const f = createReplyActivityFixture();
    const invoke = f.bridge.invoke.bind(f.bridge);
    let finish!: (value: unknown) => void;
    f.bridge.invoke = async <T,>(method: Parameters<typeof invoke>[0], params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'generate_manual_suggestion') return new Promise(resolve => { finish = resolve as (value: unknown) => void; });
      return invoke<T>(method, params);
    };
    await open(f);
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: /Sofia Martin/ }));
    await screen.findByRole('main', { name: 'Sofia Martin 的客户接待' });
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeEnabled();
    expect(activity()).not.toBeInTheDocument();
    await act(async () => finish({ job_id: 'generation-emma' }));
  });

  it('retains an acknowledged generation if its status refresh fails, without offering a second submission', async () => {
    const f = createReplyActivityFixture();
    const invoke = f.bridge.invoke.bind(f.bridge);
    let submissions = 0;
    f.bridge.invoke = async <T,>(method: Parameters<typeof invoke>[0], params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'generate_manual_suggestion') { submissions++; return { job_id: 'generation-emma' } as T; }
      if (method === 'list_jobs') throw new Error('合成状态读取中断');
      return invoke<T>(method, params);
    };
    await open(f);
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await screen.findByText('生成任务已提交，正在确认状态…');
    expect(activity()?.querySelector('.reply-activity-dots')).toBeNull();
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    expect(submissions).toBe(1);
    act(() => f.publish(f.job('succeeded')));
    await waitFor(() => expect(activity()).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeEnabled();
  });

  it('does not regress a running task to queued when an old list response arrives', async () => {
    const f = await open();
    act(() => f.publish(f.job('running')));
    act(() => f.publish(f.job('queued')));
    expect(screen.getByText('AI 正在组织回复…')).toBeInTheDocument();
    expect(screen.queryByText('等待 AI 生成回复…')).not.toBeInTheDocument();
  });

  it('restores reading position after switching procurement cases of the same customer', async () => {
    const f = createReplyActivityFixture();
    f.details.emma.cases.push({ ...f.details.emma.case, case_id: 'old-case', procurement_key: '另一采购事项' });
    const invoke = f.bridge.invoke.bind(f.bridge);
    f.bridge.invoke = async <T,>(method: Parameters<typeof invoke>[0], params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'get_conversation' && params.case_id === 'old-case') await new Promise(resolve => setTimeout(resolve, 30));
      return invoke<T>(method, params);
    };
    await open(f);
    const history = screen.getByLabelText('原始聊天记录');
    Object.defineProperties(history, { scrollHeight: { value: 1200, configurable: true }, clientHeight: { value: 200, configurable: true } });
    history.scrollTop = 150; fireEvent.scroll(history);
    fireEvent.change(screen.getByRole('combobox', { name: '当前采购事项' }), { target: { value: 'old-case' } });
    await screen.findByText('正在打开客户会话');
    await screen.findByRole('combobox', { name: '当前采购事项' });
    expect(screen.getByLabelText('原始聊天记录').scrollTop).toBe(150);
  });

  it('removes current activity on manual editing and keeps the entered text', async () => {
    const f = await open();
    act(() => f.publish(f.job()));
    fireEvent.change(screen.getByRole('textbox', { name: '回复正文' }), { target: { value: 'My handwritten reply' } });
    await screen.findByRole('button', { name: '恢复此客户自动接待' });
    expect(activity()).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '回复正文' })).toHaveValue('My handwritten reply');
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeEnabled();
    expect(f.calls.filter(call => call.method === 'generate_manual_suggestion')).toHaveLength(0);
  });

  it.each(['backend.disconnected', 'backend.closing'])('stops normal activity after %s', async event => {
    const f = await open();
    act(() => f.publish(f.job()));
    expect(activity()).toBeInTheDocument();
    act(() => f.emit({ event, data: {} }));
    expect(activity()).not.toBeInTheDocument();
  });

  it.each(['source', 'revision'])('does not present an obsolete %s as the current reply', async change => {
    const f = await open();
    act(() => f.publish(f.job()));
    if (change === 'revision') f.details.emma.case.revision++;
    else f.details.emma.messages.push({ ...f.details.emma.messages[0], message_id: 'new-buyer', timestamp_ms: 1794034805000 });
    act(() => f.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(activity()).not.toBeInTheDocument());
  });

  it('does not clear a newer generation when an older job finishes', async () => {
    const f = await open();
    act(() => { f.publish(f.job()); f.publish(f.job('queued', 'emma', { job_id: 'newer', created_at: 1794034802000 })); });
    act(() => f.publish(f.job('succeeded')));
    expect(screen.getByText('等待 AI 生成回复…')).toBeInTheDocument();
  });

  it('allows explicit manual generation while globally paused but hides revoked automatic work', async () => {
    const f = await open();
    act(() => f.publish(f.job('running', 'emma', { automatic: true })));
    expect(activity()).not.toBeInTheDocument();
    act(() => f.publish(f.job('running', 'emma', { automatic: false })));
    expect(activity()).toBeInTheDocument();
  });

  it('preserves a reader’s scroll position when activity appears', async () => {
    const f = await open();
    const history = screen.getByLabelText('原始聊天记录');
    Object.defineProperties(history, { scrollHeight: { value: 1200, configurable: true }, clientHeight: { value: 200, configurable: true } });
    history.scrollTop = 150;
    fireEvent.scroll(history);
    act(() => f.publish(f.job()));
    expect(activity()).toBeInTheDocument();
    expect(history.scrollTop).toBe(150);
    expect(screen.queryByText('有新消息，查看最新')).not.toBeInTheDocument();
  });
});

describe('truthful reception and registration copy', () => {
  const receipt = (status: string, revision = 1, kind = 'case_upsert'): Outbox => ({ operation_id: `${kind}-${revision}-${status}`, case_id: 'case-emma', case_revision: revision, kind, status, message: '', updated_at: revision });
  it('never treats send receipts as registration and summarizes rather than repeats history', async () => {
    const f = createReplyActivityFixture();
    f.snapshot.outbox = Array.from({ length: 20 }, (_, index) => receipt('accepted', index, 'send'));
    await open(f);
    const sidebar = screen.getByRole('region', { name: '采购需求' });
    expect(within(sidebar).getByText('飞书：尚无登记记录')).toBeInTheDocument();
    expect(within(sidebar).queryByText(/平台已受理/)).not.toBeInTheDocument();
  });
  it('preserves old unknowns and distinguishes current versus earlier registration versions', () => {
    const current = { case_id: 'case-emma', revision: 3 };
    expect(registrationSummary([receipt('confirmed', 1), receipt('pending', 3)], current)).toBe('飞书：v3 待同步');
    expect(registrationSummary([receipt('confirmed', 1)], current)).toContain('当前 v3 尚未登记');
    expect(registrationSummary([receipt('unknown', 1), receipt('confirmed', 3)], current)).toBe('飞书：v1 登记待核查（当前 v3）');
  });
  it.each([['succeeded', '登记已确认'], ['syncing', '正在同步'], ['superseded', '已由新版本取代']])('projects the service outbox status %s', (status, label) => {
    expect(registrationSummary([receipt(status)], { case_id: 'case-emma', revision: 1 })).toBe(`飞书：v1 ${label}`);
  });
  it('does not infer customer eligibility from a global grant or an unfrozen all-scope list', () => {
    const f = createReplyActivityFixture(); f.snapshot.reception.running = true;
    expect(receptionLabel(f.snapshot, f.details.sofia, true, false)).toBe('此客户不在当前配置名单');
    f.snapshot.reception.scope.mode = 'all'; f.snapshot.reception.scope.conversation_ids = [];
    expect(receptionLabel(f.snapshot, f.details.sofia, true, false)).toBe('全局接待已开启 · 按授权条件处理');
    f.snapshot.reception.scope.mode = 'incoming';
    expect(receptionLabel(f.snapshot, f.details.sofia, true, false)).toContain('授权后新消息');
  });
  it('drops cached legacy handoff cards once their conversation leaves the current queue', async () => {
    const f = createReplyActivityFixture();
    f.details.emma.case.state = 'waiting_quote'; f.details.emma.conversation.state = 'waiting_quote';
    await open(f);
    fireEvent.click(screen.getByRole('button', { name: '待我报价' }));
    await waitFor(() => expect(document.querySelectorAll('.handoff-card')).toHaveLength(1));
    f.details.emma.conversation.state = 'closed';
    act(() => f.emit({ event: 'state.changed', data: {} }));
    await screen.findByText('暂时没有待报价事项');
    expect(document.querySelectorAll('.handoff-card')).toHaveLength(0);
  });
  it('distinguishes an unchecked AI capability from failure and keeps direct chat available', async () => {
    const f = createReplyActivityFixture(); f.snapshot.checks.ai = { ...f.snapshot.checks.ai, status: 'not_checked' };
    await open(f);
    expect(screen.getByText(/当前工作区尚未检查 AI 回复能力/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '回复正文' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '生成回复建议' })).toBeDisabled();
  });
});
