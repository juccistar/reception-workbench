import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Plus, Save } from 'lucide-react';
import type { ApiMethod, Policy } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { Badge, Button, Empty, Feedback } from './ui';

type Category = 'product' | 'business' | 'reception' | 'style';
type Usage = 'human_only' | 'internal_guidance' | 'customer_reference';
type LocalPolicy = Policy & { category?: Category; enabled?: boolean; usage?: Usage; customer_wording?: string; internal_notes?: string; template_id?: string | null; template_version?: number | null };
type Builtin = { template_id: string; template_version: number; kind: 'policy' | 'product'; category: Category; title: string; content: string; usage: Usage; is_template: boolean; product_scope: string[]; adopted_policy_id: string | null; enabled: boolean; available_update: boolean };
type Form = { policy_id?: string; revision?: number; kind: 'policy' | 'product'; category: Category; title: string; content: string; product_scope: string; confirmed: boolean; enabled: boolean; usage: Usage; customer_wording: string; internal_notes: string; valid_until: string; priority: number };
const categories: Record<Category, string> = { product: '商品资料', business: '经营政策', reception: '接待规则', style: '聊天风格' };
const usages: Record<Usage, string> = { human_only: '仅人工查看', internal_guidance: 'AI 内部接待依据', customer_reference: '可用于客户回复' };
const blank = (): Form => ({ kind: 'policy', category: 'business', title: '', content: '', product_scope: '', confirmed: false, enabled: false, usage: 'human_only', customer_wording: '', internal_notes: '', valid_until: '', priority: 0 });
const fromPolicy = (p: LocalPolicy): Form => ({ policy_id: p.policy_id, revision: p.revision, kind: p.kind, category: p.category ?? (p.kind === 'product' ? 'product' : 'business'), title: p.title, content: p.content, product_scope: p.product_scope.join('、'), confirmed: p.confirmed, enabled: p.enabled ?? true, usage: p.usage ?? (p.public ? 'customer_reference' : 'human_only'), customer_wording: p.customer_wording ?? '', internal_notes: p.internal_notes ?? '', valid_until: p.valid_until ? new Date(p.valid_until).toISOString().slice(0, 10) : '', priority: p.priority ?? 0 });
const scopes = (value: string) => value.split(/[、,，\n]/).map(item => item.trim()).filter(Boolean);

export function PoliciesPage({ model }: { model: Workbench }) {
  const [tab, setTab] = useState<'mine' | 'builtin'>('mine');
  const [category, setCategory] = useState<Category | 'all'>('all');
  const [items, setItems] = useState<Builtin[]>([]);
  const [selected, setSelected] = useState<Builtin | null>(null);
  const [updating, setUpdating] = useState(false);
  const [form, setForm] = useState<Form>(blank);
  const formRef = useRef(form); formRef.current = form;
  const [dirty, setDirty] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const policies = (model.snapshot?.policies ?? []) as LocalPolicy[];
  const loadCatalog = async () => {
    try { const result = await model.invoke<{ items: Builtin[] }>('list_builtin_policies' as ApiMethod, {}); setItems(result.items); setCatalogError(''); }
    catch { setCatalogError('内置内容读取失败，请重试。'); }
  };
  useEffect(() => { if (model.connected) void loadCatalog(); }, [model.connected]);
  useEffect(() => { model.setFormDirty('policy', dirty); return () => model.setFormDirty('policy', false); }, [dirty, model.setFormDirty]);
  const update = <K extends keyof Form>(key: K, value: Form[K]) => { setForm(current => ({ ...current, [key]: value })); setDirty(true); setFeedback(''); };
  const canLeave = () => { if (!dirty) return true; setFeedback('当前表单有未保存修改。请先保存，或点击“放弃修改”。'); return false; };
  const choosePolicy = (policy?: LocalPolicy) => { if (!canLeave()) return; setSelected(null); setForm(policy ? fromPolicy(policy) : blank()); setTab('mine'); setFeedback(''); };
  const chooseBuiltin = (item: Builtin) => { if (!canLeave()) return; setSelected(item); setFeedback(''); };
  const editBuiltin = (item: Builtin) => {
    const existing = policies.find(p => p.policy_id === item.adopted_policy_id);
    if (item.adopted_policy_id) { if (existing) choosePolicy(existing); else setFeedback('已采用的资料尚未加载，请刷新后重试。'); return; }
    setForm({ ...blank(), kind: item.kind, category: item.category, title: item.title, content: item.is_template ? '' : item.content, product_scope: item.product_scope.join('、'), usage: item.usage });
    setDirty(true); setFeedback(item.is_template ? '请填写已核实的实际内容，不要保留待填写占位文字。' : '请审核正文和范围，采用后可继续编辑其他资料设置。');
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const sent = form;
    const source = policies.find(policy => policy.policy_id === sent.policy_id);
    const isTemplate = selected?.is_template || items.some(item => item.is_template && item.template_id === source?.template_id);
    if (isTemplate && /[\[［【][^\]］】]*(待填写|可选)[^\]］】]*[\]］】]/.test(sent.content)) { setFeedback('请删除待填写提示，并填写已核实的实际内容。'); return; }
    const adopting = !!selected && (!sent.policy_id || updating);
    const result = adopting
      ? await model.action('adopt_builtin_policy', () => model.invoke<LocalPolicy>('adopt_builtin_policy' as ApiMethod, { template_id: selected!.template_id, content: sent.content.trim(), product_scope: scopes(sent.product_scope), enabled: sent.enabled, confirmed: sent.confirmed, ...(updating ? { expected_revision: sent.revision } : {}), update: updating }))
      : await model.action('save_policy', () => model.invoke<LocalPolicy>('save_policy', { ...(sent.policy_id ? { policy_id: sent.policy_id, expected_revision: sent.revision } : {}), kind: sent.kind, title: sent.title.trim(), content: sent.content.trim(), product_scope: scopes(sent.product_scope), confirmed: sent.confirmed, public: sent.usage === 'customer_reference', category: sent.category, enabled: sent.enabled, usage: sent.usage, customer_wording: sent.customer_wording, internal_notes: sent.internal_notes, priority: sent.priority, valid_until: sent.valid_until ? new Date(`${sent.valid_until}T23:59:59`).getTime() : null }));
    if (!result) return;
    const unchanged = formRef.current === sent;
    setForm(current => unchanged ? fromPolicy(result) : { ...current, policy_id: result.policy_id, revision: result.revision });
    setSelected(null); setUpdating(false); setTab('mine'); setDirty(!unchanged);
    setFeedback(unchanged ? '已保存。引用旧版本的回复需要重新核对。' : '前一版已保存，您刚输入的修改仍待保存。');
    await model.refreshSnapshot(); void loadCatalog();
  };
  const updateBuiltin = (item: Builtin, policy: LocalPolicy) => {
    if (!canLeave()) return;
    setForm({ ...fromPolicy(policy), content: item.is_template ? policy.content : item.content });
    setUpdating(true); setDirty(true);
    setFeedback('请核对并编辑新版正文，保存后才会更新；启用状态和其他设置保持原样。');
  };
  const local = selected?.adopted_policy_id ? policies.find(p => p.policy_id === selected.adopted_policy_id) : undefined;
  const editingBuiltin = !!selected && dirty && (!form.policy_id || updating);
  return <div className="page policy-library"><header className="page-title"><div><span className="eyebrow">让每一项承诺都有依据</span><h1>资料与政策</h1><p>管理本商家的资料、接待规则和聊天风格。</p></div><Button onClick={() => choosePolicy()}><Plus size={16} />新建资料</Button></header>
    <nav className="record-tabs" aria-label="资料目录"><button type="button" className={tab === 'mine' ? 'active' : ''} onClick={() => { if (canLeave()) { setTab('mine'); setSelected(null); } }}>我的资料</button><button type="button" className={tab === 'builtin' ? 'active' : ''} onClick={() => { if (canLeave()) { setTab('builtin'); setSelected(null); } }}>内置内容</button></nav>
    <label className="policy-filter">类别<select value={category} onChange={event => setCategory(event.target.value as Category | 'all')}><option value="all">全部类别</option>{Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
    <div className="policies-layout"><aside className="policy-index"><h2>{tab === 'mine' ? '我的资料' : '内置内容'}</h2>{tab === 'mine' ? policies.filter(p => category === 'all' || (p.category ?? (p.kind === 'product' ? 'product' : 'business')) === category).map(p => <button type="button" key={p.policy_id} className={form.policy_id === p.policy_id ? 'active' : ''} onClick={() => choosePolicy(p)}><span><strong>{p.title}</strong><small>v{p.revision} · {(p.enabled ?? true) ? '已启用' : '已关闭'} · {usages[p.usage ?? (p.public ? 'customer_reference' : 'human_only')]}</small></span></button>) : items.filter(item => category === 'all' || item.category === category).map(item => <button type="button" key={item.template_id} className={selected?.template_id === item.template_id ? 'active' : ''} onClick={() => chooseBuiltin(item)}><span><strong>{item.title}</strong><small>{categories[item.category]} · {item.adopted_policy_id ? (item.enabled ? '已采用 · 已启用' : '已采用 · 已关闭') : item.is_template ? '关闭 · 待填写' : '关闭'}{item.available_update ? ' · 有新版' : ''}</small></span></button>)}{tab === 'builtin' && catalogError && <div><Feedback tone="red">{catalogError}</Feedback><Button onClick={() => void loadCatalog()}>重试</Button></div>}{tab === 'builtin' && !items.length && !catalogError && <Empty title="正在读取内置内容…" />}</aside>
      {tab === 'builtin' && !editingBuiltin ? <section className="form-card policy-preview">{selected ? <><div className="form-card-heading"><h2>{selected.title}</h2><Badge>v{selected.template_version}</Badge></div><p>{categories[selected.category]} · {usages[selected.usage]} · {selected.product_scope.length ? selected.product_scope.join('、') : '通用范围'}</p><p>内置内容默认关闭；采用后保存为本商家的本地资料。</p><h3>内置全文</h3><pre>{selected.content}</pre>{selected.is_template && <p>模板仅提供填写提示，请填写并核实实际内容。</p>}{selected.available_update && local && <><h3>当前本地正文</h3><pre>{local.content}</pre><p>上方为新版全文，下方为您当前保存的正文。更新会替换正文，请先核对差异。</p><Button disabled={!model.connected} onClick={() => updateBuiltin(selected, local)}>编辑新版并更新</Button></>}<div className="form-actions"><Button variant="primary" onClick={() => editBuiltin(selected)}>{selected.adopted_policy_id ? '打开我的资料' : '编辑并采用'}</Button></div></> : <Empty title="选择内置内容查看全文">六份规则和三份模板均默认关闭。</Empty>}{feedback && <Feedback tone="amber">{feedback}</Feedback>}</section> : <form className="form-card policy-form" onSubmit={event => void submit(event)}><div className="form-card-heading"><h2>{editingBuiltin ? '编辑并采用内置内容' : form.policy_id ? '编辑资料' : '新建资料'}</h2>{form.revision && <Badge>当前版本 v{form.revision}</Badge>}</div><div className="form-row"><label>类别<select value={form.category} disabled={editingBuiltin} onChange={event => { const value = event.target.value as Category; update('category', value); setForm(current => ({ ...current, kind: value === 'product' ? 'product' : 'policy' })); }}><option value="product">商品资料</option><option value="business">经营政策</option><option value="reception">接待规则</option><option value="style">聊天风格</option></select></label><label>有效日期（可选）<input type="date" disabled={editingBuiltin} value={form.valid_until} onChange={event => update('valid_until', event.target.value)} /></label></div><label>资料名称<input required maxLength={200} disabled={editingBuiltin} value={form.title} onChange={event => update('title', event.target.value)} /></label><label>资料内容<textarea required rows={9} value={form.content} onChange={event => update('content', event.target.value)} placeholder="填写已核实的实际内容" /></label><label>适用产品<input value={form.product_scope} onChange={event => update('product_scope', event.target.value)} placeholder="多个产品以顿号分隔；留空表示通用" /></label><label>用途<select disabled={editingBuiltin} value={form.usage} onChange={event => update('usage', event.target.value as Usage)}>{Object.entries(usages).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>{form.category === 'style' && <label>风格优先级<input type="number" min={0} max={100} step={1} required disabled={editingBuiltin} value={form.priority} onChange={event => update('priority', event.target.valueAsNumber)} /><small>适用的已启用风格只选一份：优先级高者优先；相同优先级时，产品专用风格优先，仍相同时按资料编号选择。</small></label>}<label>公开口径（可选）<textarea disabled={editingBuiltin} value={form.customer_wording} onChange={event => update('customer_wording', event.target.value)} /></label><label>仅人工备注（不进入 AI）<textarea disabled={editingBuiltin} value={form.internal_notes} onChange={event => update('internal_notes', event.target.value)} /></label><label className="checkbox-label"><input type="checkbox" checked={form.confirmed} onChange={event => update('confirmed', event.target.checked)} /><span><strong>{form.category === 'product' || form.category === 'business' ? '已由商家核实' : '已审核采用'}</strong></span></label><label className="checkbox-label"><input type="checkbox" checked={form.enabled} onChange={event => update('enabled', event.target.checked)} /><span><strong>启用资料</strong><small>关闭后保留正文；仅符合用途、确认和有效期的资料可用于生成。</small></span></label>{feedback && <Feedback tone={dirty ? 'amber' : 'green'}>{feedback}</Feedback>}<footer className="form-actions"><Button variant="primary" type="submit" loading={model.busy(editingBuiltin ? 'adopt_builtin_policy' : 'save_policy')} disabled={!model.connected || !dirty}><Save size={16} />{editingBuiltin ? (updating ? '确认更新资料' : '采用资料') : '保存资料'}</Button>{dirty && <Button disabled={model.busy('save_policy') || model.busy('adopt_builtin_policy')} onClick={() => { const original = policies.find(p => p.policy_id === form.policy_id); setForm(original ? fromPolicy(original) : blank()); setDirty(false); setUpdating(false); setFeedback('已放弃本次表单修改。'); }}>放弃修改</Button>}<span>保存后形成新版本</span></footer></form>}
    </div></div>;
}
