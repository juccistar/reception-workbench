import type { ApiMethod, ConversationDetail, Draft, Job, Preferences, ReceptionBridge, ServiceEvent, Snapshot } from '../../src/shared/api';

// Synthetic, test-only bridge. It cannot access Accio or submit a customer message.
export function createReplyActivityFixture() {
  const clone = <T,>(value: T): T => structuredClone(value);
  const stamp = 1794034800000;
  const conversations = ['emma', 'sofia'].map((id, index) => ({ conversation_id: id, buyer_id: `buyer-${id}`, customer_name: index ? 'Sofia Martin' : 'Emma Wilson', country: index ? '法国' : '德国', last_message: index ? 'Do you have this mug in white?' : 'Can we start with 500 pieces and add our logo?', last_timestamp: stamp, case_id: `case-${id}`, state: 'clarifying' as const, human_owned: false, unread_count: 1, pending_count: 1 }));
  const details: Record<string, ConversationDetail> = {};
  for (const conversation of conversations) {
    const id = conversation.conversation_id;
    const current = { case_id: conversation.case_id, conversation_id: id, buyer_id: conversation.buyer_id, seller_id: 'demo-seller', customer_name: conversation.customer_name, procurement_key: '陶瓷杯采购', revision: 1, state: 'clarifying' as const, facts: { product: { value: '陶瓷杯', evidence_ids: [`message-${id}`], status: 'confirmed' as const }, quantity: { value: '500 pieces', evidence_ids: [`message-${id}`], status: 'confirmed' as const } }, missing: ['destination'], questions: {}, summary: '采购陶瓷杯 500 件，需要定制 Logo；收货目的地待确认。', updated_at: stamp };
    details[id] = { conversation, case: current, cases: [current], messages: [{ message_id: `message-${id}`, seller_id: 'demo-seller', buyer_id: conversation.buyer_id, conversation_id: id, role: 'buyer', text: conversation.last_message, timestamp_ms: stamp, attachments: [], own: false }], history: { status: 'complete', earliest_ms: stamp, latest_ms: stamp, updated_at: stamp, next_cursor: null, message: '合成会话，本地历史已补全' }, draft: null, chat_draft: null };
  }
  const check = { status: 'demo' as const, check_id: 'synthetic', checked_at: stamp, simulated: true, message: '合成界面验证，无外部调用' };
  const snapshot: Snapshot = { app: { mode: 'demo', version: '0.2.10', seller_id: 'demo-seller', seller_suffix: 'DEMO', account_verified: false }, reception: { running: false, auto_send: false, started_at_ms: null, scope: { mode: 'selected', conversation_ids: ['emma'], max_auto_replies: 3 }, budget: { limit: 3, used: 0, remaining: 3, ai_used: 0, ai_remaining: 3 } }, checks: { runtime: check, identity: check, messages: check, ai: check, send_preview: check, feishu: check }, conversations, jobs: [], outbox: [], policies: [], settings: { workctl_path: '', poll_seconds: 5, merge_seconds: 5, required_fields: ['product', 'quantity', 'destination'], feishu: {} } };
  let preferences: Preferences = { nav_preference: 'expanded', details_preference: 'expanded', list_width: 272, details_width: 320 };
  const listeners = new Set<(event: ServiceEvent) => void>();
  const calls: { method: ApiMethod; params: Record<string, unknown> }[] = [];
  const emit = (event: ServiceEvent) => { for (const listener of listeners) listener(clone(event)); };
  const publish = (job: Job) => { snapshot.jobs = [...snapshot.jobs.filter(item => item.job_id !== job.job_id), clone(job)]; emit({ event: 'job.updated', data: job }); };
  const job = (status: Job['status'] = 'running', id = 'emma', overrides: Partial<Job> = {}): Job => ({ job_id: `generation-${id}`, kind: 'generate_draft', conversation_id: id, case_id: `case-${id}`, status, stage: status === 'queued' ? 'queued' : 'generate_draft', message: '合成生成任务', created_at: stamp + 1000, started_at: status === 'queued' ? null : stamp + 1100, completed_at: null, source_message_id: `message-${id}`, source_case_revision: 1, automatic: false, ...overrides });
  const bridge: ReceptionBridge = {
    async invoke<T>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> {
      calls.push({ method, params: clone(params) });
      let result: unknown;
      if (method === 'get_snapshot') result = snapshot;
      else if (method === 'get_conversation') { const item = details[String(params.conversation_id)]; result = { ...item, case: item.cases.find(value => value.case_id === params.case_id) ?? item.case }; }
      else if (method === 'list_jobs') result = snapshot.jobs;
      else if (method === 'list_outbox') result = snapshot.outbox;
      else if (method === 'get_messages') result = { items: [], has_more: false };
      else if (method === 'generate_manual_suggestion') { const id = String(params.conversation_id); const queued = job('queued', id, { kind: 'manual_suggestion', stage: 'manual_suggestion' }); publish(queued); result = { job_id: queued.job_id }; }
      else if (method === 'begin_manual_edit' || method === 'takeover') { details[String(params.conversation_id)].conversation.human_owned = method === 'begin_manual_edit' || params.enabled === true; result = snapshot; }
      else if (method === 'save_chat_draft') {
        const item = details[String(params.conversation_id)];
        const draft: Draft = { draft_id: `chat-${item.conversation.conversation_id}`, conversation_id: item.conversation.conversation_id, case_id: item.case.case_id, revision: Number(params.expected_revision ?? 0) + 1, body: String(params.body), status: 'pending', purpose: 'chat', origin: 'manual', source_message_id: null, source_case_revision: item.case.revision, source_conversation_revision: 1, created_at: stamp, updated_at: stamp, validation: { ready: true, issues: [], sources: [] } };
        item.chat_draft = draft; result = draft;
      } else throw new Error(`合成预览不支持 ${method}`);
      return clone(result) as T;
    },
    onEvent(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    async getPreferences() { return clone(preferences); },
    async savePreferences(value) { preferences = { ...preferences, ...value }; return clone(preferences); },
    async chooseWorkctl() { return null; },
  };
  return { bridge, snapshot, details, calls, emit, publish, job };
}
