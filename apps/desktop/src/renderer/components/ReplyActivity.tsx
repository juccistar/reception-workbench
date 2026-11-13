import { Clock3, Sparkles } from 'lucide-react';

export function ReplyActivity({ phase, message, elapsedSeconds }: { phase: 'confirming' | 'queued' | 'running' | 'retry'; message?: string; elapsedSeconds?: number }) {
  const running = phase === 'running';
  const label = running ? (message || '任务执行中…') : phase === 'retry' ? '正在等待自动重试并整理回复…' : phase === 'queued' ? '等待 AI 生成回复…' : '生成任务已提交，正在确认状态…';
  const note = running ? '生成中的内容尚未发送' : phase === 'queued' ? '任务已排队，尚未开始生成' : '保留原任务，请勿重复提交';
  return <div className={`reply-activity ${running ? 'reply-activity-running' : 'reply-activity-queued'}`} role="status" aria-live="polite" aria-atomic="true" aria-label="AI 回复生成状态">
    <div className="reply-activity-label"><Sparkles size={13} aria-hidden="true" /><span>AI 回复助手</span></div>
    <div className="reply-activity-bubble">
      {running ? <span className="reply-activity-dots" aria-hidden="true"><i /><i /><i /></span> : <Clock3 size={16} aria-hidden="true" />}
      <span>{label}</span>
    </div>
    <span className="reply-activity-note">{note}{elapsedSeconds !== undefined ? ` · 已等待 ${elapsedSeconds} 秒` : ''}</span>
  </div>;
}
