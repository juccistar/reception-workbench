import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, ChevronRight, ClipboardCheck, ClipboardList, Home, Menu, MessageCircle, PanelLeftClose, PanelLeftOpen, PanelRightOpen, Pause, Play, RefreshCw, ScrollText, Settings2, Store, UserRound, UsersRound, X } from 'lucide-react';
import type { ReceptionBridge } from '../shared/api';
import { useWorkbench, type Page, type Workbench } from './hooks/useWorkbench';
import { CustomerList, Conversation, Requirements, SupplementPanel } from './components/Reception';
import { ActivityPage, CustomersPage, HandoffPage, HomePage, PoliciesPage, SettingsPage } from './components/Pages';
import { Badge, Button, Drawer, IconButton } from './components/ui';
import { DemoConnection } from './components/DemoConnection';
import { ProductsPage, SuppliersPage, ProcurementPage } from './components/ProductProcurementPages';
import { executorHealth } from './executorHealth';
import './styles.css';
import './reception-layout.css';

const navigation: { id: Page; title: string; icon: typeof Home }[] = [
  { id: 'home', title: '开始', icon: Home }, { id: 'reception', title: '客户接待', icon: MessageCircle }, { id: 'products', title: '产品库', icon: ClipboardList }, { id: 'suppliers', title: '供应商', icon: UsersRound }, { id: 'procurement', title: '采购中心', icon: Store }, { id: 'handoff', title: '待我报价', icon: ClipboardCheck }, { id: 'customers', title: '客户资料', icon: UsersRound }, { id: 'policies', title: '资料与政策', icon: BookOpen }, { id: 'settings', title: '连接与设置', icon: Settings2 }, { id: 'activity', title: '运行记录', icon: ScrollText },
];

function Navigation({ model, icons, toggle, afterSelect }: { model: Workbench; icons: boolean; toggle: () => void; afterSelect?: () => void }) {
  const quoteCount = model.snapshot?.handoff_cases?.length ?? model.snapshot?.conversations.filter(item => item.state === 'waiting_quote').length ?? 0;
  const select = async (page: Page) => { if (await model.navigate(page)) afterSelect?.(); };
  const app = model.snapshot?.app;
  const companyName = app?.mode === 'live' && !app.account_verified ? '' : app?.company_name?.trim() || (app?.mode === 'demo' ? '演示商家' : '');
  const companyStatus = !model.connected ? '等待服务连接' : app?.mode === 'live' && !app.account_verified ? '账号尚未核验' : app?.company_name?.trim() === '' ? '公司名称读取失败' : '正在读取公司名称';
  return <nav className={`navigation ${icons ? 'navigation-icons' : ''}`} aria-label="主导航"><div className="nav-caption"><span>工作空间</span><IconButton label={icons ? '展开导航' : '收起导航'} aria-expanded={!icons} onClick={toggle}>{icons ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</IconButton></div><div className="nav-main">{navigation.slice(0, 6).map(item => { const Icon = item.icon; return <button type="button" className={`nav-item ${model.page === item.id ? 'active' : ''}`} key={item.id} onClick={() => void select(item.id)} title={item.title} aria-label={item.title} aria-current={model.page === item.id ? 'page' : undefined}><Icon size={21} strokeWidth={1.65} /><span>{item.title}</span>{item.id === 'handoff' && quoteCount > 0 && <em>{quoteCount}</em>}</button>; })}</div><div className="nav-lower">{navigation.slice(6).map(item => { const Icon = item.icon; return <button type="button" className={`nav-item ${model.page === item.id ? 'active' : ''}`} key={item.id} onClick={() => void select(item.id)} title={item.title} aria-label={item.title} aria-current={model.page === item.id ? 'page' : undefined}><Icon size={20} strokeWidth={1.65} /><span>{item.title}</span></button>; })}<div className="merchant" title={companyName || companyStatus}><span className="merchant-icon"><Store size={19} strokeWidth={1.6} /></span><div><strong>{companyName || companyStatus}</strong></div>{!companyName && app?.mode === 'live' && app.company_name?.trim() === '' && <IconButton label="重试读取公司名称" disabled={!model.connected || !app.account_verified || model.busy('refresh_company_profile')} onClick={() => void model.runJob('refresh_company_profile')}><RefreshCw size={15} /></IconButton>}</div></div></nav>;
}

export function App({ bridge = window.reception }: { bridge?: ReceptionBridge }) {
  const model = useWorkbench(bridge);
  const health = executorHealth(model.snapshot);
  const [width, setWidth] = useState(window.innerWidth);
  const [drawer, setDrawer] = useState<'navigation' | 'customers' | 'requirements' | null>(null);
  const [productId, setProductId] = useState('');
  const openProduct = (id: string) => { setProductId(id); void model.navigate('products'); };
  const locateRef = useRef<(id: string) => void>(() => {});
  const resizeCleanup = useRef<(() => void) | null>(null);
  const detailsColumn = useRef<HTMLDivElement>(null);
  const [columnHeight, setColumnHeight] = useState(0);
  const [dragRatio, setDragRatio] = useState<number | null>(null);
  const wide = width >= 1360;
  const narrow = width < 1000;
  const icons = true;
  const expandedDetails = wide && model.preferences.details_preference === 'expanded';
  const [dragWidths, setDragWidths] = useState<{ list?: number; details?: number }>({});
  const navWidth = 76;
  const desiredDetails = Math.max(280, Math.min(600, dragWidths.details ?? model.preferences.details_width));
  const desiredList = Math.max(240, Math.min(360, dragWidths.list ?? model.preferences.list_width));
  // A smaller workspace temporarily constrains widths without changing the user's preference.
  const effectiveList = wide ? Math.max(240, Math.min(desiredList, width - navWidth - 480 - desiredDetails)) : 256;
  const effectiveDetails = wide ? Math.max(280, Math.min(desiredDetails, width - navWidth - effectiveList - 480)) : desiredDetails;
  const availableHeight = Math.max(0, columnHeight - 8);
  const minimumHeight = Math.min(140, availableHeight / 2);
  const preferredRatio = dragRatio ?? model.preferences.requirements_ratio ?? 0.68;
  const requirementsHeight = Math.max(minimumHeight, Math.min(availableHeight - minimumHeight, availableHeight * preferredRatio));
  const effectiveRatio = availableHeight ? requirementsHeight / availableHeight : 0.55;

  useEffect(() => {
    if (!expandedDetails || !detailsColumn.current) return;
    setColumnHeight(detailsColumn.current.clientHeight);
    const observer = new ResizeObserver(entries => setColumnHeight(entries[0].contentRect.height));
    observer.observe(detailsColumn.current);
    return () => observer.disconnect();
  }, [expandedDetails]);

  useEffect(() => {
    const resize = () => setWidth(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => { window.removeEventListener('resize', resize); resizeCleanup.current?.(); };
  }, []);
  useEffect(() => { if (wide) setDrawer(null); }, [wide]);
  const toggleNavigation = () => {
    if (width < 1500) setDrawer('navigation');
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
    const maximum = Math.min(kind === 'list' ? 360 : 600, width - navWidth - (kind === 'list' ? other : 240) - 480);
    const minimum = kind === 'list' ? 240 : 280;
    const move = (event: PointerEvent) => { latest = Math.max(minimum, Math.min(maximum, initial + (kind === 'list' ? event.clientX - startX : startX - event.clientX))); setDragWidths(previous => ({ ...previous, [kind]: latest })); };
    const done = () => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', done); document.body.classList.remove('resizing'); model.changePreferences(kind === 'list' ? { list_width: latest } : { details_width: latest }); setDragWidths({}); resizeCleanup.current = null; };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', done, { once: true }); document.body.classList.add('resizing');
    resizeCleanup.current = done;
  };
  const setRequirementsHeight = (height: number) => {
    if (!availableHeight) return;
    const next = Math.max(minimumHeight, Math.min(availableHeight - minimumHeight, height));
    model.changePreferences({ requirements_ratio: next / availableHeight });
  };
  const beginVerticalResize = (startY: number) => {
    if (!availableHeight) return;
    const initial = requirementsHeight;
    let latest = effectiveRatio;
    const move = (event: PointerEvent) => {
      latest = Math.max(minimumHeight, Math.min(availableHeight - minimumHeight, initial + event.clientY - startY)) / availableHeight;
      setDragRatio(latest);
    };
    const done = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', done);
      document.removeEventListener('pointercancel', done);
      document.body.classList.remove('resizing-vertical');
      if (latest !== effectiveRatio) model.changePreferences({ requirements_ratio: latest });
      setDragRatio(null);
      resizeCleanup.current = null;
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', done, { once: true });
    document.addEventListener('pointercancel', done, { once: true });
    document.body.classList.add('resizing-vertical');
    resizeCleanup.current = done;
  };

  return <><div inert={model.closing || undefined} aria-busy={model.closing} className={`app ${icons ? 'nav-is-icons' : ''} ${wide ? 'layout-wide' : narrow ? 'layout-narrow' : 'layout-medium'}`} style={{ '--nav-width': `${navWidth}px`, '--list-width': `${effectiveList}px`, '--details-width': `${effectiveDetails}px` } as React.CSSProperties}>
    <header className="app-header"><div className="brand"><span className="brand-mark"><MessageCircle size={23} strokeWidth={1.8} /><span /></span><div><strong>询盘接待助手</strong><small>客户接待工作台</small></div></div><div className="header-context"><span>工作台</span><ChevronRight size={13} /><strong>{navigation.find(item => item.id === model.page)?.title}</strong></div><div className="header-actions">
      {model.snapshot?.app.mode === 'demo' && <Badge tone="amber">演示工作区</Badge>}{model.snapshot?.app.mode === 'live' && !model.snapshot.app.account_verified && <Badge tone="amber">账号未核验</Badge>}
      <span className={`reception-status ${health.ready && model.snapshot?.reception.running && model.connected ? 'running' : ''}`}><span />{model.closing ? '正在退出 · 保存结果' : !model.connected ? '服务未连接' : !health.ready ? health.message : model.snapshot?.reception.running ? '自动接待中' : '自动接待已暂停'}</span>
      <IconButton label="刷新本地工作区" onClick={() => void refresh()}><RefreshCw size={17} className={model.loading ? 'spin' : ''} /></IconButton>
      <Button className="pause-button" disabled={!model.connected || model.closing} loading={model.busy('pause_reception')} onClick={() => void model.mutate('set_reception', { enabled: !model.snapshot?.reception.running, auto_send: !model.snapshot?.reception.running }, model.snapshot?.reception.running ? '自动接待已关闭。' : '自动接待已开启。')}>{model.snapshot?.reception.running ? <Pause size={15} /> : <Play size={15} />}{model.snapshot?.reception.running ? '关闭自动接待' : '开启自动接待'}</Button>
    </div></header>
    <div className="app-body"><aside className="navigation-container"><Navigation model={model} icons={icons} toggle={toggleNavigation} /></aside><div className="workspace">
      {(model.snapshot?.app.mode === 'demo' || !model.connected) && <DemoConnection bridge={bridge} disconnected={!model.connected} onInspectConnection={() => void model.navigate('settings')} />}
      {model.error && !model.closing && <div className="global-feedback global-error" role="alert"><span>{model.error}</span><div><button type="button" onClick={() => void refresh()}>重新读取</button><IconButton label="关闭错误提示" onClick={() => model.setError('')}><X size={15} /></IconButton></div></div>}
      {model.notice && <div className="global-feedback global-notice" role="status"><span>{model.notice}</span><IconButton label="关闭状态提示" onClick={() => model.setNotice('')}><X size={15} /></IconButton></div>}
      <div className="reception-layout" hidden={model.page !== 'reception'}><div className="customer-column"><CustomerList model={model} />{wide && <div className="resize-handle list-resize" role="separator" aria-label="调整客户列表宽度" aria-orientation="vertical" tabIndex={0} onPointerDown={event => { if (event.button === 0) beginResize('list', event.clientX); }} onDoubleClick={() => model.changePreferences({ list_width: 272 })} aria-valuemin={240} aria-valuemax={Math.max(240, Math.min(360, width - navWidth - effectiveDetails - 480))} aria-valuenow={effectiveList} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); model.changePreferences({ list_width: Math.max(240, Math.min(360, effectiveList + (event.key === 'ArrowRight' ? 8 : -8), width - navWidth - effectiveDetails - 480)) }); } }} />}</div><Conversation model={model} bridge={bridge} openList={() => setDrawer('customers')} openDetails={openDetails} registerLocate={registerLocate} />{wide && <div className="requirements-column" ref={detailsColumn} style={{ '--requirements-height': `${requirementsHeight}px` } as React.CSSProperties}><div className="resize-handle details-resize" role="separator" aria-label="调整右侧栏宽度" aria-orientation="vertical" aria-valuemin={280} aria-valuemax={Math.max(280, Math.min(600, width - navWidth - 240 - 480))} aria-valuenow={effectiveDetails} tabIndex={0} title="左右拖动调整右侧栏；双击恢复默认" onPointerDown={event => { if (event.button === 0) beginResize('details', event.clientX); }} onDoubleClick={() => model.changePreferences({ details_width: 360 })} onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); model.changePreferences({ details_width: Math.max(280, Math.min(600, width - navWidth - 240 - 480, effectiveDetails + (event.key === 'ArrowLeft' ? 16 : -16))) }); } }} />{expandedDetails ? <><Requirements bridge={bridge} model={model} collapse={() => model.changePreferences({ details_preference: 'rail' })} locate={locate} /><div className="split-handle" role="separator" aria-label="调整采购需求与业务补充高度" aria-orientation="horizontal" aria-valuemin={Math.round(minimumHeight)} aria-valuemax={Math.round(availableHeight - minimumHeight)} aria-valuenow={Math.round(requirementsHeight)} tabIndex={0} title="上下拖动调整高度；双击恢复默认" onPointerDown={event => { if (event.button === 0) beginVerticalResize(event.clientY); }} onDoubleClick={() => model.changePreferences({ requirements_ratio: 0.68 })} onKeyDown={event => { if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); setRequirementsHeight(requirementsHeight + (event.key === 'ArrowDown' ? 16 : -16)); } }} /></> : <button type="button" className="requirements-expand" onClick={openDetails}>展开采购需求</button>}<SupplementPanel model={model} /></div>}</div>
      {model.page === 'products' && <ProductsPage executorReady={health.ready} executorMessage={health.message} initialProductId={productId} caseSelection={model.detail?.case?.case_id ? { case_id: model.detail.case.case_id, revision: model.detail.case.revision, customer_name: model.detail.conversation.customer_name, product_name: typeof model.detail.case.requirements?.product?.value === 'string' ? model.detail.case.requirements.product.value : '', local_product_id: model.detail.case.local_product_id, offer_id: model.detail.case.selected_offer_id } : undefined} onCaseChanged={() => void refresh()} />}{model.page === 'suppliers' && <SuppliersPage onOpenProduct={openProduct} />}{model.page === 'procurement' && <ProcurementPage executorReady={health.ready} executorMessage={health.message} onOpenProduct={openProduct} />}
      {model.page === 'home' && <HomePage model={model} />}{model.page === 'handoff' && <HandoffPage model={model} onOpenProduct={openProduct} />}{model.page === 'customers' && <CustomersPage model={model} />}{model.page === 'policies' && <PoliciesPage model={model} />}{model.page === 'settings' && <SettingsPage model={model} bridge={bridge} />}{model.page === 'activity' && <ActivityPage model={model} />}
    </div></div>
    {model.aiUnavailable && !model.closing && <Drawer title="AI不可用" onClose={model.dismissAiUnavailable}><div className="form-card"><p>{model.aiFailureReason || '暂时无法生成回复建议。'}请检查接待模型配置和网络后重试。输入的正文已保留。</p><Button variant="primary" onClick={model.dismissAiUnavailable}>知道了</Button></div></Drawer>}
    {drawer === 'navigation' && <Drawer title="工作空间" side="left" onClose={() => setDrawer(null)} className="navigation-drawer"><Navigation model={model} icons={false} toggle={() => setDrawer(null)} afterSelect={() => setDrawer(null)} /></Drawer>}
    {drawer === 'customers' && <Drawer title="客户列表" side="left" onClose={() => setDrawer(null)} className="customers-drawer"><CustomerList model={model} close={() => setDrawer(null)} /></Drawer>}
    {drawer === 'requirements' && <Drawer title="采购需求" onClose={() => setDrawer(null)}><div className="requirements-drawer-content"><Requirements bridge={bridge} model={model} locate={locate} /><SupplementPanel model={model} /></div></Drawer>}
  </div>{model.closing && <div role="status" aria-live="polite" style={{ position: 'fixed', inset: 0, zIndex: 10000, display: 'grid', placeItems: 'center', background: 'rgba(247, 248, 250, 0.92)', padding: 32 }}><section className="form-card"><h2>正在停止后台并保存结果…</h2><p>已撤销新增发送权限，正在等待在途操作的回执。</p><p>编辑已锁定，请等待退出完成。</p></section></div>}</>;
}
export default App;
