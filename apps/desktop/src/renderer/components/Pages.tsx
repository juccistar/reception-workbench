import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Check, ChevronRight, ClipboardCheck, Clock3, Copy, FileCheck2, FileText, FolderOpen, Globe2, History, Link2, MessageCircle, Pause, Play, RefreshCw, Save, Search, Settings2, ShieldCheck, Sparkles, UserRound, UsersRound, X } from 'lucide-react';
import type { ApiMethod, Case, ConversationDetail, Job, ReceptionBridge, Scope } from '../../shared/api';
import { errorText, type Workbench } from '../hooks/useWorkbench';
import { Badge, Button, Empty, Feedback, IconButton, MessageSyncSummary, StateBadge, checkLabels, dateTime, fieldLabels, jobLabels, jobStatusLabels } from './ui';
import { FeishuForm } from './FeishuForm';
import { DataStorage } from './DataStorage';
import { UpdatesPanel } from './UpdatesPanel';
import { ModelSummary } from './ModelSummary';
import { SnapshotDiagnostics } from './SnapshotDiagnostics';
import { ReadDiagnostics } from './ReadDiagnostics';
import { registrationSummary } from '../receptionStatus';
import { executorHealth } from '../executorHealth';
import { caseLabel } from './caseLabels';
import { SharedRequirementCard } from './SharedRequirementCard';
import { SupplementPanel } from './SupplementPanel';
import { ProcessingSettings, processingParameters } from './ProcessingSettings';
import './pages-042.css';

const PageTitle = ({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) => <header className="page-title"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;

export function HomePage({ model }: { model: Workbench }) {
  const conversations = model.snapshot?.conversations ?? [];
  const work = conversations.filter(item => ['clarifying', 'needs_human', 'waiting_quote', 'human_owned'].includes(item.state) || item.pending_count > 0);
  const recent = [...conversations].sort((a, b) => b.last_timestamp - a.last_timestamp).slice(0, 5);
  return <div className="page home-042"><PageTitle eyebrow="" title="开始" description="继续处理需要参与的客户。" action={<Button variant="primary" onClick={() => void model.navigate('reception')}>进入接待</Button>} /><div className="home-042-grid"><section><h2>等你处理 <small>{work.length} 位客户</small></h2>{work.length ? work.slice(0, 8).map(item => <button type="button" className="home-042-row" key={item.conversation_id} onClick={() => void model.selectConversation(item.conversation_id, item.case_id)}><span><strong>{item.customer_name}</strong><small>{item.last_message || '采购需求待整理'}</small></span><StateBadge state={item.state} /></button>) : <Empty title="目前没有需要处理的客户" />}</section><section><h2>工作区状态</h2><div className="home-042-status"><span>平台连接</span><strong>{model.connected ? '已连接' : '未连接'}</strong></div><div className="home-042-status"><span>自动接待</span><strong>{model.snapshot?.reception.running ? '运行中' : '已暂停'}</strong></div><Button variant="ghost" onClick={() => void model.navigate('settings')}>连接与设置</Button></section><section><h2>最近沟通</h2>{recent.length ? recent.map(item => <button type="button" className="home-042-row" key={item.conversation_id} onClick={() => void model.selectConversation(item.conversation_id, item.case_id)}><span><strong>{item.customer_name}</strong><small>{item.last_message || '暂无消息'}</small></span><ChevronRight size={16} /></button>) : <Empty title="暂无沟通记录" />}</section><section><h2>资料与政策</h2><p>管理接待使用的资料和规则。</p><Button variant="ghost" onClick={() => void model.navigate('policies')}>管理资料</Button></section></div></div>;
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

function QuoteSupply({ model, current, onOpenProduct }: { model: Workbench; current: Case; onOpenProduct?: (id: string) => void }) {
  const [reference, setReference] = useState<{ name: string; price: string } | null>(null);
  useEffect(() => {
    let active = true; setReference(null);
    if (current.local_product_id) void model.invoke<{ product: { default_offer_id: string | null }; offers: { offer_id: string; supplier_name?: string; unit_price: string; currency: string; unit: string }[] }>('get_product', { local_product_id: current.local_product_id }).then(result => {
      if (!active) return;
      const offer = result.offers.find(item => item.offer_id === (current.selected_offer_id || result.product.default_offer_id));
      if (offer) setReference({ name: offer.supplier_name || '供应商', price: [offer.unit_price, offer.currency, offer.unit].filter(Boolean).join(' / ') || '价格未提供' });
    }).catch(() => {});
    return () => { active = false; };
  }, [model.invoke, current.local_product_id, current.selected_offer_id]);
  return <section><h3>供货参考</h3><p>{reference ? `${reference.name}：${reference.price}` : '暂无已选供货参考'}</p>{current.local_product_id && onOpenProduct && <Button onClick={() => onOpenProduct(current.local_product_id!)}>查看产品</Button>}</section>;
}

function QuoteSupplement({ model, conversationId }: { model: Workbench; conversationId: string }) {
  const [error, setError] = useState('');
  useEffect(() => { let active = true; setError(''); void model.ensureSupplement(conversationId).catch(err => { if (active) setError(errorText(err)); }); return () => { active = false; }; }, [conversationId, model.ensureSupplement]);
  const draft = model.supplementFor(conversationId);
  return <section>{error && <Feedback>{error}</Feedback>}<SupplementPanel model={model} conversationId={conversationId} /><div className="quotes-042-save"><Button variant="primary" disabled={!draft?.dirty || !!draft.saving || !!draft.attachmentBusy} onClick={() => void model.saveSupplement(conversationId)}>保存补充</Button></div></section>;
}

export function HandoffPage({ model, onOpenProduct }: { model: Workbench; onOpenProduct?: (id: string) => void }) {
  const explicit = model.snapshot?.handoff_cases;
  const legacy = model.snapshot?.conversations.filter(item => item.state === 'waiting_quote') ?? [];
  const { records, error, loading } = useCustomerDetails(model, explicit === undefined ? legacy.map(item => item.conversation_id) : []);
  const cases = explicit ?? Object.values(records).filter(detail => legacy.some(item => item.conversation_id === detail.conversation.conversation_id)).map(detail => detail.case).filter((item): item is Case => !!item && item.state === 'waiting_quote');
  const [selected, setSelected] = useState('');
  const current = cases.find(item => item.case_id === selected) ?? cases[0];
  return <div className="page quotes-042"><PageTitle eyebrow="" title="待我报价" description="查看采购事项并补充报价信息。" />
    {error && explicit === undefined && <Feedback>{error}</Feedback>}
    {explicit === undefined && loading && !Object.keys(records).length ? <Empty title="正在读取事项…" /> : !current ? <Empty title="暂时没有待报价事项" /> : <div className="quotes-042-layout"><section className="quotes-042-list" aria-label="待报价事项">{cases.map(item => <button type="button" key={item.case_id} className={item.case_id === current.case_id ? 'active' : ''} onClick={() => void (async () => { if (await model.saveSupplement(current.conversation_id)) setSelected(item.case_id); })()}><strong>{item.customer_name}</strong><span>{caseLabel(item, cases.filter(other => other.conversation_id === item.conversation_id))}</span><small>{dateTime(item.updated_at)}</small></button>)}</section><section className="quotes-042-detail" aria-label="事项详情"><header><h2>{current.customer_name}</h2><Button onClick={() => void (async () => { if (await model.saveSupplement(current.conversation_id)) await model.selectConversation(current.conversation_id, current.case_id); })()}>打开聊天</Button></header><SharedRequirementCard key={current.case_id} model={model} current={current} /><QuoteSupply model={model} current={current} onOpenProduct={onOpenProduct} /><QuoteSupplement model={model} conversationId={current.conversation_id} /></section></div>}
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
  return <div className="page"><PageTitle eyebrow="只呈现已同步、有来源的资料" title="客户资料" description="客户、平台会话与采购事项在这里一一对应。" /><div className="directory-search"><Search size={17} /><input aria-label="搜索客户资料" value={query} onChange={event => setQuery(event.target.value)} placeholder="客户名称、地区或买家编号" /></div><div className="directory-layout"><section className="directory-list" aria-label="客户资料目录">{filtered.map(item => <button type="button" className={item.conversation_id === current?.conversation_id ? 'active' : ''} key={item.conversation_id} onClick={() => setSelected(item.conversation_id)}><span><strong>{item.customer_name}</strong><small>{item.country || '地区尚未确认'}</small></span><ChevronRight size={16} /></button>)}{!filtered.length && <Empty title="没有匹配的客户资料" />}</section><section className="profile-card">{!current ? <Empty title="选择客户查看资料" /> : <><div className="profile-heading"><div><h2>{current.customer_name}</h2><p><Globe2 size={14} />{current.country || '地区尚未确认'}</p></div><StateBadge state={current.state} /></div><dl className="profile-fields"><div><dt>买家编号</dt><dd>{current.buyer_id}</dd></div><div><dt>平台会话</dt><dd>{current.conversation_id}</dd></div><div><dt>资料来源</dt><dd>平台已同步会话与采购原文</dd></div></dl><h3>客户摘要</h3><p>{record?.conversation_summary?.body || '暂无摘要'}</p><h3>采购事项</h3>{error && <Feedback>{error}</Feedback>}{loading && !record ? <p className="muted">正在读取关联事项…</p> : record?.cases.map(item => <div key={item.case_id}><button type="button" className="profile-case" onClick={() => void model.selectConversation(current.conversation_id, item.case_id)}><FileText size={18} /><span><strong>{caseLabel(item, record.cases)}</strong><small>{typeof item.requirements?.quantity?.value === 'string' ? item.requirements.quantity.value : ''} · v{item.revision}</small></span><StateBadge state={item.state} /><ChevronRight size={15} /></button><SharedRequirementCard model={model} current={item} /></div>)}<Button variant="primary" onClick={() => void model.selectConversation(current.conversation_id, current.case_id)}><MessageCircle size={16} />回到客户会话</Button></>}</section></div></div>;
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
  const [scopeQuery, setScopeQuery] = useState('');
  const [settingsSection, setSettingsSection] = useState(0);
  const formVersion = useRef(0);
  const scopeVersion = useRef(0);
  useEffect(() => { model.setFormDirty('settings', dirty || scopeDirty); return () => model.setFormDirty('settings', false); }, [dirty, scopeDirty, model.setFormDirty]);
  useEffect(() => { if (settings && !dirty) setForm({ workctl_path: settings.workctl_path, probe_seconds: settings.probe_seconds ?? 2, poll_seconds: settings.poll_seconds ?? 30, merge_seconds: settings.merge_seconds ?? 1, required_fields: settings.required_fields, ...processingParameters(settings) }); }, [settings, dirty]);
  useEffect(() => { const scope = model.snapshot?.reception.scope; if (scope && !scopeDirty) { setScopeMode(scope.mode === 'all' ? 'all' : 'selected'); setScopeIds(scope.mode === 'selected' ? scope.conversation_ids : []); } }, [model.snapshot?.reception.scope, scopeDirty]);
  const update = <K extends keyof typeof form>(key: K, value: typeof form[K]) => { formVersion.current++; setForm(previous => ({ ...previous, [key]: value })); setDirty(true); setFeedback(''); };
  const checks = model.snapshot?.checks;
  const checkCards: { title: string; description: string; key: 'messages' | 'ai' | 'feishu'; method: ApiMethod; icon: typeof Link2; button: string }[] = [
    { title: '平台身份与消息读取', description: '只核验账号身份和读取能力，不发送消息。', key: 'messages', method: 'check_connection', icon: Link2, button: '检查读取' },
    { title: 'AI 回复能力', description: '按需检查当前模型配置。', key: 'ai', method: 'check_ai', icon: Sparkles, button: '检查模型' },
    { title: '飞书交接结构', description: '仅检查同步结构；与消息读取和 AI 独立。', key: 'feishu', method: 'check_feishu', icon: FileCheck2, button: '检查飞书' },
  ];
  const runCheck = async (method: ApiMethod) => {
    await model.runJob(method);
  };
  const saveSettings = async (event: FormEvent) => { event.preventDefault(); const version = formVersion.current; const result = await model.mutate('save_settings', form); if (result) { setDirty(formVersion.current !== version); setFeedback(formVersion.current === version ? '设置已保存，自动发送授权已撤销。' : '前一版已保存，刚输入的修改仍待保存。'); } };
  const saveScope = async () => { const version = scopeVersion.current; const result = await model.mutate('set_scope', { mode: scopeMode, conversation_ids: scopeMode === 'selected' ? scopeIds : [], max_auto_replies: null }, '本轮范围已保存。'); if (result) { setScopeDirty(scopeVersion.current !== version); } };
  const reception = model.snapshot?.reception;
  const health = executorHealth(model.snapshot);
  return <div className="page settings-page"><PageTitle eyebrow="每一种能力，分别核验" title="连接与设置" description="检查结果是带时间的历史记录。实时接待是否运行，以窗口顶部为准。" action={<Button loading={model.loading} onClick={() => void model.refreshSnapshot()}><RefreshCw size={16} />刷新服务状态</Button>} />
    {!health.ready && <Feedback tone="red">{health.message}。新任务暂未就绪，已有正文和回执保留。</Feedback>}
    {!model.connected && <Feedback tone="red">服务尚未连接。请通过桌面应用的授权启动链恢复服务。<Button onClick={() => void model.refreshSnapshot()}>重新连接</Button></Feedback>}
    {model.snapshot?.app.mode === 'demo' && <Feedback>演示模式：这里的检查使用合成数据或本地模拟，通过不代表真实 Accio、AI 或飞书已连接。</Feedback>}
    <div className="settings-042-layout"><nav aria-label="设置分类">{['自动接待', '聊天翻译', '图片与上下文', '平台连接', '数据与备份', '飞书登记', '界面布局', '版本与更新'].map((name, index) => <button type="button" key={name} className={settingsSection === index ? 'active' : ''} onClick={() => setSettingsSection(index)}>{name}</button>)}</nav><div className="settings-042-content">
    <details hidden={settingsSection !== 3} className="settings-042-diagnostics"><summary>连接诊断</summary><section className="checks-grid">{checkCards.map(item => { const check = checks?.[item.key]; const Icon = item.icon; return <article className="check-card" key={item.key}><div className="check-card-heading"><span className="check-icon"><Icon size={21} /></span><h2>{item.title}</h2><Badge tone={check?.status === 'passed' ? 'green' : check?.status === 'failed' ? 'red' : check?.status === 'demo' || check?.status === 'blocked' ? 'amber' : 'neutral'}>{check ? checkLabels[check.status] : '状态未知'}</Badge></div><p>{item.description}</p><div className="check-message">{check?.message || '尚无检查结果'}</div><div className="check-meta">{check?.simulated && <strong>模拟检查 · </strong>}{dateTime(check?.checked_at)}</div><Button disabled={!model.connected || !health.ready || model.busy(item.method)} loading={model.busy(item.method)} onClick={() => void runCheck(item.method)}>{item.button}</Button>{check?.check_id && <details className="check-details"><summary>检查详情</summary><p>检查编号：{check.check_id}</p><p>读取环境：{checks?.runtime ? checkLabels[checks.runtime.status] : '未知'} · 账号身份：{checks?.identity ? checkLabels[checks.identity.status] : '未知'}</p></details>}</article>; })}</section>
    <MessageSyncSummary sync={model.snapshot?.message_sync} /></details>
    <div hidden={settingsSection !== 4}><DataStorage bridge={bridge} /></div>
    <div hidden={settingsSection !== 7}><UpdatesPanel bridge={bridge} currentVersion={model.snapshot?.app.version} /></div>
    <div hidden={settingsSection !== 1}><ModelSummary bridge={bridge} /></div>
    <div hidden={settingsSection !== 2 && settingsSection !== 3}><form className="form-card" onSubmit={event => void saveSettings(event)}><div className="form-card-heading"><h2><Settings2 size={18} />{settingsSection === 2 ? "图片与上下文" : "平台连接"}</h2></div><div hidden={settingsSection !== 3}><label>Accio workctl 程序路径<div className="input-with-button"><input value={form.workctl_path} onChange={event => update('workctl_path', event.target.value)} placeholder="由桌面应用选择已安装程序" /><IconButton label="选择 workctl 程序" disabled={!bridge} onClick={() => void model.action('chooseWorkctl', async () => { const selected = await bridge?.chooseWorkctl(); if (selected) update('workctl_path', selected); })}><FolderOpen size={18} /></IconButton></div></label><div className="form-row"><label>新消息检测（秒）<input type="number" min={2} max={60} value={form.probe_seconds} onChange={event => update('probe_seconds', Number(event.target.value))} required /></label><label>摘要续页间隔（秒）<input type="number" min={5} max={3600} value={form.poll_seconds} onChange={event => update('poll_seconds', Number(event.target.value))} required /></label><label>连续消息合并（秒）<input type="number" min={0} max={120} value={form.merge_seconds} onChange={event => update('merge_seconds', Number(event.target.value))} required /></label></div></div><div hidden={settingsSection !== 2}><fieldset><legend>交接前必备条件</legend><div className="field-checks">{Object.entries(fieldLabels).filter(([key]) => !key.startsWith('sample')).map(([key, label]) => <label className="checkbox-label compact" key={key}><input type="checkbox" disabled={key === 'product'} checked={key === 'product' || form.required_fields.includes(key)} onChange={event => update('required_fields', event.target.checked ? [...new Set([...form.required_fields, key, 'product'])] : form.required_fields.filter(field => field !== key))} />{label}{key === 'product' && <small>必填</small>}</label>)}</div></fieldset><ProcessingSettings value={form} onChange={parameters => { formVersion.current++; setForm(previous => ({ ...previous, ...parameters })); setDirty(true); setFeedback(''); }} /></div>{feedback && <Feedback tone="green">{feedback}</Feedback>}<Button type="submit" variant="primary" disabled={!dirty || !model.connected} loading={model.busy('save_settings')}><Save size={16} />保存连接参数</Button>{dirty && <Button onClick={() => { if (settings) setForm({ workctl_path: settings.workctl_path, probe_seconds: settings.probe_seconds ?? 2, poll_seconds: settings.poll_seconds ?? 30, merge_seconds: settings.merge_seconds ?? 1, required_fields: settings.required_fields, ...processingParameters(settings) }); setDirty(false); setFeedback('已放弃连接参数修改。'); }}>放弃参数修改</Button>}</form>
    </div><div hidden={settingsSection !== 5}><FeishuForm model={model} /></div>
    <div hidden={settingsSection !== 6} className="form-card"><h2>界面布局</h2><p>在接待页调整资料栏宽度与高度。</p></div>
    <section hidden={settingsSection !== 0} className="form-card scope-card">
      <div className="form-card-heading"><h2><UsersRound size={18} />自动接待范围</h2><Badge tone={!health.ready ? 'red' : reception?.running ? 'green' : 'neutral'}>{!health.ready ? health.message : reception?.running ? '自动接待中' : '自动接待已暂停'}</Badge></div>
      <p className="form-description">按所选范围接待，人工接待中的客户除外。</p>
      <div className="scope-options"><select aria-label="自动接待范围" value={scopeMode} onChange={event => { setScopeMode(event.target.value === 'all' ? 'all' : 'selected'); setScopeDirty(true); scopeVersion.current++; }}><option value="all">全部客户</option><option value="selected">选中客户</option></select></div>
      {scopeMode === 'selected' && <div className="scope-customers"><input aria-label="搜索接待客户" placeholder="搜索客户" value={scopeQuery} onChange={event => setScopeQuery(event.target.value)} />{model.snapshot?.conversations.filter(item => `${item.customer_name} ${item.buyer_id}`.toLowerCase().includes(scopeQuery.toLowerCase())).map(item => <label className="checkbox-label compact" key={item.conversation_id}><input type="checkbox" checked={scopeIds.includes(item.conversation_id)} onChange={event => { setScopeIds(previous => event.target.checked ? [...previous, item.conversation_id] : previous.filter(id => id !== item.conversation_id)); setScopeDirty(true); scopeVersion.current++; }} />{item.customer_name}</label>)}</div>}
      <div className="scope-save"><Button disabled={!model.connected || !scopeDirty} loading={model.busy('set_scope')} onClick={() => void saveScope()}>保存范围</Button>{scopeDirty && <Button onClick={() => { const current = model.snapshot?.reception.scope; if (current) { setScopeMode(current.mode === 'all' ? 'all' : 'selected'); setScopeIds(current.mode === 'selected' ? current.conversation_ids : []); } setScopeDirty(false); }}>放弃范围修改</Button>}</div>
    </section></div></div>
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
  const filtered = model.jobs.filter(job => !/send|reconcile_operation|confirm_send|preview_send/i.test(job.kind)).filter(job => filter === 'all' || (filter === 'active' && (['queued', 'running'].includes(job.status) || job.status === 'deferred' && !!job.retry_at)) || (filter === 'attention' && (['failed', 'unknown', 'stale'].includes(job.status) || job.retry_status === 'blocked' || job.retry_status === 'exhausted')));
  const job = filtered.find(item => item.job_id === selected);
  const refresh = () => void model.action('records-refresh', async () => { await model.refreshSnapshot(); model.setNotice('运行记录已刷新。'); });
  return <div className="page"><PageTitle eyebrow="每一次检查和处理，都有独立记录" title="运行记录" description="查看连接、资料与采购等任务状态。" action={<Button disabled={!model.connected} onClick={refresh} loading={model.busy('records-refresh')}><RefreshCw size={16} />刷新记录</Button>} /><MessageSyncSummary sync={model.snapshot?.message_sync} /><div className="record-tabs">{[['all', '全部任务'], ['active', '进行中'], ['attention', '需要关注']].map(([value, label]) => <button type="button" className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div><div className="records-layout"><div className="records-table"><div className="records-head"><span>任务</span><span>状态</span><span>创建时间</span></div>{filtered.length === 0 ? <Empty title="尚无此类任务记录">检查和生成均由您明确触发，不会因刷新页面自动执行。</Empty> : filtered.map(item => <button type="button" key={item.job_id} className={`record-row ${item.job_id === selected ? 'active' : ''}`} onClick={() => setSelected(item.job_id)}><span><strong>{jobLabels[item.kind] ?? '后台任务'}</strong><small>{item.kind === 'generate_draft' && !['succeeded', 'cancelled', 'stale'].includes(item.status) && (item.retry_attempt || item.status === 'failed') ? '回复整理中或等待恢复' : item.message || item.stage}</small></span><Badge tone={item.status === 'succeeded' ? 'green' : item.status === 'failed' || item.status === 'unknown' ? 'red' : item.status === 'running' ? 'amber' : 'neutral'}>{recordStatus(item)}</Badge><time>{dateTime(item.created_at)}</time></button>)}</div>{job && <aside className="job-detail"><div className="form-card-heading"><h2>任务详情</h2><IconButton label="关闭任务详情" onClick={() => setSelected(null)}><X size={17} /></IconButton></div><h3>{jobLabels[job.kind] ?? '后台任务'}</h3><p>{job.kind === 'generate_draft' && !['succeeded', 'cancelled', 'stale'].includes(job.status) && (job.retry_attempt || job.status === 'failed') ? '回复整理中或等待恢复' : job.message || '暂无补充说明'}</p><dl><div><dt>任务编号</dt><dd>{job.job_id}</dd></div><div><dt>处理阶段</dt><dd>{job.stage || '尚未开始'}</dd></div><div><dt>创建时间</dt><dd>{dateTime(job.created_at)}</dd></div><div><dt>开始时间</dt><dd>{dateTime(job.started_at)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(job.completed_at)}</dd></div><div><dt>会话归属</dt><dd>{model.snapshot?.conversations.find(item => item.conversation_id === job.conversation_id)?.customer_name || job.conversation_id || '全局检查'}</dd></div>{job.case_id && <div><dt>采购事项</dt><dd>{job.case_id}</dd></div>}</dl>{job.error && <Feedback tone="red">{job.kind === 'generate_draft' ? '回复暂未整理完成，请查看状态并等待恢复。' : <>{job.error.message}<small>错误码：{job.error.code}</small></>}</Feedback>}{(job.retry_attempt || job.repair_count || job.retry_at) && <p>{job.kind === 'automatic_send' || job.kind === 'continuity_send' ? `自动发送已尝试 ${job.retry_attempt ?? 0} 次（最多3次）` : `自动重试 ${job.retry_attempt ?? 0} 次`} · 格式整理 {job.repair_count ?? 0} 次{job.retry_at ? ` · 下次尝试 ${dateTime(job.retry_at)}` : ''}</p>}<details><summary>诊断信息</summary><ReadDiagnostics diagnostics={job.diagnostics} /><SnapshotDiagnostics diagnostics={job.diagnostics} /></details>{job.conversation_id && <Button onClick={() => void model.selectConversation(job.conversation_id!, job.case_id ?? '')}>查看所属会话 <ArrowRight size={14} /></Button>}</aside>}</div></div>;
}
