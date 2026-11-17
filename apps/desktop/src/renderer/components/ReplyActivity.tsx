import { CheckCheck, CircleAlert, Clock3, Sparkles } from 'lucide-react';
import type { ReplyPhase } from '../receptionStatus';

export function ReplyActivity({ phase, message, note, elapsedSeconds }: { phase: ReplyPhase; message?: string; note?: string; elapsedSeconds?: number }) {
  const busy = !['complete', 'stopped', 'unknown'].includes(phase);
  const generating = ['confirming', 'queued', 'running', 'retry', 'saving'].includes(phase);
  const labels: Record<ReplyPhase, string> = {
    confirming: '生成任务已提交，正在确认状态…', queued: '等待 AI 生成回复…', running: message || '正在生成回复…',
    retry: message || '正在等待自动重试并整理回复…', saving: '正在整理并保存回复…',
    prepared: message || '回复已保存，正在准备发送', sending: message || '正在提交回复，等待平台回执',
    receipt: message || '正在等待平台回执', complete: message || '本轮回复已发送',
    stopped: message || '自动回复已停止', unknown: message || '发送结果待确认',
  };
  return <div className={`reply-activity reply-activity-${phase} ${busy ? 'reply-activity-running' : ''}`} role="status" aria-live="polite" aria-atomic="true" aria-busy={busy} aria-label={generating ? 'AI 回复生成状态' : '自动回复进度'}>
    <div className="reply-activity-label"><Sparkles size={13} aria-hidden="true" /><span>AI 回复助手</span></div>
    <div className="reply-activity-bubble">
      {busy ? <span className="reply-activity-dots" aria-hidden="true"><i /><i /><i /></span>
        : phase === 'complete' ? <CheckCheck size={16} aria-hidden="true" />
        : phase === 'unknown' ? <Clock3 size={16} aria-hidden="true" /> : <CircleAlert size={16} aria-hidden="true" />}
      <span>{labels[phase]}</span>
    </div>
    {(note || elapsedSeconds !== undefined) && <span className="reply-activity-note">{note}{note && elapsedSeconds !== undefined ? ' · ' : ''}{elapsedSeconds !== undefined ? `已用时 ${elapsedSeconds} 秒` : ''}</span>}
  </div>;
}
