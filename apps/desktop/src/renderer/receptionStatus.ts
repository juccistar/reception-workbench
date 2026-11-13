import type { Case, ConversationDetail, ConversationSummary, Job, Outbox, Snapshot } from '../shared/api';
import { compareMessages } from './hooks/useReadAcknowledgement';

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
  if (latest.source_case_revision !== undefined && latest.source_case_revision !== currentCase.revision) return null;
  if (latest.source_message_id) {
    const buyer = detail.messages.filter(message => message.role === 'buyer').sort((a, b) => compareMessages(b, a))[0];
    if (buyer?.message_id !== latest.source_message_id) return null;
  }
  return latest;
}

export function receptionLabel(snapshot: Snapshot | null, detail: ConversationDetail, connected: boolean, closing: boolean): string {
  if (closing) return '工作台正在关闭';
  if (!connected) return '接待服务未连接';
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
