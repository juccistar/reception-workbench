import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Check, ChevronRight, ClipboardCheck, Clock3, Copy, FileCheck2, FileText, FolderOpen, Globe2, History, Link2, MessageCircle, Pause, Play, RefreshCw, Save, Search, Settings2, ShieldCheck, Sparkles, UserRound, UsersRound, X } from 'lucide-react';
import type { ApiMethod, Case, ConversationDetail, Job, ReceptionBridge, Scope } from '../../shared/api';
import { errorText, type Workbench } from '../hooks/useWorkbench';
import { Badge, Button, Empty, Feedback, IconButton, MessageSyncSummary, StateBadge, checkLabels, dateTime, fieldLabels, jobLabels, jobStatusLabels } from './ui';
import { FeishuForm } from './FeishuForm';
import { DataStorage } from './DataStorage';
import { UpdatesPanel } from './UpdatesPanel';
import { TranslationSettingsPanel } from './TranslationSettings';
import { SnapshotDiagnostics } from './SnapshotDiagnostics';
import { ReadDiagnostics } from './ReadDiagnostics';
import { registrationSummary } from '../receptionStatus';
import { executorHealth } from '../executorHealth';
import { caseLabel } from './caseLabels';
import { SharedRequirementCard } from './SharedRequirementCard';
import { ProcessingSettings, processingParameters } from './ProcessingSettings';

const PageTitle = ({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) => <header className="page-title"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;

export function HomePage({ model }: { model: Workbench }) {
  const [query, setQuery] = useState('');
  const conversations = model.snapshot?.conversations ?? [];
  const matches = conversations.filter(item => `${item.customer_name} ${item.last_message}`.toLowerCase().includes(query.toLowerCase()));
  const work = query ? matches : conversations.filter(item => ['clarifying', 'needs_human', 'waiting_quote', 'human_owned'].includes(item.state) || item.pending_count > 0);
  return <div className="page home-page"><div className="home-intro"><div className="home-symbol"><MessageCircle size={31} strokeWidth={1.5} /><span /></div><span className="eyebrow">客户的下一步，从这里开始</span><h1>今天先把哪些客户<br />接待好？</h1><p>自动接待跟进客户、整理采购需求。<br />需要您回复时，打开客户会话直接聊天。</p><div className="home-ai-entry"><Button variant="primary" disabled={!model.connected || model.closing} onClick={() => void model.navigate('settings')}><Sparkles size={16} />设置自动接待</Button><p>{!executorHealth(model.snapshot).ready ? executorHealth(model.snapshot).message : model.snapshot?.reception.running ? '自动接待正在运行。手工输入会立即暂停该客户的自动接待。' : '通过账号身份、消息读取和 AI 检查后，可授权开启自动接待。'}</p></div><div className="home-search"><Search size={21} /><input aria-label="开始页搜索本地客户" placeholder="搜索已同步客户、消息…" value={query} onChange={event => setQuery(event.target.value)} /><span>本地资料</span></div></div>
    <section className="home-tasks"><div className="section-title"><h2>{query ? '本地搜索结果' : '需要您关注'}</h2><Button variant="ghost" onClick={() => void model.navigate('reception')}>进入接待 <ArrowRight size={15} /></Button></div>{!model.connected && <Feedback>服务尚未连接。可在连接与设置中查看原因。<button type="button" onClick={() => void model.navigate('settings')}>查看连接</button></Feedback>}{work.length === 0 ? <Empty title={model.loading ? '正在读取待办…' : query ? '没有匹配的本地客户' : '目前没有需要处理的事项'}>新的消息同步后会出现在这里。</Empty> : work.slice(0, 8).map(item => <button type="button" className="task-row" key={item.conversation_id} onClick={() => void model.selectConversation(item.conversation_id, item.case_id)}><span className="avatar">{item.customer_name[0]}</span><span className="task-content"><strong>{item.customer_name}</strong><span>{item.last_message || '采购需求待整理'}</span></span><StateBadge state={item.state} /><ChevronRight size={16} /></button>)}</section><div className="home-bottom"><ShieldCheck size={16} /><p>{model.snapshot?.app.mode === 'demo' ? '当前是独立演示工作区，客户和政策均为合成样本。' : '回复前核对客户、确切正文和来源，发送结果以平台回执为准。'}</p></div>
  </div>;
}

function useCustomerDetails(model: Workbench, ids: string[]) {
  const [records, setRecords] = useState<Record<string, ConversationDetail>>({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const key = ids.join('|');
  useEffect(() => {
    if (!model.connected || !ids.length) return;
    let cancelled = false;
    setLoading(true); setError('');
    void Promise.allSettled(ids.map(conversation_id => model.invoke<ConversationDetail>('get_conversation', { conversation_id }))).then(results => {
      if (cancelled) return;
      const next: Record<string, ConversationDetail> = {};
      for (const result of results) {
        if (result.status === 'fulfilled') next[result.value.conversation.conversation_id] = result.value;
        else setError('部分客户资料尚未读取成功，可刷新重试。');
      }
      setRecords(next); setLoading(false);
    });
    return () => { cancelled = true; };
  // The ID list is normalized by the caller. Refreshing backend state invalidates records without remounting the form.
  }, [key, model.connected, model.snapshot]);
  return { records, error, loading };
}

export function HandoffPage({ model }: { model: Workbench }) {
  const explicit = model.snapshot?.handoff_cases;
  const legacy = model.snapshot?.conversations.filter(item => item.state === 'waiting_quote') ?? [];
  const { records, error, loading } = useCustomerDetails(model, explicit === undefined ? legacy.map(item => item.conversation_id) : []);
  const cases = explicit ?? Object.values(records).filter(detail => legacy.some(item => item.conversation_id === detail.conversation.conversation_id)).map(detail => detail.case).filter((item): item is Case => !!item && item.state === 'waiting_quote');
  return <div className="page"><PageTitle eyebrow="把已确认的需求，交到下一步" title="待我报价" description="按采购事项交接；同一客户开始新采购后，之前待报价的需求仍保留在这里。产品成本、交期和正式报价由您核实。" action={<Button disabled={!model.connected || !executorHealth(model.snapshot).ready || model.busy('run_outbox')} loading={model.busy('run_outbox')} onClick={() => void model.runJob('run_outbox')}><RefreshCw size={16} />同步交接记录</Button>} />
    {error && explicit === undefined && <Feedback>{error}</Feedback>}
    {explicit === undefined && loading && !Object.keys(records).length ? <Empty title="正在读取交接需求…" /> : !cases.length ? <Empty title="暂时没有待报价事项" icon={<ClipboardCheck size={35} strokeWidth={1.4} />}>采购条件齐全的事项会出现在这里。</Empty> : <div className="handoff-list">{cases.map(current => {
      const registration = registrationSummary(model.snapshot?.outbox ?? [], current);
      return <article className="handoff-card" key={current.case_id}><div className="handoff-top"><div className="avatar">{current.customer_name[0]}</div><div><h2>{current.customer_name} · {caseLabel(current, cases.filter(item => item.conversation_id === current.conversation_id))}</h2><p>{typeof current.requirements?.destination?.value === 'string' ? current.requirements.destination.value : ''} · {dateTime(current.updated_at)}</p></div><StateBadge state={current.state} /></div><SharedRequirementCard key={current.case_id} model={model} current={current} /><footer><span><FileCheck2 size={15} />{registration}</span><Button variant="primary" onClick={() => void model.selectConversation(current.conversation_id, current.case_id)}>查看依据与会话 <ArrowRight size={15} /></Button></footer></article>;
    })}</div>}
  </div>;
}

export function CustomersPage({ model }: { model: Workbench }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const customers = model.snapshot?.conversations ?? [];
  const filtered = customers.filter(item => `${item.customer_name} ${item.country} ${item.buyer_id}`.toLowerCase().includes(query.toLowerCase()));
  const current = customers.find(item => item.conversation_id === selected) ?? filtered[0];
  const { records, error, loading } = useCustomerDetails(model, current ? [current.conversation_id] : []);
  const record = current ? records[current.conversation_id] : undefined;
  return <div className="page"><PageTitle eyebrow="只呈现已同步、有来源的资料" title="客户资料" description="客户、平台会话与采购事项在这里一一对应。" /><div className="directory-search"><Search size={17} /><input aria-label="搜索客户资料" value={query} onChange={event => setQuery(event.target.value)} placeholder="客户名称、地区或买家编号" /></div><div className="directory-layout"><section className="directory-list" aria-label="客户资料目录">{filtered.map(item => <button type="button" className={item.conversation_id === current?.conversation_id ? 'active' : ''} key={item.conversation_id} onClick={() => setSelected(item.conversation_id)}><span className="avatar">{item.customer_name[0]}</span><span><strong>{item.customer_name}</strong><small>{item.country || '地区尚未确认'}</small></span><ChevronRight size={16} /></button>)}{!filtered.length && <Empty title="没有匹配的客户资料" />}</section><section className="profile-card">{!current ? <Empty title="选择客户查看资料" /> : <><div className="profile-heading"><span className="avatar large">{current.customer_name[0]}</span><div><h2>{current.customer_name}</h2><p><Globe2 size={14} />{current.country || '地区尚未确认'}</p></div><StateBadge state={current.state} /></div><dl className="profile-fields"><div><dt>买家编号</dt><dd>{current.buyer_id}</dd></div><div><dt>平台会话</dt><dd>{current.conversation_id}</dd></div><div><dt>资料来源</dt><dd>平台已同步会话与采购原文</dd></div></dl><h3>采购事项</h3>{error && <Feedback>{error}</Feedback>}{loading && !record ? <p className="muted">正在读取关联事项…</p> : record?.cases.map(item => <div key={item.case_id}><button type="button" className="profile-case" onClick={() => void model.selectConversation(current.conversation_id, item.case_id)}><FileText size={18} /><span><strong>{caseLabel(item, record.cases)}</strong><small>{typeof item.requirements?.quantity?.value === 'string' ? item.requirements.quantity.value : ''} · v{item.revision}</small></span><StateBadge state={item.state} /><ChevronRight size={15} /></button><SharedRequirementCard model={model} current={item} /></div>)}<Button variant="primary" onClick={() => void model.selectConversation(current.conversation_id, current.case_id)}><MessageCircle size={16} />回到客户会话</Button></>}</section></div></div>;
}

export { PoliciesPage } from './PolicyLibrary';

export function SettingsPage({ model, bridge }: { model: Workbench; bridge?: ReceptionBridge }) {
  const settings = model.snapshot?.settings;
  const [form, setForm] = useState({ workctl_path: '', probe_seconds: 2, poll_seconds: 30, merge_seconds: 1, required_fields: ['product', 'quantity', 'specifications', 'destination'], ...processingParameters() });
  const [dirty, setDirty] = useState(false);
  const [scopeMode, setScopeMode] = useState<Scope['mode']>('selected');
  const [scopeIds, setScopeIds] = useState<string[]>([]);
  const [scopeDirty, setScopeDirty] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [autoConsent, setAutoConsent] = useState(false);
  const formVersion = useRef(0);
  const scopeVersion = useRef(0);
  useEffect(() => { model.setFormDirty('settings', dirty || scopeDirty); return () => model.setFormDirty('settings', false); }, [dirty, scopeDirty, model.setFormDirty]);
  useEffect(() => { if (settings && !dirty) setForm({ workctl_path: settings.workctl_path, probe_seconds: settings.probe_seconds ?? 2, poll_seconds: settings.poll_seconds ?? 30, merge_seconds: settings.merge_seconds ?? 1, required_fields: settings.required_fields, ...processingParameters(settings) }); }, [settings, dirty]);
  useEffect(() => { const scope = model.snapshot?.reception.scope; if (scope && !scopeDirty) { setScopeMode(scope.mode); setScopeIds(scope.conversation_ids); } }, [model.snapshot?.reception.scope, scopeDirty]);
  const update = <K extends keyof typeof form>(key: K, value: typeof form[K]) => { formVersion.current++; setForm(previous => ({ ...previous, [key]: value })); setDirty(true); setFeedback(''); };
  const checks = model.snapshot?.checks;
  const checkCards: { title: string; description: string; key: 'messages' | 'ai' | 'send_preview' | 'feishu'; method: ApiMethod; icon: typeof Link2; button: string }[] = [
    { title: '平台身份与消息读取', description: '只核验账号身份和读取能力，不发送消息。', key: 'messages', method: 'check_connection', icon: Link2, button: '检查读取' },
    { title: 'AI 回复能力', description: '明确调用一次；检查前先暂停接待，不自动重试。', key: 'ai', method: 'check_ai', icon: Sparkles, button: '暂停并检查 AI' },
    { title: 'AI 草稿发送诊断', description: '针对当前 AI 建议做原生预演，不提交客户消息；不作为全局自动接待的启动条件。', key: 'send_preview', method: 'preview_send', icon: ShieldCheck, button: '预演当前 AI 草稿' },
    { title: '飞书交接结构', description: '仅检查同步结构；与消息读取和 AI 独立。', key: 'feishu', method: 'check_feishu', icon: FileCheck2, button: '检查飞书' },
  ];
  const runCheck = async (method: ApiMethod) => {
    if (method === 'check_ai') { const stopped = await model.mutate('set_reception', { enabled: false, auto_send: false }); if (!stopped) return; setAutoConsent(false); }
    if (method === 'preview_send') await model.preview();
    else await model.runJob(method);
  };
  const saveSettings = async (event: FormEvent) => { event.preventDefault(); const version = formVersion.current; const result = await model.mutate('save_settings', form); if (result) { setDirty(formVersion.current !== version); setAutoConsent(false); setFeedback(formVersion.current === version ? '设置已保存，自动发送授权已撤销。' : '前一版已保存，刚输入的修改仍待保存。'); } };
  const saveScope = async () => { const version = scopeVersion.current; const result = await model.mutate('set_scope', { mode: scopeMode, conversation_ids: scopeMode === 'selected' ? scopeIds : [], max_auto_replies: null }, '本轮范围已保存。'); if (result) { setScopeDirty(scopeVersion.current !== version); setAutoConsent(false); } };
  const reception = model.snapshot?.reception;
  const health = executorHealth(model.snapshot);
  useEffect(() => { if (!health.ready) setAutoConsent(false); }, [health.ready]);
  return <div className="page settings-page"><PageTitle eyebrow="每一种能力，分别核验" title="连接与设置" description="检查结果是带时间的历史记录。实时接待是否运行，以窗口顶部为准。" action={<Button loading={model.loading} onClick={() => void model.refreshSnapshot()}><RefreshCw size={16} />刷新服务状态</Button>} />
    {!health.ready && <Feedback tone="red">{health.message}。新任务暂未就绪，已有正文和回执保留。</Feedback>}
    {!model.connected && <Feedback tone="red">服务尚未连接。请通过桌面应用的授权启动链恢复服务。<Button onClick={() => void model.refreshSnapshot()}>重新连接</Button></Feedback>}
    {model.snapshot?.app.mode === 'demo' && <Feedback>演示模式：这里的检查使用合成数据或本地模拟，通过不代表真实 Accio、AI 或飞书已连接。</Feedback>}
    <section className="checks-grid">{checkCards.map(item => { const check = checks?.[item.key]; const Icon = item.icon; return <article className="check-card" key={item.key}><div className="check-card-heading"><span className="check-icon"><Icon size={21} /></span><h2>{item.title}</h2><Badge tone={check?.status === 'passed' ? 'green' : check?.status === 'failed' ? 'red' : check?.status === 'demo' || check?.status === 'blocked' ? 'amber' : 'neutral'}>{check ? checkLabels[check.status] : '状态未知'}</Badge></div><p>{item.description}</p><div className="check-message">{check?.message || '尚无检查结果'}</div><div className="check-meta">{check?.simulated && <strong>模拟检查 · </strong>}{dateTime(check?.checked_at)}</div><Button disabled={!model.connected || !health.ready || model.busy(item.method) || (item.method === 'preview_send' && !model.canPreview)} loading={model.busy(item.method)} onClick={() => void runCheck(item.method)}>{item.button}</Button>{check?.check_id && <details className="check-details"><summary>检查详情</summary><p>检查编号：{check.check_id}</p><p>读取环境：{checks?.runtime ? checkLabels[checks.runtime.status] : '未知'} · 账号身份：{checks?.identity ? checkLabels[checks.identity.status] : '未知'}</p></details>}</article>; })}</section>
    <MessageSyncSummary sync={model.snapshot?.message_sync} />
    <DataStorage bridge={bridge} />
    <UpdatesPanel bridge={bridge} currentVersion={model.snapshot?.app.version} />
    <TranslationSettingsPanel bridge={bridge} disabled={!model.connected || model.closing} onDirtyChange={dirty => model.setFormDirty('translation', dirty)} />
    <div className="settings-columns"><form className="form-card" onSubmit={event => void saveSettings(event)}><div className="form-card-heading"><h2><Settings2 size={18} />本地连接参数</h2></div><label>Accio workctl 程序路径<div className="input-with-button"><input value={form.workctl_path} onChange={event => update('workctl_path', event.target.value)} placeholder="由桌面应用选择已安装程序" /><IconButton label="选择 workctl 程序" disabled={!bridge} onClick={() => void model.action('chooseWorkctl', async () => { const selected = await bridge?.chooseWorkctl(); if (selected) update('workctl_path', selected); })}><FolderOpen size={18} /></IconButton></div></label><div className="form-row"><label>新消息检测（秒）<input type="number" min={2} max={60} value={form.probe_seconds} onChange={event => update('probe_seconds', Number(event.target.value))} required /></label><label>摘要续页间隔（秒）<input type="number" min={5} max={3600} value={form.poll_seconds} onChange={event => update('poll_seconds', Number(event.target.value))} required /></label><label>连续消息合并（秒）<input type="number" min={0} max={120} value={form.merge_seconds} onChange={event => update('merge_seconds', Number(event.target.value))} required /></label></div><fieldset><legend>交接前必备条件</legend><div className="field-checks">{Object.entries(fieldLabels).filter(([key]) => !key.startsWith('sample')).map(([key, label]) => <label className="checkbox-label compact" key={key}><input type="checkbox" disabled={key === 'product'} checked={key === 'product' || form.required_fields.includes(key)} onChange={event => update('required_fields', event.target.checked ? [...new Set([...form.required_fields, key, 'product'])] : form.required_fields.filter(field => field !== key))} />{label}{key === 'product' && <small>必填</small>}</label>)}</div></fieldset><ProcessingSettings value={form} onChange={parameters => { formVersion.current++; setForm(previous => ({ ...previous, ...parameters })); setDirty(true); setFeedback(''); }} />{feedback && <Feedback tone="green">{feedback}</Feedback>}<Button type="submit" variant="primary" disabled={!dirty || !model.connected} loading={model.busy('save_settings')}><Save size={16} />保存连接参数</Button>{dirty && <Button onClick={() => { if (settings) setForm({ workctl_path: settings.workctl_path, probe_seconds: settings.probe_seconds ?? 2, poll_seconds: settings.poll_seconds ?? 30, merge_seconds: settings.merge_seconds ?? 1, required_fields: settings.required_fields, ...processingParameters(settings) }); setDirty(false); setFeedback('已放弃连接参数修改。'); }}>放弃参数修改</Button>}</form>
    <FeishuForm model={model} /></div>
    <section className="form-card scope-card">
      <div className="form-card-heading"><h2><UsersRound size={18} />自动接待授权</h2><Badge tone={!health.ready ? 'red' : reception?.running ? 'green' : 'neutral'}>{!health.ready ? health.message : reception?.running ? '自动接待中' : '自动接待已暂停'}</Badge></div>
      <p className="form-description">以下范围用于自动接待。您可以随时打开客户会话直接聊天，输入时会暂停该客户的自动接待。保存配置或重新启动后，需要明确重新授权；选定客户及已同步客户模式会冻结开启时的名单。</p>
      {!model.aiReady && <p className="form-description">AI 未通过检查，暂不能开启自动接待；您仍可在客户页手写发送。</p>}
      <div className="scope-options">
        <label><input type="radio" name="scope" checked={scopeMode === 'selected'} onChange={() => { setScopeMode('selected'); setScopeDirty(true); scopeVersion.current++; }} />仅选定客户</label>
        <label><input type="radio" name="scope" checked={scopeMode === 'all'} onChange={() => { setScopeMode('all'); setScopeDirty(true); scopeVersion.current++; }} />仅开启时已同步客户（不含后来新客户）</label>
        <label><input type="radio" name="scope" checked={scopeMode === 'incoming'} onChange={() => { setScopeMode('incoming'); setScopeDirty(true); scopeVersion.current++; }} />只接待授权后新消息（含新客户）</label>
      </div>
      {scopeMode === 'incoming' && <p className="form-description">只处理本账号接待起点后收到的新买家消息，包含后来出现的新客户；早于起点的历史询盘需手工处理。尚无客户时也可保存此范围，真实模式开启前仍需通过账号身份、消息读取和 AI 检查。</p>}
      {scopeMode === 'selected' && <div className="scope-customers">{model.snapshot?.conversations.map(item => <label className="checkbox-label compact" key={item.conversation_id}><input type="checkbox" checked={scopeIds.includes(item.conversation_id)} onChange={event => { setScopeIds(previous => event.target.checked ? [...previous, item.conversation_id] : previous.filter(id => id !== item.conversation_id)); setScopeDirty(true); scopeVersion.current++; }} />{item.customer_name}</label>)}</div>}
      <div className="scope-save"><Button disabled={!model.connected || !scopeDirty} loading={model.busy('set_scope')} onClick={() => void saveScope()}>保存本轮范围</Button>{scopeDirty && <Button onClick={() => { const current = model.snapshot?.reception.scope; if (current) { setScopeMode(current.mode); setScopeIds(current.conversation_ids); } setScopeDirty(false); setAutoConsent(false); }}>放弃范围修改</Button>}</div>
      {reception && (reception.budget.limit === null ? <><p>接待 AI 已用 {reception.budget.ai_used} 次，不限次数。</p><p>自动发送已用 {reception.budget.used} 次，不限次数。</p></> : <><p>自动 AI 额度：已用 {reception.budget.ai_used} / {reception.budget.limit}，剩余 {reception.budget.ai_remaining ?? '—'} 次。</p><p>自动发送额度：已用 {reception.budget.used} / {reception.budget.limit}，剩余 {reception.budget.remaining ?? '—'} 次。</p></>)}
      <p>接待起点：{reception?.started_at_ms ? dateTime(reception.started_at_ms) : '尚未开始，成功开启时建立起点'}。暂停后恢复将延续已有起点及使用记录。</p>
      <div className="auto-send-area"><label className="checkbox-label"><input type="checkbox" checked={autoConsent} disabled={!model.connected || !model.aiReceptionReady || dirty || scopeDirty || model.busy('set_scope')} onChange={event => setAutoConsent(event.target.checked)} /><span><strong>我确认允许在本轮范围内自动发送</strong><small>只有读取、AI、范围与服务校验全部就绪时才能生效，还需在系统确认窗口核对授权。</small></span></label><Button variant="primary" disabled={!model.connected || model.closing || !model.aiReceptionReady || !autoConsent || dirty || scopeDirty || model.busy('set_scope') || (scopeMode === 'selected' && !scopeIds.length) || reception?.auto_send} loading={model.busy('set_reception')} onClick={() => void model.mutate('set_reception', { enabled: true, auto_send: true, manual_only: false }, '接待授权请求已处理，请核对顶部实时状态。').then(() => setAutoConsent(false))}><Play size={15} />授权并开始自动接待</Button></div>
    </section>
  </div>;
}

function recordStatus(job: Job) {
  if (job.retry_status === 'blocked' || job.retry_status === 'exhausted') return '已停止重试';
  if (job.status === 'deferred' && job.retry_at) return '等待重试';
  return jobStatusLabels[job.status] ?? job.status;
}

export function ActivityPage({ model }: { model: Workbench }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const [feedback, setFeedback] = useState('');
  const filtered = model.jobs.filter(job => filter === 'all' || (filter === 'active' && (['queued', 'running'].includes(job.status) || job.status === 'deferred' && !!job.retry_at)) || (filter === 'attention' && (['failed', 'unknown', 'stale'].includes(job.status) || job.retry_status === 'blocked' || job.retry_status === 'exhausted')));
  const job = model.jobs.find(item => item.job_id === selected);
  const refresh = () => void model.action('records-refresh', async () => { await model.refreshSnapshot(); model.setNotice('运行记录已刷新。'); });
  const reconcile = async (id: string) => { if (!executorHealth(model.snapshot).ready) { setFeedback('后台执行暂不可用，原操作核查需要后台任务；回执已保留，请恢复后核查。'); return; } const result = await model.action(`reconcile:${id}`, () => model.invoke<{ status?: string; message?: string; job_id?: string }>('reconcile_operation', { operation_id: id })); if (result) { setFeedback(result.job_id ? '原操作核查已排队，不会重新发送；完成后更新回执。' : `${jobStatusLabels[result.status ?? 'unknown'] ?? result.status}：${result.message ?? '暂无新的权威回执'}`); await model.refreshSnapshot(); } };
  return <div className="page"><PageTitle eyebrow="每一次检查和处理，都有独立记录" title="运行记录" description="任务完成不等于平台已受理。结果未知时保留原操作编号，先核查再处理。" action={<Button disabled={!model.connected} onClick={refresh} loading={model.busy('records-refresh')}><RefreshCw size={16} />刷新记录</Button>} /><MessageSyncSummary sync={model.snapshot?.message_sync} /><div className="record-tabs">{[['all', '全部任务'], ['active', '进行中'], ['attention', '需要关注']].map(([value, label]) => <button type="button" className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div><div className="records-layout"><div className="records-table"><div className="records-head"><span>任务</span><span>状态</span><span>创建时间</span></div>{filtered.length === 0 ? <Empty title="尚无此类任务记录">检查和生成均由您明确触发，不会因刷新页面自动执行。</Empty> : filtered.map(item => <button type="button" key={item.job_id} className={`record-row ${item.job_id === selected ? 'active' : ''}`} onClick={() => setSelected(item.job_id)}><span><strong>{jobLabels[item.kind] ?? '后台任务'}</strong><small>{item.kind === 'generate_draft' && !['succeeded', 'cancelled', 'stale'].includes(item.status) && (item.retry_attempt || item.status === 'failed') ? '回复整理中或等待恢复' : item.message || item.stage}</small></span><Badge tone={item.status === 'succeeded' ? 'green' : item.status === 'failed' || item.status === 'unknown' ? 'red' : item.status === 'running' ? 'amber' : 'neutral'}>{recordStatus(item)}</Badge><time>{dateTime(item.created_at)}</time></button>)}</div>{job && <aside className="job-detail"><div className="form-card-heading"><h2>任务详情</h2><IconButton label="关闭任务详情" onClick={() => setSelected(null)}><X size={17} /></IconButton></div><h3>{jobLabels[job.kind] ?? '后台任务'}</h3><p>{job.kind === 'generate_draft' && !['succeeded', 'cancelled', 'stale'].includes(job.status) && (job.retry_attempt || job.status === 'failed') ? '回复整理中或等待恢复' : job.message || '暂无补充说明'}</p><dl><div><dt>任务编号</dt><dd>{job.job_id}</dd></div><div><dt>处理阶段</dt><dd>{job.stage || '尚未开始'}</dd></div><div><dt>创建时间</dt><dd>{dateTime(job.created_at)}</dd></div><div><dt>开始时间</dt><dd>{dateTime(job.started_at)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(job.completed_at)}</dd></div><div><dt>会话归属</dt><dd>{model.snapshot?.conversations.find(item => item.conversation_id === job.conversation_id)?.customer_name || job.conversation_id || '全局检查'}</dd></div>{job.case_id && <div><dt>采购事项</dt><dd>{job.case_id}</dd></div>}</dl>{job.error && <Feedback tone="red">{job.kind === 'generate_draft' ? '回复暂未整理完成，请查看状态并等待恢复。' : <>{job.error.message}<small>错误码：{job.error.code}</small></>}</Feedback>}{(job.retry_attempt || job.repair_count || job.retry_at) && <p>{job.kind === 'automatic_send' || job.kind === 'continuity_send' ? `自动发送已尝试 ${job.retry_attempt ?? 0} 次（最多3次）` : `自动重试 ${job.retry_attempt ?? 0} 次`} · 格式整理 {job.repair_count ?? 0} 次{job.retry_at ? ` · 下次尝试 ${dateTime(job.retry_at)}` : ''}</p>}<ReadDiagnostics diagnostics={job.diagnostics} /><SnapshotDiagnostics diagnostics={job.diagnostics} />{job.conversation_id && <Button onClick={() => void model.selectConversation(job.conversation_id!, job.case_id ?? '')}>查看所属会话 <ArrowRight size={14} /></Button>}</aside>}</div><section className="outbox-section"><div className="section-title"><h2>交接与发送操作</h2><Button disabled={!model.connected || !executorHealth(model.snapshot).ready || model.busy('run_outbox')} loading={model.busy('run_outbox')} onClick={() => void model.runJob('run_outbox')}><RefreshCw size={15} />处理同步队列</Button></div>{feedback && <Feedback>{feedback}</Feedback>}{!model.snapshot?.outbox.length ? <Empty title="尚无操作回执">有明确的交接或发送操作后，在这里跟踪结果。</Empty> : model.snapshot.outbox.map(item => <article className="operation-row" key={item.operation_id}><FileCheck2 size={19} /><div><strong>{item.kind === 'send' ? '客户消息发送' : item.kind === 'register' ? '需求交接同步' : item.kind}</strong><p>{item.message || '等待操作结果'}</p><small>操作 {item.operation_id} · 事项 {item.case_id} v{item.case_revision} · {dateTime(item.updated_at)}</small>{item.kind === 'send' && item.message_id && <small>平台消息编号：{item.message_id}</small>}</div><Badge tone={item.status === 'unknown' ? 'red' : 'neutral'}>{jobStatusLabels[item.status] ?? item.status}</Badge><Button disabled={!model.connected || model.busy('reconcile_operation')} loading={model.busy(`reconcile:${item.operation_id}`)} onClick={() => void reconcile(item.operation_id)}>核查原操作</Button></article>)}</section></div>;
}
