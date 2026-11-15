import { useCallback, useEffect, useRef, useState } from 'react';
import type { WhatsAppAccountBridge, WhatsAppLauncherSnapshot, WhatsAppMerchantChoice } from '../shared/whatsapp-account';
import './whatsapp-accounts.css';

type Props = { bridge: WhatsAppAccountBridge; onBack: () => void; onModels: () => void };

function MerchantOptions({ merchants, value }: { merchants: WhatsAppMerchantChoice[]; value: string }) {
  return <><option value="">不指定商家</option>{value && !merchants.some(m => m.merchant_id === value) && <option value={value}>当前商家（离线）</option>}{merchants.map(m => <option key={m.merchant_id} value={m.merchant_id} title={m.name}>{m.name} {m.account_tail ? `(${m.account_tail})` : ''}{m.available ? '' : '（离线）'}</option>)}</>;
}

export function WhatsAppAccounts({ bridge, onBack, onModels }: Props) {
  const [snapshot, setSnapshot] = useState<WhatsAppLauncherSnapshot | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [label, setLabel] = useState('');
  const [chatId, setChatId] = useState('');
  const [merchant, setMerchant] = useState('');
  const [conversation, setConversation] = useState('');
  const [defaultMerchant, setDefaultMerchant] = useState('');
  const [allowed, setAllowed] = useState<string[]>([]);
  const [capability, setCapability] = useState(false);
  const mounted = useRef(false);
  const generation = useRef(0);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const refreshQueued = useRef(false);
  const activeChat = useRef('');
  const state = snapshot?.state;
  const binding = state?.bindings.find(item => item.chat_id === chatId);
  const chat = state?.conversations.find(item => item.chat_id === chatId);
  const identity = state?.account_id && state.connection_epoch ? { expected_account_id: state.account_id, expected_connection_epoch: state.connection_epoch } : null;

  const refresh = useCallback(() => {
    if (refreshQueued.current || document.visibilityState === 'hidden') return;
    refreshQueued.current = true;
    const token = generation.current;
    const run = async () => {
      try {
        if (!mounted.current || token !== generation.current || document.visibilityState === 'hidden') return;
        const next = await bridge.getWhatsAppAccounts();
        if (mounted.current && token === generation.current) { setSnapshot(next); setError(''); }
      } catch { if (mounted.current && token === generation.current) setError('无法读取 WhatsApp 状态，请重试。'); }
      finally { refreshQueued.current = false; }
    };
    queue.current = queue.current.then(run, run);
  }, [bridge]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    bridge.getWhatsAppWritebackCapabilities().then(() => { if (mounted.current) setCapability(true); }).catch(() => { if (mounted.current) setCapability(false); });
    const timer = window.setInterval(refresh, 5000);
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => { mounted.current = false; generation.current++; window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [bridge, refresh]);

  // Reset form drafts only when the selected entity or its authoritative revision changes.
  useEffect(() => { setMerchant(binding?.merchant_id ?? ''); setConversation(binding?.conversation_id ?? ''); }, [chatId, state?.workspace_id, state?.account_id, state?.connection_epoch, binding?.revision]);
  useEffect(() => { setDefaultMerchant(state?.default_merchant_id ?? ''); }, [state?.workspace_id, state?.account_id, state?.connection_epoch, state?.default_revision]);
  useEffect(() => { setAllowed(state?.allowed_merchant_ids ?? []); }, [state?.workspace_id, state?.account_id, state?.connection_epoch, state?.enabled, state?.allowed_merchant_ids.join('\u0000')]);
  useEffect(() => { activeChat.current = chatId; }, [chatId]);

  const action = (fn: () => Promise<WhatsAppLauncherSnapshot | void>, options?: { select?: boolean; chat?: string }) => {
    if (busy) return;
    if (options?.select) { generation.current++; setChatId(''); activeChat.current = ''; }
    const token = generation.current;
    setBusy(true); setError('');
    const run = async () => {
      try {
        if (!mounted.current || token !== generation.current) return;
        const next = await fn();
        if (mounted.current && token === generation.current && (!options?.chat || activeChat.current === options.chat)) {
          if (next) setSnapshot(next); else refresh();
        }
      } catch { if (mounted.current && token === generation.current) setError('操作未完成，请检查连接和授权后重试。'); }
      finally { if (mounted.current) setBusy(false); }
    };
    queue.current = queue.current.then(run, run);
  };
  const selectChat = (id: string) => { activeChat.current = id; setChatId(id); };
  const name = (id: string | null) => id ? (snapshot?.merchants.find(m => m.merchant_id === id)?.name ?? id) : '未指定';

  return <div className="wa-accounts">
    <div className="wa-heading"><div><h1>WhatsApp 账号</h1><p>使用专用的 Google Chrome 登录。请在新窗口重新扫码，此登录与旧内嵌登录分开。</p></div><button className="quiet" onClick={onBack}>返回</button></div>
    <main className="wa-scroll">
      {error && <p className="wa-error" role="alert">{error}</p>}
      {!snapshot ? <p>正在读取账号…</p> : <>
        <section><h2>账号环境</h2><p>可以保存多个环境名称，同一时间连接一个账号。日常人工聊天请在 Chrome 原窗口进行。</p>
          {snapshot.profiles.length > 0 && <label>选择环境<select aria-label="选择环境" value={snapshot.selected_workspace_id ?? ''} disabled={busy} onChange={e => { const id = e.target.value; action(() => bridge.selectWhatsAppAccount(id), { select: true }); }}><option value="" disabled>请选择环境</option>{snapshot.profiles.map(p => <option key={p.workspace_id} value={p.workspace_id} title={p.label}>{p.label}{p.account_tail ? ` (${p.account_tail})` : ''}</option>)}</select></label>}
          {!snapshot.profiles.length && <p>还没有账号环境，请先创建。</p>}
          {creating ? <div className="wa-create"><label>环境名称<input value={label} maxLength={100} onChange={e => setLabel(e.target.value)} /></label><div className="wa-actions"><button className="secondary" disabled={busy || !label.trim()} onClick={() => { const value = label.trim(); action(async () => { const next = await bridge.createWhatsAppAccount(value); if (mounted.current) { setCreating(false); setLabel(''); } return next; }, { select: true }); }}>创建</button><button className="quiet" onClick={() => setCreating(false)}>取消</button></div></div> : <button className="secondary" disabled={busy} onClick={() => setCreating(true)}>新建环境</button>}
          {snapshot.selected_workspace_id && <div className="wa-actions"><button className="primary" disabled={busy || !!state?.connected} onClick={() => action(() => bridge.connectWhatsAppAccount())}>连接真实 Chrome</button><button className="secondary" disabled={busy} onClick={() => action(() => bridge.openWhatsAppAccount())}>打开或切换到 Chrome</button><button className="secondary" disabled={busy || !state?.connected} onClick={() => action(() => bridge.disconnectWhatsAppAccount())}>断开连接</button></div>}
          <p>Chrome 状态：{state?.connected ? '已连接' : '未连接'}{snapshot.host.status ? `（${snapshot.host.status}）` : ''}</p>
        </section>
        {state && <>
          <section><h2>接待与模型</h2><p>模型：{state.model.ready ? '已就绪' : '未就绪'}{state.model.message ? ` · ${state.model.message}` : ''}</p><button className="quiet" onClick={onModels}>前往模型配置</button><p>旧登录来源：{state.legacy.status === 'ready' ? '已核验' : state.legacy.status === 'blocked' ? '受阻' : '未核验'}{state.legacy.reason ? ` · ${state.legacy.reason}` : ''}</p><button className="secondary" disabled={busy || !state.connected} onClick={() => action(() => bridge.verifyWhatsAppLegacySources())}>核查旧接入记录</button>
            <fieldset disabled={busy || !identity}><legend>允许自动接待的商家</legend>{snapshot.merchants.length === 0 && <p>暂无商家。</p>}{snapshot.merchants.map(m => <label className="wa-check" key={m.merchant_id} title={m.name}><input type="checkbox" checked={allowed.includes(m.merchant_id)} onChange={e => setAllowed(e.target.checked ? [...allowed, m.merchant_id] : allowed.filter(id => id !== m.merchant_id))} /><span className="wa-ellipsis">{m.name}{m.available ? '' : '（离线）'}</span></label>)}{allowed.filter(id => !snapshot.merchants.some(m => m.merchant_id === id)).map(id => <label className="wa-check" key={id}><input type="checkbox" checked onChange={() => setAllowed(allowed.filter(item => item !== id))} /><span className="wa-ellipsis" title={id}>{id}（离线）</span></label>)}</fieldset>
            <div className="wa-actions"><button className="secondary" disabled={busy || !identity || (!state.enabled && !allowed.length)} onClick={() => identity && action(() => bridge.setWhatsAppAccountReception({ ...identity, enabled: !state.enabled, allowed_merchant_ids: allowed }))}>{state.enabled ? '关闭自动接待' : '开启自动接待'}</button>{state.enabled && <button className="secondary" disabled={busy || !identity || !allowed.length} onClick={() => identity && action(() => bridge.setWhatsAppAccountReception({ ...identity, enabled: true, allowed_merchant_ids: allowed }))}>保存允许商家</button>}</div><p>当前自动接待：{state.enabled ? '开启' : '关闭'}。开启和变更授权需要系统确认。</p>
            <label>新联系人的默认商家<select value={defaultMerchant} disabled={busy || !identity} onChange={e => setDefaultMerchant(e.target.value)}><MerchantOptions merchants={snapshot.merchants} value={defaultMerchant} /></select></label><button className="secondary" disabled={busy || !identity} onClick={() => identity && action(() => bridge.saveWhatsAppDefaultMerchant({ ...identity, merchant_id: defaultMerchant || null, expected_revision: state.default_revision }))}>保存默认商家</button><p>默认商家只用于新联系人；未绑定且没有默认商家的联系人不会使用 AI 接待。</p>
          </section>
          <section><h2>联系人</h2><button className="secondary" disabled={busy || !state.connected} onClick={() => action(() => bridge.readWhatsAppContacts())}>读取联系人</button>{state.conversations.length === 0 ? <p>暂无联系人，请连接后读取。</p> : <label>选择联系人<select value={chatId} onChange={e => selectChat(e.target.value)}><option value="">请选择联系人</option>{state.conversations.map(c => <option key={c.chat_id} value={c.chat_id} title={c.name}>{c.name}</option>)}</select></label>}
            {chat && <div className="wa-contact"><h3 className="wa-ellipsis" title={chat.name}>{chat.name}</h3><p>当前商家：<span title={name(binding?.merchant_id ?? null)}>{name(binding?.merchant_id ?? null)}</span> · {chat.human_owned ? '人工接管' : '自动接待'}</p><label>联系人商家<select value={merchant} disabled={busy || !identity} onChange={e => setMerchant(e.target.value)}><MerchantOptions merchants={snapshot.merchants} value={merchant} /></select></label><label>国际站会话 ID（可选）<input value={conversation} disabled={busy || !identity} onChange={e => setConversation(e.target.value)} /></label><p>填写后会核验会话归属。</p><div className="wa-actions"><button className="secondary" disabled={busy || !identity} onClick={() => identity && action(() => bridge.saveWhatsAppAccountBinding({ ...identity, chat_id: chatId, merchant_id: merchant || null, conversation_id: conversation.trim() || null, expected_revision: binding?.requested_binding_revision ?? binding?.revision ?? 0 }), { chat: chatId })}>保存联系人绑定</button><button className="secondary" disabled={busy || !identity} onClick={() => identity && action(() => bridge.takeoverWhatsAppContact({ ...identity, chat_id: chatId, enabled: !chat.human_owned }), { chat: chatId })}>{chat.human_owned ? '恢复自动接待' : '人工接管'}</button></div>{!binding?.merchant_id && !state.default_merchant_id && <p>此联系人未绑定商家且没有默认商家，不会使用 AI 接待。</p>}{binding?.requested_binding && <p>请求的绑定：{name(binding.requested_binding.merchant_id)}，等待原发送结果明确后生效。</p>}</div>}
          </section>
          <section><h2>待核查发送</h2>{state.pending_operations.length ? state.pending_operations.map(op => <div className="wa-operation" key={op.operation_id}><p>原操作 ID：<span title={op.operation_id}>{op.operation_id}</span></p><p>商家：<span title={name(op.merchant_id)}>{name(op.merchant_id)}</span> · 状态：{op.status} · 联系人：<span title={op.chat_id}>{op.chat_id}</span></p><button className="secondary" disabled={busy} onClick={() => action(() => bridge.reconcileWhatsAppAccountOperation(op.operation_id))}>核查原发送结果</button></div>) : <p>没有待核查发送。</p>}<p>核查沿用原操作，不会重新发送。</p></section>
          <section><h2>回写</h2><p>商家工作台及国际站回写尚不可用，未启用。{!capability && ' 能力状态暂不可读取。'}</p></section>
        </>}
      </>}
    </main>
  </div>;
}
