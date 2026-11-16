import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, Check, LoaderCircle, X, Inbox } from 'lucide-react';
import type { CaseState, CheckStatus, MessageSync } from '../../shared/api';
export const stateLabels: Record<CaseState, string> = { clarifying: '需求整理中', waiting_customer: '等待客户', waiting_quote: '待我报价', needs_human: '需人工', human_owned: '人工接管', closed: '已结束' };
export const fieldLabels: Record<string, string> = { product: '产品', quantity: '数量', specifications: '规格', customization: '定制', destination: '目的地', delivery: '交期', budget: '预算', packaging: '包装', contact: '联系方式', sample_cost: '样品费用', sample_lead_time: '样品周期' };
export const checkLabels: Record<CheckStatus, string> = { not_checked: '尚未检查', running: '检查中', passed: '已通过', failed: '未通过', blocked: '未就绪', demo: '演示结果' };
export const jobLabels: Record<string, string> = { sync_messages: '同步近期消息', fill_history: '补全客户历史', generate_draft: '生成回复建议', manual_suggestion: '生成回复建议', translation: '中文翻译', check_connection: '检查平台读取', check_ai: '检查 AI', preview_send: '发送预演', confirm_send: '发送回复', send_chat: '发送聊天消息', automatic_send: '自动发送回复', continuity_send: '发送接待跟进', run_outbox: '同步交接记录', check_feishu: '检查飞书', reconcile_operation: '核查原操作', source_product: '查找产品货源', load_product_main_image: '读取商品主图' };
export const jobStatusLabels: Record<string, string> = { queued: '等待执行', running: '处理中', succeeded: '已完成', deferred: '待补读', failed: '失败', cancelled: '已取消', stale: '依据已变化', unknown: '结果未知', pending: '待处理', accepted: '平台已受理', rejected: '未受理', blocked: '未就绪', demo: '演示', synced: '已同步', superseded: '已由新版本取代' };
export const time = (value?: number | null) => value ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '时间未知';
export const messageDateTime = (value?: number | null) => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '时间未知';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '时间未知';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};
export const dateTime = (value?: number | null) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '尚无记录';
export function MessageSyncSummary({ sync }: { sync?: MessageSync }) {
  if (!sync) return null;
  return <p className="form-description" role="status">消息同步：每 {sync.poll_seconds} 秒检测新活动，最近检测：{dateTime(sync.checked_at)}；待同步 {sync.pending_count} 个会话{sync.active_read_count !== undefined && <>；排队或读取中 {sync.active_read_count} 个会话</>}{sync.background_pending_count !== undefined && <>；历史待补 {sync.background_pending_count} 个会话（空闲时读取）</>}{sync.waiting_activity_count !== undefined && <>；空历史等待新活动 {sync.waiting_activity_count} 个会话（已暂停自动读取）</>}{sync.last_body_sync_at !== undefined && <>；最近正文同步：{dateTime(sync.last_body_sync_at)}</>}。{sync.error_code && '消息检测暂缓，正在等待重试。'}</p>;
}
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'green' | 'amber' | 'red' | 'neutral' }) { return <span className={`badge badge-${tone}`}>{children}</span>; }
export function StateBadge({ state }: { state: CaseState }) { return <Badge tone={state === 'needs_human' || state === 'human_owned' || state === 'waiting_quote' ? 'amber' : state === 'clarifying' ? 'green' : 'neutral'}>{state === 'waiting_quote' ? '待报价' : stateLabels[state] ?? state}</Badge>; }
export function Button({ children, loading, variant = 'secondary', className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { loading?: boolean; variant?: 'primary' | 'secondary' | 'ghost' | 'danger' }) {
  return <button type="button" {...props} disabled={props.disabled || loading} className={`button button-${variant} ${className}`} aria-busy={loading || undefined}>{loading && <LoaderCircle size={16} className="spin" />}{children}</button>;
}
export function IconButton({ label, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) { return <button type="button" {...props} className={`icon-button ${props.className ?? ''}`} aria-label={label} title={props.title ?? label}>{children}</button>; }
export function Empty({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) { return <div className="empty">{icon ?? <Inbox size={32} strokeWidth={1.3} />}<strong>{title}</strong>{children && <p>{children}</p>}</div>; }
export function Feedback({ children, tone = 'amber' }: { children: ReactNode; tone?: 'amber' | 'red' | 'green' }) { return <div className={`feedback feedback-${tone}`} role={tone === 'red' ? 'alert' : 'status'}>{tone === 'green' ? <Check size={16} /> : <AlertCircle size={16} />}<div>{children}</div></div>; }
export function Drawer({ title, children, onClose, side = 'right', className = '' }: { title: string; children: ReactNode; onClose: () => void; side?: 'left' | 'right' | 'center'; className?: string }) {
  const root = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose); onCloseRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const panel = root.current;
    const first = panel?.querySelector<HTMLElement>('button, input, select, textarea, [tabindex="0"]');
    first?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onCloseRef.current(); }
      if (event.key !== 'Tab' || !panel) return;
      const focusable = [...panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')].filter(element => element.getClientRects().length > 0);
      const start = focusable[0]; const end = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === start || !panel.contains(document.activeElement))) { event.preventDefault(); end?.focus(); }
      else if (!event.shiftKey && (document.activeElement === end || !panel.contains(document.activeElement))) { event.preventDefault(); start?.focus(); }
    };
    document.addEventListener('keydown', key, true);
    return () => { document.removeEventListener('keydown', key, true); previous?.focus(); };
  }, []);
  return <div className={`drawer-layer drawer-${side}`}><div className="drawer-scrim" onClick={onClose} /><div ref={root} className={`drawer-panel ${className}`} role="dialog" aria-modal="true" aria-label={title}><div className="drawer-title"><h2>{title}</h2><IconButton label={`关闭${title}`} onClick={onClose}><X size={19} /></IconButton></div>{children}</div></div>;
}
