import { webcrypto } from 'node:crypto';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/renderer/App';
import type { ApiMethod, Case, ConversationDetail, ConversationSummary, Draft, Job, JobAck, Preferences, ReceptionBridge, SendConfirmation, ServiceEvent, Snapshot, Supplement } from '../src/shared/api';
import { validateChatParameters } from '../src/shared/chat-protocol';

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const makeConversation = (id: string, name: string): ConversationSummary => ({ conversation_id: id, buyer_id: `buyer-${id}`, customer_name: name, country: id === 'emma' ? '德国' : '', last_message: id === 'emma' ? 'Could you make a sample with our logo first?' : 'Do you have this mug in white?', last_timestamp: 1793950000000, case_id: `case-${id}`, state: 'clarifying', human_owned: false, unread_count: 1, pending_count: 1 });
const makeCase = (conversation: ConversationSummary): Case => ({ case_id: conversation.case_id, conversation_id: conversation.conversation_id, seller_id: 'demo-seller', buyer_id: conversation.buyer_id, customer_name: conversation.customer_name, procurement_key: '陶瓷杯采购', revision: 1, state: 'clarifying', facts: { product: { value: '陶瓷杯', status: 'confirmed', evidence_ids: [`message-${conversation.conversation_id}`] }, quantity: { value: '500 pieces', status: 'confirmed', evidence_ids: [`message-${conversation.conversation_id}`] } }, missing: ['destination'], questions: {}, summary: '陶瓷杯，定制 Logo 样品', updated_at: 1793950000000 });
const makeDraft = (id = 'emma', body = 'I will check the sample details for you.', revision = 1): Draft => ({ draft_id: `chat-${id}`, case_id: `case-${id}`, conversation_id: id, revision, body, status: 'pending', purpose: 'chat', source_message_id: null, source_case_revision: 1, source_conversation_revision: 1, origin: 'manual', created_at: 1793950000000, updated_at: 1793950000000, validation: { ready: true, issues: [], sources: [] } });
const makeDetail = (conversation: ConversationSummary): ConversationDetail => ({ conversation, case: makeCase(conversation), cases: [makeCase(conversation)], messages: [{ message_id: `message-${conversation.conversation_id}`, seller_id: 'demo-seller', buyer_id: conversation.buyer_id, conversation_id: conversation.conversation_id, role: 'buyer', text: conversation.last_message, timestamp_ms: 1793950000000, attachments: [], own: false }], history: { status: 'partial', earliest_ms: 1793950000000, latest_ms: 1793950000000, updated_at: 1793950000000, next_cursor: null, message: '较早历史尚未补全' }, draft: { ...makeDraft(conversation.conversation_id, 'Separate AI suggestion'), draft_id: `ai-${conversation.conversation_id}`, purpose: 'suggestion', origin: 'ai' }, chat_draft: makeDraft(conversation.conversation_id) });

class FakeBridge implements ReceptionBridge {
  listeners = new Set<(event: ServiceEvent) => void>();
  calls: { method: ApiMethod; params: Record<string, unknown> }[] = [];
  conversations = [makeConversation('emma', 'Emma'), makeConversation('sofia', 'Sofia')];
  details: Record<string, ConversationDetail> = Object.fromEntries(this.conversations.map(item => [item.conversation_id, makeDetail(item)]));
  preferences: Preferences = { nav_preference: 'expanded', details_preference: 'expanded', list_width: 272, details_width: 320 };
  snapshot: Snapshot;
  saveHandler?: (params: Record<string, unknown>) => Promise<Draft>;
  beginHandler?: (params: Record<string, unknown>) => Promise<Snapshot>;
  sendHandler?: (params: Record<string, unknown>) => Promise<JobAck>;
  detailHandler?: (params: Record<string, unknown>) => Promise<ConversationDetail>;
  reconcileHandler?: (params: Record<string, unknown>) => Promise<{ status?: string; message?: string; job_id?: string }>;
  requests = new Map<string, { params: Record<string, unknown>; ack: JobAck }>();
  confirmSend = vi.fn(async (_confirmation: SendConfirmation): Promise<JobAck | null> => ({ job_id: 'legacy-send' }));
  constructor() {
    const check = { status: 'demo' as const, check_id: 'demo-check', checked_at: 1793950000000, simulated: true, message: '本地演示检查' };
    this.snapshot = { app: { mode: 'demo', version: '0.1.2', seller_id: 'demo-seller', seller_suffix: 'DEMO', account_verified: false }, reception: { running: false, auto_send: false, started_at_ms: null, scope: { mode: 'selected', conversation_ids: [], max_auto_replies: 10 }, budget: { used: 0, limit: 10, remaining: 10, ai_used: 0, ai_remaining: 10 } }, checks: { runtime: check, identity: check, messages: check, ai: check, send_preview: check, feishu: check }, conversations: this.conversations, jobs: [], policies: [], outbox: [], settings: { workctl_path: '', poll_seconds: 15, merge_seconds: 5, required_fields: ['product', 'quantity'], feishu: {} } };
  }
  own(id: string, enabled = true) {
    this.details[id].conversation.human_owned = enabled;
    this.snapshot.conversations.find(item => item.conversation_id === id)!.human_owned = enabled;
    return clone(this.snapshot);
  }
  enqueue(params: Record<string, unknown>): JobAck {
    const key = String(params.client_request_id);
    const existing = this.requests.get(key);
    if (existing) {
      if (JSON.stringify(existing.params) !== JSON.stringify(params)) throw new Error('同一请求编号参数不一致');
      return clone(existing.ack);
    }
    const draft = this.details[String(params.conversation_id)].chat_draft!;
    if (draft.draft_id !== params.draft_id || draft.revision !== params.expected_revision) throw new Error('版本不一致');
    const ack = { job_id: `chat-job-${this.requests.size + 1}` };
    this.requests.set(key, { params: clone(params), ack });
    draft.status = 'sending';
    this.snapshot.jobs.push({ job_id: ack.job_id, kind: 'send_chat', conversation_id: draft.conversation_id, case_id: draft.case_id, status: 'queued', stage: 'queued', message: '等待发送执行', created_at: Date.now(), started_at: null, completed_at: null });
    return ack;
  }
  finish(jobId: string, status: 'accepted' | 'unknown' | 'rejected' = 'accepted') {
    const job = this.snapshot.jobs.find(item => item.job_id === jobId)!;
    job.status = status === 'accepted' ? 'succeeded' : status === 'unknown' ? 'unknown' : 'failed';
    job.completed_at = Date.now(); job.message = status === 'accepted' ? '已取得发送回执' : '需要核查发送';
    const detail = this.details[job.conversation_id!];
    detail.chat_draft!.status = status;
    const request = [...this.requests.values()].find(item => item.ack.job_id === jobId)!;
    this.snapshot.outbox.push({ operation_id: `operation-${jobId}`, kind: 'send', status, case_id: job.case_id!, case_revision: 1, conversation_id: job.conversation_id!, draft_id: detail.chat_draft!.draft_id, draft_revision: Number(request.params.expected_revision), message: status === 'accepted' ? '平台受理此条' : '等待原操作核查', updated_at: Date.now(), message_id: status === 'accepted' ? `message-${jobId}` : null });
    this.emit({ event: 'job.updated', data: clone(job) });
  }
  async invoke<T = unknown>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> {
    this.calls.push({ method, params: clone(params) });
    if (method === 'get_snapshot') return clone(this.snapshot) as T;
    if (method === 'get_conversation') {
      if (this.detailHandler) return await this.detailHandler(params) as T;
      const detail = clone(this.details[String(params.conversation_id)]);
      if (params.case_id) detail.case = detail.cases.find(item => item.case_id === params.case_id)!;
      return detail as T;
    }
    if (method === 'get_translations') return { items: (params.items as { kind: string; source_id: string; field?: string }[]).map(item => ({ ...item, status: 'ready', text: null, error: null })) } as T;
    if (method === 'save_conversation_supplement') { const id = String(params.conversation_id); const result = { conversation_id: id, body: String(params.body), revision: Number(params.expected_revision ?? 0) + 1, updated_at: Date.now() }; this.details[id].supplement = result; return clone(result) as T; }
    if (method === 'generate_manual_suggestion') { const id = String(params.conversation_id); const jobId = 'manual-job-' + String(params.client_request_id); this.details[id].manual_suggestion = { suggestion_id: 'suggestion-' + String(params.client_request_id), conversation_id: id, case_id: params.case_id as string | null, body: 'New suggested reply', request_id: String(params.client_request_id), context_fingerprint: 'test', status: 'ready', updated_at: Date.now() }; this.snapshot.jobs.push({ job_id: jobId, kind: 'manual_suggestion', conversation_id: id, case_id: params.case_id as string | null, status: 'succeeded', stage: 'completed', message: '合成建议已完成', created_at: Date.now(), started_at: Date.now(), completed_at: Date.now() }); return { job_id: jobId } as T; }
    if (method === 'list_jobs') return clone(this.snapshot.jobs) as T;
    if (method === 'list_outbox') return clone(this.snapshot.outbox) as T;
    if (method === 'begin_manual_edit') return (this.beginHandler ? await this.beginHandler(params) : this.own(String(params.conversation_id))) as T;
    if (method === 'takeover') return this.own(String(params.conversation_id), params.enabled === true) as T;
    if (method === 'send_chat') return (this.sendHandler ? await this.sendHandler(params) : this.enqueue(params)) as T;
    if (method === 'set_scope') {
      this.snapshot.reception.scope = clone(params) as unknown as Snapshot['reception']['scope'];
      const budget = this.snapshot.reception.budget;
      budget.limit = Number(params.max_auto_replies); budget.remaining = Math.max(0, budget.limit - budget.used); budget.ai_remaining = Math.max(0, budget.limit - budget.ai_used);
      this.snapshot.reception.running = false; this.snapshot.reception.auto_send = false; return clone(this.snapshot) as T;
    }
    if (method === 'set_reception') {
      this.snapshot.reception.running = params.enabled === true;
      this.snapshot.reception.auto_send = params.enabled === true && params.auto_send === true;
      if (params.enabled === true && this.snapshot.reception.started_at_ms === null) this.snapshot.reception.started_at_ms = Date.now();
      return clone(this.snapshot) as T;
    }
    if (method === 'save_chat_draft') {
      if (this.saveHandler) return await this.saveHandler(params) as T;
      const id = String(params.conversation_id);
      const result = makeDraft(id, String(params.body), Number(params.expected_revision ?? 0) + 1);
      const previous = this.details[id].chat_draft;
      const linked = previous?.blocked_batch_id ?? previous?.batch_id;
      if (linked && !params.remaining_batch_id) { result.blocked_batch_id = linked; result.blocked_accepted_parts = previous?.blocked_accepted_parts; }
      if (Array.isArray(params.message_parts)) result.message_parts = [...params.message_parts] as string[];
      this.details[id].chat_draft = result; return clone(result) as T;
    }
    if (method === 'reconcile_operation') return (this.reconcileHandler ? await this.reconcileHandler(params) : { status: 'unknown', message: '合成操作尚无权威回执' }) as T;
    if (method === 'get_messages') return { items: [], has_more: false } as T;
    return { job_id: `job-${method}` } as T;
  }
  onEvent(listener: (event: ServiceEvent) => void) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  async getPreferences() { return clone(this.preferences); }
  async savePreferences(value: Partial<Preferences>) { this.preferences = { ...this.preferences, ...value }; return clone(this.preferences); }
  async chooseWorkctl() { return null; }
  async chooseSupplementAttachment(_conversationId: string): Promise<Supplement | null> { return null; }
  emit(event: ServiceEvent) { for (const listener of this.listeners) listener(event); }
}

const failedAiBridge = () => {
  const bridge = new FakeBridge(); bridge.snapshot.app.mode = 'live'; bridge.snapshot.app.account_verified = true;
  for (const key of ['runtime', 'identity', 'messages'] as const) bridge.snapshot.checks[key] = { ...bridge.snapshot.checks[key], status: 'passed', simulated: false };
  bridge.snapshot.checks.ai = { ...bridge.snapshot.checks.ai, status: 'failed', simulated: false };
  bridge.snapshot.checks.send_preview = { ...bridge.snapshot.checks.send_preview, status: 'not_checked', simulated: false };
  return bridge;
};
const openApp = async (bridge = new FakeBridge()) => { render(<App bridge={bridge} />); await screen.findByRole('textbox', { name: '回复正文' }, { timeout: 5000 }); return bridge; };
const textarea = () => screen.getByRole('textbox', { name: '回复正文' });
const sendButton = () => screen.getByRole('button', { name: '发送', exact: true });
const calls = (bridge: FakeBridge, method: ApiMethod) => bridge.calls.filter(call => call.method === method);
const exposeMessagesInViewport = () => vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ top: 0, bottom: 400, height: 400, left: 0, right: 500, width: 500, x: 0, y: 0, toJSON: () => ({}) });
const edit = async (body: string) => { fireEvent.change(textarea(), { target: { value: body } }); await waitFor(() => expect(sendButton()).toBeEnabled()); };
const noLegacySend = (bridge: FakeBridge) => { expect(bridge.confirmSend).not.toHaveBeenCalled(); expect(bridge.calls.some(call => ['prepare_send', 'confirm_send', 'save_draft', 'preview_send', 'check_ai', 'generate_draft'].includes(call.method))).toBe(false); };

beforeEach(() => { localStorage.clear(); Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1500 }); });

describe('0.3.15 compact workspace', () => {
  const ready = (bridge: FakeBridge, id = 'pending-0315') => {
    bridge.details.emma.manual_suggestion = { suggestion_id: id, conversation_id: 'emma', case_id: 'case-emma', body: 'A synthetic reply for review.', request_id: id, context_fingerprint: 'synthetic', status: 'ready', updated_at: 1 };
  };
  it('adopts ordered suggestion parts, edits, deletes and merges them', async () => {
    const bridge = new FakeBridge(); ready(bridge);
    bridge.details.emma.manual_suggestion!.body = 'first\n\nsecond';
    bridge.details.emma.manual_suggestion!.reply_messages = [{ body: 'first', reply_sources: [] }, { body: 'second', reply_sources: [] }];
    await openApp(bridge);
    expect(screen.getByRole('region', { name: '待处理回复建议' })).toHaveTextContent('1. first');
    fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
    expect(await screen.findByRole('textbox', { name: '第 1 条回复正文' })).toHaveValue('first');
    fireEvent.change(screen.getByRole('textbox', { name: '第 2 条回复正文' }), { target: { value: 'changed' } });
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => JSON.stringify(call.params.message_parts) === JSON.stringify(['first', 'changed']))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: '删除第 1 条' }));
    expect(screen.getByRole('textbox', { name: '第 1 条回复正文' })).toHaveValue('changed');
    fireEvent.click(screen.getByRole('button', { name: '合为一条' }));
    expect(textarea()).toHaveValue('changed');
    expect(screen.queryByRole('region', { name: '待处理回复建议' })).not.toBeInTheDocument();
  });
  it('preserves later edits when an earlier reply batch completes', async () => {
    const bridge = new FakeBridge(); ready(bridge);
    bridge.details.emma.manual_suggestion!.body = 'first\n\nsecond';
    bridge.details.emma.manual_suggestion!.reply_messages = [{ body: 'first', reply_sources: [] }, { body: 'second', reply_sources: [] }];
    bridge.sendHandler = async params => {
      const ack = bridge.enqueue(params);
      bridge.details.emma.reply_batches = [{ batch_id: 'batch-1', status: 'sending', total: 2, accepted: 0, current_index: 0, pending_operation_ids: [] }];
      return { ...ack, batch_id: 'batch-1' };
    };
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
    await screen.findByRole('textbox', { name: '第 2 条回复正文' });
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => Array.isArray(call.params.message_parts))).toBe(true));
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    fireEvent.change(screen.getByRole('textbox', { name: '第 2 条回复正文' }), { target: { value: 'later input' } });
    bridge.details.emma.reply_batches = [{ batch_id: 'batch-1', status: 'accepted', total: 2, accepted: 2, current_index: null, pending_operation_ids: [] }];
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await screen.findByText('已发送 2/2 条');
    expect(screen.getByRole('textbox', { name: '第 2 条回复正文' })).toHaveValue('later input');
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => call.params.body === 'first\n\nlater input')).toBe(true));
  });
  it('requires an explicit remaining-only draft after partial acceptance', async () => {
    const bridge = new FakeBridge(); ready(bridge);
    bridge.details.emma.manual_suggestion!.body = 'first\n\nsecond';
    bridge.details.emma.manual_suggestion!.reply_messages = [{ body: 'first', reply_sources: [] }, { body: 'second', reply_sources: [] }];
    bridge.sendHandler = async params => {
      const ack = bridge.enqueue(params);
      if (bridge.requests.size === 1) {
        bridge.details.emma.chat_draft!.batch_id = 'batch-partial';
        bridge.details.emma.reply_batches = [{ batch_id: 'batch-partial', status: 'sending', total: 2, accepted: 0, current_index: 0, pending_operation_ids: [] }];
        return { ...ack, batch_id: 'batch-partial' };
      }
      return ack;
    };
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
    await screen.findByRole('textbox', { name: '第 2 条回复正文' });
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => Array.isArray(call.params.message_parts))).toBe(true));
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    fireEvent.change(screen.getByRole('textbox', { name: '第 2 条回复正文' }), { target: { value: 'new second' } });
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => call.params.body === 'first\n\nnew second')).toBe(true));
    bridge.details.emma.chat_draft!.blocked_batch_id = 'batch-partial';
    bridge.details.emma.chat_draft!.blocked_accepted_parts = ['first'];
    bridge.details.emma.reply_batches = [{ batch_id: 'batch-partial', status: 'stopped', total: 2, accepted: 1, current_index: 1, pending_operation_ids: [] }];
    bridge.snapshot.jobs[0].status = 'failed';
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await screen.findByRole('button', { name: '只保留未发送内容' });
    expect(sendButton()).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '第 2 条回复正文' })).toHaveValue('new second');
    fireEvent.click(screen.getByRole('button', { name: '只保留未发送内容' }));
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => call.params.remaining_batch_id === 'batch-partial' && call.params.body === 'new second')).toBe(true));
    expect(screen.getByRole('textbox', { name: '第 1 条回复正文' })).toHaveValue('new second');
    await waitFor(() => expect(sendButton()).toBeEnabled());
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(2));
    expect(bridge.details.emma.chat_draft!.body).toBe('new second');
  });
  it('polishes only the selected saved part and rejects a stale replacement', async () => {
    const originalSubtle = Object.getOwnPropertyDescriptor(globalThis.crypto, 'subtle');
    Object.defineProperty(globalThis.crypto, 'subtle', { configurable: true, value: webcrypto.subtle });
    try {
      const bridge = new FakeBridge(); ready(bridge);
      bridge.details.emma.manual_suggestion!.body = 'first\n\nsecond';
      bridge.details.emma.manual_suggestion!.reply_messages = [{ body: 'first', reply_sources: [] }, { body: 'second', reply_sources: [] }];
      await openApp(bridge);
      fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
      const second = await screen.findByRole('textbox', { name: '第 2 条回复正文' });
      fireEvent.focus(second);
      await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => Array.isArray(call.params.message_parts))).toBe(true));
      fireEvent.click(screen.getByRole('button', { name: '润色当前回复' }));
      await waitFor(() => expect(calls(bridge, 'generate_manual_suggestion')).toHaveLength(1));
      const params = calls(bridge, 'generate_manual_suggestion')[0].params;
      expect(params).toMatchObject({ input_body: 'second', part_index: 1 });
      bridge.details.emma.manual_suggestion = { suggestion_id: 'part-result', conversation_id: 'emma', case_id: 'case-emma', body: 'polished', request_id: 'part-result', context_fingerprint: 'synthetic', status: 'ready', updated_at: 1, mode: 'polish', input_part_index: 1, input_revision: params.input_revision as number, input_hash: params.input_hash as string };
      act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
      await screen.findByText('polished');
      fireEvent.change(second, { target: { value: 'newer edit' } });
      fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
      expect(screen.getByRole('textbox', { name: '第 2 条回复正文' })).toHaveValue('newer edit');
      expect(screen.getByRole('textbox', { name: '第 1 条回复正文' })).toHaveValue('first');
    } finally {
      if (originalSubtle) Object.defineProperty(globalThis.crypto, 'subtle', originalSubtle);
      else Reflect.deleteProperty(globalThis.crypto, 'subtle');
    }
  });
  it('shows full local timestamps above messages without date separators', async () => {
    const bridge = new FakeBridge();
    bridge.details.emma.messages[0].timestamp_ms = new Date(2026, 0, 2, 3, 4, 5).getTime();
    await openApp(bridge);
    const header = document.querySelector('.message-meta');
    expect(header).toHaveTextContent('2026-01-02 03:04:05');
    expect(document.querySelector('.date-separator')).toBeNull();
  });
  it('shows pending suggestions directly and removes duplicate composer and metadata rows', async () => {
    const bridge = new FakeBridge(); ready(bridge); await openApp(bridge);
    expect(screen.getByRole('region', { name: '待处理回复建议' })).toHaveTextContent('A synthetic reply for review.');
    expect(screen.queryByRole('button', { name: '查看建议' })).not.toBeInTheDocument();
    expect(screen.queryByText('回复客户', { exact: true })).not.toBeInTheDocument();
    expect(document.querySelector('.history-bar, .translation-toggle, .suggestion-banner, .composer-source')).toBeNull();
    expect(screen.getByRole('checkbox', { name: '自动中文翻译' })).toBeInTheDocument();
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
  });
  it('adopts once without sending and does not reopen the handled suggestion on customer return', async () => {
    const bridge = new FakeBridge(); ready(bridge); await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
    expect(textarea()).toHaveValue('A synthetic reply for review.');
    expect(screen.queryByRole('region', { name: '待处理回复建议' })).not.toBeInTheDocument();
    await waitFor(() => expect(calls(bridge, 'begin_manual_edit')).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByRole('main', { name: 'Sofia 的客户接待' });
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await screen.findByRole('main', { name: 'Emma 的客户接待' });
    expect(screen.queryByRole('region', { name: '待处理回复建议' })).not.toBeInTheDocument();
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
  });
  it('keeps dismissal across remount and shows a newly generated suggestion', async () => {
    const bridge = new FakeBridge(); ready(bridge);
    const view = render(<App bridge={bridge} />); await screen.findByRole('textbox', { name: '回复正文' });
    const original = (textarea() as HTMLTextAreaElement).value;
    fireEvent.click(screen.getByRole('button', { name: '弃用' })); view.unmount();
    await openApp(bridge);
    expect(screen.queryByRole('region', { name: '待处理回复建议' })).not.toBeInTheDocument();
    ready(bridge, 'new-pending-0315'); act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await screen.findByRole('region', { name: '待处理回复建议' });
    expect(textarea()).toHaveValue(original);
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
  });
  it('shows five local gallery images during legacy completion and supports switching', async () => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case!;
    current.requirements_format = 'ai-v1'; current.requirements = { product_id: { value: '123', evidence_ids: [] } };
    current.product_main_image = { status: 'loading', local_product_id: 'p', updated_at: 1 }; bridge.details.emma.cases[0] = clone(current);
    const original = bridge.invoke.bind(bridge);
    bridge.invoke = vi.fn(async (method, params = {}) => method === 'get_product_main_image' ? { status: 'loading', message: '不展示的后台说明', images: [0,1,2,3,4].map(() => ({status:'ready',data_url:'data:image/png;base64,AAAA'})) } as never : original(method, params));
    await openApp(bridge);
    await screen.findByRole('button', { name: '放大查看商品图片 5' });
    expect(document.querySelectorAll('.product-image-gallery img')).toHaveLength(5);
    expect(screen.queryByText('不展示的后台说明')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '放大查看商品图片 1' }));
    fireEvent.click(screen.getByRole('button', { name: '下一张商品图片' }));
    expect(screen.getByRole('dialog', { name: '商品图片' })).toHaveTextContent('2 / 5');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '商品图片' })).not.toBeInTheDocument();
  });
  it('uploads paste and drop files through the restricted bridge while ordinary text keeps normal paste', async () => {
    const bridge = new FakeBridge();
    const upload = vi.fn(async (conversationId: string, files: { name: string; content_b64: string }[]) => {
      const previous = bridge.details[conversationId].supplement;
      const supplement = { conversation_id: conversationId, body: previous?.body ?? '', revision: (previous?.revision ?? 0) + 1, updated_at: 1, attachments: [...previous?.attachments ?? [], ...files.map((file, index) => ({id:file.name+index,name:file.name,media_type:'text/plain',size:5,sha256:'hash',status:'ready',origin:'business_supplement' as const}))] };
      bridge.details[conversationId].supplement = supplement;
      return { supplement, errors: [] };
    });
    const nativePaste = vi.fn(async () => ({ supplement: null, errors: [] }));
    Object.assign(bridge, { uploadSupplementAttachments: upload, pasteSupplementAttachments: nativePaste });
    await openApp(bridge); const note = screen.getByRole('textbox', { name: '业务补充内容' });
    fireEvent.paste(note, { clipboardData: { files: [new File(['hello'], 'pasted.txt', {type:'text/plain'})], getData: () => '' } });
    await screen.findByText('pasted.txt');
    expect(upload.mock.calls[0][0]).toBe('emma'); expect(upload.mock.calls[0][1][0]).toMatchObject({name:'pasted.txt',content_b64:'aGVsbG8='});
    fireEvent.drop(document.querySelector('.supplement-composer')!, { dataTransfer: { files: [new File(['world'], 'dropped.txt', {type:'text/plain'})], types:['Files'] } });
    await screen.findByText('dropped.txt');
    fireEvent.paste(note, { clipboardData: { files: [], getData: () => '普通文字' } });
    expect(upload).toHaveBeenCalledTimes(2); expect(nativePaste).not.toHaveBeenCalled();
    fireEvent.paste(note, { clipboardData: { files: [], getData: () => '' } });
    await waitFor(() => expect(nativePaste).toHaveBeenCalledWith('emma'));
  });
  it('keeps valid dropped files when a neighbouring format is rejected', async () => {
    const bridge = new FakeBridge();
    const upload = vi.fn(async () => ({ supplement: { conversation_id: 'emma', body: '', revision: 1, updated_at: 1, attachments: [] }, errors: [] }));
    Object.assign(bridge, { uploadSupplementAttachments: upload }); await openApp(bridge);
    fireEvent.drop(document.querySelector('.supplement-composer')!, { dataTransfer: { files: [new File(['invalid'], 'bad.exe'), new File(['hello'], 'valid.txt')], types: ['Files'] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith('emma', [{ name: 'valid.txt', content_b64: 'aGVsbG8=' }]));
    expect(await screen.findByText(/附件格式不支持/)).toBeInTheDocument();
  });
});

describe('mounted requirement workspace', () => {
  it('shows only the thirteen AI demand fields and hides legacy reminders and metadata', async () => {
    const bridge = new FakeBridge();
    const values = { product: '陶瓷杯', product_id: '123', product_url: 'https://example.com/product', product_images: ['img-1'], quantity: '500件', specifications: '白色', customization: '印标', destination: '法国', delivery: '30天', budget: '2美元', packaging: '纸盒', certifications: 'CE', contact: '邮件' };
    const current = bridge.details.emma.case;
    current.requirements_format = 'ai-v1';
    current.requirements = Object.fromEntries(Object.entries({ ...values, supplier: '不应出现' }).map(([key, value]) => [key, { value, evidence_ids: ['secret-evidence'], display_value: '旧译文' }]));
    bridge.details.emma.cases[0] = clone(current);
    bridge.details.emma.conversation.business_progress = { state: 'needs_input', label: '旧提醒', reason: '旧原因', issues: [], updated_at: 1 };
    const original = bridge.invoke.bind(bridge);
    bridge.invoke = vi.fn(async (method, params = {}) => method === 'get_requirement_media' ? { case_id: 'case-emma', revision: 0, cover_id: null, items: [{ media_id: 'img-1', status: 'ready', data_url: 'https://example.com/unsafe.png' }] } as never : original(method, params));
    await openApp(bridge);
    const panel = screen.getByRole('region', { name: '采购需求' });
    expect(panel.querySelectorAll('.canonical-field')).toHaveLength(13);
    for (const value of ['陶瓷杯', '123', '500件', '白色', '法国']) expect(within(panel).getByText(value)).toBeInTheDocument();
    for (const text of ['旧译文', '不应出现', 'secret-evidence', '旧提醒', '旧原因']) expect(screen.queryByText(text)).not.toBeInTheDocument();
    await waitFor(() => expect(bridge.invoke).toHaveBeenCalledWith('get_requirement_media', { case_id: 'case-emma' }));
    expect(panel.querySelector('img[src^="https:"]')).toBeNull();
  });
  it('renders a local product main image separately from AI-selected customer images', async () => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case!;
    current.requirements_format = 'ai-v1'; current.requirements = { product: { value: '棉帽', evidence_ids: [] }, product_id: { value: '123', evidence_ids: [] }, product_url: { value: 'https://www.alibaba.com/product-detail/123.html', evidence_ids: [] }, product_images: { value: ['customer'], evidence_ids: [] } };
    current.local_product_id = 'selected-other-product'; current.product_main_image = { status: 'ready', local_product_id: 'product-123', updated_at: 1 };
    bridge.details.emma.cases[0] = clone(current);
    const original = bridge.invoke.bind(bridge);
    bridge.invoke = vi.fn(async (method, params = {}) => method === 'get_product_main_image' ? { status: 'ready', data_url: 'data:image/png;base64,AAAA' } as never : method === 'get_requirement_media' ? { case_id: 'case-emma', revision: 1, cover_id: null, items: [{ media_id: 'customer', status: 'ready', data_url: 'data:image/png;base64,BBBB' }] } as never : original(method, params));
    await openApp(bridge);
    const main = await screen.findByRole('button', { name: '放大查看商品图片 1' });
    expect(screen.getByText('商品图片')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '放大查看产品图片' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'https://www.alibaba.com/product-detail/123.html' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '在浏览器打开产品链接' })).toBeInTheDocument();
    expect(bridge.invoke).toHaveBeenCalledWith('get_product_main_image', { local_product_id: 'product-123' });
    fireEvent.click(main); expect(screen.getByRole('dialog', { name: '商品图片' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' }); expect(main).toHaveFocus();
  });
  it('copies a product field through the host and ignores a late result after changing customer', async () => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case!;
    current.requirements_format = 'ai-v1'; current.requirements = { product_id: { value: '123', evidence_ids: [] }, product_url: { value: 'https://example.com/product', evidence_ids: [] } };
    bridge.details.emma.cases[0] = clone(current);
    let finish!: () => void;
    const copyProductText = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    Object.assign(bridge, { copyProductText });
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '复制产品ID' }));
    expect(copyProductText).toHaveBeenCalledWith({ field: 'product_id', case_id: 'case-emma' });
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByRole('main', { name: 'Sofia 的客户接待' });
    await act(async () => finish());
    expect(screen.queryByText('产品ID已复制')).not.toBeInTheDocument();
  });
  it('does not fabricate a URL or trust a remote main image for a custom ID', async () => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case!;
    current.requirements_format = 'ai-v1'; current.requirements = { product: { value: '定制帽', evidence_ids: [] }, product_id: { value: '定制-6262662', evidence_ids: [] }, product_url: { value: null, evidence_ids: [] } };
    current.local_product_id = 'custom-product'; current.product_main_image = { status: 'ready', local_product_id: 'other-platform-product' };
    bridge.details.emma.cases[0] = clone(current);
    const original = bridge.invoke.bind(bridge);
    bridge.invoke = vi.fn(async (method, params = {}) => method === 'get_product_main_image' ? { status: 'ready', data_url: 'https://example.com/unsafe.png' } as never : original(method, params));
    await openApp(bridge);
    expect(bridge.invoke).not.toHaveBeenCalledWith('get_product_main_image', expect.anything());
    const panel = screen.getByRole('region', { name: '采购需求' });
    expect(within(panel).getByText('定制-6262662')).toBeInTheDocument();
    expect(within(panel).getByText('无平台主图')).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: '复制产品链接' })).not.toBeInTheDocument();
    expect(panel.querySelector('img[src^="https:"]')).toBeNull();
  });
  it.each([['loading', '正在读取商品图片'], ['not_loaded', '尚未读取商品图片'], ['unavailable', '暂无商品图片'], ['failed', '商品图片读取失败'], ['unknown', '商品图片读取未完成']] as const)('shows product image %s without requesting it in the renderer', async (status, label) => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case!;
    current.requirements_format = 'ai-v1'; current.requirements = { product_id: { value: '123', evidence_ids: [] } };
    current.product_main_image = { status }; bridge.details.emma.cases[0] = clone(current);
    await openApp(bridge);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(calls(bridge, 'get_requirement_media')).toHaveLength(0);
    expect(calls(bridge, 'load_requirement_media')).toHaveLength(0);
  });
  it('shows only an AI-selected local image and supports keyboard close', async () => {
    const bridge = new FakeBridge(); const current = bridge.details.emma.case;
    current.requirements_format = 'ai-v1'; current.requirements = { product_images: { value: ['approved'], evidence_ids: ['message-emma'] } };
    bridge.details.emma.cases[0] = clone(current);
    const original = bridge.invoke.bind(bridge);
    bridge.invoke = vi.fn(async (method, params = {}) => method === 'get_requirement_media' ? { case_id: 'case-emma', revision: 1, cover_id: null, items: [{ media_id: 'approved', status: 'ready', data_url: 'data:image/png;base64,AAAA' }, { media_id: 'other', status: 'ready', data_url: 'data:image/png;base64,BBBB' }] } as never : original(method, params));
    await openApp(bridge);
    const preview = await screen.findByRole('button', { name: '放大查看产品图片' });
    expect(screen.getAllByRole('img', { name: '产品图片' })).toHaveLength(1);
    fireEvent.click(preview);
    expect(screen.getByRole('dialog', { name: '产品图片' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '产品图片' })).not.toBeInTheDocument();
    expect(preview).toHaveFocus();
  });
  it('keeps the conversation summary when switching cases and shows it without a demand', async () => {
    const bridge = new FakeBridge();
    bridge.details.emma.conversation_summary = { conversation_id: 'emma', body: '客户询问目录，需要确认目录链接。', revision: 1, updated_at: 1 };
    bridge.details.emma.case.summary = '不显示的旧摘要';
    bridge.details.emma.cases.push({ ...clone(bridge.details.emma.case), case_id: 'another-case', summary: '另一旧摘要' });
    await openApp(bridge);
    expect(screen.getByLabelText('会话摘要')).toHaveTextContent('客户询问目录，需要确认目录链接。');
    expect(screen.getByText('当前没有采购需求')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: '当前采购事项' }), { target: { value: 'another-case' } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue('another-case'));
    expect(screen.getByLabelText('会话摘要')).toHaveTextContent('客户询问目录，需要确认目录链接。');
    expect(screen.queryByText('另一旧摘要')).not.toBeInTheDocument();
  });
  it('renders the conversation summary when no case exists', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.conversations[0].case_id = null; bridge.details.emma.conversation.case_id = null;
    bridge.details.emma.case = null; bridge.details.emma.cases = [];
    const status = { state: 'blocked', reason: '当前客户事项或归属不可用', owner: 'none' as const, activity: null, blockers: [{ code: 'case', message: '当前客户事项或归属不可用' }], observed_at: 1 };
    bridge.details.emma.conversation.reception_status = status; bridge.snapshot.conversations[0].reception_status = status;
    bridge.details.emma.conversation_summary = { conversation_id: 'emma', body: '客户已收货，询问目录。', revision: 1, updated_at: 1 };
    await openApp(bridge);
    expect(screen.getByLabelText('会话摘要')).toHaveTextContent('客户已收货，询问目录。');
    expect(screen.getByText('当前没有采购需求')).toBeInTheDocument();
    expect(screen.queryByText('当前客户事项或归属不可用')).not.toBeInTheDocument();
    expect(screen.queryByText('查看暂未回复的原因')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('业务补充内容'), { target: { value: '目录链接稍后补充' } });
    await edit('Here is the catalogue');
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    await waitFor(() => expect(calls(bridge, 'save_conversation_supplement')).toHaveLength(1));
    expect(calls(bridge, 'save_chat_draft')[0].params).toMatchObject({ conversation_id: 'emma', body: 'Here is the catalogue' });
  });
  it('does not render legacy assistance counts or fact values', async () => {
    const bridge = new FakeBridge(); bridge.snapshot.conversations[0].assistance_open_count = 5;
    await openApp(bridge);
    expect(screen.getByText('当前没有采购需求')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '待你补充' })).not.toBeInTheDocument();
    expect(screen.queryByText('500 pieces')).not.toBeInTheDocument();
  });
});

describe('manual suggestion and supplement', () => {
  it('keeps an attachment with its original customer when selection changes', async () => {
    const bridge = new FakeBridge();
    const pending = deferred<Supplement | null>();
    vi.spyOn(bridge, 'chooseSupplementAttachment').mockReturnValue(pending.promise);
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '添加附件' }));
    await waitFor(() => expect(bridge.chooseSupplementAttachment).toHaveBeenCalledWith('emma'));
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByRole('main', { name: 'Sofia 的客户接待' });
    const saved: Supplement = { conversation_id: 'emma', body: '', revision: 1, updated_at: 1,
      attachments: [{ id: 'a1', name: 'emma-file.txt', media_type: 'text/plain', size: 5, sha256: 'hash', status: 'ready', origin: 'business_supplement' }] };
    bridge.details.emma.supplement = saved;
    await act(async () => pending.resolve(saved));
    expect(screen.queryByText(/emma-file.txt/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await screen.findByRole('main', { name: 'Emma 的客户接待' });
    expect(await screen.findByText(/emma-file.txt/)).toBeInTheDocument();
  });
  it('keeps new note edits while the selected customer attachment is being added', async () => {
    const bridge = new FakeBridge();
    const pending = deferred<Supplement | null>();
    const choose = vi.spyOn(bridge, 'chooseSupplementAttachment').mockReturnValue(pending.promise);
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '添加附件' }));
    await waitFor(() => expect(choose).toHaveBeenCalledWith('emma'));
    fireEvent.change(screen.getByRole('textbox', { name: '业务补充内容' }), { target: { value: '正在输入的新正文' } });
    const saved: Supplement = { conversation_id: 'emma', body: '', revision: 1, updated_at: 1,
      attachments: [{ id: 'a1', name: 'note.txt', media_type: 'text/plain', size: 5, sha256: 'hash', status: 'ready', origin: 'business_supplement', text_preview: 'hello' }] };
    bridge.details.emma.supplement = saved;
    await act(async () => pending.resolve(saved));
    expect(screen.getByText(/note.txt/)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '业务补充内容' })).toHaveValue('正在输入的新正文');
    await waitFor(() => expect(calls(bridge, 'save_conversation_supplement').some(call => call.params.body === '正在输入的新正文' && call.params.expected_revision === 1)).toBe(true), { timeout: 3000 });
  });
  it('preserves later input while recovering an automatic tail', async () => {
    const bridge = new FakeBridge();
    bridge.details.emma.chat_draft = null;
    bridge.details.emma.reply_batches = [{ batch_id: 'old', status: 'stopped', total: 2, accepted: 1, current_index: null, pending_operation_ids: [] }];
    bridge.details.emma.recoverable_reply_batches = [{ batch_id: 'old', messages_hash: 'a'.repeat(64), total: 2, accepted: 1, remaining: 1 }];
    const pending = deferred<Draft>();
    const invoke = bridge.invoke.bind(bridge);
    bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'recover_reply_batch_draft') { bridge.calls.push({ method, params }); return await pending.promise as T; }
      return invoke<T>(method, params);
    };
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '取出未发送内容' }));
    fireEvent.change(textarea(), { target: { value: '迟到的新输入' } });
    const recovered = makeDraft('emma', '原冻结尾部', 1); recovered.message_parts = ['原冻结尾部'];
    bridge.details.emma.chat_draft = recovered;
    await act(async () => pending.resolve(recovered));
    expect(textarea()).toHaveValue('迟到的新输入');
    await waitFor(() => expect(calls(bridge, 'save_chat_draft').some(call => call.params.body === '迟到的新输入' && call.params.expected_revision === 1)).toBe(true));
  });
  it('validates the frozen polish source at the desktop bridge', () => {
    const valid = { conversation_id: 'emma', case_id: null, client_request_id: 'polish-1',
      input_body: '14.5 USD each, shipping excluded', input_revision: 2, input_hash: 'a'.repeat(64) };
    expect(() => validateChatParameters('generate_manual_suggestion', valid)).not.toThrow();
    expect(() => validateChatParameters('generate_manual_suggestion', { ...valid, input_revision: null })).toThrow();
    expect(() => validateChatParameters('generate_manual_suggestion', { ...valid, input_body: 'changed', input_hash: null })).toThrow();
    expect(() => validateChatParameters('generate_manual_suggestion', { ...valid, foreign_body: 'other customer' })).toThrow();
    expect(() => validateChatParameters('generate_manual_suggestion', { ...valid, part_index: 2 })).not.toThrow();
    expect(() => validateChatParameters('generate_manual_suggestion', { ...valid, part_index: 3 })).toThrow();
    const parts = { conversation_id: 'emma', body: 'one\n\ntwo', expected_revision: null, client_mutation_id: 'm', message_parts: ['one', 'two'] };
    expect(() => validateChatParameters('save_chat_draft', parts)).not.toThrow();
    expect(() => validateChatParameters('save_chat_draft', { ...parts, body: 'different' })).toThrow();
    expect(() => validateChatParameters('save_chat_draft', { ...parts, remaining_batch_id: 'batch' })).not.toThrow();
    expect(() => validateChatParameters('save_chat_draft', { ...parts, remaining_batch_id: '' })).toThrow();
    expect(() => validateChatParameters('save_chat_draft', { ...parts, message_parts: undefined, remaining_batch_id: 'batch' })).toThrow();
    const recovery = { conversation_id: 'emma', batch_id: 'batch', expected_messages_hash: 'a'.repeat(64), expected_chat_draft_revision: null, client_mutation_id: 'm' };
    expect(() => validateChatParameters('recover_reply_batch_draft', recovery)).not.toThrow();
    expect(() => validateChatParameters('recover_reply_batch_draft', { ...recovery, body: 'injected' })).toThrow();
    expect(() => validateChatParameters('recover_reply_batch_draft', { ...recovery, expected_messages_hash: 'bad' })).toThrow();
  });
  it('polishes the exact saved body and displays the suggestion translation without editing the reply', async () => {
    const originalSubtle = Object.getOwnPropertyDescriptor(globalThis.crypto, 'subtle');
    Object.defineProperty(globalThis.crypto, 'subtle', { configurable: true, value: webcrypto.subtle });
    const bridge = new FakeBridge();
    bridge.details.emma.manual_suggestion = null;
    const invoke = bridge.invoke.bind(bridge);
    bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'get_translations') return { items: (params.items as { kind: string; source_id: string }[]).map(item =>
        ({ ...item, status: 'ready', text: item.kind === 'manual_suggestion' ? '建议中文译文' : null, error: null })) } as T;
      return invoke<T>(method, params);
    };
    await openApp(bridge);
    const original = (textarea() as HTMLTextAreaElement).value;
    fireEvent.click(screen.getByRole('button', { name: '润色当前回复' }));
    await waitFor(() => expect(calls(bridge, 'generate_manual_suggestion')).toHaveLength(1));
    const params = calls(bridge, 'generate_manual_suggestion')[0].params;
    expect(params).toMatchObject({ input_body: original, input_revision: 1, conversation_id: 'emma' });
    expect(params.input_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(calls(bridge, 'save_chat_draft')).toHaveLength(0);
    expect(textarea()).toHaveValue(original);
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    const toggle = screen.getByRole('checkbox', { name: '自动中文翻译' });
    if (!(toggle as HTMLInputElement).checked) fireEvent.click(toggle);
    await screen.findByRole('region', { name: '待处理回复建议' });
    expect(await screen.findByText('建议中文译文')).toBeInTheDocument();
    expect(textarea()).toHaveValue(original);
    fireEvent.change(textarea(), { target: { value: 'Typed while viewing the suggestion' } });
    expect(textarea()).toHaveValue('Typed while viewing the suggestion');
    if ((toggle as HTMLInputElement).checked) fireEvent.click(toggle);
    if (originalSubtle) Object.defineProperty(globalThis.crypto, 'subtle', originalSubtle);
    else Reflect.deleteProperty(globalThis.crypto, 'subtle');
  });
  it('saves supplement before generating without taking over, then adopts by replacing the entire editor', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.chat_draft = null;
    await openApp(bridge);
    const initial = (textarea() as HTMLTextAreaElement).value;
    fireEvent.change(screen.getByRole('textbox', { name: '业务补充内容' }), { target: { value: '报价 20 美元' } });
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await waitFor(() => expect(calls(bridge, 'generate_manual_suggestion')).toHaveLength(1));
    expect(calls(bridge, 'save_conversation_supplement')[0].params.body).toBe('报价 20 美元');
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
    expect((textarea() as HTMLTextAreaElement).value).toBe(initial);
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await screen.findByRole('region', { name: '待处理回复建议' });
    fireEvent.click(await screen.findByRole('button', { name: '替换到输入框' }));
    expect((textarea() as HTMLTextAreaElement).value).toBe('New suggested reply');
    await waitFor(() => expect(calls(bridge, 'begin_manual_edit')).toHaveLength(1));
  });
  it('generates for a paused, human-owned conversation without a procurement case', async () => {
    const bridge = new FakeBridge(); bridge.own('emma'); bridge.details.emma.chat_draft = null;
    bridge.snapshot.conversations[0].case_id = ''; bridge.details.emma.conversation.case_id = '';
    bridge.details.emma.case = null as unknown as Case; bridge.details.emma.cases = [];
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await waitFor(() => expect(calls(bridge, 'generate_manual_suggestion')).toHaveLength(1));
    expect(calls(bridge, 'generate_manual_suggestion')[0].params.case_id).toBeNull();
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
  });
  it('shows cached message translations without editing or taking over the conversation', async () => {
    exposeMessagesInViewport();
    const bridge = new FakeBridge(); const invoke = bridge.invoke.bind(bridge);
    bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> => {
      if (method === 'get_translations') return { items: (params.items as { kind: string; source_id: string }[]).map(item => ({ ...item, status: 'ready', text: item.kind === 'message' ? '请问可以先制作样品吗？' : null, error: null })) } as T;
      return invoke<T>(method, params);
    };
    await openApp(bridge); const original = (textarea() as HTMLTextAreaElement).value;
    fireEvent.click(screen.getByRole('checkbox', { name: '自动中文翻译' }));
    expect(await screen.findByText('请问可以先制作样品吗？')).toBeInTheDocument();
    expect(within(screen.getByLabelText('原始聊天记录')).getByText('Could you make a sample with our logo first?')).toBeInTheDocument();
    expect(textarea()).toHaveValue(original); expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
    fireEvent.click(screen.getByRole('checkbox', { name: '自动中文翻译' }));
  });
  it('waits for live AI readiness and resumes the enabled translations after recovery', async () => {
    exposeMessagesInViewport();
    const bridge = failedAiBridge();
    await openApp(bridge);
    const toggle = screen.getByRole('checkbox', { name: '自动中文翻译' });
    if (!(toggle as HTMLInputElement).checked) fireEvent.click(toggle);
    await act(async () => { await Promise.resolve(); });
    expect(calls(bridge, 'get_translations')).toHaveLength(0);
    expect(calls(bridge, 'request_translations')).toHaveLength(0);
    bridge.snapshot.checks.ai = { ...bridge.snapshot.checks.ai, status: 'passed' };
    act(() => bridge.emit({ event: 'state.changed', data: { reason: 'ai_checked', conversation_id: 'emma' } }));
    await waitFor(() => expect(calls(bridge, 'get_translations').some(call =>
      (call.params.items as { kind: string }[]).some(item => item.kind === 'message'))).toBe(true));
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
    expect(calls(bridge, 'set_scope')).toHaveLength(0);
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
    fireEvent.click(toggle);
  });
  it('refreshes translations without refreshing the workspace on translation events', async () => {
    exposeMessagesInViewport();
    const bridge = await openApp();
    const toggle = screen.getByRole('checkbox', { name: '自动中文翻译' });
    if (!(toggle as HTMLInputElement).checked) fireEvent.click(toggle);
    await waitFor(() => expect(calls(bridge, 'get_translations').length).toBeGreaterThan(0));
    const beforeSnapshot = calls(bridge, 'get_snapshot').length;
    const beforeDetail = calls(bridge, 'get_conversation').length;
    const beforeTranslation = calls(bridge, 'get_translations').length;
    act(() => bridge.emit({ event: 'state.changed', data: { reason: 'translations_updated', conversation_id: 'emma' } }));
    await waitFor(() => expect(calls(bridge, 'get_translations').length).toBeGreaterThan(beforeTranslation));
    act(() => bridge.emit({ event: 'job.updated', data: { job_id: 'translation-1', kind: 'translation', conversation_id: 'emma', status: 'succeeded' } }));
    await new Promise(resolve => setTimeout(resolve, 120));
    expect(calls(bridge, 'get_snapshot')).toHaveLength(beforeSnapshot);
    expect(calls(bridge, 'get_conversation')).toHaveLength(beforeDetail);
  });
  it('keeps supplement visible when procurement needs are collapsed', async () => {
    await openApp();
    fireEvent.click(screen.getByRole('button', { name: '收起需求侧栏' }));
    expect(screen.getByRole('textbox', { name: '业务补充内容' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '查看需求' })).not.toBeInTheDocument();
  });
});

describe('conversation chat and automatic reception', () => {
  it('refreshes the visible workspace when the backend becomes ready after the initial request failed', async () => {
    const bridge = new FakeBridge();
    const invoke = vi.spyOn(bridge, 'invoke').mockRejectedValueOnce(new Error('后台尚未就绪。'));
    render(<App bridge={bridge} />);
    await screen.findByRole('region', { name: '启动与连接指引' });
    act(() => bridge.emit({ event: 'state.changed', data: { reason: 'backend_ready' } }));
    await screen.findByRole('region', { name: '演示模式连接指引' });
    expect(screen.queryByText('服务未连接')).not.toBeInTheDocument();
    expect(screen.getAllByText('自动接待已暂停').length).toBeGreaterThan(0);
    expect(invoke.mock.calls.every(([method]) => ['get_snapshot', 'get_executor_health', 'get_conversation', 'get_requirement_media'].includes(method))).toBe(true);
  });
  it('sends consecutive messages, shows queued versus accepted and clears each successful body', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.chat_draft = null;
    await openApp(bridge);
    expect(textarea()).toHaveValue('');
    await edit('First human message');
    fireEvent.click(sendButton());
    await screen.findByText('发送任务已排队，尚未确认平台受理');
    expect(textarea()).toHaveValue('First human message');
    expect(screen.queryByText('平台已受理')).not.toBeInTheDocument();
    expect(calls(bridge, 'save_chat_draft')[0].params).toMatchObject({ conversation_id: 'emma', expected_revision: null, body: 'First human message' });
    expect(calls(bridge, 'send_chat')[0].params).toMatchObject({ conversation_id: 'emma', draft_id: 'chat-emma', expected_revision: 1 });
    act(() => bridge.finish('chat-job-1'));
    await waitFor(() => expect(textarea()).toHaveValue(''));
    await screen.findByText('平台已受理');
    expect(within(screen.getByLabelText('原始聊天记录')).queryByText('First human message')).not.toBeInTheDocument();
    await edit('Second human message');
    expect(screen.queryByText('平台已受理')).not.toBeInTheDocument();
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(2));
    expect(calls(bridge, 'send_chat')[1].params.expected_revision).toBe(2);
    expect(calls(bridge, 'send_chat')[1].params.client_request_id).not.toBe(calls(bridge, 'send_chat')[0].params.client_request_id);
    expect(textarea()).toHaveValue('Second human message');
    act(() => bridge.finish('chat-job-2'));
    await waitFor(() => expect(textarea()).toHaveValue(''));
    noLegacySend(bridge);
  });

  it('dismisses success after four seconds and keeps unknown recovery visible', async () => {
    const bridge = await openApp();
    await edit('First message'); fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    act(() => bridge.finish('chat-job-1'));
    await screen.findByText('平台已受理');
    await waitFor(() => expect(screen.queryByText('平台已受理')).not.toBeInTheDocument(), { timeout: 5000 });
    await edit('Second message'); fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(2));
    act(() => bridge.finish('chat-job-2', 'unknown'));
    await screen.findByText('发送结果未知，请核查原操作');
    await screen.findByRole('button', { name: '核查原操作 operation-chat-job-2' });
    vi.useFakeTimers();
    try {
      act(() => vi.advanceTimersByTime(10000));
      expect(screen.getByText('发送结果未知，请核查原操作')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '核查原操作 operation-chat-job-2' })).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it.each(['paused', 'pausing', 'no-ai', 'closed', 'no-facts', 'seller-latest', 'invalid-ai'] as const)('allows direct chat with %s without AI or procurement preflight', async state => {
    const bridge = failedAiBridge();
    if (state === 'pausing') bridge.snapshot.reception.running = true;
    if (state === 'closed') { bridge.details.emma.case.state = 'closed'; bridge.details.emma.conversation.state = 'closed'; }
    if (state === 'no-facts') { bridge.details.emma.case.facts = {}; bridge.snapshot.policies = []; }
    if (state === 'seller-latest') bridge.details.emma.messages[0].role = 'seller';
    if (state === 'invalid-ai') { bridge.details.emma.draft!.status = 'stale'; bridge.details.emma.draft!.validation.ready = false; }
    const paused = deferred<Snapshot>();
    if (state === 'pausing') {
      const original = bridge.invoke.bind(bridge);
      bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}) => {
        if (method === 'set_reception') { bridge.calls.push({ method, params }); return paused.promise as Promise<T>; }
        return original<T>(method, params);
      };
    }
    await openApp(bridge);
    if (state === 'pausing') fireEvent.click(screen.getByRole('button', { name: '暂停自动接待', exact: true }));
    await edit('A normal reply regardless of automation readiness.');
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    noLegacySend(bridge);
    if (state === 'pausing') { bridge.snapshot.reception.running = false; await act(async () => paused.resolve(clone(bridge.snapshot))); }
  });

  it('pauses only this customer on the first input immediately, and fences send until the FAST acknowledgement', async () => {
    const bridge = new FakeBridge(); bridge.snapshot.reception.running = true; bridge.snapshot.reception.auto_send = true;
    const begun = deferred<Snapshot>(); bridge.beginHandler = async () => begun.promise;
    await openApp(bridge);
    fireEvent.change(textarea(), { target: { value: 'first keystroke' } });
    expect(calls(bridge, 'begin_manual_edit').map(call => call.params)).toEqual([{ conversation_id: 'emma' }]);
    expect(calls(bridge, 'save_chat_draft')).toHaveLength(0);
    expect(sendButton()).toBeDisabled(); fireEvent.keyDown(textarea(), { key: 'Enter', ctrlKey: true });
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
    expect(calls(bridge, 'set_reception')).toHaveLength(0);
    await act(async () => begun.resolve(bridge.own('emma')));
    await waitFor(() => expect(sendButton()).toBeEnabled());
    expect(screen.getByRole('button', { name: '恢复此客户自动接待' })).toBeInTheDocument();
    expect(bridge.details.sofia.conversation.human_owned).toBe(false);
    expect(bridge.snapshot.reception.running).toBe(true);
  });

  it('keeps send disabled after a failed first takeover until an explicit retry succeeds', async () => {
    const bridge = new FakeBridge(); let attempts = 0;
    bridge.beginHandler = async () => { if (++attempts === 1) throw new Error('模拟暂停请求失败'); return bridge.own('emma'); };
    await openApp(bridge);
    fireEvent.change(textarea(), { target: { value: 'Preserved while takeover retries' } });
    await screen.findByText('模拟暂停请求失败');
    expect(sendButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重试暂停此客户自动接待' }));
    await waitFor(() => expect(sendButton()).toBeEnabled());
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
  });

  it('flushes an in-flight autosave on send and binds only the returned saved revision', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.chat_draft = null;
    const saved = deferred<Draft>();
    bridge.saveHandler = async () => saved.promise;
    bridge.sendHandler = async () => ({ job_id: 'bound-version-job' });
    await openApp(bridge);
    await edit('Wait for this exact save');
    await waitFor(() => expect(calls(bridge, 'save_chat_draft')).toHaveLength(1), { timeout: 2000 });
    expect(sendButton()).toBeEnabled(); fireEvent.click(sendButton());
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
    bridge.details.emma.chat_draft = makeDraft('emma', 'Wait for this exact save', 7);
    await act(async () => saved.resolve(clone(bridge.details.emma.chat_draft!)));
    await waitFor(() => expect(calls(bridge, 'send_chat')[0]?.params.expected_revision).toBe(7));
    expect(calls(bridge, 'save_chat_draft')).toHaveLength(1);
  });

  it('preserves the same request and exact arguments after a lost acknowledgement even with new input', async () => {
    const bridge = new FakeBridge(); let tries = 0;
    bridge.sendHandler = async params => { const ack = bridge.enqueue(params); if (++tries === 1) throw new Error('模拟通信失败'); return ack; };
    await openApp(bridge); await edit('Original exact body');
    fireEvent.click(sendButton());
    const retry = await screen.findByRole('button', { name: '重试原发送' });
    fireEvent.change(textarea(), { target: { value: 'New text for the next message' } });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(2));
    expect(calls(bridge, 'send_chat')[1].params).toEqual(calls(bridge, 'send_chat')[0].params);
    expect(bridge.requests.size).toBe(1);
    expect(calls(bridge, 'save_chat_draft')).toHaveLength(1);
    act(() => bridge.finish('chat-job-1'));
    await waitFor(() => expect(sendButton()).toBeEnabled());
    expect(textarea()).toHaveValue('New text for the next message');
    noLegacySend(bridge);
  });

  it('preserves text typed during sending and ignores a late receipt from the previous revision', async () => {
    const bridge = await openApp();
    await edit('First message'); fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    fireEvent.change(textarea(), { target: { value: 'New input during send' } });
    act(() => bridge.finish('chat-job-1'));
    await waitFor(() => expect(sendButton()).toBeEnabled());
    expect(textarea()).toHaveValue('New input during send');
    fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(2));
    act(() => bridge.emit({ event: 'job.updated', data: clone(bridge.snapshot.jobs[0]) }));
    await screen.findByText('发送任务已排队，尚未确认平台受理');
    expect(textarea()).toHaveValue('New input during send');
    expect(sendButton()).toBeDisabled();
    act(() => bridge.finish('chat-job-2'));
    await waitFor(() => expect(textarea()).toHaveValue(''));
  });

  it('handles a completed job event arriving before its send acknowledgement', async () => {
    const bridge = new FakeBridge(); const ack = deferred<JobAck>();
    bridge.sendHandler = async params => { const result = bridge.enqueue(params); bridge.finish(result.job_id); return ack.promise; };
    await openApp(bridge); await edit('Fast completion'); fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    await act(async () => ack.resolve({ job_id: 'chat-job-1' }));
    await waitFor(() => expect(textarea()).toHaveValue(''));
    expect(screen.queryByRole('button', { name: '重试原发送' })).not.toBeInTheDocument();
  });

  it('fences unknown sends for the entire conversation across procurement cases and reconciles the original operation', async () => {
    const bridge = new FakeBridge();
    const older = { ...clone(bridge.details.emma.case), case_id: 'old-case', state: 'closed' as const };
    bridge.details.emma.cases.push(older);
    bridge.snapshot.outbox = [{ operation_id: 'original-unknown', case_id: 'old-case', case_revision: 1, kind: 'send', status: 'unknown', message: '原发送结果待核查', updated_at: Date.now() }];
    bridge.snapshot.jobs = [{ job_id: 'persisted-unknown-job', kind: 'send_chat', conversation_id: 'emma', case_id: 'old-case', operation_id: 'original-unknown', status: 'unknown', stage: 'complete', message: '原任务结果未知', created_at: Date.now(), started_at: null, completed_at: Date.now() }];
    await openApp(bridge);
    expect(sendButton()).toBeDisabled();
    fireEvent.change(screen.getByRole('combobox', { name: '当前采购事项' }), { target: { value: 'old-case' } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue('old-case'));
    expect(sendButton()).toBeDisabled(); fireEvent.keyDown(textarea(), { key: 'Enter', ctrlKey: true });
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '查看并核查原操作' }));
    const receipt = await screen.findByText('原发送结果待核查');
    const row = receipt.closest('article')!;
    bridge.reconcileHandler = async () => { bridge.snapshot.outbox[0].status = 'accepted'; return { status: 'accepted', message: '已核实原发送' }; };
    fireEvent.click(within(row).getByRole('button', { name: '核查原操作' }));
    await screen.findByText(/已核实原发送/);
    expect(calls(bridge, 'reconcile_operation')[0].params).toEqual({ operation_id: 'original-unknown' });
    expect(calls(bridge, 'send_chat')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '客户接待', exact: true }));
    await waitFor(() => expect(sendButton()).toBeEnabled());
  });

  it('queries an unknown original operation without discarding new unsaved input or creating a send', async () => {
    const bridge = await openApp();
    await edit('The original message'); fireEvent.click(sendButton());
    await waitFor(() => expect(calls(bridge, 'send_chat')).toHaveLength(1));
    fireEvent.change(textarea(), { target: { value: 'Unsaved next message' } });
    act(() => bridge.finish('chat-job-1', 'unknown'));
    const query = await screen.findByRole('button', { name: '核查原操作 operation-chat-job-1' });
    expect(sendButton()).toBeDisabled();
    bridge.reconcileHandler = async () => {
      bridge.snapshot.outbox[0].status = 'accepted';
      bridge.details.emma.chat_draft!.status = 'accepted';
      return { status: 'accepted', message: '原操作已核实' };
    };
    fireEvent.click(query);
    await screen.findByText('原操作已核实');
    await waitFor(() => expect(sendButton()).toBeEnabled());
    expect(textarea()).toHaveValue('Unsaved next message');
    expect(calls(bridge, 'send_chat')).toHaveLength(1);
    expect(calls(bridge, 'reconcile_operation')[0].params).toEqual({ operation_id: 'operation-chat-job-1' });
  });

  it('fences an unknown send job before the operation row has arrived', async () => {
    const bridge = await openApp();
    act(() => bridge.emit({ event: 'job.updated', data: { job_id: 'unknown-early', kind: 'send_chat', conversation_id: 'emma', case_id: 'a-different-case', status: 'unknown', stage: 'send', message: '等待核查', created_at: Date.now(), started_at: null, completed_at: Date.now() } satisfies Job }));
    expect(sendButton()).toBeDisabled();
    expect(screen.getByRole('button', { name: '查看并核查原操作' })).toBeInTheDocument();
  });

  it('keeps chat text across Case changes and never saves it to the AI suggestion', async () => {
    const bridge = new FakeBridge();
    bridge.details.emma.cases.push({ ...clone(bridge.details.emma.case), case_id: 'another-case', procurement_key: '另一采购', state: 'closed' });
    await openApp(bridge); await edit('Conversation-level body');
    fireEvent.change(screen.getByRole('combobox', { name: '当前采购事项' }), { target: { value: 'another-case' } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue('another-case'));
    expect(textarea()).toHaveValue('Conversation-level body');
    expect(bridge.details.emma.draft!.body).toBe('Separate AI suggestion');
    expect(calls(bridge, 'save_chat_draft')[0].params).not.toHaveProperty('case_id');
    expect(sendButton()).toBeEnabled();
  });

  it('keeps retired suggestion warnings and internal guidance out of the pending reply', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.policies = [{ policy_id: 'rule', kind: 'policy', title: '内部接待规则', content: '绝不可公开的内部原因', customer_wording: '可以公开的说明', internal_notes: '仅人工备注', usage: 'internal_guidance', enabled: true, product_scope: [], confirmed: true, public: false, revision: 2, updated_at: 1, valid_until: null }];
    bridge.details.emma.draft = { ...bridge.details.emma.draft!, applied_policies: [{ source_id: 'rule:internal', title: '内部接待规则', revision: 2, usage: 'internal_guidance' }] } as Draft;
    bridge.details.emma.manual_suggestion = { suggestion_id: 's1', conversation_id: 'emma', case_id: 'case-emma', body: '建议正文', request_id: 'r', context_fingerprint: 'f', status: 'ready', updated_at: 1, quality_notes: [{ code: 'q', message: '请核对表述' }], assistance_suggestions: [{ kind: 'verify', detail: '核实交期', fact_keys: [], source_message_ids: [] }], applied_policies: [{ source_id: 'rule:internal', title: '内部接待规则', revision: 2, usage: 'internal_guidance' }] };
    await openApp(bridge);
    await screen.findByText('建议正文');
    expect(screen.queryByText('请核对表述')).not.toBeInTheDocument();
    expect(screen.queryByText('核实交期')).not.toBeInTheDocument();
    expect(screen.queryByText('建议人工协助（尚未创建任务）')).not.toBeInTheDocument();
    expect(screen.queryByText('本次应用的资料与规则')).not.toBeInTheDocument();
    expect(screen.queryByText('本次应用的内部规则')).not.toBeInTheDocument();
    expect(screen.queryByText('可以公开的说明')).not.toBeInTheDocument();
    expect(screen.queryByText('绝不可公开的内部原因')).not.toBeInTheDocument();
    expect(screen.queryByText('仅人工备注')).not.toBeInTheDocument();
    expect(calls(bridge, 'start_assistance_task')).toHaveLength(0);
  });
  it('keeps late AI suggestions separate; adopting one immediately begins manual editing', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.chat_draft = null;
    bridge.details.emma.manual_suggestion = { suggestion_id: 's1', conversation_id: 'emma', case_id: 'case-emma', body: 'Separate AI suggestion', request_id: 'r1', context_fingerprint: 'test', status: 'ready', updated_at: Date.now() };
    await openApp(bridge);
    await screen.findByText('Separate AI suggestion');
    expect(textarea()).toHaveValue(''); expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '替换到输入框' }));
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(1);
    await waitFor(() => expect(sendButton()).toBeEnabled());
    fireEvent.change(textarea(), { target: { value: 'A carefully edited human answer' } });
    bridge.details.emma.manual_suggestion = { ...bridge.details.emma.manual_suggestion!, suggestion_id: 's2', body: 'A late AI suggestion' };
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(calls(bridge, 'get_conversation').length).toBeGreaterThan(1));
    expect(textarea()).toHaveValue('A carefully edited human answer');
    await screen.findByText('A late AI suggestion');
    expect(calls(bridge, 'save_draft')).toHaveLength(0);
  });

  it('explicitly resumes this customer with takeover enabled:false and pauses again on the next edit', async () => {
    const bridge = new FakeBridge(); bridge.own('emma');
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '恢复此客户自动接待' }));
    await screen.findByRole('button', { name: '暂停此客户自动接待' });
    expect(calls(bridge, 'takeover')[0].params).toEqual({ conversation_id: 'emma', enabled: false });
    await edit('A fresh manual edit');
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(1);
    expect(bridge.snapshot.reception.running).toBe(false);
  });

  it.each(['开始', '连接与设置'])('removes the separate manual mode from %s and gates automatic authorization on checks', async page => {
    const bridge = await openApp(failedAiBridge());
    fireEvent.click(screen.getByRole('button', { name: page, exact: true }));
    await screen.findByRole('heading', { name: page === '开始' ? /今天先把哪些客户/ : '连接与设置', exact: page !== '开始' });
    expect(screen.queryByRole('region', { name: '人工接待模式' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /开启人工接待|恢复人工接待|切换为人工接待/ })).not.toBeInTheDocument();
    if (page === '连接与设置') expect(screen.getByRole('button', { name: '授权并开始自动接待' })).toBeDisabled();
    expect(bridge.calls.some(call => call.params.manual_only === true)).toBe(false);
  });

  it('does not block AI generation because a legacy manual_only flag is present', async () => {
    const bridge = new FakeBridge(); bridge.snapshot.reception.manual_only = true; bridge.details.emma.chat_draft = null;
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '生成回复建议' }));
    await waitFor(() => expect(calls(bridge, 'generate_manual_suggestion')).toHaveLength(1));
  });
});

describe('message sync summaries and explicit empty-history refresh', () => {
  it.each(['连接与设置', '运行记录'])('shows sync totals in a single paragraph on %s and updates zero counts and missing sync time', async page => {
    const bridge = failedAiBridge();
    const lastSync = 1793950060000;
    bridge.snapshot.message_sync = { status: 'running', checked_at: 1793950000000, poll_seconds: 5, error_code: null, pending_count: 11, summary_requests: 123, waiting_activity_count: 7, background_pending_count: 3, active_read_count: 1, last_body_sync_at: lastSync };
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: page, exact: true }));
    const summary = await screen.findByText(/^消息同步：/);
    expect(summary.tagName).toBe('P'); expect(summary).toHaveAttribute('role', 'status');
    expect(summary).toHaveTextContent('每 5 秒检测新活动');
    expect(summary).toHaveTextContent('待同步 11 个会话');
    expect(summary).toHaveTextContent('排队或读取中 1 个会话');
    expect(summary).toHaveTextContent('历史待补 3 个会话（空闲时读取）');
    expect(summary).toHaveTextContent('空历史等待新活动 7 个会话（已暂停自动读取）');
    expect(summary).toHaveTextContent(`最近正文同步：${new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(lastSync)}`);
    expect(summary.textContent).not.toMatch(/waiting_activity_count|background_pending_count|active_read_count|last_body_sync_at|summary_requests/);
    bridge.snapshot.message_sync = { ...bridge.snapshot.message_sync, pending_count: 0, waiting_activity_count: 0, background_pending_count: 0, active_read_count: 0, last_body_sync_at: null, error_code: 'native_timeout' };
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(screen.getByText(/^消息同步：/)).toHaveTextContent('排队或读取中 0 个会话'));
    expect(summary).toHaveTextContent('历史待补 0 个会话');
    expect(summary).toHaveTextContent('空历史等待新活动 0 个会话');
    expect(summary).toHaveTextContent('最近正文同步：尚无记录');
    expect(summary).toHaveTextContent('消息检测暂缓，正在等待重试。');
    expect(summary).not.toHaveTextContent('native_timeout');
    expect(calls(bridge, 'get_conversation').some(call => call.params.refresh_empty === true)).toBe(false);
  });

  it.each(['absent', 'legacy'] as const)('accepts a %s sync snapshot on both pages without inventing totals', async shape => {
    const bridge = failedAiBridge();
    if (shape === 'legacy') bridge.snapshot.message_sync = { status: 'running', checked_at: null, poll_seconds: 15, error_code: null, pending_count: 2, summary_requests: 0 };
    await openApp(bridge);
    for (const page of ['连接与设置', '运行记录']) {
      fireEvent.click(screen.getByRole('button', { name: page, exact: true }));
      await screen.findByRole('heading', { name: page, exact: true });
      if (shape === 'absent') expect(screen.queryByText(/^消息同步：/)).not.toBeInTheDocument();
      else {
        const summary = screen.getByText(/^消息同步：/);
        expect(summary).toHaveTextContent('待同步 2 个会话');
        expect(summary).not.toHaveTextContent(/排队或读取中|历史待补|空历史等待新活动|最近正文同步|undefined|NaN/);
      }
    }
  });

  it('requests empty history only on explicit selection, including the initially selected customer', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.messages = []; bridge.details.sofia.messages = [];
    await openApp(bridge);
    expect(calls(bridge, 'get_conversation').map(call => call.params)).toEqual([{ conversation_id: 'emma', case_id: 'case-emma' }]);
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await waitFor(() => expect(calls(bridge, 'get_conversation')).toHaveLength(2));
    expect(calls(bridge, 'get_conversation')[1].params).toEqual({ conversation_id: 'emma', refresh_empty: true });
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByRole('main', { name: 'Sofia 的客户接待' });
    expect(calls(bridge, 'get_conversation')[2].params).toEqual({ conversation_id: 'sofia', refresh_empty: true });
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
    expect(calls(bridge, 'takeover')).toHaveLength(0);
    noLegacySend(bridge);
  });

  it('preserves current manual input and ownership when explicitly reopening the same customer', async () => {
    const bridge = await openApp(); await edit('Manual text survives reopening');
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await waitFor(() => expect(calls(bridge, 'get_conversation')).toHaveLength(2));
    expect(calls(bridge, 'get_conversation')[1].params.refresh_empty).toBe(true);
    expect(textarea()).toHaveValue('Manual text survives reopening');
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(1);
    expect(bridge.details.emma.conversation.human_owned).toBe(true);
  });

  it('keeps state events, completed-job events, workspace refresh and profile loads from retrying empty history', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.messages = [];
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await waitFor(() => expect(calls(bridge, 'get_conversation')).toHaveLength(2));
    const passive = async (trigger: () => void) => {
      const before = calls(bridge, 'get_conversation').length;
      act(trigger);
      await waitFor(() => expect(calls(bridge, 'get_conversation').length).toBeGreaterThan(before));
      for (const call of calls(bridge, 'get_conversation').slice(before)) expect(call.params).not.toHaveProperty('refresh_empty');
    };
    await passive(() => bridge.emit({ event: 'state.changed', data: {} }));
    await passive(() => bridge.emit({ event: 'job.updated', data: { job_id: 'completed-history', kind: 'fill_history', conversation_id: 'emma', case_id: 'case-emma', status: 'succeeded', stage: 'complete', message: '本地历史已更新', created_at: Date.now(), started_at: null, completed_at: Date.now() } satisfies Job }));
    await passive(() => fireEvent.click(screen.getByRole('button', { name: '刷新本地工作区' })));
    await passive(() => fireEvent.click(screen.getByRole('button', { name: '客户资料', exact: true })));
    await screen.findByRole('heading', { name: '客户资料', exact: true });
    await passive(() => bridge.emit({ event: 'state.changed', data: {} }));
    expect(calls(bridge, 'get_conversation').filter(call => call.params.refresh_empty === true)).toHaveLength(1);
    expect(calls(bridge, 'begin_manual_edit')).toHaveLength(0);
  });

  it('keeps a consolidated read failure visible in attention records and shows its approved error', async () => {
    const bridge = await openApp();
    const failure: Job = { job_id: 'combined-read-failure', kind: 'sync_messages', conversation_id: null, case_id: null, status: 'failed', stage: 'complete', message: '消息读取失败，累计 4 次；最后时间：11-07 10:30', error: { code: 'native_timeout', message: '消息读取超时，请稍后重试。' }, diagnostics: { original_exception: 'PRIVATE_EXCEPTION_DETAIL' }, created_at: Date.now(), started_at: null, completed_at: Date.now() };
    bridge.snapshot.jobs = [failure];
    act(() => bridge.emit({ event: 'job.updated', data: clone(failure) }));
    fireEvent.click(screen.getByRole('button', { name: '运行记录', exact: true }));
    await screen.findByRole('heading', { name: '运行记录', exact: true });
    fireEvent.click(screen.getByRole('button', { name: '需要关注', exact: true }));
    fireEvent.click(screen.getByRole('button', { name: /同步近期消息.*累计 4 次.*最后时间：11-07 10:30/s }));
    const detail = screen.getByRole('heading', { name: '任务详情' }).closest('aside')!;
    expect(within(detail).getByText(failure.message)).toBeInTheDocument();
    expect(within(detail).getByRole('alert')).toHaveTextContent('消息读取超时，请稍后重试。');
    expect(within(detail).getByRole('alert')).toHaveTextContent('错误码：native_timeout');
    expect(within(detail).queryByRole('button', { name: '查看所属会话' })).not.toBeInTheDocument();
    expect(screen.queryByText(/PRIVATE_EXCEPTION_DETAIL/)).not.toBeInTheDocument();
    failure.message = '消息读取失败，累计 5 次；最后时间：11-07 10:35';
    act(() => bridge.emit({ event: 'job.updated', data: clone(failure) }));
    await screen.findByRole('button', { name: /同步近期消息.*累计 5 次.*最后时间：11-07 10:35/s });
    expect(screen.queryByText(/累计 4 次/)).not.toBeInTheDocument();
    expect(screen.getAllByText('失败')).toHaveLength(1);
  });
});

describe('editing safeguards and existing workspace layout', () => {
  it('retains input typed during a save and advances the exact server revision for the next save', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.chat_draft = null;
    const first = deferred<Draft>(); let count = 0;
    bridge.saveHandler = async params => ++count === 1 ? first.promise : makeDraft('emma', String(params.body), 2);
    await openApp(bridge); await edit('First manual text');
    await waitFor(() => expect(calls(bridge, 'save_chat_draft')).toHaveLength(1), { timeout: 2000 });
    fireEvent.change(textarea(), { target: { value: 'First manual text plus newer input' } });
    await act(async () => first.resolve(makeDraft('emma', 'First manual text', 1)));
    expect(textarea()).toHaveValue('First manual text plus newer input');
    await waitFor(() => expect(calls(bridge, 'save_chat_draft')).toHaveLength(2), { timeout: 2000 });
    const saves = calls(bridge, 'save_chat_draft');
    expect(saves[0].params.expected_revision).toBeNull(); expect(saves[1].params.expected_revision).toBe(1);
    expect(saves[1].params.body).toBe('First manual text plus newer input');
    expect(saves[1].params.client_mutation_id).not.toBe(saves[0].params.client_mutation_id);
    await waitFor(() => expect(document.querySelector('.composer .save-state')).toHaveTextContent('已保存'));
  });

  it('keeps the current customer and edited body when a switch-time save fails', async () => {
    const bridge = new FakeBridge(); bridge.saveHandler = async () => { throw new Error('模拟保存失败，请重试'); };
    await openApp(bridge); await edit('Do not lose this manual reply');
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByText(/模拟保存失败，请重试 当前正文已保留/);
    expect(screen.getByRole('main', { name: 'Emma 的客户接待' })).toBeInTheDocument();
    expect(textarea()).toHaveValue('Do not lose this manual reply');
    expect(calls(bridge, 'get_conversation').some(call => call.params.conversation_id === 'sofia')).toBe(false);
  });

  it('keeps the editor mounted while collapsing both sidebars and opening responsive drawers', async () => {
    const bridge = await openApp(); const original = textarea(); await edit('Layout preserves input');
    fireEvent.click(screen.getByRole('button', { name: '收起导航' }));
    fireEvent.click(screen.getByRole('button', { name: '收起需求侧栏' }));
    expect(textarea()).toBe(original); expect(textarea()).toHaveValue('Layout preserves input');
    expect(calls(bridge, 'get_conversation')).toHaveLength(1);
    act(() => { Object.defineProperty(window, 'innerWidth', { value: 850 }); window.dispatchEvent(new Event('resize')); });
    fireEvent.click(screen.getByRole('button', { name: '采购需求' }));
    expect(within(screen.getByRole('dialog', { name: '采购需求' })).getByText('当前没有采购需求')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(textarea()).toHaveValue('Layout preserves input');
    expect(bridge.preferences.nav_preference).toBe('icons'); expect(bridge.preferences.details_preference).toBe('rail');
  });

  it('protects window unload and flushes conversation drafts on the native save-before-close event', async () => {
    await openApp(); await edit('A reply not yet persisted');
    const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await act(async () => {
      const saved = await new Promise<boolean>(resolve => window.dispatchEvent(new CustomEvent('reception:save-before-close', { detail: { finish: resolve } })));
      expect(saved).toBe(true);
    });
    const clean = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(clean); expect(clean.defaultPrevented).toBe(false);
  });

  it('keeps unsaved policy input across navigation and protects window close', async () => {
    await openApp(); fireEvent.click(screen.getByRole('button', { name: '资料与政策', exact: true }));
    fireEvent.change(await screen.findByRole('textbox', { name: '资料名称' }), { target: { value: 'Unsaved sample policy' } });
    fireEvent.click(screen.getByRole('button', { name: '客户接待', exact: true }));
    expect(screen.getByRole('heading', { name: '资料与政策', exact: true })).toBeInTheDocument();
    const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); expect(event.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '放弃修改', exact: true }));
    fireEvent.click(screen.getByRole('button', { name: '客户接待', exact: true })); await screen.findByRole('textbox', { name: '回复正文' });
  });

  it('ignores a stale customer response after selection changes again', async () => {
    const bridge = await openApp(); const sofia = deferred<ConversationDetail>();
    bridge.detailHandler = async params => params.conversation_id === 'sofia' ? sofia.promise : clone(bridge.details.emma);
    fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await waitFor(() => expect(calls(bridge, 'get_conversation').some(call => call.params.conversation_id === 'sofia')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: /Emma.*Could you make a sample/s }));
    await screen.findByRole('main', { name: 'Emma 的客户接待' });
    await act(async () => sofia.resolve(clone(bridge.details.sofia)));
    expect(textarea()).toHaveValue(bridge.details.emma.chat_draft!.body);
  });

  it('retains earlier waiting-quote cases when the customer starts another purchase', async () => {
    const bridge = new FakeBridge();
    const older: Case = { ...clone(bridge.details.emma.case), case_id: 'older-procurement', state: 'waiting_quote', requirements_format: 'ai-v1', requirements: { product: { value: '原陶瓷杯采购', evidence_ids: ['message-emma'] } } };
    bridge.snapshot.handoff_cases = [older]; bridge.details.emma.cases.push(older);
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '待我报价', exact: true }));
    await screen.findByText('原陶瓷杯采购'); fireEvent.click(screen.getByRole('button', { name: '查看依据与会话' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue(older.case_id));
    expect(calls(bridge, 'get_conversation').at(-1)?.params).toEqual({ conversation_id: 'emma', case_id: older.case_id, refresh_empty: true });
  });

  it('blocks send immediately after disconnect and retains the input', async () => {
    const bridge = await openApp(); await edit('Preserved offline text');
    act(() => bridge.emit({ event: 'backend.disconnected', data: {} }));
    expect(sendButton()).toBeDisabled(); expect(textarea()).toHaveValue('Preserved offline text');
    fireEvent.keyDown(textarea(), { key: 'Enter', ctrlKey: true }); expect(calls(bridge, 'send_chat')).toHaveLength(0);
  });

  it('locks on closing and ignores expected disconnects and pending refreshes', async () => {
    const bridge = await openApp(); const app = textarea().closest('.app')!; const before = bridge.calls.length;
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    act(() => bridge.emit({ event: 'backend.closing', data: {} }));
    await screen.findByText('正在停止后台并保存结果…'); expect(app).toHaveAttribute('inert');
    act(() => bridge.emit({ event: 'backend.disconnected', data: { expected: true } }));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)); });
    expect(bridge.calls).toHaveLength(before); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps pause effective while an older automatic-start acknowledgement is still pending', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.reception.scope = { mode: 'all', conversation_ids: [], max_auto_replies: 10 };
    const started = deferred<Snapshot>(); const original = bridge.invoke.bind(bridge);
    bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}) => {
      if (method === 'set_reception') {
        bridge.calls.push({ method, params });
        if (params.enabled === true) return started.promise as Promise<T>;
        bridge.snapshot.reception.running = false; bridge.snapshot.reception.auto_send = false;
        return clone(bridge.snapshot) as T;
      }
      return original<T>(method, params);
    };
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    fireEvent.click(await screen.findByRole('checkbox', { name: /我确认允许在本轮范围内自动发送/ }));
    fireEvent.click(screen.getByRole('button', { name: '授权并开始自动接待' }));
    await waitFor(() => expect(calls(bridge, 'set_reception').some(call => call.params.enabled === true)).toBe(true));
    const stale = clone(bridge.snapshot); stale.reception.running = true; stale.reception.auto_send = true;
    bridge.snapshot.reception.running = true; bridge.snapshot.reception.auto_send = true;
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await screen.findByRole('button', { name: '暂停自动接待', exact: true });
    fireEvent.click(screen.getByRole('button', { name: '暂停并检查 AI' }));
    await waitFor(() => expect(calls(bridge, 'set_reception').some(call => call.params.enabled === false)).toBe(true));
    await act(async () => started.resolve(stale));
    await waitFor(() => expect(screen.getAllByText('自动接待已暂停').length).toBeGreaterThan(0));
    expect(bridge.snapshot.reception.auto_send).toBe(false);
  });

  it('waits for a receipt query job without claiming that its acknowledgement is a platform receipt', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.outbox = [{ operation_id: 'queued-receipt', case_id: 'case-emma', case_revision: 1, kind: 'send', status: 'unknown', message: '等待原操作核查', updated_at: Date.now() }];
    bridge.reconcileHandler = async () => {
      bridge.snapshot.jobs = [{ job_id: 'reconcile-job', kind: 'reconcile_operation', case_id: 'case-emma', conversation_id: 'emma', status: 'running', stage: 'reconcile', message: '只读核查中', created_at: Date.now(), started_at: Date.now(), completed_at: null }];
      return { job_id: 'reconcile-job' };
    };
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '运行记录', exact: true }));
    await screen.findByText('等待原操作核查'); fireEvent.click(screen.getByRole('button', { name: '核查原操作', exact: true }));
    await screen.findByText(/原操作核查已排队/);
    expect(screen.queryByText(/平台消息编号/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '核查原操作', exact: true })).toBeDisabled();
    noLegacySend(bridge);
  });

  it('shows a deferred history read separately from an actual interface failure', async () => {
    const bridge = new FakeBridge();
    const base = { kind: 'sync_messages', case_id: null, conversation_id: 'emma', stage: 'complete', created_at: Date.now(), started_at: Date.now(), completed_at: Date.now() };
    bridge.snapshot.jobs = [
      { ...base, job_id: 'empty-history', status: 'deferred', message: '历史正文暂不可用，已安排稍后补读' },
      { ...base, job_id: 'network-error', status: 'failed', message: '接口读取超时', error: { code: 'native_timeout', message: '接口读取超时' } },
    ];
    await openApp(bridge);
    fireEvent.click(screen.getByRole('button', { name: '运行记录', exact: true }));
    await screen.findByText('待补读');
    expect(screen.getByText('待补读')).toHaveClass('badge-neutral');
    fireEvent.click(screen.getByRole('button', { name: '需要关注', exact: true }));
    expect(screen.queryByText('待补读')).not.toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('接口读取超时')).toBeInTheDocument();
  });

  it('persists the Feishu write switch separately from enabling the integration', async () => {
    const bridge = await openApp(); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    fireEvent.change(await screen.findByRole('textbox', { name: '飞书 CLI 完整路径' }), { target: { value: 'C:/Tools/lark-cli.exe' } });
    for (const [name, value] of [['多维表格标识', 'synthetic-base'], ['数据表 ID', 'synthetic-table'], ['原表单分享标识', 'synthetic-form'], ['联系账号选项', 'synthetic-account'], ['初始接待状态', 'pending']]) fireEvent.change(screen.getByRole('textbox', { name }), { target: { value } });
    fireEvent.click(screen.getByText('数据表字段映射（首次配置必填）'));
    for (const label of ['买家编号', '联系账号', '接待状态', '产品名称', '自动编号']) fireEvent.change(screen.getByRole('textbox', { name: label + ' *', exact: true }), { target: { value: label } });
    fireEvent.click(screen.getByRole('checkbox', { name: /启用飞书交接/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /允许本次运行写入交接记录/ }));
    fireEvent.click(screen.getByRole('button', { name: '保存飞书配置', exact: true }));
    await waitFor(() => expect(calls(bridge, 'save_settings').some(call => (call.params.feishu as { write_enabled?: boolean })?.write_enabled === true)).toBe(true));
  });

  it('keeps the empty scope by default and saves unlimited scope without starting automation', async () => {
    const bridge = await openApp(); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    expect(await screen.findByRole('radio', { name: '仅选定客户', exact: true })).toBeChecked();
    expect(screen.queryByRole('spinbutton', { name: /自动 AI 与发送各自上限/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '授权并开始自动接待' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '应用首晚建议' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '只接待授权后新消息（含新客户）' }));
    fireEvent.click(screen.getByRole('button', { name: '保存本轮范围' }));
    await screen.findByText('本轮范围已保存。');
    expect(calls(bridge, 'set_scope')[0].params).toEqual({ mode: 'incoming', conversation_ids: [], max_auto_replies: null });
    expect(calls(bridge, 'set_reception')).toHaveLength(0);
  });

  it.each(['not_checked', 'failed'] as const)('authorizes incoming without customers or drafts when send_preview is %s', async previewStatus => {
    const bridge = new FakeBridge(); bridge.snapshot.conversations = [];
    bridge.snapshot.app.mode = 'live'; bridge.snapshot.app.account_verified = true;
    for (const key of ['identity', 'messages', 'ai'] as const) bridge.snapshot.checks[key] = { ...bridge.snapshot.checks[key], status: 'passed', simulated: false };
    bridge.snapshot.checks.send_preview = { ...bridge.snapshot.checks.send_preview, status: previewStatus, simulated: false };
    bridge.snapshot.reception.scope = { mode: 'incoming', conversation_ids: [], max_auto_replies: 5 };
    render(<App bridge={bridge} />); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    await waitFor(() => expect(screen.getByRole('radio', { name: '只接待授权后新消息（含新客户）' })).toBeChecked());
    expect(screen.getByRole('button', { name: '预演当前 AI 草稿' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /我确认允许在本轮范围内自动发送/ }));
    expect(screen.getByRole('button', { name: '授权并开始自动接待' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '授权并开始自动接待' }));
    await waitFor(() => expect(calls(bridge, 'set_reception')[0]?.params).toEqual({ enabled: true, auto_send: true, manual_only: false }));
    expect(calls(bridge, 'preview_send')).toHaveLength(0);
  });

  it.each(['identity', 'messages', 'ai'] as const)('still requires the current %s check to start automatic reception', async key => {
    const bridge = new FakeBridge(); bridge.snapshot.reception.scope = { mode: 'incoming', conversation_ids: [], max_auto_replies: 5 };
    bridge.snapshot.checks[key] = { ...bridge.snapshot.checks[key], status: 'failed' };
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    const consent = await screen.findByRole('checkbox', { name: /我确认允许在本轮范围内自动发送/ });
    expect(consent).toBeDisabled();
    expect(screen.getByRole('button', { name: '授权并开始自动接待' })).toBeDisabled();
    expect(calls(bridge, 'set_reception')).toHaveLength(0);
  });

  it('preserves independent automatic AI and send budgets and the reception start time', async () => {
    const bridge = new FakeBridge(); bridge.snapshot.reception.started_at_ms = 1793950000000;
    bridge.snapshot.reception.budget = { limit: 5, used: 1, remaining: 4, ai_used: 3, ai_remaining: 2 };
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    expect(await screen.findByText('自动 AI 额度：已用 3 / 5，剩余 2 次。')).toBeInTheDocument();
    expect(screen.getByText('自动发送额度：已用 1 / 5，剩余 4 次。')).toBeInTheDocument();
    expect(screen.getByText(/接待起点：/)).toHaveTextContent('暂停后恢复将延续已有起点及使用记录');
  });

  it('shows null budgets as unlimited while keeping usage counts', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.reception.budget = { limit: null, used: 101, remaining: null, ai_used: 105, ai_remaining: null, unlimited: true };
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    expect(await screen.findByText('接待 AI 已用 105 次，不限次数。')).toBeInTheDocument();
    expect(screen.getByText('自动发送已用 101 次，不限次数。')).toBeInTheDocument();
  });

  it('reports native AI check cancellation without inventing a failed job', async () => {
    const bridge = new FakeBridge(); const original = bridge.invoke.bind(bridge);
    bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}) => method === 'check_ai' ? { cancelled: true } as T : original<T>(method, params);
    await openApp(bridge); fireEvent.click(screen.getByRole('button', { name: '连接与设置', exact: true }));
    fireEvent.click(await screen.findByRole('button', { name: '暂停并检查 AI' })); await screen.findByText('已取消本次操作。');
  });

  it('does not invent service readiness without a desktop bridge', async () => {
    delete window.reception; render(<App />);
    expect(await screen.findByText(/未找到桌面服务桥/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '设置自动接待' })).toBeDisabled();
  });
});

describe('merchant card and adjustable reception layout', () => {
  it('shows only the current company name, with loading, failure and retry states', async () => {
    const bridge = new FakeBridge();
    bridge.snapshot.app = { ...bridge.snapshot.app, mode: 'live', seller_id: '123456', seller_suffix: '3456', account_verified: true, company_name: 'Example Trading Company' };
    await openApp(bridge);
    const navigation = screen.getByRole('navigation', { name: '主导航' });
    const card = navigation.querySelector('.merchant')!;
    expect(card).toHaveTextContent('Example Trading Company');
    expect(card).toHaveAttribute('title', 'Example Trading Company');
    expect(card.textContent).not.toMatch(/尾号|当前账号工作区|3456/);
    bridge.snapshot.app.company_name = null;
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(card).toHaveTextContent('正在读取公司名称'));
    bridge.snapshot.app.company_name = '';
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(card).toHaveTextContent('公司名称读取失败'));
    const readsBeforeRetry = calls(bridge, 'get_snapshot').length;
    fireEvent.click(within(card as HTMLElement).getByRole('button', { name: '重试读取公司名称' }));
    await waitFor(() => expect(calls(bridge, 'refresh_company_profile')).toHaveLength(1));
    expect(calls(bridge, 'get_snapshot')).toHaveLength(readsBeforeRetry);
    bridge.snapshot.app.account_verified = false;
    bridge.snapshot.app.company_name = 'Other seller name';
    act(() => bridge.emit({ event: 'state.changed', data: {} }));
    await waitFor(() => expect(card).toHaveTextContent('账号尚未核验'));
    expect(card).not.toHaveTextContent('Other seller name');
  });

  it('saves both split positions, restores them after reopening, and keeps small windows in drawer mode', async () => {
    const height = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.classList.contains('requirements-column') ? 608 : 0; });
    try {
      const bridge = new FakeBridge();
      bridge.preferences.requirements_ratio = 0.55;
      const first = render(<App bridge={bridge} />);
      await screen.findByRole('textbox', { name: '回复正文' });
      const side = screen.getByRole('separator', { name: '调整右侧栏宽度' });
      const vertical = screen.getByRole('separator', { name: '调整采购需求与业务补充高度' });
      expect(vertical).toHaveAttribute('aria-valuenow', '330');
      fireEvent.pointerDown(side, { button: 0, clientX: 700 });
      fireEvent.pointerMove(document, { clientX: 650 });
      fireEvent.pointerUp(document);
      await waitFor(() => expect(bridge.preferences.details_width).toBe(370));
      fireEvent.pointerDown(vertical, { button: 0, clientY: 300 });
      fireEvent.pointerMove(document, { clientY: 350 });
      fireEvent.pointerUp(document);
      await waitFor(() => expect(bridge.preferences.requirements_ratio).toBeCloseTo(380 / 600));
      first.unmount();
      render(<App bridge={bridge} />);
      await screen.findByRole('textbox', { name: '回复正文' });
      expect(screen.getByRole('separator', { name: '调整右侧栏宽度' })).toHaveAttribute('aria-valuenow', '370');
      expect(screen.getByRole('separator', { name: '调整采购需求与业务补充高度' })).toHaveAttribute('aria-valuenow', '380');
      fireEvent.keyDown(screen.getByRole('separator', { name: '调整采购需求与业务补充高度' }), { key: 'ArrowUp' });
      await waitFor(() => expect(bridge.preferences.requirements_ratio).toBeCloseTo(364 / 600));
      fireEvent.doubleClick(screen.getByRole('separator', { name: '调整采购需求与业务补充高度' }));
      fireEvent.doubleClick(screen.getByRole('separator', { name: '调整右侧栏宽度' }));
      await waitFor(() => expect(bridge.preferences).toMatchObject({ details_width: 360, requirements_ratio: 0.68 }));
      act(() => { window.innerWidth = 900; window.dispatchEvent(new Event('resize')); });
      expect(screen.queryByRole('separator', { name: '调整右侧栏宽度' })).not.toBeInTheDocument();
      expect(bridge.preferences).toMatchObject({ details_width: 360, requirements_ratio: 0.68 });
    } finally { height.mockRestore(); }
  });

  it('temporarily constrains saved pane sizes when the window becomes short or narrow', async () => {
    let heightPx = 260;
    let notifyResize: ((height: number) => void) | undefined;
    const height = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.classList.contains('requirements-column') ? heightPx : 0; });
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: (entries: { contentRect: { height: number } }[]) => void) {}
      observe(element: Element) { if (element.classList.contains('requirements-column')) notifyResize = value => this.callback([{ contentRect: { height: value } }]); }
      disconnect() {}
    });
    try {
      const bridge = new FakeBridge();
      bridge.preferences = { ...bridge.preferences, details_width: 600, requirements_ratio: 0.8 };
      await openApp(bridge);
      const side = screen.getByRole('separator', { name: '调整右侧栏宽度' });
      const vertical = screen.getByRole('separator', { name: '调整采购需求与业务补充高度' });
      await waitFor(() => expect(side).toHaveAttribute('aria-valuenow', '596'));
      expect(vertical).toHaveAttribute('aria-valuenow', '126');
      expect(bridge.preferences).toMatchObject({ details_width: 600, requirements_ratio: 0.8 });
      expect(notifyResize).toBeTypeOf('function');
      heightPx = 608;
      act(() => notifyResize?.(heightPx));
      expect(vertical).toHaveAttribute('aria-valuenow', '460');
      act(() => { window.innerWidth = 1900; window.dispatchEvent(new Event('resize')); });
      expect(side).toHaveAttribute('aria-valuenow', '600');
      expect(bridge.preferences).toMatchObject({ details_width: 600, requirements_ratio: 0.8 });
    } finally { height.mockRestore(); vi.unstubAllGlobals(); }
  });
});


describe('0.3.16 conversation controls', () => {
  it('follows the current case until the user pins an older case', async () => {
    const bridge = new FakeBridge();
    const original = bridge.details.emma.case!;
    const other = { ...original, case_id: 'case-other', requirements_format: 'ai-v1' as const, requirements: { product: { value: '帽子', evidence_ids: [] } } };
    original.requirements_format = 'ai-v1'; original.requirements = { product: { value: '眼镜', evidence_ids: [] } };
    bridge.details.emma.cases = [original, other];
    bridge.detailHandler = async params => { const detail = clone(bridge.details[String(params.conversation_id)]); detail.case = detail.cases.find(item => item.case_id === (params.case_id || detail.conversation.case_id)) ?? null; return detail; };
    await openApp(bridge);
    bridge.details.emma.conversation.case_id = other.case_id;
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue('case-other'));
    fireEvent.change(screen.getByRole('combobox', { name: '当前采购事项' }), { target: { value: original.case_id } });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue(original.case_id));
    act(() => bridge.emit({ event: 'state.changed', data: { conversation_id: 'emma' } }));
    await waitFor(() => expect(calls(bridge, 'get_conversation').at(-1)?.params.case_id).toBe(original.case_id));
    expect(screen.getByRole('combobox', { name: '当前采购事项' })).toHaveValue(original.case_id);
  });
  it('submits history directly and blocks repeated clicks while the task is active', async () => {
    const bridge = new FakeBridge(); await openApp(bridge);
    const button = screen.getByRole('button', { name: '补全当前客户历史' });
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(calls(bridge, 'fill_history')).toHaveLength(1));
    expect(button).toBeDisabled();
  });
  it('closes information popovers on outside clicks, Escape and customer changes, retaining the summary icon', async () => {
    const bridge = new FakeBridge(); bridge.details.emma.conversation_summary = { conversation_id: 'emma', body: '会话摘要', revision: 1, updated_at: 1 };
    await openApp(bridge);
    expect(screen.getByLabelText('会话摘要').querySelector('.lucide-book-open')).not.toBeNull();
    const status = screen.getByLabelText('查看会话状态'); const help = screen.getByRole('button', { name: '附件格式与限制' });
    fireEvent.click(status); expect(screen.getByLabelText('此客户接待状态')).toBeVisible();
    fireEvent.pointerDown(help); fireEvent.click(help); expect(screen.queryByLabelText('此客户接待状态')).not.toBeVisible();
    expect(help).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(document, { key: 'Escape' }); expect(help).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(help); fireEvent.pointerDown(document.body); expect(help).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(help); fireEvent.click(screen.getByRole('button', { name: /Sofia.*Do you have this mug in white/s }));
    await screen.findByRole('main', { name: 'Sofia 的客户接待' }); expect(screen.getByRole('button', { name: '附件格式与限制' })).toHaveAttribute('aria-expanded', 'false');
    expect(document.querySelector('.supplement-scroll + .supplement-toolbar')).not.toBeNull();
  });

});

it('shows executor failure without losing editing or allowing queued sends and checks', async () => {
  const bridge = new FakeBridge();
  bridge.snapshot.reception.running = true;
  bridge.snapshot.executor_health = { state: 'failed', worker_alive: false, stage: 'dispatch', job_id: null, last_progress_at_ms: 1, observed_at_ms: 2, queue_depth: 1, error_code: 'failed' };
  await openApp(bridge);
  expect(screen.getAllByText('后台执行异常，自动发送已暂停').length).toBeGreaterThan(0);
  expect(sendButton()).toBeDisabled();
  expect(screen.getByRole('button', { name: /润色当前回复/ })).toBeDisabled();
  fireEvent.change(textarea(), { target: { value: '保留的正文' } });
  await waitFor(() => expect(calls(bridge, 'save_chat_draft').length).toBeGreaterThan(0));
  expect(textarea()).toHaveValue('保留的正文');
  fireEvent.click(screen.getByRole('button', { name: '连接与设置' }));
  expect(await screen.findByRole('button', { name: '暂停并检查 AI' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '授权并开始自动接待' })).toBeDisabled();
});

it('treats long external waits as healthy and recovery as temporarily unavailable', async () => {
  const { executorHealth } = await import('../src/renderer/executorHealth');
  const bridge = new FakeBridge();
  const health = { state: 'waiting_external' as const, worker_alive: true, stage: 'ai', job_id: 'job', last_progress_at_ms: 1, observed_at_ms: 999999999, queue_depth: 0, error_code: null };
  bridge.snapshot.executor_health = health;
  expect(executorHealth(bridge.snapshot).ready).toBe(true);
  bridge.snapshot.executor_health = { ...health, state: 'idle' };
  expect(executorHealth(bridge.snapshot).ready).toBe(true);
  bridge.snapshot.executor_health = { ...health, state: 'recovering' };
  expect(executorHealth(bridge.snapshot)).toEqual({ ready: false, message: '后台正在恢复，暂不能启动新任务或授权自动发送' });
});

it.each([10, 20])('keeps independent failure visible over a delayed healthy snapshot observed at %i', async observed => {
  const bridge = new FakeBridge(); bridge.snapshot.reception.running = true;
  const initial = { state: 'idle' as const, worker_alive: true, stage: 'idle', job_id: null, last_progress_at_ms: 1, observed_at_ms: observed, queue_depth: 0, error_code: null };
  bridge.snapshot.executor_health = initial;
  await openApp(bridge);
  const blocked = deferred<Snapshot>();
  const original = bridge.invoke.bind(bridge);
  bridge.invoke = async <T,>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> => method === 'get_snapshot' ? await blocked.promise as T : original<T>(method, params);
  fireEvent.click(screen.getByRole('button', { name: '刷新本地工作区' }));
  act(() => bridge.emit({ event: 'executor.health', data: { ...initial, state: 'failed', worker_alive: false, observed_at_ms: 20 } }));
  expect((await screen.findAllByText('后台执行异常，自动发送已暂停')).length).toBeGreaterThan(0);
  expect(sendButton()).toBeDisabled();
  await act(async () => blocked.resolve(clone(bridge.snapshot)));
  expect(screen.getAllByText('后台执行异常，自动发送已暂停').length).toBeGreaterThan(0);
  expect(sendButton()).toBeDisabled();
});
