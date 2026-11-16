import { useEffect, useState } from 'react';
import type { ReceptionBridge } from '../../shared/api';
import type { ModelSettingsSnapshot } from '../../shared/model-settings';
import { Button } from './ui';

export function ModelSummary({ bridge }: { bridge?: ReceptionBridge }) {
  const [settings, setSettings] = useState<ModelSettingsSnapshot | null>(null);
  const [message, setMessage] = useState('');
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
  return <section className="form-card" aria-label="全局模型配置"><div className="form-card-heading"><h2>全局模型配置</h2></div><p className="form-description">所有商家共用模型配置。</p><p>接待 API：{settings ? name('reception') : '读取中'}　翻译 API：{settings ? name('translation') : '读取中'}</p>{message && <p role="status">{message}</p>}<Button disabled={!bridge?.openModelLauncher} onClick={() => void bridge?.openModelLauncher?.().catch(() => setMessage('无法打开启动台。'))}>在启动台管理</Button></section>;
}
