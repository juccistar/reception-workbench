import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowLeft, ArrowUpRight, BookOpen, CheckCheck, ChevronDown, ChevronRight, CircleHelp, ClipboardList, Clock3, FileText, Globe2, History, Languages, Layers3, ListFilter, MapPin, MessageCircle, Package, PanelRightClose, PencilLine, RefreshCw, Save, Search, Send, ShieldAlert, Sparkles, Tag, UserRound, UsersRound } from 'lucide-react';
import type { ConversationSummary, Message, ReadingSnapshot, ReceptionBridge, TranslationItem } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { currentGeneration, effectiveReceptionStatus, receptionLabel, registrationSummary } from '../receptionStatus';
import { compareMessages, useReadAcknowledgement, type ObservedMessage } from '../hooks/useReadAcknowledgement';
import { ReplyActivity } from './ReplyActivity';
import { MessageImages } from './MessageImages';
import { useTranslations } from '../hooks/useTranslations';
import { caseLabel } from './caseLabels';
import { SharedRequirementCard } from './SharedRequirementCard';

const scrollBehavior = (): ScrollBehavior => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ? 'auto' : 'smooth';
import { Badge, Button, Empty, Feedback, IconButton, StateBadge, dateTime, fieldLabels, jobLabels, time } from './ui';

export function CustomerList({ model, close }: { model: Workbench; close?: () => void }) {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const conversations = model.snapshot?.conversations ?? [];
  const filtered = conversations.filter(item => `${item.customer_name} ${item.last_message} ${item.country || ''}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())).filter(item => filter === 'all' || (filter === 'pending' && item.reply_pending === true) || (filter === 'human' && item.human_owned) || (filter === 'quote' && item.state === 'waiting_quote'));
  const choose = async (item: ConversationSummary) => { if (await model.selectConversation(item.conversation_id, item.case_id)) close?.(); };
  return <section className="customer-list" aria-label="客户列表">
    <div className="list-heading"><h2>客户接待 <span>{model.snapshot ? conversations.length : '—'}</span></h2><IconButton label="同步近期消息" disabled={!model.connected || model.busy('sync_messages')} onClick={() => void model.runJob('sync_messages')}><RefreshCw size={17} className={model.busy('sync_messages') ? 'spin' : ''} /></IconButton></div>
    <div className="list-search"><Search size={17} /><input aria-label="搜索已同步客户" placeholder="搜索客户、地区或最近消息" value={search} onChange={event => setSearch(event.target.value)} /></div>
    <div className="list-tabs" aria-label="筛选客户">{[['all', '全部'], ['pending', '待回复'], ['human', '人工接管'], ['quote', '待报价']].map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div>
    <div className="customer-rows">{model.loading ? <div className="list-loading">正在读取本地客户…</div> : filtered.length === 0 ? <Empty title={search || filter !== 'all' ? '没有匹配的客户' : '尚无已同步客户'}>{search ? '这里只搜索已同步的本地资料。' : '先在连接与设置中核验读取，再同步消息。'}</Empty> : filtered.map(item => <button type="button" className={`customer-row ${model.selection.conversationId === item.conversation_id ? 'active' : ''}`} key={item.conversation_id} data-conversation-id={item.conversation_id} onClick={() => void choose(item)} aria-current={model.selection.conversationId === item.conversation_id ? 'true' : undefined}>
      <div className="customer-top"><span className="customer-name">{item.customer_name}</span><time>{time(item.last_timestamp)}</time></div><p>{item.last_message || '暂无消息正文'}</p><div className="customer-bottom"><StateBadge state={item.state} />{item.reply_pending === true && <Badge tone="amber">待回复</Badge>}{item.unread_count > 0 && <span className="unread" aria-label={`${item.unread_count} 条未读`}>{item.unread_count}</span>}{item.human_owned && <UserRound size={13} aria-label="人工接管" />}</div>
    </button>)}</div>
    <div className="list-foot"><span className={`connection-dot ${model.connected ? 'on' : ''}`} />{model.connected ? '本地已同步客户' : '服务未连接'}<ListFilter size={13} /></div>
  </section>;
}

export function Requirements({ model, collapse, locate }: { model: Workbench; bridge?: ReceptionBridge; collapse?: () => void; locate: (id: string) => void }) {
  const detail = model.detail;
  if (!detail) return <div className="requirements"><Empty title="选择客户查看采购需求" /></div>;
  const current = detail.case;
  return <section className="requirements" aria-label="采购需求">
    <div className="requirements-heading"><h2>采购需求</h2>{collapse && <IconButton label="收起需求侧栏" aria-expanded="true" onClick={collapse}><PanelRightClose size={18} /></IconButton>}</div>
    <div className="requirements-scroll">
      {current && detail.cases.length > 1 && <select className="case-select" aria-label="当前采购事项" value={current.case_id} onChange={event => void model.selectConversation(detail.conversation.conversation_id, event.target.value)}>{detail.cases.map((item, index) => <option value={item.case_id} key={item.case_id}>{typeof item.requirements?.product?.value === 'string' && item.requirements.product.value || `采购事项 ${index + 1}`}</option>)}</select>}
      {current ? <SharedRequirementCard key={current.case_id} model={model} current={current} locate={locate} actions /> : <p className="requirements-empty">当前没有采购需求</p>}
    </div>
  </section>;
}

export { SupplementPanel } from './SupplementPanel';

export function Conversation({ model, bridge, openList, openDetails, registerLocate }: { model: Workbench; bridge?: ReceptionBridge; openList: () => void; openDetails: () => void; registerLocate: (locate: (id: string) => void) => void }) {
  const detail = model.detail;
  const editor = model.editor;
  const receiptAttempt = editor?.send;
  const successReceiptKey = receiptAttempt && ['accepted', 'succeeded'].includes(receiptAttempt.status) ? receiptAttempt.params.client_request_id : '';
  const [dismissedReceipts, setDismissedReceipts] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (!successReceiptKey || dismissedReceipts.has(successReceiptKey)) return;
    const dismiss = () => setDismissedReceipts(previous => new Set(previous).add(successReceiptKey));
    if (editor?.body) { dismiss(); return; }
    const timer = setTimeout(dismiss, 4000);
    return () => clearTimeout(timer);
  }, [successReceiptKey, editor?.body, dismissedReceipts]);
  const [olderByConversation, setOlderByConversation] = useState<Record<string, ObservedMessage[]>>({});
  const [hasMore, setHasMore] = useState<Record<string, boolean>>({});
  const [loadingMore, setLoadingMore] = useState(false);
  const [newMessages, setNewMessages] = useState(false);
  const [summaryExpanded, setSummaryExpanded] = useState(false);
  const statusPanel = useRef<HTMLDetailsElement>(null);
  const replyInput = useRef<HTMLTextAreaElement>(null);
  const [now, setNow] = useState(Date.now());
  const listRef = useRef<HTMLDivElement>(null);
  const previousConversation = useRef('');
  const wasVisible = useRef(false);
  const previousLastMessage = useRef('');
  const pinned = useRef(true);
  const older = olderByConversation[model.selection.conversationId] ?? [];
  const currentMessages = (detail?.messages ?? []).map(message => ({ ...message, observed_sequence: detail?.reading?.observed_sequence ?? null }));
  const messages = [...new Map([...older, ...currentMessages].map(message => [message.message_id, message])).values()].sort(compareMessages);
  const conversationId = model.selection.conversationId;
  const seller = model.snapshot?.app.seller_id ?? '';
  const [translate, setTranslate] = useState(false);
  useEffect(() => { try { setTranslate(localStorage.getItem(`reception.translation.${seller}`) === 'on'); } catch { setTranslate(false); } }, [seller]);
  const toggleTranslate = (enabled: boolean) => { setTranslate(enabled); try { localStorage.setItem(`reception.translation.${seller}`, enabled ? 'on' : 'off'); } catch { /* preference remains in memory */ } };
  const [visibleMessages, setVisibleMessages] = useState<{ conversationId: string; ids: string[] }>({ conversationId: '', ids: [] });
  const messageIds = messages.map(message => message.message_id).join('|');
  useLayoutEffect(() => {
    const root = listRef.current;
    if (!root) return;
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const bounds = root.getBoundingClientRect();
        const ids = [...root.querySelectorAll<HTMLElement>('[data-message-id]')].filter(node => {
          const box = node.getBoundingClientRect();
          return bounds.height > 0 && box.bottom > bounds.top && box.top < bounds.bottom;
        }).map(node => node.dataset.messageId!);
        setVisibleMessages(previous => previous.conversationId === conversationId && previous.ids.join('|') === ids.join('|') ? previous : { conversationId, ids });
      });
    };
    measure(); root.addEventListener('scroll', measure, { passive: true }); window.addEventListener('resize', measure);
    const observer = new ResizeObserver(measure); observer.observe(root);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); root.removeEventListener('scroll', measure); window.removeEventListener('resize', measure); };
  }, [conversationId, messageIds, translate]);
  const translationSources: { item: TranslationItem; version: string; requestable?: boolean }[] = messages.filter(message => visibleMessages.conversationId === conversationId && visibleMessages.ids.includes(message.message_id)
    && !!message.text.trim() && message.text.trim() !== '[image]' && message.text.trim() !== '[Card]' && message.text.length <= 50000)
    .map(message => ({ item: { kind: 'message' as const, source_id: message.message_id }, version: message.text }));
  const suggestion = detail?.conversation.conversation_id === conversationId ? detail.manual_suggestion : null;
  if (suggestion?.body.trim() && suggestion.body.length <= 50000) translationSources.push({ item: { kind: 'manual_suggestion', source_id: suggestion.suggestion_id }, version: suggestion.body, requestable: suggestion.status === 'ready' });
  const translationConfig = model.snapshot?.translation;
  const translationReady = model.snapshot?.app.mode === 'demo' && translationConfig?.enabled !== false || !!(translationConfig?.enabled && translationConfig.has_key && translationConfig.model);
  const translations = useTranslations(bridge, conversationId, translate, translationSources, seller, translationConfig?.revision ?? 0, translationReady);
  const [imageRevision, setImageRevision] = useState(0);
  const autoImages = new Set(messages.flatMap(message => message.attachments.flatMap((attachment, index) => attachment.kind === 'image' ? [JSON.stringify([message.message_id, index])] : [])).slice(-4));
  useEffect(() => bridge?.onEvent(event => {
    if (event.event !== 'state.changed') return;
    const data = event.data && typeof event.data === 'object' ? event.data as Record<string, unknown> : {};
    if (data.reason !== 'translations_updated' && (!data.conversation_id || data.conversation_id === conversationId)) setImageRevision(value => value + 1);
  }), [bridge, conversationId]);
  const visibleConversation = useRef(conversationId);
  visibleConversation.current = conversationId;
  const selectedCaseId = model.selection.caseId;
  const activeJobs = model.jobs.filter(job => job.kind !== 'generate_draft' && (job.status === 'running' || job.status === 'queued') && (job.case_id === selectedCaseId || job.conversation_id === conversationId));
  const generationJob = detail && detail.conversation.conversation_id === conversationId && (detail.case?.case_id ?? '') === selectedCaseId
    && model.connected && !model.closing && !detail.conversation.human_owned && editor?.manualState !== 'pending'
    ? currentGeneration(detail, model.jobs) : null;
  const replyActivity = generationJob && (!generationJob.automatic || model.snapshot?.reception.running) ? generationJob : null;
  const pendingGeneration = detail && detail.conversation.conversation_id === conversationId && (detail.case?.case_id ?? '') === selectedCaseId
    && model.connected && !model.closing && !detail.conversation.human_owned && editor?.manualState !== 'pending'
    ? model.generationRequests.find(item => item.conversation_id === conversationId && item.case_id === selectedCaseId) : null;
  const activityKey = replyActivity ? `${replyActivity.job_id}:${replyActivity.status}` : pendingGeneration?.job_id ?? '';
  const previousActivity = useRef('');
  const reading = useReadAcknowledgement({ viewport: listRef, conversationId, detail, messages,
    active: model.page === 'reception' && model.connected && !model.closing,
    acknowledge: model.markMessagesRead });
  const status = detail ? effectiveReceptionStatus(model.snapshot, detail.conversation) : undefined;
  const visibleBlockers = status?.blockers.filter(blocker => (detail?.case || detail?.conversation.case_id || blocker.code !== 'case') && blocker.message !== status.reason) ?? [];

  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => { setSummaryExpanded(false); if (statusPanel.current) statusPanel.current.open = false; }, [selectedCaseId, conversationId]);
  useEffect(() => {
    const close = (event: Event) => { if (statusPanel.current && !statusPanel.current.contains(event.target as Node)) statusPanel.current.open = false; };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape' && statusPanel.current?.open) { statusPanel.current.open = false; statusPanel.current.querySelector('summary')?.focus(); } };
    document.addEventListener('pointerdown', close); document.addEventListener('keydown', key);
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', key); };
  }, []);
  useLayoutEffect(() => {
    const input = replyInput.current;
    if (!input) return;
    input.style.height = '0px';
    input.style.height = `${Math.max(68, Math.min(180, input.scrollHeight))}px`;
  }, [editor?.body, conversationId]);
  useLayoutEffect(() => {
    const node = listRef.current;
    if (model.page !== 'reception') { wasVisible.current = false; return; }
    if (!node || !detail) { wasVisible.current = false; return; }
    const last = messages[messages.length - 1]?.message_id ?? '';
    if (previousConversation.current !== conversationId || !wasVisible.current) {
      node.scrollTop = model.scrollPositions.current.get(conversationId) ?? node.scrollHeight;
      pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
      setNewMessages(false);
      previousConversation.current = conversationId;
    } else if (last !== previousLastMessage.current) {
      if (pinned.current) node.scrollTop = node.scrollHeight;
      else setNewMessages(true);
    }
    if (previousActivity.current !== activityKey && pinned.current) node.scrollTop = node.scrollHeight;
    previousActivity.current = activityKey;
    previousLastMessage.current = last;
    wasVisible.current = true;
  }, [conversationId, detail, messages.length, model.scrollPositions, model.page, activityKey]);
  useEffect(() => {
    const node = listRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      if (pinned.current && model.page === 'reception') node.scrollTop = node.scrollHeight;
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [conversationId, !!detail, model.page]);
  const [pendingLocate, setPendingLocate] = useState<{ conversationId: string; id: string } | null>(null);
  const locateSequence = useRef(0);
  useEffect(() => {
    if (!pendingLocate || pendingLocate.conversationId !== conversationId) return;
    const message = [...(listRef.current?.querySelectorAll<HTMLElement>('[data-message-id]') ?? [])].find(node => node.dataset.messageId === pendingLocate.id);
    if (message) { message.scrollIntoView({ block: 'center', behavior: scrollBehavior() }); message.classList.add('message-highlight'); setTimeout(() => message.classList.remove('message-highlight'), 2000); setPendingLocate(null); }
  }, [pendingLocate, conversationId, messageIds]);
  const locate = (id: string) => {
    const owner = conversationId;
    const request = ++locateSequence.current;
    setPendingLocate({ conversationId: owner, id });
    if (messages.some(message => message.message_id === id)) return;
    void (async () => {
      let cursor = messages[0]?.message_id;
      const collected: ObservedMessage[] = [];
      try {
        while (cursor && visibleConversation.current === owner && locateSequence.current === request) {
          const result = await model.invoke<{ items: Message[]; has_more: boolean; reading?: ReadingSnapshot }>('get_messages', { conversation_id: owner, before_id: cursor, limit: 200 });
          if (visibleConversation.current !== owner || locateSequence.current !== request) return;
          collected.unshift(...result.items.map(message => ({ ...message, observed_sequence: result.reading?.observed_sequence ?? null })));
          if (result.items.some(message => message.message_id === id)) { setOlderByConversation(previous => ({ ...previous, [owner]: [...collected, ...(previous[owner] ?? [])] })); return; }
          const next = result.items[0]?.message_id;
          if (!result.has_more || !next || next === cursor) break;
          cursor = next;
        }
        if (visibleConversation.current === owner && locateSequence.current === request) { setPendingLocate(null); model.setNotice('本地历史中未找到这条原文，可先补全历史后重试。'); }
      } catch { if (visibleConversation.current === owner && locateSequence.current === request) { setPendingLocate(null); model.setNotice('读取依据原文失败，请重试。'); } }
    })();
  };
  useEffect(() => { registerLocate(locate); });
  const loadOlder = async () => {
    if (!detail || loadingMore) return;
    const owner = conversationId;
    const oldHeight = listRef.current?.scrollHeight ?? 0;
    const oldTop = listRef.current?.scrollTop ?? 0;
    setLoadingMore(true);
    try {
      const result = await model.invoke<{ items: Message[]; has_more: boolean; reading?: ReadingSnapshot }>('get_messages', { conversation_id: owner, before_id: messages[0]?.message_id, limit: 50 });
      const observed = result.items.map(message => ({ ...message, observed_sequence: result.reading?.observed_sequence ?? null }));
      setOlderByConversation(previous => ({ ...previous, [owner]: [...observed, ...(previous[owner] ?? [])] }));
      setHasMore(previous => ({ ...previous, [owner]: result.has_more }));
      requestAnimationFrame(() => { if (listRef.current && visibleConversation.current === owner) listRef.current.scrollTop = oldTop + listRef.current.scrollHeight - oldHeight; });
    } catch (failure) { model.setNotice(failure instanceof Error ? failure.message : '本地历史读取失败。'); }
    finally { setLoadingMore(false); }
  };

  const refreshReading = async () => {
    if (!detail || loadingMore) return;
    const owner = conversationId;
    setLoadingMore(true);
    try {
      const refreshed: ObservedMessage[] = [];
      let cursor = detail.messages[0]?.message_id;
      while (refreshed.length < older.length) {
        const result = await model.invoke<{ items: Message[]; has_more: boolean; reading?: ReadingSnapshot }>('get_messages', { conversation_id: owner, before_id: cursor, limit: Math.min(200, older.length - refreshed.length) });
        refreshed.unshift(...result.items.map(message => ({ ...message, observed_sequence: result.reading?.observed_sequence ?? null })));
        if (!result.has_more || !result.items.length) break;
        const next = result.items[0].message_id;
        if (next === cursor) throw new Error('历史读取没有推进，请稍后重试。');
        cursor = next;
      }
      setOlderByConversation(previous => ({ ...previous, [owner]: refreshed }));
      if (visibleConversation.current === owner) {
        await model.loadDetail(owner, selectedCaseId, true);
        reading.retry();
      }
    } catch (failure) { model.setNotice(failure instanceof Error ? failure.message : '阅读进度暂未保存。'); }
    finally { setLoadingMore(false); }
  };

  if (!detail) return <main className="conversation"><div className="empty-chat"><div className="empty-chat-mark"><MessageCircle size={35} strokeWidth={1.4} /></div><h2>{model.detailLoading ? '正在打开客户会话' : model.detailError ? '会话暂时无法打开' : '每一次询盘，都从好好回应开始'}</h2><p>{model.detailError || '选择左侧客户，查看原始消息、整理需求和准备回复。'}</p>{model.detailError && <Button onClick={() => void model.loadDetail(conversationId, selectedCaseId)}>重新读取会话</Button>}<Button className="mobile-list-button" onClick={openList}><UsersRound size={16} />选择客户</Button></div></main>;
  const current = detail.case;
  const critical: string[] = [];
  if (!model.connected) critical.push('服务已断开，正文已保留，暂不能发送或确认发送结果。');
  if (model.sendIssue.includes('结果未知')) critical.push(model.sendIssue);
  const attempt = editor?.send;
  const sending = model.pending.includes(`send:${conversationId}`) || (!!attempt && ['requesting', 'queued', 'running'].includes(attempt.status));
  const canSend = model.canSend;
  const generating = model.generationRequests.some(item => item.conversation_id === conversationId) || model.pending.includes(`generate_manual_suggestion:${conversationId}`) || model.jobs.some(job => job.kind === 'manual_suggestion' && job.conversation_id === conversationId && ['queued', 'running'].includes(job.status));
  const saveLabel = editor?.saveState === 'saving' ? '保存中…' : editor?.saveState === 'error' ? '保存失败' : editor?.dirty ? '等待保存' : editor?.server ? '已保存' : '尚未输入';
  const sendLabels: Record<string, string> = { requesting: '正在提交发送请求', retry: '请求尚未确认，可重试原发送', queued: '发送任务已排队，尚未确认平台受理', running: '正在发送，等待平台回执', succeeded: '发送任务已完成', accepted: '平台已受理', rejected: '平台未受理', failed: '发送任务失败', stale: '发送版本已失效', cancelled: '发送任务已取消', unknown: '发送结果未知，请核查原操作' };

  return <main className="conversation" aria-label={`${detail.conversation.customer_name} 的客户接待`}>
    <header className="conversation-header"><div className="conversation-identity"><IconButton label="打开客户列表" className="mobile-list-button" onClick={openList}><UsersRound size={20} /></IconButton><div className="avatar large">{detail.conversation.customer_name.slice(0, 1).toUpperCase()}</div><div><div className="identity-line"><h1>{detail.conversation.customer_name}</h1>{detail.conversation.human_owned && <Badge tone="amber"><UserRound size={12} />人工接管</Badge>}</div>{detail.conversation.country && <p><MapPin size={12} />{detail.conversation.country}</p>}</div></div><div className="conversation-tools"><details className="history-tool"><summary aria-label="查看历史覆盖与补全" title="历史覆盖与补全"><History size={16} /></summary><div className="conversation-tool-panel"><strong>历史覆盖</strong><p>{detail.history.status === 'complete' ? '本地历史已补全' : detail.history.status === 'failed' ? '历史补全未完成' : '较早历史尚未完整'}</p><Button disabled={!model.connected || model.busy('fill_history')} onClick={() => void model.runJob('fill_history', { conversation_id: conversationId })}>补全历史</Button></div></details><label className={`translation-tool ${translate ? 'enabled' : ''}`} title={translate ? '关闭中文翻译' : '开启中文翻译'}><input type="checkbox" aria-label="自动中文翻译" checked={translate} onChange={event => toggleTranslate(event.target.checked)} /><Languages size={17} /></label><details ref={statusPanel} className="conversation-status"><summary aria-label="查看会话状态"><span className={`connection-dot ${model.connected ? 'on' : ''}`} />状态<ChevronDown size={12} /></summary><div className="conversation-tool-panel" aria-label="此客户接待状态"><strong>会话状态</strong><dl><div><dt>全局接待</dt><dd>{model.closing ? '正在关闭' : model.snapshot?.reception.running ? '已开启' : '已暂停'}</dd></div><div><dt>当前会话</dt><dd>{detail.conversation.human_owned ? '人工接管' : '自动接待范围按授权执行'}</dd></div><div><dt>AI 能力</dt><dd>{model.snapshot?.checks.ai.status === 'passed' ? '已通过' : model.snapshot?.checks.ai.status === 'demo' ? '演示可用' : model.snapshot?.checks.ai.status === 'running' ? '检查中' : '尚未就绪'}</dd></div><div><dt>中文翻译</dt><dd>{!translate ? '已关闭' : translationReady ? '已开启' : '仅已保存译文'}</dd></div></dl><p>{receptionLabel(model.snapshot, detail, model.connected, model.closing)}</p>{visibleBlockers.length > 0 && <ul>{visibleBlockers.map(blocker => <li key={blocker.code}>{blocker.message}</li>)}</ul>}{model.generationIssue && <p>{model.generationIssue}</p>}{translate && !translationReady && <p>可在连接与设置中配置聊天翻译。</p>}<Button variant="ghost" disabled={!model.connected || model.closing || editor?.dirty || editor?.saving || editor?.manualState === 'pending' || sending || attempt?.status === 'retry' || attempt?.status === 'unknown' || model.busy('takeover')} title="恢复后仍遵循全局启停、授权范围与能力检查。" onClick={() => void model.mutate('takeover', { conversation_id: conversationId, enabled: !detail.conversation.human_owned }, detail.conversation.human_owned ? '已恢复此客户自动接待；实际运行仍遵循全局状态和授权范围。' : '已暂停此客户自动接待，您可以直接聊天。')}><UserRound size={14} />{detail.conversation.human_owned ? '恢复此客户自动接待' : '暂停此客户自动接待'}</Button></div></details><IconButton label="采购需求" className={`open-requirements ${model.preferences.details_preference === 'rail' ? 'is-collapsed' : ''}`} onClick={openDetails}><ClipboardList size={17} /></IconButton></div></header>
    {detail.conversation_summary?.conversation_id === conversationId && detail.conversation_summary.body.trim() && <section className={`conversation-summary ${summaryExpanded ? 'expanded' : ''}`} aria-label="会话摘要"><button type="button" aria-expanded={summaryExpanded} onClick={() => setSummaryExpanded(value => !value)}><BookOpen size={14} /><strong>摘要</strong><span>{detail.conversation_summary.body}</span><ChevronDown size={13} /></button></section>}
    <div className="conversation-notices">
      {model.jobs.filter(job => job.conversation_id === conversationId && job.photo_coverage).sort((a, b) => b.created_at - a.created_at).slice(0, 1).map(job => { const coverage = job.photo_coverage!; const reads = job.diagnostics?.image_reads; return <details key={job.job_id} className="photo-coverage"><summary>本次图片覆盖：{coverage.ready_count}/{coverage.total_count} 张原图已准备</summary><p>准备就绪不代表模型已读取。{typeof reads === 'number' && Number.isFinite(reads) ? ` 本轮已核实读取 ${reads} 张。` : ' 本轮读取结果尚未核实。'}</p><ul>{coverage.items.map((entry, index) => <li key={`${entry.message_id}:${index}`}>{entry.role === 'buyer' ? '买家' : '商家'}图片 · {entry.status === 'ready' ? '已准备' : entry.status === 'failed' ? '准备失败' : '待处理'}{entry.reason ? ` · ${entry.reason}` : ''} <button type="button" className="source-link" onClick={() => locate(entry.message_id)}>查看原文</button></li>)}</ul></details>; })}
      {critical.length > 0 && <div className="critical-strip" role="status"><ShieldAlert size={17} /><div>{critical.map(issue => <p key={issue}>{issue}</p>)}</div></div>}
      {activeJobs.length > 0 && <div className="active-task" role="status"><span className="tiny-spinner" /><span>{activeJobs.map(job => `${jobLabels[job.kind] ?? '后台任务'} · ${job.message || job.stage || '处理中'} · 已等待 ${Math.max(0, Math.floor((now - (job.started_at || job.created_at)) / 1000))} 秒`).join('；')}</span></div>}
    </div>
    {reading.failed && <div className="read-progress-error" role="status">阅读进度暂未保存，未读数会保留。<button type="button" disabled={loadingMore || !model.connected} onClick={() => void refreshReading()}>刷新原文并重试</button></div>}
    <div className="message-viewport">
    <div className="messages" ref={listRef} data-conversation-id={conversationId} aria-label="原始聊天记录" onScroll={event => { const node = event.currentTarget; model.scrollPositions.current.set(conversationId, node.scrollTop); pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80; if (pinned.current) setNewMessages(false); }}>
      <div className="history-load">{hasMore[conversationId] !== false && <button type="button" disabled={loadingMore || !model.connected} onClick={() => void loadOlder()}>{loadingMore ? '正在读取…' : '加载本地更早消息'}</button>}</div>
      {messages.length === 0 ? <Empty title="此客户尚无本地原始消息">草稿不会被当作已发送历史。</Empty> : messages.map(message => <article className={`message ${message.role === 'seller' ? 'seller-message' : 'buyer-message'}`} key={message.message_id} data-message-id={message.message_id}><div className="message-meta"><span>{message.role === 'buyer' ? detail.conversation.customer_name : '商家'}</span><time title={dateTime(message.timestamp_ms)}>{time(message.timestamp_ms)}</time></div><div className="message-bubble">{message.text && !(message.text.trim() === '[image]' && message.attachments.some(attachment => attachment.kind === 'image')) && <p>{message.text}</p>}{translate && (translationSources.some(source => source.item.source_id === message.message_id) || translations.get({ kind: 'message', source_id: message.message_id }, message.text)) && (() => { const item = { kind: 'message' as const, source_id: message.message_id }; const result = translations.get(item, message.text); return result?.status === 'ready' && result.text?.trim() === message.text.trim() ? null : <div className="message-translation" title={result?.provider}>{result?.status === 'ready' ? <>{result.text}{result.legacy && <small> · 历史缓存</small>}</> : result?.status === 'failed' ? <span>{result.error || '翻译未完成'} <button type="button" disabled={!translationReady} onClick={() => translations.retry(item, message.text)}>重试</button></span> : translationReady ? '正在翻译…' : '尚无缓存译文'}</div>; })()}{message.attachments.length > 0 && <MessageImages key={`${conversationId}:${message.message_id}`} attachments={message.attachments} conversationId={conversationId} messageId={message.message_id} role={message.role} autoLoadIndices={message.attachments.map((_, index) => index).filter(index => autoImages.has(JSON.stringify([message.message_id, index])))} revision={imageRevision} bridge={bridge} />}</div></article>)}
      {(replyActivity || pendingGeneration) && <ReplyActivity key={replyActivity?.job_id ?? pendingGeneration!.job_id} phase={replyActivity ? replyActivity.status === 'deferred' ? 'retry' : replyActivity.status === 'queued' ? 'queued' : 'running' : 'confirming'} message={replyActivity?.message} elapsedSeconds={replyActivity ? Math.max(0, Math.floor((now - replyActivity.created_at) / 1000)) : undefined} />}
    </div>
    {newMessages && <button type="button" className="new-messages" onClick={() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: scrollBehavior() }); setNewMessages(false); pinned.current = true; }}><ArrowDown size={14} />有新消息，查看最新</button>}
    </div>
    <section className="composer" aria-label="回复草稿">
      {(generating || suggestion && model.pendingSuggestion) && <section className="suggestion-preview" aria-label="待处理回复建议"><div className="suggestion-heading"><strong><Sparkles size={14} />{suggestion?.mode === 'polish' ? '润色建议' : '回复建议'}</strong><span>{generating ? '正在生成…' : suggestion?.status === 'stale' ? '依据已变化' : '待处理'}</span></div>{suggestion && model.pendingSuggestion && <><p>{suggestion.body}</p>{translate && (() => { const item = { kind: 'manual_suggestion' as const, source_id: suggestion.suggestion_id }; const result = translations.get(item, suggestion.body); return result?.status === 'ready' && result.text?.trim() === suggestion.body.trim() ? null : <div className="message-translation" title={result?.provider}>{result?.status === 'ready' ? result.text : result?.status === 'failed' ? <span>{result.error || '翻译未完成'} <button type="button" disabled={!translationReady} onClick={() => translations.retry(item, suggestion.body)}>重试</button></span> : translationReady ? '正在翻译…' : '尚无缓存译文'}</div>; })()}<div className="suggestion-actions"><Button variant="primary" disabled={generating} onClick={() => model.adoptSuggestion()}>替换到输入框</Button><Button variant="ghost" disabled={!model.canGenerate || generating} onClick={() => void model.generate()}>重新生成</Button><Button variant="ghost" disabled={generating} onClick={model.dismissSuggestion}>弃用</Button></div></>}</section>}
      <div className="composer-content">
      <textarea ref={replyInput} aria-label="回复正文" placeholder="输入回复…" value={editor?.body ?? ''} onChange={event => model.editBody(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && event.ctrlKey) { event.preventDefault(); if (canSend && !sending) void model.send(); } }} spellCheck={false} />
      {editor?.manualState === 'pending' && <p className="composer-mode" role="status">正在暂停此客户自动接待，确认成功后即可发送。</p>}
      {editor?.manualState === 'error' && <Feedback tone="red">{editor.manualError}<Button onClick={() => void model.beginManualEdit(conversationId)}>重试暂停此客户自动接待</Button></Feedback>}
      {editor?.error && <Feedback tone="red">{editor.error} 当前正文已保留。<Button variant="ghost" onClick={() => void model.saveDraft(conversationId)}>重试保存</Button></Feedback>}
      {attempt && (!successReceiptKey || (!editor?.body && !dismissedReceipts.has(successReceiptKey))) && <div className="chat-send-receipt" role="status"><strong>{sendLabels[attempt.status]}</strong>{!successReceiptKey && !['requesting', 'queued', 'running'].includes(attempt.status) && attempt.message && <p>{attempt.message}</p>}{['retry', 'unknown'].includes(attempt.status) && <details><summary>查看原发送正文</summary><p className="preserve-text">{attempt.body}</p><small>请求 {attempt.params.client_request_id}</small>{attempt.jobId && <small>任务 {attempt.jobId}</small>}</details>}</div>}
      {model.sendIssue.includes('结果未知') && <div>{model.unknownOperations.map(operation => <Button key={operation.operation_id} disabled={!model.connected} loading={model.busy(`reconcile:${operation.operation_id}`)} onClick={() => void model.reconcileOperation(operation.operation_id)}>核查原操作 {operation.operation_id}</Button>)}<Button variant="ghost" onClick={() => void model.navigate('activity')}>查看并核查原操作</Button></div>}
      </div>
      <div className="composer-actions"><span className={`save-state ${editor?.saveState === 'error' ? 'save-error' : ''}`} aria-live="polite"><CheckCheck size={12} />{saveLabel}</span><div className="composer-send-actions"><span className="send-shortcut">Ctrl + Enter</span><Button variant="ghost" loading={generating} disabled={!model.canGenerate || generating} title={model.generationIssue || undefined} onClick={() => void model.generate()}><Sparkles size={16} />{editor?.body.trim() ? '润色当前回复' : '生成回复建议'}</Button><Button variant="primary" loading={sending} disabled={!canSend} onClick={() => void model.send()} title={model.sendIssue || (attempt?.status === 'retry' ? '使用原请求编号、客户、正文和版本重试' : '保存确切正文后发送本条回复')}><Send size={16} />{attempt?.status === 'retry' ? '重试原发送' : '发送'}</Button></div></div>
    </section>
  </main>;
}
