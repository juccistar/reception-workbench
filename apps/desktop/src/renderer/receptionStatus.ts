import type { Case, ConversationDetail, ConversationSummary, Job, Outbox, Snapshot } from '../shared/api';
import { compareMessages } from './hooks/useReadAcknowledgement';
import { executorHealth } from './executorHealth';

export function effectiveReceptionStatus(snapshot: Snapshot | null, conversation: ConversationSummary) {
  const listed = snapshot?.conversations.find(item => item.conversation_id === conversation.conversation_id)?.reception_status;
  const detail = conversation.reception_status;
  return listed && (!detail || listed.observed_at >= detail.observed_at) ? listed : detail;
}

// This is a temporary task projection, never an optimistic message or a send receipt.
export function currentGeneration(detail: ConversationDetail, jobs: Job[]): Job | null {
  if (!detail.case) return null;
  const currentCase = detail.case;
  const candidates = jobs.filter(job => job.kind === 'generate_draft'
    && job.conversation_id === detail.conversation.conversation_id
    && job.case_id === currentCase.case_id).sort((a, b) => b.created_at - a.created_at);
  const latest = candidates[0];
  if (!latest || !(['queued', 'running'].includes(latest.status) || (latest.status === 'deferred' && !!latest.retry_at))) return null;
  const saving = latest.stage === 'save_draft' || latest.stage === 'prepare_send' && latest.draft_id && latest.draft_id === detail.draft?.draft_id;
  if (!saving && latest.source_case_revision !== undefined && latest.source_case_revision !== currentCase.revision) return null;
  if (latest.source_message_id) {
    const buyer = detail.messages.filter(message => message.role === 'buyer').sort((a, b) => compareMessages(b, a))[0];
    if (buyer?.message_id !== latest.source_message_id) return null;
  }
  return latest;
}

export type ReplyPhase = 'confirming' | 'queued' | 'running' | 'retry' | 'saving' | 'prepared' | 'sending' | 'receipt' | 'complete' | 'stopped' | 'unknown';
export interface ReplyProgress { key: string; phase: ReplyPhase; message?: string; created_at?: number; note?: string }

/** Current activity only; send acceptance comes from persisted batches/receipts. */
export function currentReplyProgress(detail: ConversationDetail, jobs: Job[], now: number, automaticRunning: boolean): ReplyProgress | null {
  const cid = detail.conversation.conversation_id;
  const selectedCase = detail.case?.case_id;
  const currentCase = selectedCase && selectedCase === detail.conversation.case_id;
  const generation = currentGeneration(detail, jobs);
  const latestGeneration = jobs.filter(job => job.kind === 'generate_draft' && job.conversation_id === cid && job.case_id === selectedCase)
    .sort((a, b) => b.created_at - a.created_at)[0];
  const batches = currentCase ? (detail.reply_batches ?? []).filter(batch => (!batch.origin || batch.origin === 'automatic')
    && (!batch.channel || batch.channel === 'international') && (!batch.conversation_id || batch.conversation_id === cid))
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0)) : [];
  const activeBatch = batches.find(batch => ['prepared', 'sending', 'waiting_sync', 'waiting_retry', 'waiting_receipt', 'unknown'].includes(batch.status)
    || (batch.pending_operation_ids ?? []).length > 0);
  const recentBatch = batches[0];
  const batch = activeBatch ?? (recentBatch && now - (recentBatch.updated_at ?? 0) < 4000
    && (!latestGeneration || (recentBatch.created_at ?? 0) >= latestGeneration.created_at) ? recentBatch : null);
  if (batch) {
    const count = `已发送 ${batch.accepted}/${batch.total} 条`;
    const index = Math.min(batch.total, (batch.current_index ?? batch.accepted) + 1);
    const phase: ReplyPhase = ['unknown', 'waiting_receipt'].includes(batch.status) ? 'unknown'
      : batch.status === 'waiting_retry' ? 'retry' : batch.status === 'waiting_sync' ? 'receipt'
      : ['accepted', 'completed', 'succeeded'].includes(batch.status) ? 'complete'
      : ['stopped', 'cancelled', 'failed'].includes(batch.status) ? 'stopped' : batch.status === 'prepared' ? 'prepared' : 'sending';
    const message = phase === 'unknown' ? `${count}，第 ${index} 条发送结果待确认`
      : phase === 'complete' ? `本轮回复已发送，共 ${batch.accepted} 条`
      : phase === 'stopped' ? `${count}，剩余回复已停止`
      : phase === 'retry' ? `${count}，${batch.retry_at && batch.retry_at > now ? `${Math.ceil((batch.retry_at - now) / 1000)} 秒后重试` : '等待重试'}`
      : phase === 'receipt' ? `${count}，正在同步发送结果`
      : `${phase === 'prepared' ? '准备发送' : '正在提交'}第 ${index}/${batch.total} 条${phase === 'sending' ? '，等待平台回执' : ''}`;
    return { key: `automatic:${cid}`, phase, message, note: batch.message || undefined };
  }
  const sends = jobs.filter(job => ['automatic_send', 'continuity_send'].includes(job.kind) && job.conversation_id === cid && job.case_id === selectedCase)
    .sort((a, b) => b.created_at - a.created_at);
  const send = sends.find(job => ['queued', 'running', 'deferred', 'unknown'].includes(job.status)) ?? sends[0];
  if (send && (!generation || send.created_at >= generation.created_at)) {
    const live = ['queued', 'running', 'deferred', 'unknown'].includes(send.status);
    const recent = now - (send.completed_at ?? 0) < 4000 && (!latestGeneration || send.created_at >= latestGeneration.created_at);
    const accepted = send.status === 'succeeded' && send.result?.status === 'accepted';
    if (live || recent && (send.status !== 'succeeded' || accepted || send.result?.batch_id)) {
      const phase: ReplyPhase = send.status === 'unknown' || send.result?.status === 'unknown' ? 'unknown'
        : accepted ? 'complete' : send.status === 'deferred' ? 'retry'
        : ['cancelled', 'failed', 'stale'].includes(send.status) ? 'stopped'
        : send.status === 'queued' || send.result?.batch_id ? 'prepared' : 'sending';
      return { key: `automatic:${cid}`, phase, message: phase === 'complete' ? '本轮回复已发送'
        : phase === 'unknown' ? '发送结果待确认' : phase === 'stopped' ? '自动回复已停止'
        : phase === 'retry' ? send.message : phase === 'prepared' ? '回复已保存，正在准备发送' : '正在提交回复，等待平台回执',
        note: phase === 'stopped' ? send.error?.message || send.message : undefined };
    }
  }
  if (generation && (!generation.automatic || automaticRunning) && !detail.conversation.human_owned) {
    return { key: generation.automatic ? `automatic:${cid}` : generation.job_id,
      phase: generation.status === 'deferred' ? 'retry' : generation.status === 'queued' ? 'queued'
        : generation.stage === 'save_draft' ? 'saving' : generation.stage === 'prepare_send' ? 'prepared' : 'running',
      message: generation.message, created_at: generation.created_at };
  }
  const draft = detail.draft;
  if (currentCase && automaticRunning && !detail.conversation.human_owned && latestGeneration?.status === 'succeeded'
      && latestGeneration.result?.draft_id) {
    if (draft?.status === 'pending' && latestGeneration.result.draft_id === draft.draft_id) {
      return { key: `automatic:${cid}`, phase: 'prepared', message: '回复已保存，等待发送' };
    }
    if (now - (latestGeneration.completed_at ?? 0) < 4000 && latestGeneration.result.draft_id !== draft?.draft_id) {
      return { key: `automatic:${cid}`, phase: 'prepared', message: '回复已保存，正在更新发送状态' };
    }
  }
  return null;
}

export function receptionLabel(snapshot: Snapshot | null, detail: ConversationDetail, connected: boolean, closing: boolean): string {
  if (closing) return '工作台正在关闭';
  if (!connected) return '接待服务未连接';
  const health = executorHealth(snapshot);
  if (!health.ready) return health.message;
  const projected = effectiveReceptionStatus(snapshot, detail.conversation);
  if (projected && (detail.case || detail.conversation.case_id || !projected.blockers.some(blocker => blocker.code === 'case' && blocker.message === projected.reason))) return projected.reason;
  if (projected && !detail.case && !detail.conversation.case_id) return projected.blockers.find(blocker => blocker.code !== 'case')?.message || (detail.conversation.human_owned ? '此客户由人工接待' : '客户会话');
  if (detail.conversation.human_owned) return '此客户由人工接待';
  if (!snapshot?.reception.running) return '自动接待已暂停';
  const { scope } = snapshot.reception;
  if (scope.mode === 'selected' && !scope.conversation_ids.includes(detail.conversation.conversation_id)) return '此客户不在当前配置名单';
  // A global grant is not proof that this case currently passes every scheduling gate.
  return scope.mode === 'incoming' ? '全局接待已开启 · 仅处理授权后新消息' : '全局接待已开启 · 按授权条件处理';
}

export function registrationSummary(outbox: Outbox[], current: Pick<Case, 'case_id' | 'revision'>): string {
  const records = outbox.filter(item => item.kind === 'case_upsert' && item.case_id === current.case_id)
    .sort((a, b) => b.case_revision - a.case_revision || b.updated_at - a.updated_at);
  const unknown = records.find(item => item.status === 'unknown');
  if (unknown) return `飞书：v${unknown.case_revision} 登记待核查${unknown.case_revision !== current.revision ? `（当前 v${current.revision}）` : ''}`;
  if (!records.length) return '飞书：尚无登记记录';
  const latest = records.find(item => item.case_revision === current.revision) ?? records[0];
  const labels: Record<string, string> = { succeeded: '登记已确认', confirmed: '登记已确认', synced: '登记已确认', pending: '待同步', queued: '等待同步', running: '正在同步', syncing: '正在同步', sending: '正在同步', failed: '同步失败', rejected: '登记未受理', disabled: '未启用同步', blocked: '同步未就绪', deferred: '等待同步', stale: '版本已过期', superseded: '已由新版本取代', cancelled: '同步已取消' };
  const result = `飞书：v${latest.case_revision} ${labels[latest.status] ?? '登记状态待确认'}`;
  return latest.case_revision === current.revision ? result : `${result} · 当前 v${current.revision} 尚未登记`;
}
