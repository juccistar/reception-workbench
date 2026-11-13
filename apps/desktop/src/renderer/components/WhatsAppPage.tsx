import { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, Link2, MessageCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { ReceptionBridge } from '../../shared/api';
import type { WhatsAppContact, WhatsAppContext, WhatsAppHostInfo, WhatsAppState } from '../../shared/whatsapp';
import type { Workbench } from '../hooks/useWorkbench';
import { errorText } from '../hooks/useWorkbench';
import { Badge, Button, Drawer, Empty, messageDateTime } from './ui';

const hostLabels: Record<string, string> = { disconnected: '未连接', connecting: '正在连接', qr: '请在原始页面扫码', authenticated: '正在加载聊天', ready: '已连接', error: '连接失败，可重新连接' };
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
  const surface = useRef<HTMLDivElement>(null);
  const live = useRef(true);
  const readSequence = useRef(0);
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

  const refresh = useCallback(async () => {
    if (!bridge) return;
    const sequence = ++readSequence.current;
    const [business, native] = await Promise.all([bridge.invoke<WhatsAppState>('get_whatsapp_state'), bridge.getWhatsAppHostInfo?.() ?? Promise.resolve(emptyHost)]);
    if (live.current && sequence === readSequence.current) { setState(business); setHost(native); }
  }, [bridge]);
  useEffect(() => {
    live.current = true;
    void refresh().catch(reason => { if (live.current) setError(errorText(reason)); });
    const unsubscribe = bridge?.onEvent(event => {
      if (event.event === 'state.changed' || event.event.startsWith('whatsapp.')) void refresh().catch(() => {});
    });
    return () => { live.current = false; ++readSequence.current; ++contextSequence.current; unsubscribe?.(); };
  }, [bridge, refresh]);

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
  }, [bridge, host.status, panel, obscured, model.closing]);

  const act = async (key: string, action: () => Promise<unknown>, success = '') => {
    if (pending || model.closing) return;
    setPending(key); setError(''); setNotice('');
    try { await action(); await refresh(); if (live.current && success) setNotice(success); }
    catch (reason) { if (live.current) setError(errorText(reason)); }
    finally { if (live.current) setPending(''); }
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
    catch (reason) { if (live.current && sequence === contextSequence.current) setError(errorText(reason)); }
    finally { if (live.current && sequence === contextSequence.current) setContextLoading(false); }
  };
  const saveBinding = () => act('binding', async () => {
    await bridge!.invoke('save_whatsapp_binding', { ...identity, chat_id: selected, conversation_id: bindingTarget || null, expected_revision: bindingVersion });
    if (live.current) setPanel(null);
  }, '国际站背景已更新，后续回复将使用新背景。');
  const unresolved = state?.pending_operations.filter(item => ['prepared', 'sending', 'submitted', 'unknown'].includes(item.status)) ?? [];
  const aiReady = model.snapshot?.checks.ai.status === 'passed';
  const disabled = !!pending || model.closing || !bridge;

  return <section className="whatsapp-page" aria-label="WhatsApp 工作区">
    <header className="whatsapp-toolbar">
      <div className="whatsapp-heading"><MessageCircle size={23} /><div><h1>WhatsApp</h1><span>原始聊天界面 · 全部单聊接待</span></div><Badge tone={connected ? 'green' : 'neutral'}>{hostLabels[host.status] ?? '正在读取连接状态'}</Badge>{connected && host.account_id && <span className="whatsapp-account">账号：{host.account_id.replace(/@(c\.us|lid)$/, '')}</span>}</div>
      <div className="whatsapp-actions">
        {host.status === 'disconnected' || host.status === 'error' ? <Button disabled={disabled || !liveMerchant || !bridge?.connectWhatsApp} loading={pending === 'connect'} onClick={() => void act('connect', () => bridge!.connectWhatsApp!())}>连接 WhatsApp</Button> : <Button disabled={disabled || !bridge?.disconnectWhatsApp} onClick={() => void act('disconnect', () => bridge!.disconnectWhatsApp!(), 'WhatsApp 已断开，自动接待授权已撤销。')}>断开连接</Button>}
        <Button variant={state?.enabled ? 'secondary' : 'primary'} disabled={disabled || !connected || (!state?.enabled && !aiReady)} loading={pending === 'reception'} onClick={() => void act('reception', () => bridge!.invoke('set_whatsapp_reception', { ...identity, enabled: !state?.enabled }), state?.enabled ? 'WhatsApp 自动接待已暂停。' : '已开启全部单聊自动接待，后续新联系人同样适用。')}>{state?.enabled ? <Pause size={15} /> : <Play size={15} />}{state?.enabled ? '暂停 WhatsApp 接待' : '开启全部单聊接待'}</Button>
      </div>
    </header>
    {!liveMerchant && <div className="whatsapp-hint">请从目标商家的 Accio 授权会话启动工作台，再连接 WhatsApp。<Button variant="ghost" onClick={() => void model.navigate('settings')}>查看连接方式</Button></div>}
    {liveMerchant && connected && !aiReady && <div className="whatsapp-hint">当前商家的 AI 尚未通过检查。<Button variant="ghost" onClick={() => void model.navigate('settings')}>前往检查 AI</Button></div>}
    <div className="whatsapp-context-bar">
      <label>背景与接管对象<select aria-label="选择 WhatsApp 单聊" value={selected} onChange={event => chooseChat(event.target.value)} disabled={disabled}><option value="">选择单聊客户</option>{allContacts.map(item => <option key={item.chat_id} value={item.chat_id}>{item.name || item.chat_id}</option>)}</select></label>
      <Button disabled={disabled || !connected} loading={pending === 'contacts'} onClick={() => void loadContacts()} title="读取可绑定的单聊联系人"><RefreshCw size={14} />读取联系人</Button>
      <span className="whatsapp-binding-label">{selected ? binding?.conversation_id ? `国际站：${backgroundName || '已绑定客户'}` : '未绑定 · 使用本渠道对话与通用资料' : '国际站绑定可选，未绑定客户同样适用自动接待'}</span>
      <Button disabled={disabled || !selected} onClick={showBinding}><Link2 size={14} />国际站绑定</Button>
      <Button disabled={disabled || !selected} onClick={() => void showContext()}><BookOpen size={14} />查看上下文</Button>
      {selected && <Button disabled={disabled || !connected} onClick={() => void act('takeover', () => bridge!.invoke('takeover_whatsapp', { ...identity, chat_id: selected, enabled: !current?.human_owned }), current?.human_owned ? '已恢复所选客户自动接待。' : '所选客户已由人工接管。')}>{current?.human_owned ? '恢复此客户接待' : '人工接管此客户'}</Button>}
    </div>
    {(error || notice) && <div className={`whatsapp-feedback ${error ? 'is-error' : ''}`} role={error ? 'alert' : 'status'}>{error || notice}</div>}
    {unresolved.length > 0 && <div className="whatsapp-pending" role="status"><span>有 {unresolved.length} 条自动发送正在处理或等待确切回执。相关单聊会暂停新增自动发送；原始界面仍可人工操作。</span>{unresolved.filter(item => item.status === 'unknown' || item.status === 'submitted').map(item => <Button key={item.operation_id} disabled={disabled} onClick={() => void act('reconcile', () => bridge!.invoke('reconcile_whatsapp_operation', { operation_id: item.operation_id }), '已请求核查原发送回执。')}>核查 {allContacts.find(chat => chat.chat_id === item.chat_id)?.name || '单聊'} 的回执</Button>)}</div>}
    <div className="whatsapp-native-surface" ref={surface} data-testid="whatsapp-native-surface"><Empty title={host.status === 'disconnected' ? '连接 WhatsApp 后在这里使用原始界面' : host.status === 'error' ? '连接未完成，请重新连接' : '正在加载 WhatsApp Web'} icon={<MessageCircle size={40} strokeWidth={1.3} />}>登录二维码、聊天列表和输入框由 WhatsApp Web 原始页面提供。</Empty></div>
    <footer className="whatsapp-footer">{state?.enabled ? '全部单聊自动接待已开启' : 'WhatsApp 自动接待已暂停'}<span>在原始界面或手机上人工回复，会接管相应客户。</span></footer>
    {panel === 'binding' && <Drawer title="国际站背景绑定" onClose={() => setPanel(null)} className="whatsapp-drawer"><div className="whatsapp-panel-body"><p>WhatsApp：<strong>{allContacts.find(item => item.chat_id === selected)?.name || selected}</strong></p><label>追加哪位国际站客户的背景<select aria-label="选择国际站客户背景" value={bindingTarget} onChange={event => setBindingTarget(event.target.value)}><option value="">不绑定国际站客户</option>{model.snapshot?.conversations.map(item => <option key={item.conversation_id} value={item.conversation_id}>{item.customer_name}</option>)}</select></label><p>绑定后，国际站聊天、摘要、采购需求与业务补充将供该单聊的 AI 回复参考。背景只进入接待上下文。</p><p>未绑定时，AI 仍使用 WhatsApp 对话、独立摘要和当前商家适用的通用资料。绑定变更会使旧的未提交回复失效。</p><Button variant="primary" disabled={disabled} loading={pending === 'binding'} onClick={() => void saveBinding()}>保存背景绑定</Button></div></Drawer>}
    {panel === 'context' && <Drawer title="WhatsApp 接待上下文" onClose={() => { ++contextSequence.current; setPanel(null); }} className="whatsapp-drawer"><div className="whatsapp-panel-body">{contextLoading ? <p role="status">正在读取当前背景…</p> : context ? <><h3>WhatsApp 独立摘要</h3><p className="whatsapp-context-text">{context.wa_summary || '尚无摘要'}</p><h3>WhatsApp 文字依据</h3><p>{context.whatsapp_messages.length} 条近期消息</p>{context.whatsapp_messages.map(item => <article className="whatsapp-evidence" key={item.source_id}><small>{item.role === 'buyer' ? '客户' : '商家'} {messageDateTime(item.timestamp_ms)}</small><p>{item.text || '非文字消息'}</p></article>)}<h3>商家适用资料</h3><p>公开资料 {context.knowledge.length} 份，接待规则 {context.guidance.length} 份，聊天风格 {context.style.length} 份。</p>{context.international_background ? <><h3>国际站背景</h3><p className="whatsapp-context-text">{context.international_background.summary?.body || '尚无独立摘要'}</p>{context.international_background.messages.map(item => <article className="whatsapp-evidence" key={item.source_id}><small>{item.role === 'buyer' ? '客户' : '商家'} {messageDateTime(item.timestamp_ms)}</small><p>{item.text}</p></article>)}<h3>只读采购需求</h3>{Object.entries(context.international_background.requirements).filter(([key]) => key !== 'product_url' && key !== 'product_images').map(([key, item]) => { const value = item && typeof item === 'object' && 'value' in item ? (item as { value: unknown }).value : item; return typeof value === 'string' && value ? <p key={key}><strong>{requirementLabels[key] || key}：</strong>{value}</p> : null; })}<h3>业务补充</h3><p className="whatsapp-context-text">{context.international_background.supplement?.body || '暂无文字补充'}</p>{context.international_background.supplement?.attachments?.map(item => <p key={item.id}>{item.name}{item.text ? `：${item.text}` : ''}</p>)}</> : <p>未绑定国际站客户，本轮使用 WhatsApp 自身背景。</p>}</> : <p>上下文暂不可用，请关闭后重试。</p>}</div></Drawer>}
  </section>;
}
