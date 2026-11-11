import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, ChevronRight, ClipboardCheck, ClipboardList, Home, Menu, MessageCircle, PanelLeftClose, PanelLeftOpen, PanelRightOpen, Pause, Play, RefreshCw, ScrollText, Settings2, Store, UserRound, UsersRound, X } from 'lucide-react';
import type { ReceptionBridge } from '../shared/api';
import { useWorkbench, type Page, type Workbench } from './hooks/useWorkbench';
import { CustomerList, Conversation, Requirements, SupplementPanel } from './components/Reception';
import { ActivityPage, CustomersPage, HandoffPage, HomePage, PoliciesPage, SettingsPage } from './components/Pages';
import { Badge, Button, Drawer, IconButton } from './components/ui';
import { DemoConnection } from './components/DemoConnection';
import './styles.css';

const navigation: { id: Page; title: string; icon: typeof Home }[] = [
  { id: 'home', title: '开始', icon: Home }, { id: 'reception', title: '客户接待', icon: MessageCircle }, { id: 'handoff', title: '待我报价', icon: ClipboardCheck }, { id: 'customers', title: '客户资料', icon: UsersRound }, { id: 'policies', title: '资料与政策', icon: BookOpen }, { id: 'settings', title: '连接与设置', icon: Settings2 }, { id: 'activity', title: '运行记录', icon: ScrollText },
];

function Navigation({ model, icons, toggle, afterSelect }: { model: Workbench; icons: boolean; toggle: () => void; afterSelect?: () => void }) {
  const quoteCount = model.snapshot?.handoff_cases?.length ?? model.snapshot?.conversations.filter(item => item.state === 'waiting_quote').length ?? 0;
  const select = async (page: Page) => { if (await model.navigate(page)) afterSelect?.(); };
  return <nav className={`navigation ${icons ? 'navigation-icons' : ''}`} aria-label="主导航"><div className="nav-caption"><span>工作空间</span><IconButton label={icons ? '展开导航' : '收起导航'} aria-expanded={!icons} onClick={toggle}>{icons ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</IconButton></div><div className="nav-main">{navigation.slice(0, 5).map(item => { const Icon = item.icon; return <button type="button" className={`nav-item ${model.page === item.id ? 'active' : ''}`} key={item.id} onClick={() => void select(item.id)} title={item.title} aria-label={item.title} aria-current={model.page === item.id ? 'page' : undefined}><Icon size={21} strokeWidth={1.65} /><span>{item.title}</span>{item.id === 'handoff' && quoteCount > 0 && <em>{quoteCount}</em>}</button>; })}</div><div className="nav-lower">{navigation.slice(5).map(item => { const Icon = item.icon; return <button type="button" className={`nav-item ${model.page === item.id ? 'active' : ''}`} key={item.id} onClick={() => void select(item.id)} title={item.title} aria-label={item.title} aria-current={model.page === item.id ? 'page' : undefined}><Icon size={20} strokeWidth={1.65} /><span>{item.title}</span></button>; })}<div className="merchant"><span className="merchant-icon"><Store size={19} strokeWidth={1.6} /></span><div><strong>{model.snapshot?.app.mode === 'demo' ? '演示商家' : model.snapshot?.app.account_verified ? `商家 · 尾号 ${model.snapshot.app.seller_suffix}` : '账号尚未核验'}</strong><small>{model.snapshot?.app.mode === 'demo' ? '独立合成工作区' : model.connected ? '当前账号工作区' : '等待服务连接'}</small></div></div></div></nav>;
}

export function App({ bridge = window.reception }: { bridge?: ReceptionBridge }) {
  const model = useWorkbench(bridge);
  const [width, setWidth] = useState(window.innerWidth);
  const [drawer, setDrawer] = useState<'navigation' | 'customers' | 'requirements' | null>(null);
  const locateRef = useRef<(id: string) => void>(() => {});
  const resizeCleanup = useRef<(() => void) | null>(null);
  const wide = width >= 1360;
  const narrow = width < 1000;
  const icons = !wide || model.preferences.nav_preference === 'icons';
  const expandedDetails = wide && model.preferences.details_preference === 'expanded';
  const [dragWidths, setDragWidths] = useState<{ list?: number; details?: number }>({});
  const navWidth = icons ? 72 : 224;
  const desiredDetails = Math.max(280, Math.min(380, dragWidths.details ?? model.preferences.details_width));
  const desiredList = Math.max(240, Math.min(360, dragWidths.list ?? model.preferences.list_width));
  // A smaller workspace temporarily constrains widths without changing the user's preference.
  const effectiveList = wide ? Math.max(240, Math.min(desiredList, width - navWidth - 480 - desiredDetails)) : 256;
  const effectiveDetails = wide ? Math.max(280, Math.min(desiredDetails, width - navWidth - effectiveList - 480)) : desiredDetails;

  useEffect(() => {
    const resize = () => setWidth(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); resizeCleanup.current?.(); };
  }, []);
  useEffect(() => { if (wide) setDrawer(null); }, [wide]);
  const toggleNavigation = () => {
    if (!wide) setDrawer('navigation');
    else model.changePreferences({ nav_preference: icons ? 'expanded' : 'icons' });
  };
  const openDetails = () => {
    if (wide) model.changePreferences({ details_preference: 'expanded' });
    else setDrawer('requirements');
  };
  const registerLocate = useCallback((locate: (id: string) => void) => { locateRef.current = locate; }, []);
  const locate = (id: string) => { locateRef.current(id); setDrawer(null); };
  const refresh = async () => {
    await model.refreshSnapshot();
    if (model.selection.conversationId) await model.loadDetail(model.selection.conversationId, model.selection.caseId, true);
  };
  const beginResize = (kind: 'list' | 'details', startX: number) => {
    if (!wide) return;
    const initial = kind === 'list' ? effectiveList : effectiveDetails;
    let latest = initial;
    const other = kind === 'list' ? effectiveDetails : effectiveList;
    const maximum = Math.min(kind === 'list' ? 360 : 380, width - navWidth - other - 480);
    const minimum = kind === 'list' ? 240 : 280;
    const move = (event: PointerEvent) => { latest = Math.max(minimum, Math.min(maximum, initial + (kind === 'list' ? event.clientX - startX : startX - event.clientX))); setDragWidths(previous => ({ ...previous, [kind]: latest })); };
    const done = () => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', done); document.body.classList.remove('resizing'); model.changePreferences(kind === 'list' ? { list_width: latest } : { details_width: latest }); setDragWidths({}); resizeCleanup.current = null; };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', done, { once: true }); document.body.classList.add('resizing');
    resizeCleanup.current = done;
  };

  return <><div inert={model.closing || undefined} aria-busy={model.closing} className={`app ${icons ? 'nav-is-icons' : ''} ${wide ? 'layout-wide' : narrow ? 'layout-narrow' : 'layout-medium'}`} style={{ '--nav-width': `${navWidth}px`, '--list-width': `${effectiveList}px`, '--details-width': `${effectiveDetails}px` } as React.CSSProperties}>
    <header className="app-header"><div className="brand"><span className="brand-mark"><MessageCircle size={23} strokeWidth={1.8} /><span /></span><div><strong>询盘接待助手</strong><small>客户接待工作台</small></div></div><div className="header-context"><span>工作台</span><ChevronRight size={13} /><strong>{navigation.find(item => item.id === model.page)?.title}</strong></div><div className="header-actions">
      {model.snapshot?.app.mode === 'demo' && <Badge tone="amber">演示工作区</Badge>}{model.snapshot?.app.mode === 'live' && !model.snapshot.app.account_verified && <Badge tone="amber">账号未核验</Badge>}
      <span className={`reception-status ${model.snapshot?.reception.running && model.connected ? 'running' : ''}`}><span />{model.closing ? '正在退出 · 保存结果' : !model.connected ? '服务未连接' : model.snapshot?.reception.running ? '自动接待中' : '自动接待已暂停'}</span>
      <IconButton label="刷新本地工作区" onClick={() => void refresh()}><RefreshCw size={17} className={model.loading ? 'spin' : ''} /></IconButton>
      <Button className="pause-button" disabled={!model.connected || model.closing} loading={model.busy('pause_reception')} onClick={() => model.snapshot?.reception.running ? void model.mutate('set_reception', { enabled: false, auto_send: false }, '自动接待已暂停，您仍可直接与客户聊天。') : void model.navigate('settings')}>{model.snapshot?.reception.running ? <Pause size={15} /> : <Play size={15} />}{model.snapshot?.reception.running ? '暂停自动接待' : '设置自动接待'}</Button>
    </div></header>
    <div className="app-body"><aside className="navigation-container"><Navigation model={model} icons={icons} toggle={toggleNavigation} /></aside><div className="workspace">
      {(model.snapshot?.app.mode === 'demo' || !model.connected) && <DemoConnection bridge={bridge} disconnected={!model.connected} onInspectConnection={() => void model.navigate('settings')} />}
      {model.error && !model.closing && <div className="global-feedback global-error" role="alert"><span>{model.error}</span><div><button type="button" onClick={() => void refresh()}>重新读取</button><IconButton label="关闭错误提示" onClick={() => model.setError('')}><X size={15} /></IconButton></div></div>}
      {model.notice && <div className="global-feedback global-notice" role="status"><span>{model.notice}</span><IconButton label="关闭状态提示" onClick={() => model.setNotice('')}><X size={15} /></IconButton></div>}
      <div className="reception-layout" hidden={model.page !== 'reception'}><div className="customer-column"><CustomerList model={model} />{wide && <div className="resize-handle list-resize" role="separator" aria-label="调整客户列表宽度" aria-orientation="vertical" tabIndex={0} onPointerDown={event => beginResize('list', event.clientX)} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); model.changePreferences({ list_width: Math.max(240, Math.min(360, effectiveList + (event.key === 'ArrowRight' ? 8 : -8), width - navWidth - effectiveDetails - 480)) }); } }} />}</div><Conversation model={model} bridge={bridge} openList={() => setDrawer('customers')} openDetails={openDetails} registerLocate={registerLocate} />{wide && <div className="requirements-column">{expandedDetails ? <Requirements bridge={bridge} model={model} collapse={() => model.changePreferences({ details_preference: 'rail' })} locate={locate} /> : <button type="button" className="requirements-expand" onClick={openDetails}>展开采购需求</button>}<SupplementPanel model={model} /></div>}</div>
      {model.page === 'home' && <HomePage model={model} />}{model.page === 'handoff' && <HandoffPage model={model} />}{model.page === 'customers' && <CustomersPage model={model} />}{model.page === 'policies' && <PoliciesPage model={model} />}{model.page === 'settings' && <SettingsPage model={model} bridge={bridge} />}{model.page === 'activity' && <ActivityPage model={model} />}
    </div></div>
    {drawer === 'navigation' && <Drawer title="工作空间" side="left" onClose={() => setDrawer(null)} className="navigation-drawer"><Navigation model={model} icons={false} toggle={() => setDrawer(null)} afterSelect={() => setDrawer(null)} /></Drawer>}
    {drawer === 'customers' && <Drawer title="客户列表" side="left" onClose={() => setDrawer(null)} className="customers-drawer"><CustomerList model={model} close={() => setDrawer(null)} /></Drawer>}
    {drawer === 'requirements' && <Drawer title="采购需求" onClose={() => setDrawer(null)}><div className="requirements-drawer-content"><Requirements bridge={bridge} model={model} locate={locate} /><SupplementPanel model={model} /></div></Drawer>}
  </div>{model.closing && <div role="status" aria-live="polite" style={{ position: 'fixed', inset: 0, zIndex: 10000, display: 'grid', placeItems: 'center', background: 'rgba(247, 248, 250, 0.92)', padding: 32 }}><section className="form-card"><h2>正在停止后台并保存结果…</h2><p>已撤销新增发送权限，正在等待在途操作的回执。</p><p>编辑已锁定，请等待退出完成。</p></section></div>}</>;
}
export default App;
