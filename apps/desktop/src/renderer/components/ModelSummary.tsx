import { useEffect, useState } from 'react';
import type { ReceptionBridge } from '../../shared/api';
import type { ModelSettingsSnapshot } from '../../shared/model-settings';
import type { TranslationInfo } from '../../shared/translation';
import { Button } from './ui';

export function ModelSummary({ bridge }: { bridge?: ReceptionBridge }) {
  const [settings, setSettings] = useState<ModelSettingsSnapshot | null>(null);
  const [message, setMessage] = useState('');
  const [translation, setTranslation] = useState<TranslationInfo | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => { void bridge?.getTranslationSettings?.().then(value => { if (active) setTranslation(value); }).catch(() => { if (active) setTranslation(null); }); };
    refresh(); window.addEventListener('focus', refresh);
    return () => { active = false; window.removeEventListener('focus', refresh); };
  }, [bridge]);
  const toggle = async () => {
    if (!translation || !bridge?.setTranslationEnabled || busy) return;
    setBusy(true); setMessage('');
    try { setTranslation(await bridge.setTranslationEnabled(!translation.enabled)); }
    catch { setTranslation(null); setMessage('翻译设置未更改，请检查商家身份和模型配置后重试。'); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    let active = true;
    let sequence = 0;
    const refresh = () => { const request = ++sequence; void bridge?.getModelSummary?.().then(value => { if (active && request === sequence) { setSettings(value); setMessage(''); } }).catch(() => { if (active && request === sequence) { setSettings(null); setMessage('无法读取全局模型配置。'); } }); };
    refresh();
    window.addEventListener('focus', refresh);
    return () => { active = false; window.removeEventListener('focus', refresh); };
  }, [bridge]);
  const name = (purpose: 'reception' | 'translation') => {
    const id = settings?.routes[purpose].profile_id;
    return settings?.profiles.find(profile => profile.profile_id === id)?.name || '未选择';
  };
  return <section className="form-card" aria-label="全局模型配置"><div className="form-card-heading"><h2>全局模型配置</h2></div><p className="form-description">所有商家共用模型配置，商家接待授权仍需单独开启。</p><p>接待 API：{settings ? name('reception') : '读取中'}　翻译 API：{settings ? name('translation') : '读取中'}</p><p>当前商家聊天翻译：{translation ? translation.enabled ? '已启用' : '已关闭' : '状态不可用'}</p><Button disabled={!translation || !bridge?.setTranslationEnabled || busy} onClick={() => void toggle()}>{translation?.enabled ? '关闭聊天翻译' : '启用聊天翻译'}</Button><Button disabled={!bridge?.importLegacyTranslation || busy} onClick={() => { setBusy(true); void bridge?.importLegacyTranslation?.().then(() => { setMessage('已迁入旧翻译配置，请在启动台检查模型。'); return bridge.getModelSummary?.(); }).then(value => { if (value) setSettings(value); }).catch(() => setMessage('未迁入旧配置；请确认旧配置存在且当前未选择翻译模型。')).finally(() => setBusy(false)); }}>迁入旧翻译配置</Button>{message && <p role="status">{message}</p>}<Button disabled={!bridge?.openModelLauncher} onClick={() => void bridge?.openModelLauncher?.().catch(() => setMessage('无法打开启动台。'))}>在启动台管理</Button></section>;
}
