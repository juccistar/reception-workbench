import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Check, ChevronRight, ClipboardCheck, Clock3, Copy, FileCheck2, FileText, FolderOpen, Globe2, History, Link2, MessageCircle, Pause, Play, RefreshCw, Save, Search, Settings2, ShieldCheck, Sparkles, UserRound, UsersRound } from 'lucide-react';
import type { ApiMethod, Case, ConversationDetail, Job, ReceptionBridge, Scope } from '../../shared/api';
import { errorText, type Workbench } from '../hooks/useWorkbench';
import { Badge, Button, Drawer, Empty, Feedback, IconButton, MessageSyncSummary, StateBadge, checkLabels, dateTime, fieldLabels, jobLabels, jobStatusLabels } from './ui';
import { FeishuForm } from './FeishuForm';
import { DataStorage } from './DataStorage';
import { UpdatesPanel } from './UpdatesPanel';
import { SnapshotDiagnostics } from './SnapshotDiagnostics';
import { ReadDiagnostics } from './ReadDiagnostics';
import { registrationSummary } from '../receptionStatus';
import { executorHealth } from '../executorHealth';
import { caseLabel } from './caseLabels';
import { SharedRequirementCard } from './SharedRequirementCard';
import { ProductMainImage } from './ProductMainImage';
import { SupplementPanel } from './SupplementPanel';
import { ProcessingSettings, processingParameters } from './ProcessingSettings';
import './pages-042.css';
import './business-pages-042.css';

const PageTitle = ({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) => <header className="page-title"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;

export function HomePage({ model }: { model: Workbench }) {
  const conversations = model.snapshot?.conversations ?? [];
  const work = conversations.filter(item => ['clarifying', 'needs_human', 'waiting_quote', 'human_owned'].includes(item.state) || item.pending_count > 0);
  const recent = [...conversations].sort((a, b) => b.last_timestamp - a.last_timestamp).slice(0, 5);
  const policies = (model.snapshot?.policies ?? []).filter(item => item.enabled);
  return <div className="page home-042"><PageTitle eyebrow="" title="开始" description={`${new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())}。继续处理需要你参与的客户。`} action={<Button variant="primary" onClick={() => void model.navigate('reception')}><MessageCircle size={15} />进入接待</Button>} /><div className="home-042-grid">
    <section><header><h2>等你处理 <small>{work.length} 位客户</small></h2><button className="business-042-link" onClick={() => void model.navigate('reception')}>查看全部 <ChevronRight size={14} /></button></header>{work.length ? work.slice(0, 8).map(item => <button type="button" className="home-042-task" key={item.conversation_id} onClick={() => void model.selectConversation(item.conversation_id, item.case_id)}><MessageCircle size={21} /><span><strong>{item.customer_name} <StateBadge state={item.state} /></strong><small>{item.last_message || '采购需求待整理'}</small></span><span className="business-042-link">{item.state === 'waiting_quote' ? '处理报价' : '继续沟通'} <ChevronRight size={14} /></span></button>) : <Empty title="目前没有需要处理的客户" />}</section>
    <section><header><h2>工作区状态</h2><ShieldCheck size={18} /></header><div className="home-042-statuses"><div className="home-042-status"><span>平台连接</span><strong>{model.connected ? '已连接' : '未连接'}</strong></div><div className="home-042-status"><span>自动接待</span><strong>{model.snapshot?.reception.running ? '运行中' : '已关闭'}</strong></div><div className="home-042-status"><span>资料与政策</span><strong>{policies.length} 份已启用</strong></div><button className="business-042-link" onClick={() => void model.navigate('settings')}>连接与设置 <ChevronRight size={14} /></button></div></section>
    <section><header><h2>最近沟通</h2><button className="business-042-link" onClick={() => void model.navigate('customers')}>客户资料 <ChevronRight size={14} /></button></header>{recent.length ? recent.map(item => <button type="button" className="home-042-row" key={item.conversation_id} onClick={() => void model.selectConversation(item.conversation_id, item.case_id)}><span><strong>{item.customer_name}</strong><small>{[item.country, dateTime(item.last_timestamp)].filter(Boolean).join('　')}</small></span><span className="business-042-link">打开聊天 <ChevronRight size={14} /></span></button>) : <Empty title="暂无沟通记录" />}</section>
    <section><header><h2>常用资料</h2><button className="business-042-link" onClick={() => void model.navigate('policies')}>管理资料 <ChevronRight size={14} /></button></header>{policies.length ? policies.slice(0, 3).map(item => <button type="button" className="home-042-reading" key={item.policy_id} onClick={() => void model.navigate('policies')}><FileText size={16} /><span>{item.title}<small>第 {item.revision} 版，已启用</small></span></button>) : <Empty title="暂无启用的资料" />}</section>
  </div></div>;
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
  return <section className="quotes-042-supply"><span>供货参考{reference && <small>{reference.name}：{reference.price}</small>}</span>{current.local_product_id && onOpenProduct ? <button type="button" className="business-042-link" onClick={() => onOpenProduct(current.local_product_id!)}>查看产品找货进度 <ChevronRight size={14} /></button> : <span>暂无已选供货参考</span>}</section>;
}

function QuoteSupplement({ model, conversationId }: { model: Workbench; conversationId: string }) {
  const [error, setError] = useState('');
  useEffect(() => { let active = true; setError(''); void model.ensureSupplement(conversationId).catch(err => { if (active) setError(errorText(err)); }); return () => { active = false; }; }, [conversationId, model.ensureSupplement]);
  const draft = model.supplementFor(conversationId);
  return <section className="quotes-042-supplement">{error && <Feedback>{error}</Feedback>}<SupplementPanel model={model} conversationId={conversationId} /><p>保存到当前客户的业务补充，由接待结合聊天使用。</p><div className="quotes-042-save"><span>同一客户的多个事项共用业务补充</span><Button variant="primary" disabled={!draft?.dirty || !!draft.saving || !!draft.attachmentBusy} onClick={() => void model.saveSupplement(conversationId)}>保存补充</Button></div></section>;
}

export function HandoffPage({ model, onOpenProduct }: { model: Workbench; onOpenProduct?: (id: string) => void }) {
  const explicit = model.snapshot?.handoff_cases;
  const legacy = model.snapshot?.conversations.filter(item => item.state === 'waiting_quote') ?? [];
  const { records, error, loading } = useCustomerDetails(model, explicit === undefined ? legacy.map(item => item.conversation_id) : []);
  const cases = explicit ?? Object.values(records).filter(detail => legacy.some(item => item.conversation_id === detail.conversation.conversation_id)).map(detail => detail.case).filter((item): item is Case => !!item && item.state === 'waiting_quote');
  const [selected, setSelected] = useState('');
  const current = cases.find(item => item.case_id === selected) ?? cases[0];
  const field = (item: Case, name: string) => { const value = item.requirements_format === 'ai-v1' ? item.requirements?.[name]?.value : null; return typeof value === 'string' && value.trim() ? value : ''; };
  const conversation = model.snapshot?.conversations.find(item => item.conversation_id === current?.conversation_id);
  return <div className="page quotes-042"><PageTitle eyebrow="" title="待我报价" description={`有 ${cases.length} 项采购需求等待商家报价，先确认产品与交付条件。`} />
    {error && explicit === undefined && <Feedback>{error}</Feedback>}
    {explicit === undefined && loading && !Object.keys(records).length ? <Empty title="正在读取事项…" /> : !current ? <Empty title="暂时没有待报价事项" /> : <div className="quotes-042-layout"><section className="quotes-042-list" aria-label="待报价事项">{cases.map(item => { const customer = model.snapshot?.conversations.find(row => row.conversation_id === item.conversation_id); return <button type="button" key={item.case_id} className={item.case_id === current.case_id ? 'active' : ''} onClick={() => void (async () => { if (await model.saveSupplement(current.conversation_id)) setSelected(item.case_id); })()}><strong>{item.customer_name}</strong><small>{caseLabel(item, cases.filter(other => other.conversation_id === item.conversation_id))}</small><span>{field(item, 'product') || '产品待整理'}</span><span>{[field(item, 'quantity'), customer?.country].filter(Boolean).join('　') || '需求待整理'}</span><div><StateBadge state={item.state} /><time>{dateTime(item.updated_at)}</time></div></button>; })}</section><section className="quotes-042-detail" aria-label="事项详情"><header><div><h2>{current.customer_name}</h2><p>{conversation?.country || '地区未提供'}</p></div><button type="button" className="business-042-link" onClick={() => void (async () => { if (await model.saveSupplement(current.conversation_id)) await model.selectConversation(current.conversation_id, current.case_id); })()}>打开聊天 <ChevronRight size={14} /></button></header><div className="quotes-042-product"><ProductMainImage key={current.case_id} caseId={current.case_id} localProductId={current.local_product_id} metadata={current.product_main_image} invoke={model.invoke} bridge={model.bridge} canRequest={/^[0-9]+$/.test(field(current, 'product_id'))} /><div><h3>{field(current, 'product') || '产品待整理'}</h3><p>{field(current, 'specifications') || '规格待确认'}</p></div></div><div className="quotes-042-facts">{[['quantity', '采购数量'], ['customization', '定制要求'], ['destination', '收货地'], ['delivery', '交期']].map(([key, label]) => <div key={key}><span>{label}</span><strong>{field(current, key) || '待确认'}</strong></div>)}</div><details className="quotes-042-more"><summary>查看完整采购需求</summary><SharedRequirementCard key={current.case_id} model={model} current={current} /></details><QuoteSupply model={model} current={current} onOpenProduct={onOpenProduct} /><QuoteSupplement model={model} conversationId={current.conversation_id} /></section></div>}
  </div>;
}

export function CustomersPage({ model }: { model: Workbench }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const customers = model.snapshot?.conversations ?? [];
  const filtered = customers.filter(item => `${item.customer_name} ${item.country} ${item.buyer_id}`.toLowerCase().includes(query.toLowerCase()));
  const current = customers.find(item => item.conversation_id === selected);
  const { records, error, loading } = useCustomerDetails(model, current ? [current.conversation_id] : []);
  const record = current ? records[current.conversation_id] : undefined;
  return <div className="page customers-042"><PageTitle eyebrow="" title="客户资料" description="从已同步的会话查找客户，查看摘要与历次采购需求。" />
    <div className="customers-042-toolbar"><span>已同步客户</span><label className="directory-search"><Search size={17} /><input aria-label="搜索客户资料" value={query} onChange={event => setQuery(event.target.value)} placeholder="姓名、地区或买家标识" /></label></div>
    <div className="customers-042-table-wrap"><table className="customers-042-table"><thead><tr><th>客户</th><th>国家 / 地区</th><th>最近采购需求</th><th>当前状态</th><th>最近沟通</th><th>操作</th></tr></thead><tbody>{filtered.map(item => <tr key={item.conversation_id}><td><strong>{item.customer_name}</strong><small>{item.buyer_id || '—'}</small></td><td>{item.country || '—'}</td><td>{(() => { const requirement = (item as typeof item & { latest_requirement?: { product: string | null; quantity: string | null } | null }).latest_requirement; return requirement ? <><strong>{requirement.product || '产品待整理'}</strong>{requirement.quantity && <small>{requirement.quantity}</small>}</> : '暂无采购需求'; })()}</td><td><StateBadge state={item.state} /></td><td>{dateTime(item.last_timestamp)}</td><td><button type="button" className="business-042-link" onClick={() => setSelected(item.conversation_id)}>查看资料 <ChevronRight size={14} /></button></td></tr>)}{!filtered.length && <tr><td colSpan={6}>没有找到客户</td></tr>}</tbody></table><footer><span>客户姓名与标识来自平台会话</span><span>{filtered.length} 位客户</span></footer></div>
    {current && <Drawer title="客户资料" onClose={() => setSelected('')}><div className="customer-042-detail"><h2>{current.customer_name}</h2><p>{current.country || '—'}</p><section><h3>客户摘要</h3><p>{record?.conversation_summary?.body || '暂无摘要'}</p></section><section><h3>采购需求</h3>{error && <Feedback>{error}</Feedback>}{loading && !record ? <p>正在读取关联事项…</p> : record?.cases.length ? record.cases.map(item => <div key={item.case_id}><button type="button" className="profile-case" onClick={() => void model.selectConversation(current.conversation_id, item.case_id)}><FileText size={18} /><span><strong>{caseLabel(item, record.cases)}</strong></span><StateBadge state={item.state} /><ChevronRight size={15} /></button><SharedRequirementCard model={model} current={item} /></div>) : <p>当前还没有采购需求，可以直接继续聊天。</p>}</section><details><summary>来源信息</summary><dl><dt>买家标识</dt><dd>{current.buyer_id || '—'}</dd><dt>平台会话</dt><dd>{current.conversation_id}</dd></dl></details><footer><Button onClick={() => setSelected('')}>关闭</Button><Button variant="primary" onClick={() => void model.selectConversation(current.conversation_id, current.case_id)}>打开聊天</Button></footer></div></Drawer>}
  </div>;
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
  const [layout, setLayout] = useState(() => ({ nav_preference: model.preferences?.nav_preference ?? 'expanded', details_preference: model.preferences?.details_preference ?? 'expanded' }));
  const [layoutDirty, setLayoutDirty] = useState(false);
  const layoutVersion = useRef(0);
  useEffect(() => { if (!layoutDirty) setLayout({ nav_preference: model.preferences?.nav_preference ?? 'expanded', details_preference: model.preferences?.details_preference ?? 'expanded' }); }, [model.preferences?.nav_preference ?? 'expanded', model.preferences?.details_preference ?? 'expanded', layoutDirty]);
  useEffect(() => { model.setFormDirty('layout', layoutDirty); return () => model.setFormDirty('layout', false); }, [layoutDirty, model.setFormDirty]);
  const saveLayout = async () => { const version = layoutVersion.current; const saved = await model.changePreferences(layout); if (saved && version === layoutVersion.current) setLayoutDirty(false); return !!saved && version === layoutVersion.current; };
  useEffect(() => model.registerFormSaver?.('layout', saveLayout), [model.registerFormSaver, layout]);
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
  const saveSettings = async (event?: FormEvent): Promise<boolean> => { event?.preventDefault(); const version = formVersion.current; const result = await model.mutate('save_settings', form); if (result) { setDirty(formVersion.current !== version); setFeedback(formVersion.current === version ? '设置已保存，自动发送授权已撤销。' : '前一版已保存，刚输入的修改仍待保存。'); } return !!result && formVersion.current === version; };
  const saveScope = async (): Promise<boolean> => { const version = scopeVersion.current; const result = await model.mutate('set_scope', { mode: scopeMode, conversation_ids: scopeMode === 'selected' ? scopeIds : [], max_auto_replies: null }, '本轮范围已保存。'); if (result) { setScopeDirty(scopeVersion.current !== version); } return !!result && scopeVersion.current === version; };
  useEffect(() => model.registerFormSaver?.('settings', async () => {
    const formAtStart = formVersion.current, scopeAtStart = scopeVersion.current;
    if (dirty && !(await saveSettings())) return false;
    if (scopeDirty && !(await saveScope())) return false;
    return formVersion.current === formAtStart && scopeVersion.current === scopeAtStart;
  }), [model.registerFormSaver, form, dirty, scopeMode, scopeIds, scopeDirty]);
  const reception = model.snapshot?.reception;
  const health = executorHealth(model.snapshot);
  return <div className="page settings-page"><PageTitle eyebrow="" title="连接与设置" description="常用配置集中管理，维护细节按需展开。" />
    {!health.ready && <Feedback tone="red">{health.message}。新任务暂未就绪，已有正文和回执保留。</Feedback>}
    {!model.connected && <Feedback tone="red">服务尚未连接。请通过桌面应用的授权启动链恢复服务。<Button onClick={() => void model.refreshSnapshot()}>重新连接</Button></Feedback>}
    {model.snapshot?.app.mode === 'demo' && <Feedback>演示模式：这里的检查使用合成数据或本地模拟，通过不代表真实 Accio、AI 或飞书已连接。</Feedback>}
    <div className="settings-042-layout"><nav aria-label="设置分类">{['自动接待', '图片与上下文', '平台连接', '数据与备份', '飞书登记', '界面布局', '版本与更新'].map((name, index) => <button type="button" key={name} className={settingsSection === index ? 'active' : ''} onClick={() => setSettingsSection(index)}>{name}</button>)}</nav><div className="settings-042-content">
    <section hidden={settingsSection !== 2} className="form-card settings-042-connection"><h2>平台连接</h2><p>当前商家身份与读取状态。</p><div className="settings-042-line"><span>{model.snapshot?.app.account_verified ? '当前商家' : '商家身份'}<small>国际站授权会话</small></span><Badge tone={model.connected && model.snapshot?.app.account_verified ? 'green' : 'neutral'}>{!model.connected ? '未连接' : model.snapshot?.app.mode === 'demo' ? '演示' : model.snapshot?.app.account_verified ? '已连接' : '待核验'}</Badge></div>{checkCards.filter(item => item.key !== 'feishu').map(item => { const check = checks?.[item.key]; return <div className="settings-042-line" key={item.key}><span>{item.key === 'messages' ? '身份与平台读取' : '接待模型检查'}<small>{check?.message || (item.key === 'messages' ? '尚无检查结果' : '单独检查模型可用性。')}</small></span><Button disabled={!model.connected || !health.ready || model.busy(item.method)} loading={model.busy(item.method)} onClick={() => void runCheck(item.method)}>{item.key === 'messages' ? '检查连接' : '检查模型'}</Button></div>; })}<p className="settings-042-note">检查不会提交客户消息。实际连接应从目标商家的授权会话启动。</p><details className="settings-042-advanced"><summary>连接详情</summary><MessageSyncSummary sync={model.snapshot?.message_sync} /><p>读取环境：{checks?.runtime ? checkLabels[checks.runtime.status] : '未知'} · 账号身份：{checks?.identity ? checkLabels[checks.identity.status] : '未知'}</p><label>Accio workctl 程序路径<div className="input-with-button"><input value={form.workctl_path} onChange={event => update('workctl_path', event.target.value)} /><IconButton label="选择 workctl 程序" disabled={!bridge} onClick={() => void model.action('chooseWorkctl', async () => { const selected = await bridge?.chooseWorkctl(); if (selected) update('workctl_path', selected); })}><FolderOpen size={18} /></IconButton></div></label>{dirty && <Button disabled={!model.connected} onClick={() => void saveSettings()}>保存设置</Button>}</details></section>
    <div hidden={settingsSection !== 3}><DataStorage bridge={bridge} /></div>
    <div hidden={settingsSection !== 6}><UpdatesPanel bridge={bridge} currentVersion={model.snapshot?.app.version} /></div>
    <div hidden={settingsSection !== 1}><form className="form-card" onSubmit={event => void saveSettings(event)}><div className="form-card-heading"><h2><Settings2 size={18} />{settingsSection === 1 ? "图片与上下文" : "平台连接"}</h2></div><div hidden={settingsSection !== 1}><p className="form-description">为当前需求准备相关图片，和文字一起提供给接待。</p><div className="settings-042-media-toggle"><span>相关原图<small>只使用当前任务适用的受控图片。</small></span><input type="checkbox" role="switch" aria-label="相关原图" checked={form.reception_images_enabled} onChange={event => update('reception_images_enabled', event.target.checked)} /></div><div className="settings-042-media-grid"><label>图片准备时间（秒）<input type="number" min={10} max={300} required value={form.image_prepare_seconds} onChange={event => update('image_prepare_seconds', Number(event.target.value))} /></label><label>整批图片预算（MiB）<input type="number" min={8} max={256} required value={form.image_batch_mib} onChange={event => update('image_batch_mib', Number(event.target.value))} /></label><label>图片准备并行数<select value={form.image_parallelism} onChange={event => update('image_parallelism', Number(event.target.value))}>{[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}</select></label><label>近期文字消息（条）<input type="number" min={1} max={200} required value={form.context_message_count} onChange={event => update('context_message_count', Number(event.target.value))} /></label></div><p className="settings-042-note">商品展示图仅用于浏览与找货，不替代客户原图。</p></div><div className="settings-042-media-footer"><span>{feedback || (dirty ? '有未保存修改' : '设置已保存')}</span><Button type="submit" variant="primary" disabled={!dirty || !model.connected} loading={model.busy('save_settings')}><Save size={16} />保存设置</Button>{dirty && <Button onClick={() => { if (settings) setForm({ workctl_path: settings.workctl_path, probe_seconds: settings.probe_seconds ?? 2, poll_seconds: settings.poll_seconds ?? 30, merge_seconds: settings.merge_seconds ?? 1, required_fields: settings.required_fields, ...processingParameters(settings) }); setDirty(false); setFeedback('已放弃连接参数修改。'); }}>放弃参数修改</Button>}</div></form>
    </div><div hidden={settingsSection !== 4}><FeishuForm model={model} /></div>
    <section hidden={settingsSection !== 5} className="form-card settings-042-display"><h2>界面布局</h2><p>选择日常接待使用的工作区布局。</p><label>主导航<select value={layout.nav_preference} onChange={event => { layoutVersion.current++; setLayout(previous => ({ ...previous, nav_preference: event.target.value as 'expanded' | 'icons' })); setLayoutDirty(true); }}><option value="expanded">图标与短名称</option><option value="icons">仅图标</option></select></label><label>客户资料<select value={layout.details_preference} onChange={event => { layoutVersion.current++; setLayout(previous => ({ ...previous, details_preference: event.target.value as 'expanded' | 'rail' })); setLayoutDirty(true); }}><option value="expanded">宽窗口默认展开</option><option value="rail">默认收起</option></select></label><p className="settings-042-note">客户列表与聊天顶部使用姓名和状态，不显示头像。</p><div className="settings-042-media-footer"><span>{layoutDirty ? '有未保存修改' : '设置已保存'}</span><Button variant="primary" disabled={!layoutDirty || !bridge} onClick={() => void saveLayout()}>保存设置</Button></div></section>
    <section hidden={settingsSection !== 0} className="form-card scope-card settings-042-reception">
      <div className="settings-042-section"><h2>自动接待</h2><p>在顶部开启或关闭自动接待，人工接待中的客户继续由你处理。</p><div className="settings-042-line"><span>当前接待状态</span><Badge tone={!health.ready ? 'red' : reception?.running ? 'green' : 'neutral'}>{!health.ready ? health.message : reception?.running ? '已开启' : '已关闭'}</Badge></div><div className="settings-042-line"><span>接待模型<small>接待与翻译模型在启动台统一配置。</small></span></div></div>
      <div className="settings-042-section"><h2>自动接待范围</h2><p className="form-description">选择自动接待覆盖的客户；当前聊天仍从客户列表打开。</p>
      <div className="scope-options"><select aria-label="自动接待范围" value={scopeMode} onChange={event => { setScopeMode(event.target.value === 'all' ? 'all' : 'selected'); setScopeDirty(true); scopeVersion.current++; }}><option value="all">全部客户</option><option value="selected">选中客户</option></select></div>
      {scopeMode === 'selected' && <div className="scope-customers"><header><strong>选择客户</strong><span>已选 {scopeIds.length} 位</span></header><input aria-label="搜索接待客户" placeholder="搜索客户" value={scopeQuery} onChange={event => setScopeQuery(event.target.value)} />{model.snapshot?.conversations.filter(item => `${item.customer_name} ${item.buyer_id}`.toLowerCase().includes(scopeQuery.toLowerCase())).map(item => <label className="checkbox-label compact" key={item.conversation_id}><input type="checkbox" checked={scopeIds.includes(item.conversation_id)} onChange={event => { setScopeIds(previous => event.target.checked ? [...previous, item.conversation_id] : previous.filter(id => id !== item.conversation_id)); setScopeDirty(true); scopeVersion.current++; }} />{item.customer_name}</label>)}</div>}
      <p className="settings-042-note">按所选范围接待，人工接待中的客户除外。</p><details className="settings-042-advanced"><summary>高级处理设置</summary><div className="form-row"><label>活动检测间隔（秒）<input type="number" min={2} max={60} value={form.probe_seconds} onChange={event => update('probe_seconds', Number(event.target.value))} /></label><label>消息合并窗口（秒）<input type="number" min={0} max={120} value={form.merge_seconds} onChange={event => update('merge_seconds', Number(event.target.value))} /></label></div><fieldset><legend>交接前必备条件</legend><div className="field-checks">{Object.entries(fieldLabels).filter(([key]) => !key.startsWith('sample')).map(([key, label]) => <label className="checkbox-label compact" key={key}><input type="checkbox" disabled={key === 'product'} checked={key === 'product' || form.required_fields.includes(key)} onChange={event => update('required_fields', event.target.checked ? [...new Set([...form.required_fields, key, 'product'])] : form.required_fields.filter(field => field !== key))} />{label}{key === 'product' && <small>必填</small>}</label>)}</div></fieldset><ProcessingSettings value={form} onChange={parameters => { formVersion.current++; setForm(previous => ({ ...previous, ...parameters })); setDirty(true); setFeedback(''); }} />{dirty && <Button disabled={!model.connected} loading={model.busy('save_settings')} onClick={() => void saveSettings()}>保存处理设置</Button>}</details></div>
      <div className="scope-save"><span>{scopeDirty ? '有未保存修改' : '设置已保存'}</span><Button variant="primary" disabled={!model.connected || !scopeDirty} loading={model.busy('set_scope')} onClick={() => void saveScope()}>保存范围</Button>{scopeDirty && <Button onClick={() => { const current = model.snapshot?.reception.scope; if (current) { setScopeMode(current.mode === 'all' ? 'all' : 'selected'); setScopeIds(current.mode === 'selected' ? current.conversation_ids : []); } setScopeDirty(false); }}>放弃范围修改</Button>}</div>
    </section></div></div>
  </div>;
}

function activityCategory(job: Job): 'task' | 'procurement' | 'connection' | 'materials' {
  if (/source_product|procurement|supplier|product_main_image/i.test(job.kind)) return 'procurement';
  if (/connection|check_ai|check_feishu|identity/i.test(job.kind)) return 'connection';
  if (/policy|material|knowledge/i.test(job.kind)) return 'materials';
  return 'task';
}

function needsAttention(job: Job) {
  return ['failed', 'unknown', 'stale', 'blocked'].includes(job.status) || job.retry_status === 'blocked' || job.retry_status === 'exhausted';
}

function recordStatus(job: Job) {
  if (job.retry_status === 'blocked' || job.retry_status === 'exhausted') return '已停止重试';
  if (job.status === 'deferred' && job.retry_at) return '等待重试';
  return jobStatusLabels[job.status] ?? job.status;
}

export function ActivityPage({ model }: { model: Workbench }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const jobs = model.jobs.filter(job => !/send|reconcile_operation/i.test(job.kind));
  const filtered = jobs.filter(job => filter === 'all' || (filter === 'attention' ? needsAttention(job) : activityCategory(job) === filter)).slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const job = jobs.find(item => item.job_id === selected);
  const refresh = () => void model.action('records-refresh', async () => { await model.refreshSnapshot(); model.setNotice('运行记录已刷新。'); });
  return <div className="page activity-042"><PageTitle eyebrow="" title="运行记录" description="查看连接、资料与业务任务的进度，遇到问题再展开详情。" action={<Button disabled={!model.connected} onClick={refresh} loading={model.busy('records-refresh')}><RefreshCw size={16} />刷新记录</Button>} /><MessageSyncSummary sync={model.snapshot?.message_sync} /><div className="record-tabs">{[['all', '全部'], ['attention', `需关注 ${jobs.filter(needsAttention).length}`], ['task', '业务任务'], ['procurement', '采购'], ['connection', '连接'], ['materials', '资料']].map(([value, label]) => <button type="button" className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div><div className="activity-042-list">{filtered.length === 0 ? <Empty title="尚无此类任务记录" /> : filtered.map(item => <article className="activity-042-event" key={item.job_id}><time>{dateTime(item.created_at)}</time><span className={`activity-042-marker ${needsAttention(item) ? 'attention' : ''}`} /><div><h2>{jobLabels[item.kind] ?? '后台任务'}</h2><p>{item.message || item.stage || recordStatus(item)}</p></div><button type="button" className="business-042-link" onClick={() => setSelected(item.job_id)}>查看详情 <ChevronRight size={14} /></button></article>)}</div>{job && <Drawer title="任务详情" onClose={() => setSelected(null)}><aside className="job-detail"><h3>{jobLabels[job.kind] ?? '后台任务'}</h3><p>{job.kind === 'generate_draft' && !['succeeded', 'cancelled', 'stale'].includes(job.status) && (job.retry_attempt || job.status === 'failed') ? '回复整理中或等待恢复' : job.message || '暂无补充说明'}</p><dl><div><dt>任务编号</dt><dd>{job.job_id}</dd></div><div><dt>处理阶段</dt><dd>{job.stage || '尚未开始'}</dd></div><div><dt>创建时间</dt><dd>{dateTime(job.created_at)}</dd></div><div><dt>开始时间</dt><dd>{dateTime(job.started_at)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(job.completed_at)}</dd></div><div><dt>会话归属</dt><dd>{model.snapshot?.conversations.find(item => item.conversation_id === job.conversation_id)?.customer_name || job.conversation_id || '全局检查'}</dd></div>{job.case_id && <div><dt>采购事项</dt><dd>{job.case_id}</dd></div>}</dl>{job.error && <Feedback tone="red">{job.kind === 'generate_draft' ? '回复暂未整理完成，请查看状态并等待恢复。' : <>{job.error.message}<small>错误码：{job.error.code}</small></>}</Feedback>}{(job.retry_attempt || job.repair_count || job.retry_at) && <p>{job.kind === 'automatic_send' || job.kind === 'continuity_send' ? `自动发送已尝试 ${job.retry_attempt ?? 0} 次（最多3次）` : `自动重试 ${job.retry_attempt ?? 0} 次`} · 格式整理 {job.repair_count ?? 0} 次{job.retry_at ? ` · 下次尝试 ${dateTime(job.retry_at)}` : ''}</p>}<details><summary>诊断信息</summary><ReadDiagnostics diagnostics={job.diagnostics} /><SnapshotDiagnostics diagnostics={job.diagnostics} /></details>{job.conversation_id && <Button onClick={() => void model.selectConversation(job.conversation_id!, job.case_id ?? '')}>查看所属会话 <ArrowRight size={14} /></Button>}</aside></Drawer>}</div>;
}
