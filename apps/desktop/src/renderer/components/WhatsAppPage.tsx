import { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, Link2, MessageCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { ReceptionBridge } from '../../shared/api';
import type { WhatsAppContact, WhatsAppContext, WhatsAppHostInfo, WhatsAppState } from '../../shared/whatsapp';
import type { Workbench } from '../hooks/useWorkbench';
import { executorHealth } from '../executorHealth';
import { Badge, Button, Drawer, Empty, messageDateTime } from './ui';
import { ReplyBatchRecords } from './ReplyBatchRecords';

const hostLabels: Record<string, string> = { disconnected: '未连接', connecting: '正在连接', qr: '请在原始页面扫码', authenticated: '正在加载聊天', ready: '已连接', error: '连接失败，可重新连接' };
const hostErrors: Record<string, string> = { browser_unsupported: 'WhatsApp 未识别为受支持的浏览器，请检查更新', http_error: '登录页请求失败', network_error: '网络连接失败，请重新连接', tls_error: '安全连接失败，请检查网络', navigation_timeout: '连接超时，请重新连接', sdk_timeout: '初始化超时，请重新连接', ready_timeout: '加载聊天超时，请重新连接', auth_failed: '认证失败，请重新连接', sdk_error: '初始化失败，请重新连接', invalid_response: '登录页响应无效，请重新连接', page_closed: '页面已关闭，请重新连接', unexpected_navigation: '页面导航异常，请重新连接' };
const phaseLabels: Record<string, string> = { view_setup: '创建页面', cdp_connect: '连接页面', auth_setup: '准备认证', version_cache: '准备版本缓存', page_navigation: '加载登录页', app_modules: '加载网页模块', sdk_inject: '注入接待组件', sdk_callbacks: '注册接待回调', qr_wait: '等待扫码', authenticated: '加载聊天', ready: '接待就绪' };
Object.assign(phaseLabels, { page_reload: '网页重新加载' });
Object.assign(hostErrors, { page_database_error: 'WhatsApp 网页报告数据库错误，需重建登录环境', cdp_error: '页面连接失败，请重新连接', sdk_resource_missing: '接待组件资源缺失，请检查安装', page_context_lost: '页面环境已变化，请重新连接', app_module_error: '网页模块加载失败，请重新连接' });
function hostReason(host: WhatsAppHostInfo): string {
  if (host.error_code === 'http_error' && host.http_status === 429) return '请求过于频繁（HTTP 429），请等待后重连';
  if (host.error_code === 'http_error' && Number.isInteger(host.http_status) && host.http_status! >= 100 && host.http_status! <= 599) return `登录页请求失败（HTTP ${host.http_status}）`;
  const code = host.error_code ?? '';
  return Object.hasOwn(hostErrors, code) ? hostErrors[code] : hostLabels.error;
}
function hostLabel(host: WhatsAppHostInfo): string {
  if (host.status !== 'error') {
    if (host.status === 'connecting') return host.phase === 'page_navigation' ? '正在加载登录页' : '正在初始化接待';
    return hostLabels[host.status] ?? '正在读取连接状态';
  }
  if (host.page_state === 'available') return '原始页面可用，自动接待未就绪';
  if (host.page_state === 'loading') return '网页仍在加载，自动接待初始化失败';
  return hostReason(host);
}
const requirementLabels: Record<string, string> = { product: '产品', product_id: '产品 ID', quantity: '数量', specifications: '规格', customization: '定制', destination: '目的地', delivery: '交期', budget: '预算', packaging: '包装', certifications: '认证', contact: '联系方式' };
const emptyHost: WhatsAppHostInfo = { status: 'disconnected', account_id: '', connection_epoch: '' };

export function WhatsAppPage({ model, bridge, obscured = false }: { model: Workbench; bridge?: ReceptionBridge; obscured?: boolean }) {
  const [state, setState] = useState<WhatsAppState | null>(null);
  const [host, setHost] = useState<WhatsAppHostInfo>(emptyHost);
  const [contacts, setContacts] = useState<WhatsAppContact[]>([]);
  const [selected, setSelected] = useState('');
  const [panel, setPanel] = useState<'binding' | 'context' | null>(null);
  const [bindingTarget, setBindingTarget] = useState('');
  const [bindingVersion, setBindingVersion] = useState(0);
  const [context, setContext] = useState<WhatsAppContext | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [recoveryStage, setRecoveryStage] = useState('');
  const recoveryStageRef = useRef('');
  const surface = useRef<HTMLDivElement>(null);
  const live = useRef(true);
  const readSequence = useRef(0);
  const hostSequence = useRef(0);
  const contextSequence = useRef(0);
  const connected = !!state?.connected;
  const identity = { expected_account_id: state?.account_id, expected_connection_epoch: state?.connection_epoch };
  useEffect(() => {
    ++contextSequence.current;
    setContacts([]); setSelected(''); setPanel(null); setContext(null); setError(''); setNotice('');
  }, [state?.account_id, state?.connection_epoch, connected]);
  const liveMerchant = model.snapshot?.app.mode === 'live' && model.snapshot.app.account_verified;
  const current = state?.conversations.find(item => item.chat_id === selected);
  const binding = state?.bindings.find(item => item.chat_id === selected);
  const backgroundName = model.snapshot?.conversations.find(item => item.conversation_id === binding?.conversation_id)?.customer_name;
  const allContacts = [...new Map([...(state?.conversations ?? []), ...contacts].map(item => [item.chat_id, item])).values()];

  const refreshBusiness = useCallback(async () => {
    if (!bridge) return;
    const sequence = ++readSequence.current;
    const business = await bridge.invoke<WhatsAppState>('get_whatsapp_state');
    if (live.current && sequence === readSequence.current) setState(business);
  }, [bridge]);
  const refreshHost = useCallback(async () => {
    if (!bridge) return;
    const sequence = ++hostSequence.current;
    const native = await (bridge.getWhatsAppHostInfo?.() ?? Promise.resolve(emptyHost));
    if (live.current && sequence === hostSequence.current) setHost(native);
  }, [bridge]);
  const refresh = useCallback(async () => {
    await Promise.all([refreshBusiness(), refreshHost()]);
  }, [refreshBusiness, refreshHost]);
  useEffect(() => {
    live.current = true;
    void refresh().catch(() => { if (live.current) setError('操作未完成，请重试或查看运行记录。'); });
    const unsubscribe = bridge?.onEvent(event => {
      const data = event.data as Partial<WhatsAppHostInfo> | null;
      if (event.event === 'whatsapp.host.changed' && data && typeof data === 'object' && typeof data.status === 'string' && typeof data.account_id === 'string' && typeof data.connection_epoch === 'string') {
        ++hostSequence.current;
        setHost(data as WhatsAppHostInfo);
        void refreshBusiness().catch(() => {});
      } else if (event.event === 'whatsapp.recovery.changed') {
        const stage = (event.data as { stage?: string } | null)?.stage;
        recoveryStageRef.current = stage ?? '';
        setRecoveryStage(stage === 'waiting' ? '正在等待在途发送和旧连接收尾…' : stage === 'committing' ? '正在保存新登录环境…' : stage === 'connecting' ? '正在连接新登录环境…' : '');
      } else if (event.event === 'state.changed' || event.event.startsWith('whatsapp.')) {
        void refresh().catch(() => {});
      }
    });
    return () => { live.current = false; ++readSequence.current; ++hostSequence.current; ++contextSequence.current; unsubscribe?.(); };
  }, [bridge, refresh, refreshBusiness]);

  useEffect(() => {
    if (!bridge?.setWhatsAppView) return;
    let stopped = false;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (stopped) return;
        const rect = surface.current?.getBoundingClientRect();
        const visible = !panel && !obscured && !model.closing && !!rect && rect.width >= 120 && rect.height >= 80;
        void bridge.setWhatsAppView!({ visible, ...(visible && rect ? { bounds: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.floor(rect.width), height: Math.floor(rect.height) } } : {}) }).catch(() => {});
      });
    };
    const observer = new ResizeObserver(update);
    if (surface.current) observer.observe(surface.current);
    window.addEventListener('resize', update);
    update();
    return () => { stopped = true; cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener('resize', update); void bridge.setWhatsAppView!({ visible: false }).catch(() => {}); };
  }, [bridge, host.status, host.page_state, host.phase, host.error_code, host.http_status, host.diagnostic_id, detailsOpen, panel, obscured, model.closing, error, notice, state?.pending_operations.length, state?.reply_batches?.length, connected]);

  const act = async (key: string, action: () => Promise<unknown>, success = '') => {
    if (pending || model.closing) return;
    setPending(key); setError(''); setNotice('');
    if (key === 'rebuild') recoveryStageRef.current = '';
    try { await action(); await refresh(); if (live.current && success) setNotice(success); }
    catch (reason) { if (live.current) setError(key === 'rebuild' ? ({ waiting: '旧连接或在途发送收尾未完成，原登录环境保留。', committing: '保存新登录环境未完成，请检查配置并保留原目录。', connecting: '新登录环境连接未完成，请检查连接状态。' } as Record<string, string>)[recoveryStageRef.current] || '重建登录环境未完成，请检查商家身份和配置。' : '操作未完成，请重试或查看运行记录。'); }
    finally { if (live.current) { setPending(''); if (key === 'rebuild') setRecoveryStage(''); } }
  };
  const loadContacts = async () => {
    if (!bridge?.listWhatsAppChats || !connected) return;
    await act('contacts', async () => { const items = await bridge.listWhatsAppChats!(); if (live.current) setContacts(items); });
  };
  const chooseChat = (id: string) => { ++contextSequence.current; setSelected(id); setPanel(null); setContext(null); setError(''); setNotice(''); };
  const showBinding = () => { setBindingTarget(binding?.conversation_id ?? ''); setBindingVersion(binding?.revision ?? 0); setPanel('binding'); };
  const showContext = async () => {
    if (!bridge || !selected) return;
    setPanel('context'); setContext(null); setContextLoading(true); setError('');
    const sequence = ++contextSequence.current;
    try { const value = await bridge.invoke<WhatsAppContext>('get_whatsapp_context', { chat_id: selected }); if (live.current && sequence === contextSequence.current) setContext(value); }
    catch (reason) { if (live.current && sequence === contextSequence.current) setError('操作未完成，请重试或查看运行记录。'); }
    finally { if (live.current && sequence === contextSequence.current) setContextLoading(false); }
  };
  const saveBinding = () => act('binding', async () => {
    await bridge!.invoke('save_whatsapp_binding', { ...identity, chat_id: selected, conversation_id: bindingTarget || null, expected_revision: bindingVersion });
    if (live.current) setPanel(null);
  }, '国际站背景已更新，后续回复将使用新背景。');
  const unresolved = state?.pending_operations.filter(item => ['prepared', 'sending', 'submitted', 'unknown'].includes(item.status)) ?? [];
  const aiReady = model.snapshot?.checks.ai.status === 'passed';
  const health = executorHealth(model.snapshot);
  const disabled = !!pending || model.closing || !bridge;
  const receptionReady = connected && host.status === 'ready';
  const phase = typeof host.phase === 'string' && Object.hasOwn(phaseLabels, host.phase) ? phaseLabels[host.phase] : '';
  const diagnosticId = typeof host.diagnostic_id === 'string' && /^wa_diag_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(host.diagnostic_id) ? host.diagnostic_id : '';

  return <section className="whatsapp-page" aria-label="WhatsApp 工作区">
    <header className="whatsapp-toolbar">
      <div className="whatsapp-heading"><MessageCircle size={23} /><div><h1>WhatsApp</h1><span>原始聊天界面 · 全部单聊接待</span></div><Badge tone={host.status === 'ready' && connected ? 'green' : host.status === 'error' ? 'red' : 'neutral'}>{hostLabel(host)}</Badge>{connected && host.account_id && <span className="whatsapp-account">账号：{host.account_id.replace(/@(c\.us|lid)$/, '')}</span>}</div>
      <div className="whatsapp-actions">
        {host.status === 'disconnected' || host.status === 'error' ? <Button disabled={disabled || !liveMerchant || !bridge?.connectWhatsApp} loading={pending === 'connect'} onClick={() => void act('connect', () => bridge!.connectWhatsApp!())}>连接 WhatsApp</Button> : <Button disabled={disabled || !bridge?.disconnectWhatsApp} onClick={() => void act('disconnect', () => bridge!.disconnectWhatsApp!(), 'WhatsApp 已断开，自动接待授权已撤销。')}>断开连接</Button>}
        {host.status === 'error' && (connected || host.page_state === 'available' || host.page_state === 'loading') && <Button disabled={disabled || !bridge?.disconnectWhatsApp} onClick={() => void act('disconnect', () => bridge!.disconnectWhatsApp!(), 'WhatsApp 已断开，自动接待授权已撤销。')}>断开连接</Button>}
        <Button variant={state?.enabled ? 'secondary' : 'primary'} disabled={disabled || (!state?.enabled && (!receptionReady || !aiReady || !health.ready))} loading={pending === 'reception'} onClick={() => void act('reception', () => bridge!.invoke('set_whatsapp_reception', { ...identity, enabled: !state?.enabled }), state?.enabled ? 'WhatsApp 自动接待已暂停。' : '已开启全部单聊自动接待，后续新联系人同样适用。')}>{state?.enabled ? <Pause size={15} /> : <Play size={15} />}{state?.enabled ? '暂停 WhatsApp 接待' : '开启全部单聊接待'}</Button>
      </div>
    </header>
    {host.status === 'error' && <div className="whatsapp-hint"><Button variant="ghost" aria-expanded={detailsOpen} onClick={() => setDetailsOpen(open => !open)}>{detailsOpen ? '收起失败详情' : '查看失败详情'}</Button>{detailsOpen && <span>{phase && <>阶段：{phase}。 </>}原因：{hostReason(host)}。{diagnosticId && <> 诊断编号：{diagnosticId}</>}</span>}</div>}
    {host.error_code === 'page_database_error' && <div className="whatsapp-hint">WhatsApp 网页报告数据库错误，需重建登录环境。<Button disabled={disabled || !liveMerchant || !bridge?.rebuildWhatsAppSession} loading={pending === 'rebuild'} onClick={() => void act('rebuild', () => bridge!.rebuildWhatsAppSession!())}>重建 WhatsApp 登录环境</Button></div>}
    {recoveryStage && <div className="whatsapp-hint" role="status">{recoveryStage}</div>}
    {!liveMerchant && <div className="whatsapp-hint">请从目标商家的 Accio 授权会话启动工作台，再连接 WhatsApp。<Button variant="ghost" onClick={() => void model.navigate('settings')}>查看连接方式</Button></div>}
    {!health.ready && <div className="whatsapp-hint">{health.message}。新任务暂未就绪，原发送回执仍可核查。</div>}
    {liveMerchant && connected && !aiReady && <div className="whatsapp-hint">当前商家的 AI 尚未通过检查。<Button variant="ghost" onClick={() => void model.navigate('settings')}>前往检查 AI</Button></div>}
    <div className="whatsapp-context-bar">
      <label>背景与接管对象<select aria-label="选择 WhatsApp 单聊" value={selected} onChange={event => chooseChat(event.target.value)} disabled={disabled}><option value="">选择单聊客户</option>{allContacts.map(item => <option key={item.chat_id} value={item.chat_id}>{item.name || item.chat_id}</option>)}</select></label>
      <Button disabled={disabled || !connected} loading={pending === 'contacts'} onClick={() => void loadContacts()} title="读取可绑定的单聊联系人"><RefreshCw size={14} />读取联系人</Button>
      <span className="whatsapp-binding-label">{selected ? binding?.conversation_id ? `国际站：${backgroundName || '已绑定客户'}` : '未绑定 · 使用本渠道对话与通用资料' : '国际站绑定可选，未绑定客户同样适用自动接待'}</span>
      <Button disabled={disabled || !selected} onClick={showBinding}><Link2 size={14} />国际站绑定</Button>
      <Button disabled={disabled || !selected} onClick={() => void showContext()}><BookOpen size={14} />查看上下文</Button>
      {selected && <Button disabled={disabled || !connected || (!!current?.human_owned && (!health.ready || !receptionReady))} onClick={() => void act('takeover', () => bridge!.invoke('takeover_whatsapp', { ...identity, chat_id: selected, enabled: !current?.human_owned }), current?.human_owned ? '已恢复所选客户自动接待。' : '所选客户已由人工接管。')}>{current?.human_owned ? '恢复此客户接待' : '人工接管此客户'}</Button>}
    </div>
    {(error || notice) && <div className={`whatsapp-feedback ${error ? 'is-error' : ''}`} role={error ? 'alert' : 'status'}>{error || notice}</div>}
    <ReplyBatchRecords batches={state?.reply_batches ?? []} channel="whatsapp" contactName={id => allContacts.find(chat => chat.chat_id === id)?.name || id} reconcileDisabled={disabled} onReconcile={id => void act('reconcile', () => bridge!.invoke('reconcile_whatsapp_operation', { operation_id: id }), '已请求核查原发送回执。')} />
    {(state?.other_account_pending_count ?? 0) > 0 && <div className="whatsapp-pending" role="status">存在 {state!.other_account_pending_count} 条旧账号未决记录。请登录原 WhatsApp 账号后按原操作核查；新账号不能代替核查。</div>}
    {unresolved.length > 0 && <div className="whatsapp-pending" role="status"><span>有 {unresolved.length} 条自动发送正在处理或等待确切回执。相关单聊会暂停新增自动发送；原始界面仍可人工操作。</span>{unresolved.filter(item => item.status === 'unknown' || item.status === 'submitted').map(item => <Button key={item.operation_id} disabled={disabled} onClick={() => void act('reconcile', () => bridge!.invoke('reconcile_whatsapp_operation', { operation_id: item.operation_id }), '已请求核查原发送回执。')}>核查 {allContacts.find(chat => chat.chat_id === item.chat_id)?.name || '单聊'} 的回执</Button>)}</div>}
    <div className="whatsapp-native-surface" ref={surface} data-testid="whatsapp-native-surface"><Empty title={host.status === 'disconnected' ? '连接 WhatsApp 后在这里使用原始界面' : host.status === 'error' ? '连接未完成，请重新连接' : '正在加载 WhatsApp Web'} icon={<MessageCircle size={40} strokeWidth={1.3} />}>登录二维码、聊天列表和输入框由 WhatsApp Web 原始页面提供。</Empty></div>
    <footer className="whatsapp-footer">{!health.ready ? health.message : !receptionReady && state?.enabled ? '自动接待未就绪' : state?.enabled ? '全部单聊自动接待已开启' : 'WhatsApp 自动接待已暂停'}<span>在原始界面或手机上人工回复，会接管相应客户。</span></footer>
    {panel === 'binding' && <Drawer title="国际站背景绑定" onClose={() => setPanel(null)} className="whatsapp-drawer"><div className="whatsapp-panel-body"><p>WhatsApp：<strong>{allContacts.find(item => item.chat_id === selected)?.name || selected}</strong></p><label>追加哪位国际站客户的背景<select aria-label="选择国际站客户背景" value={bindingTarget} onChange={event => setBindingTarget(event.target.value)}><option value="">不绑定国际站客户</option>{model.snapshot?.conversations.map(item => <option key={item.conversation_id} value={item.conversation_id}>{item.customer_name}</option>)}</select></label><p>绑定后，国际站聊天、摘要、采购需求与业务补充将供该单聊的 AI 回复参考。背景只进入接待上下文。</p><p>未绑定时，AI 仍使用 WhatsApp 对话、独立摘要和当前商家适用的通用资料。绑定变更会使旧的未提交回复失效。</p><Button variant="primary" disabled={disabled} loading={pending === 'binding'} onClick={() => void saveBinding()}>保存背景绑定</Button></div></Drawer>}
    {panel === 'context' && <Drawer title="WhatsApp 接待上下文" onClose={() => { ++contextSequence.current; setPanel(null); }} className="whatsapp-drawer"><div className="whatsapp-panel-body">{contextLoading ? <p role="status">正在读取当前背景…</p> : context ? <><h3>WhatsApp 独立摘要</h3><p className="whatsapp-context-text">{context.wa_summary || '尚无摘要'}</p><h3>WhatsApp 文字依据</h3><p>{context.whatsapp_messages.length} 条近期消息</p>{context.whatsapp_messages.map(item => <article className="whatsapp-evidence" key={item.source_id}><small>{item.role === 'buyer' ? '客户' : '商家'} {messageDateTime(item.timestamp_ms)}</small><p>{item.text || '非文字消息'}</p></article>)}<h3>商家适用资料</h3><p>公开资料 {context.knowledge.length} 份，接待规则 {context.guidance.length} 份，聊天风格 {context.style.length} 份。</p>{context.international_background ? <><h3>国际站背景</h3><p className="whatsapp-context-text">{context.international_background.summary?.body || '尚无独立摘要'}</p>{context.international_background.messages.map(item => <article className="whatsapp-evidence" key={item.source_id}><small>{item.role === 'buyer' ? '客户' : '商家'} {messageDateTime(item.timestamp_ms)}</small><p>{item.text}</p></article>)}<h3>只读采购需求</h3>{Object.entries(context.international_background.requirements).filter(([key]) => key !== 'product_url' && key !== 'product_images').map(([key, item]) => { const value = item && typeof item === 'object' && 'value' in item ? (item as { value: unknown }).value : item; return typeof value === 'string' && value ? <p key={key}><strong>{requirementLabels[key] || key}：</strong>{value}</p> : null; })}<h3>业务补充</h3><p className="whatsapp-context-text">{context.international_background.supplement?.body || '暂无文字补充'}</p>{context.international_background.supplement?.attachments?.map(item => <p key={item.id}>{item.name}{item.text ? `：${item.text}` : ''}</p>)}</> : <p>未绑定国际站客户，本轮使用 WhatsApp 自身背景。</p>}</> : <p>上下文暂不可用，请关闭后重试。</p>}</div></Drawer>}
  </section>;
}
