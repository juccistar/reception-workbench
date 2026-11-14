import { useEffect, useRef, useState } from 'react';
import { Button, messageDateTime } from './ui';
import './ReplyBatchRecords.css';

// The extra fields are optional while both channel projections are being extended.
type Batch = {
  batch_id: string; status: string; total: number; accepted: number; current_index: number | null;
  pending_operation_ids: string[]; origin?: string | null; channel?: string | null; phase?: string | null;
  created_at?: number | null; updated_at?: number | null; conversation_id?: string | null; chat_id?: string | null;
  parts?: { part_index: number; status: string; message_id?: string | null }[] | null;
  message?: string | null; retry_at?: number | null; wait_reason?: string | null; last_gate_reason?: string | null;
};

const reasons: Record<string, string> = {
  source_sync_pending: '正在同步会话', communication_token_missing: '未取得会话通信令牌，请检查连接',
  authorization_revoked: '发送授权已撤销', authorization_rejected: '发送授权未通过', source_changed: '会话来源已变化',
  basis_changed: '回复依据已变化', conversation_taken_over: '客户已由人工接管', closing: '工作台正在关闭',
  send_attempts_exhausted: '原条目已达到最多 3 次发送尝试', guard_evaluation_failed: '发送条件检查失败',
  sync_timeout: '等待会话同步超时', sync_failed: '会话同步失败', source_sync_timeout: '等待会话同步超时', source_sync_failed: '会话同步失败',
};
const statuses: Record<string, string> = {
  planned: '待发送', prepared: '待发送', sending: '发送中', submitted: '等待回执', accepted: '平台已受理',
  waiting_sync: '等待同步', waiting_retry: '等待重试', waiting_receipt: '等待核查', unknown: '结果未知',
  rejected: '未受理', stopped: '已停止', cancelled: '已停止', failed: '发送失败',
};
const active = new Set(['prepared', 'sending', 'waiting_retry', 'waiting_receipt', 'waiting_sync', 'unknown', 'generating']);
const completed = (batch: Batch) => ['accepted', 'completed', 'succeeded'].includes(batch.status);
const pending = (batch: Batch) => batch.pending_operation_ids ?? [];
const reason = (batch: Batch) => batch.message || reasons[batch.last_gate_reason || batch.wait_reason || ''] || (batch.last_gate_reason || batch.wait_reason ? '请查看发送记录中的状态' : '');
const statusLabel = (status: string) => statuses[status] || '状态待更新';

function progressText(batch: Batch, now: number) {
  const count = `已发送 ${batch.accepted}/${batch.total} 条`;
  const index = (batch.current_index ?? batch.accepted) + 1;
  if (batch.status === 'generating') return '正在生成自动回复';
  if (completed(batch)) return `自动回复已发送，共 ${batch.accepted} 条`;
  if (batch.status === 'stopped') return `${count}，剩余 ${Math.max(0, batch.total - batch.accepted)} 条已停止`;
  if (batch.status === 'waiting_sync') return `${count}，正在同步后继续`;
  if (batch.status === 'waiting_receipt' || batch.status === 'unknown') return `${count}，正在确认第 ${index} 条发送结果`;
  if (batch.status === 'waiting_retry') return batch.retry_at && batch.retry_at > now ? `${count}，第 ${index} 条将在 ${Math.ceil((batch.retry_at - now) / 1000)} 秒后重试` : `${count}，第 ${index} 条等待重试`;
  return `正在发送第 ${index} 条，${count}`;
}

type Props = {
  batches: Batch[]; channel: 'international' | 'whatsapp'; ownerId?: string;
  contactName?: (chatId: string) => string; onReconcile?: (operationId: string) => void; reconcileDisabled?: boolean;
  generating?: boolean;
};

export function ReplyBatchRecords({ batches, channel, ownerId, contactName, onReconcile, reconcileDisabled, generating = false }: Props) {
  const [now, setNow] = useState(Date.now());
  const seen = useRef(new Map<string, { status: string; completedAt?: number; stoppedHere?: boolean }>());
  const scoped = batches.filter(batch => (!batch.channel || batch.channel === channel) && (!ownerId || !batch.conversation_id || batch.conversation_id === ownerId));
  useEffect(() => {
    const stamp = Date.now();
    for (const batch of scoped) {
      const previous = seen.current.get(batch.batch_id);
      const completedAt = completed(batch) ? previous?.completedAt ?? (previous && !['accepted', 'completed', 'succeeded'].includes(previous.status) ? stamp : batch.updated_at || undefined) : undefined;
      const stoppedHere = batch.status === 'stopped' && !!previous && (previous.stoppedHere || active.has(previous.status));
      seen.current.set(batch.batch_id, { status: batch.status, completedAt, stoppedHere });
    }
  }, [batches, channel, ownerId]);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);

  const groups = new Map<string, Batch[]>();
  for (const batch of scoped) {
    const key = channel === 'whatsapp' ? batch.chat_id || '' : ownerId || '';
    groups.set(key, [...(groups.get(key) ?? []), batch]);
  }
  const current: Batch[] = [];
  for (const rows of groups.values()) {
    const automatic = rows.filter(batch => !batch.origin || batch.origin === 'automatic');
    const live = automatic.filter(batch => active.has(batch.status) || pending(batch).length > 0);
    if (live.length) { current.push(...live); continue; }
    const ordered = [...automatic].reverse().sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
    const latest = ordered[0];
    if (!latest) continue;
    const previous = seen.current.get(latest.batch_id);
    const transitioned = !!previous && active.has(previous.status);
    const finishedAt = previous?.completedAt ?? (completed(latest) && transitioned ? now : latest.updated_at);
    if (!generating && (latest.status === 'stopped' && (previous?.stoppedHere || transitioned) || completed(latest) && finishedAt && now - finishedAt < 3000)) current.push(latest);
  }
  const currentIds = new Set(current.map(batch => batch.batch_id));
  const history = scoped.filter(batch => !currentIds.has(batch.batch_id));
  const label = (batch: Batch) => `${batch.channel === 'whatsapp' || channel === 'whatsapp' ? 'WhatsApp' : '国际站'} · ${batch.origin === 'manual' ? '人工' : batch.origin === 'automatic' ? '自动' : '类型未标注'}${batch.phase === 'supplement' ? ' · 补充续答' : ''}${batch.chat_id ? ` · ${contactName?.(batch.chat_id) || batch.chat_id}` : ''}`;
  const actions = (batch: Batch) => onReconcile && ['waiting_receipt', 'unknown', 'stopped'].includes(batch.status) && pending(batch).map(id => <Button key={id} disabled={reconcileDisabled} onClick={() => onReconcile(id)}>核查原发送</Button>);
  if (!scoped.length && !generating) return null;
  return <section className="reply-batch-records" aria-label="发送进度与记录">
    {(generating || current.length > 0) && <section aria-label="自动回复进度">
      {generating && <div className="reply-batch-current" role="status"><strong>正在生成自动回复</strong></div>}
      {current.map(batch => <div className="reply-batch-current" role="status" key={batch.batch_id}>
        {channel === 'whatsapp' && <span className="reply-batch-contact">{batch.chat_id && (contactName?.(batch.chat_id) || batch.chat_id) || '单聊'}</span>}
        <strong>{progressText(batch, now)}</strong>{reason(batch) && <span>{reason(batch)}</span>}{actions(batch)}
      </div>)}
    </section>}
    {history.length > 0 && <details className="reply-batch-history"><summary>发送记录（{history.length}）</summary><div className="reply-batch-history-list">{history.map(batch => <details className="reply-batch-entry" key={batch.batch_id}><summary><span>{label(batch)}</span><span>{messageDateTime(batch.created_at)}</span><span>{batch.accepted}/{batch.total} 条已受理 · {statusLabel(batch.status)}</span></summary><div className="reply-batch-entry-content">{batch.parts?.length ? <ol>{[...batch.parts].sort((a, b) => a.part_index - b.part_index).map(part => <li key={part.part_index}>第 {part.part_index + 1} 条：{statusLabel(part.status)}</li>)}</ol> : <p>逐条状态暂不可用</p>}{reason(batch) && <p>原因：{reason(batch)}</p>}{actions(batch)}</div></details>)}</div></details>}
  </section>;
}
