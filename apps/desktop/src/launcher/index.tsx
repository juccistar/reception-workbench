import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { LauncherMerchant } from '../shared/launcher';
import { type ModelSettingsSnapshot } from '../shared/model-settings';
import { WhatsAppAccounts } from './WhatsAppAccounts';
import './launcher.css';

type Profile = ModelSettingsSnapshot['profiles'][number];
type KeyChange = { action: 'keep' | 'clear' } | { action: 'replace'; key: string };
const capabilityNames: Record<string, string> = { text: '文字', structure: '结构化', single_image: '单图', multi_image: '多图', combined: '图文组合', translation: '翻译' };

function App() {
  const [merchants, setMerchants] = useState<LauncherMerchant[]>([]);
  const [page, setPage] = useState<'home' | 'models'>('home');
  const [modal, setModal] = useState<'authorization' | 'editor' | null>(null);
  const [authorization, setAuthorization] = useState('');
  const [snapshot, setSnapshot] = useState<ModelSettingsSnapshot | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [routes, setRoutes] = useState<ModelSettingsSnapshot['routes'] | null>(null);
  const [keys, setKeys] = useState<Record<string, KeyChange>>({});
  const [editor, setEditor] = useState<Profile | null>(null);
  const [keyInput, setKeyInput] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState<{ profileId: string; purpose: 'reception' | 'translation' } | null>(null);
  const [message, setMessage] = useState('');
  const error = (reason: unknown) => setMessage(reason instanceof Error ? reason.message : '操作未完成，请重试。');
  const refreshMerchants = async () => { const items = await window.launcher.merchants(); setMerchants(items); };
  const currentPage = useRef(page); currentPage.current = page;
  const loadingModels = useRef(false);
  const loadModels = async () => {
    if (loadingModels.current) return;
    loadingModels.current = true;
    try {
      const value = await window.launcher.modelSettings();
      setSnapshot(value); setProfiles(value.profiles); setRoutes(value.routes); setKeys({}); setPage('models'); setMessage('');
    } finally { loadingModels.current = false; }
  };
  useEffect(() => {
    let active = true;
    const route = (requested: 'home' | 'models') => {
      if (active && requested === 'models' && currentPage.current !== 'models') void loadModels().catch(error);
    };
    void window.launcher.getInitialPage?.().then(route).catch(error);
    const unsubscribe = window.launcher.onPageRequest?.(route);
    return () => { active = false; unsubscribe?.(); };
  }, []);
  useEffect(() => {
    let stopped = false, pending = false;
    const refresh = async () => {
      if (pending || stopped) return;
      pending = true;
      try { const items = await window.launcher.merchants(); if (!stopped) { setMerchants(items); } }
      catch (e) { if (!stopped) error(e); }
      finally { pending = false; }
    };
    void refresh();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh(); }, 5000);
    window.addEventListener('focus', refresh);
    return () => { stopped = true; window.clearInterval(timer); window.removeEventListener('focus', refresh); };
  }, []);
  useEffect(() => { if (!modal) return; const listener = (event: KeyboardEvent) => { if (event.key === 'Escape') setModal(null); }; document.addEventListener('keydown', listener); return () => document.removeEventListener('keydown', listener); }, [modal]);
  async function showAuthorization() { try { setAuthorization(await window.launcher.authorization()); setModal('authorization'); } catch (e) { error(e); } }
  function edit(profile?: Profile) {
    setEditor(profile ? { ...profile } : { profile_id: crypto.randomUUID(), name: '', protocol: 'chat-completions', base_url: '', model: '', credential_ref: null, has_key: false, config_revision: 0, credential_revision: 0, capability_revision: 0, capabilities: Object.fromEntries(Object.keys(capabilityNames).map(id => [id, { status: 'not_checked', checked_at: null }])) as Profile['capabilities'] });
    setKeyInput(''); setClearKey(false); setModal('editor');
  }
  function finishEditor() { if (!editor || !editor.name.trim() || !editor.base_url.trim() || !editor.model.trim()) { setMessage('请填写名称、服务地址和模型 ID。'); return; } setProfiles(current => current.some(p => p.profile_id === editor.profile_id) ? current.map(p => p.profile_id === editor.profile_id ? editor : p) : [...current, editor]); if (clearKey) setKeys(current => ({ ...current, [editor.profile_id]: { action: 'clear' } })); else if (keyInput) setKeys(current => ({ ...current, [editor.profile_id]: { action: 'replace', key: keyInput } })); setKeyInput(''); setModal(null); setMessage(''); }
  async function save() { if (!snapshot || !routes) return; const original = snapshot.routes.reception.profile_id; const selectedProfile = profiles.find(p => p.profile_id === routes.reception.profile_id); const oldProfile = snapshot.profiles.find(p => p.profile_id === original); const affected = original !== routes.reception.profile_id || (!!original && (!selectedProfile || !oldProfile || selectedProfile.protocol !== oldProfile.protocol || selectedProfile.base_url !== oldProfile.base_url || selectedProfile.model !== oldProfile.model || !!keys[original])); if (affected && !window.confirm('保存后，所有商家的自动接待将暂停，需按新配置重新开启。继续保存？')) return; setBusy(true); try { const value = await window.launcher.saveModelSettings({ expected_directory_revision: snapshot.directory_revision, profiles: profiles.map(({ profile_id, name, protocol, base_url, model, credential_ref }) => ({ profile_id, name, protocol, base_url, model, credential_ref })), routes: { reception: routes.reception.profile_id, translation: routes.translation.profile_id }, credential_changes: keys }); setSnapshot(value); setProfiles(value.profiles); setRoutes(value.routes); setKeys({}); setMessage('配置已保存。'); } catch (e) { error(e); } finally { setBusy(false); } }
  async function check(profile: Profile, purpose: 'reception' | 'translation') { if (!snapshot || JSON.stringify(profiles) !== JSON.stringify(snapshot.profiles) || JSON.stringify(routes) !== JSON.stringify(snapshot.routes) || Object.keys(keys).length) { setMessage('请先保存配置，再进行检查。'); return; } setBusy(true); setChecking({ profileId: profile.profile_id, purpose }); setMessage('正在检查，可能产生 API 费用。'); try { const value = await window.launcher.checkModel(profile.profile_id, purpose); setSnapshot(value); setProfiles(value.profiles); setRoutes(value.routes); setEditor(current => current ? value.profiles.find(p => p.profile_id === current.profile_id) ?? current : current); setMessage('检查已结束，请查看各项能力状态。'); } catch (e) { error(e); } finally { setChecking(null); setBusy(false); } }
  return <main className="launcher">
    <div style={{ display: page === 'home' ? 'contents' : 'none' }}><WhatsAppAccounts bridge={window.launcher} merchants={merchants} refreshMerchants={refreshMerchants} onAuthorization={() => void showAuthorization()} onModels={() => void loadModels().catch(error)} /></div>
    {page === 'models' && <div className="model-page">
      <button className="model-back" disabled={busy} onClick={() => { setPage('home'); setMessage(''); }}>‹　账号</button>
      <div className="model-heading"><div><h1>共享模型</h1><p>所有账号共用 API，接待和翻译可分别选择。</p></div><button className="primary" disabled={busy} onClick={() => edit()}>＋ 添加 API</button></div>
      <section className="model-card"><div className="model-section-heading"><div><h2>用途设置</h2><p>为不同工作选择模型</p></div></div><div className="model-route-grid">{routes && (['reception', 'translation'] as const).map(purpose => <div key={purpose}><span className="model-route-icon" aria-hidden="true">{purpose === 'reception' ? '✧' : '文'}</span><label>{purpose === 'reception' ? '接待模型' : '翻译模型'}<select disabled={busy} value={routes[purpose].profile_id ?? ''} onChange={event => setRoutes({ ...routes, [purpose]: { ...routes[purpose], profile_id: event.target.value || null } })}><option value="">未选择</option>{profiles.map(p => <option key={p.profile_id} value={p.profile_id}>{p.name} · {p.model}</option>)}</select></label></div>)}</div></section>
      <section className="model-card"><div className="model-section-heading"><div><h2>API 列表</h2><p>管理连接信息</p></div><span>{profiles.length} 个 API</span></div>{profiles.length === 0 && <p className="muted">还没有 API 配置，可先添加后选择用途。</p>}{profiles.map(profile => <div className="model-row" key={profile.profile_id}><span className="model-mark" aria-hidden="true">◇</span><div className="model-info"><strong>{profile.name}</strong><span>{profile.model} · {profile.base_url}</span></div><span className="model-badge">{keys[profile.profile_id]?.action === 'clear' ? '未配置 Key' : profile.has_key || keys[profile.profile_id]?.action === 'replace' ? '已配置' : '未配置 Key'}</span><button className="secondary" disabled={busy} onClick={() => edit(profile)}>编辑</button></div>)}</section>
      {checking && <button className="secondary" onClick={() => void window.launcher.cancelModelCheck(checking.profileId, checking.purpose).then(() => setMessage('正在取消检查…')).catch(error)}>取消检查</button>}
      <p className="feedback" role="status">{message}</p><div className="model-save"><button className="secondary" disabled={busy} onClick={() => { setPage('home'); setMessage(''); }}>取消</button><button className="primary" disabled={busy || !snapshot} onClick={() => void save()}>保存配置</button></div>
    </div>}
    {modal === 'authorization' && <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) setModal(null); }}><div className="dialog" role="dialog" aria-modal="true" aria-label="添加或重新授权商家"><div className="dialog-title"><h2>添加或重新授权商家</h2><button className="quiet" aria-label="关闭" onClick={() => setModal(null)}>✕</button></div><p>请在对应商家的 Accio Work 授权会话中执行启动指令。授权环境无法确认长期复用时，需要重新授权。</p><pre>{authorization}</pre><button className="primary" onClick={() => void window.launcher.copyAuthorization().then(() => setMessage('口令已复制。')).catch(error)}>复制口令</button><p role="status">{message}</p></div></div>}
    {modal === 'editor' && editor && <div className="model-drawer-overlay" onMouseDown={event => { if (event.target === event.currentTarget) setModal(null); }}><div className="model-drawer" role="dialog" aria-modal="true" aria-label={profiles.some(p => p.profile_id === editor.profile_id) ? '编辑 API' : '添加 API'}><div className="model-drawer-heading"><h2>{profiles.some(p => p.profile_id === editor.profile_id) ? '编辑 API' : '添加 API'}</h2><button className="quiet" aria-label="关闭" onClick={() => setModal(null)}>✕</button></div><div className="model-drawer-body">
      <label>名称<input autoFocus value={editor.name} onChange={e => setEditor({ ...editor, name: e.target.value })}/></label><label>协议<select value={editor.protocol} onChange={e => setEditor({ ...editor, protocol: e.target.value as Profile['protocol'] })}><option value="chat-completions">OpenAI 兼容</option><option value="anthropic">Anthropic</option></select></label><label>地址<input value={editor.base_url} onChange={e => setEditor({ ...editor, base_url: e.target.value })}/></label><label>模型名<input value={editor.model} onChange={e => setEditor({ ...editor, model: e.target.value })}/></label><label>Key<input type="password" autoComplete="new-password" value={keyInput} onChange={e => { setKeyInput(e.target.value); setClearKey(false); }} placeholder={editor.has_key || keys[editor.profile_id]?.action === 'replace' ? '留空保留已有 Key' : '输入 API Key'}/><small>Key 保存后不会显示。</small></label>{(editor.has_key || keys[editor.profile_id]?.action === 'replace') && <label className="model-clear-key"><input type="checkbox" checked={clearKey} onChange={e => { setClearKey(e.target.checked); if (e.target.checked) setKeyInput(''); }}/>清除已保存的 Key</label>}
      <details className="model-details"><summary>连接与能力检查</summary><p>能力检查会发送合成文字或图片，可能产生 API 费用。请先保存配置。</p><p>{Object.entries(capabilityNames).map(([id, label]) => `${label}：${editor.capabilities[id as keyof Profile['capabilities']]?.status === 'passed' ? '通过' : editor.capabilities[id as keyof Profile['capabilities']]?.status === 'failed' ? '失败' : '未检查'}`).join(' / ')}</p><div className="model-detail-actions"><button className="secondary" disabled={busy || !!keyInput || clearKey || JSON.stringify(editor) !== JSON.stringify(profiles.find(p => p.profile_id === editor.profile_id))} onClick={() => void check(editor, 'reception')}>检查接待</button><button className="secondary" disabled={busy || !!keyInput || clearKey || JSON.stringify(editor) !== JSON.stringify(profiles.find(p => p.profile_id === editor.profile_id))} onClick={() => void check(editor, 'translation')}>检查翻译</button>{checking && <button className="secondary" onClick={() => void window.launcher.cancelModelCheck(checking.profileId, checking.purpose).then(() => setMessage('正在取消检查…')).catch(error)}>取消检查</button>}</div></details>
      {profiles.some(p => p.profile_id === editor.profile_id) && <button className="model-delete" disabled={busy} onClick={() => { if (routes?.reception.profile_id === editor.profile_id || routes?.translation.profile_id === editor.profile_id) { setMessage('该 API 仍被用途引用，请先改选或取消选择。'); return; } if (window.confirm(`删除“${editor.name}”？`)) { setProfiles(current => current.filter(p => p.profile_id !== editor.profile_id)); setKeys(current => { const next = { ...current }; delete next[editor.profile_id]; return next; }); setModal(null); } }}>删除 API</button>}
      <p className="feedback" role="status">{message}</p></div><div className="model-drawer-footer"><button className="secondary" onClick={() => setModal(null)}>取消</button><button className="primary" onClick={finishEditor}>保存</button></div></div></div>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<App/>);
