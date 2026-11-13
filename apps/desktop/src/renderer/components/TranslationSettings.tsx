import { useEffect, useRef, useState } from 'react';
import type { ReceptionBridge } from '../../shared/api';
import type { TranslationHostBridge, TranslationInfo, TranslationSettingsInput } from '../../shared/translation';
import { parseTranslationEndpoint, TRANSLATION_ENDPOINT_ERROR } from '../../shared/translation-endpoint';
import { Button, Feedback } from './ui';

const defaultUrl = 'https://api.deepseek.com/v1';
const statusLabels: Record<TranslationInfo['status'], string> = {
  not_configured: '未配置', disabled: '已停用', ready: '可用', checking: '正在检查', failed: '检查失败', demo: '演示模式',
};
type Form = TranslationSettingsInput & { api_key: string; clear_key: boolean };
const initialForm = (): Form => ({ enabled: false, base_url: defaultUrl, model: '', revision: 0, shared_revision: 0, api_key: '', clear_key: false });
function formFromInfo(info: TranslationInfo): Form {
  return { enabled: info.enabled, base_url: info.base_url || defaultUrl, model: info.model || '', revision: info.revision,
    shared_revision: info.shared_revision ?? 0, api_key: '', clear_key: false };
}

function saveFailureMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : '';
  // Electron prefixes IPC exceptions. Display only fixed application messages,
  // never arbitrary backend/provider text which might contain credentials.
  const known = [TRANSLATION_ENDPOINT_ERROR, '已取消保存翻译设置。',
    '翻译设置已变化，请保留输入并重新读取。', '翻译配置已变化，请重新读取。',
    '启用前请填写模型并配置 API Key。', '翻译设置格式无效。', 'API Key 格式无效。',
    '请在数据管理或退出结束后修改翻译设置。', '翻译密钥或配置未能安全保存，请重试。',
    '共享翻译配置已变化，请保留输入并重新读取。',
    '共享翻译配置已保存，当前窗口尚未确认，请重新读取。'];
  return known.find(value => message === value || message.endsWith(`: ${value}`)) || '保存失败，请检查设置并重试。';
}

export function TranslationSettingsPanel({ bridge, disabled = false, onDirtyChange }: { bridge?: ReceptionBridge; disabled?: boolean; onDirtyChange?: (dirty: boolean) => void }) {
  // The host methods are optional while older desktop bridges remain supported.
  const host = bridge as (ReceptionBridge & Partial<TranslationHostBridge>) | undefined;
  const available = !!(host?.getTranslationSettings && host.saveTranslationSettings && host.checkTranslationProvider && host.clearTranslationCache);
  const [info, setInfo] = useState<TranslationInfo | null>(null);
  const [form, setForm] = useState<Form>(initialForm);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const dirtyRef = useRef(false);
  const version = useRef(0);
  const keyVersion = useRef(0);
  const request = useRef(0);
  const active = useRef(false);
  const locked = useRef(false);
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  useEffect(() => () => dirtyCallback.current?.(false), []);

  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    active.current = true;
    dirtyRef.current = false; setDirty(false); setInfo(null); setForm(initialForm()); setMessage('');
    const refresh = () => {
      if (!host?.getTranslationSettings) return;
      const id = ++request.current;
      void host.getTranslationSettings().then(value => {
        if (!active.current || id !== request.current) return;
        setInfo(value);
        if (!dirtyRef.current) setForm(formFromInfo(value));
      }).catch(() => { if (active.current && id === request.current) setMessage('无法读取翻译设置，请重试。'); });
    };
    if (available) refresh();
    const unsubscribe = host?.onEvent?.(event => {
      if (event.event.startsWith('translation.') || event.event === 'job.updated') refresh();
    });
    return () => { active.current = false; request.current++; unsubscribe?.(); };
  }, [bridge, available]);

  const edit = (patch: Partial<Form>, keyChanged = false) => {
    version.current++;
    if (keyChanged) keyVersion.current++;
    dirtyRef.current = true; setDirty(true);
    setForm(current => ({ ...current, ...patch }));
  };
  const save = async () => {
    if (!host?.saveTranslationSettings || locked.current || disabled) return;
    locked.current = true; setBusy(true); setMessage('');
    const submitted = { ...form };
    const editAtStart = version.current;
    const keyAtStart = keyVersion.current;
    ++request.current;
    try {
      parseTranslationEndpoint(submitted.base_url);
      const value = await host.saveTranslationSettings(submitted);
      if (!active.current) return;
      ++request.current;
      setInfo(value);
      if (version.current === editAtStart) {
        dirtyRef.current = false; setDirty(false); setForm(formFromInfo(value));
      } else {
        setForm(current => ({ ...current, revision: value.revision, shared_revision: value.shared_revision ?? 0,
          api_key: keyVersion.current === keyAtStart ? '' : current.api_key,
          clear_key: keyVersion.current === keyAtStart ? false : current.clear_key }));
      }
      setMessage('翻译设置已保存。');
    } catch (error) {
      if (active.current) {
        setMessage(saveFailureMessage(error));
        const id = ++request.current;
        try {
          const latest = await host.getTranslationSettings?.();
          if (latest && active.current && id === request.current) setInfo(latest);
        } catch { /* Keep edits and the original safe failure message. */ }
      }
    }
    finally { locked.current = false; if (active.current) setBusy(false); }
  };
  const run = async (action: 'check' | 'clear') => {
    if (!host || locked.current || disabled) return;
    locked.current = true; setBusy(true); setMessage('');
    try {
      if (action === 'check') { await host.checkTranslationProvider!(); if (active.current) setMessage('已开始合成检查。'); }
      else { const result = await host.clearTranslationCache!(); if (active.current) setMessage(`已清除 ${result.removed} 条聊天译文。`); }
    } catch { if (active.current) setMessage('操作失败，请重试。'); }
    finally { locked.current = false; if (active.current) setBusy(false); }
  };

  return <section className="form-card" aria-label="聊天翻译设置">
    <div className="form-card-heading"><h2>聊天翻译设置</h2></div>
    {!available ? <Feedback>当前桌面版本不支持第三方聊天翻译。</Feedback> : <>
      {!info && !message && <p role="status">正在读取翻译设置…</p>}
      {info && <>
        <p role="status">翻译状态：{statusLabels[info.status]}{info.message && ` · ${info.message}`}</p>
        {info.connection_scope === 'shared' && <p className="form-description">{info.status === 'demo'
          ? '演示模式的服务配置在演示窗口间共享，与真实商家配置隔离。'
          : '服务地址、模型和 API Key 在本机同一 Windows 用户的各商家间自动同步。启用状态、聊天和译文按商家分别保存。'}</p>}
        <p className="form-description">密钥：{info.has_key ? '已保存' : '未保存'}。翻译时会将买卖双方聊天文字或人工生成/润色的回复建议，以及最多 2 条前文发送给所配置的第三方服务。</p>
        <label><input type="checkbox" checked={form.enabled} disabled={disabled} onChange={event => edit({ enabled: event.target.checked })} />启用第三方聊天翻译</label>
        <label>服务地址<input type="url" value={form.base_url} disabled={disabled} onChange={event => edit({ base_url: event.target.value })} /></label>
        <p className="form-description">支持 HTTPS 服务；本机网关可使用 HTTP，例如 http://127.0.0.1:3425/v1。</p>
        <label>模型<input value={form.model} disabled={disabled} onChange={event => edit({ model: event.target.value })} placeholder="请输入模型名称" /></label>
        <label>新 API Key<input type="password" autoComplete="new-password" value={form.api_key} disabled={disabled} onChange={event => edit({ api_key: event.target.value, clear_key: false }, true)} /></label>
        <p className="form-description">留空保留已保存的 Key；不会显示已保存的 Key。{info.connection_scope === 'shared' && (info.status === 'demo'
          ? '更换或清除 Key 会同步影响本机其他演示窗口。'
          : '更换或清除 Key 会同步影响本机其他商家；启用开关只针对当前商家。')}</p>
        <Button disabled={disabled || !info.has_key} onClick={() => edit({ api_key: '', clear_key: !form.clear_key, enabled: form.clear_key ? form.enabled : false }, true)}>{form.clear_key ? '撤销清除 Key' : '清除已保存 Key'}</Button>
        {form.clear_key && <p>保存后将清除已保存的 Key。</p>}
        <div className="storage-actions">
          <Button disabled={disabled || busy || !dirty || !form.base_url.trim() || !form.model.trim()} onClick={() => void save()}>保存翻译设置</Button>
          {dirty && <Button disabled={disabled || busy} onClick={() => { version.current++; keyVersion.current++; dirtyRef.current = false; setDirty(false); setForm(formFromInfo(info)); setMessage('已放弃本次翻译设置修改。'); }}>放弃修改</Button>}
          <Button disabled={disabled || busy || dirty} onClick={() => void run('check')}>合成检查</Button>
          <Button disabled={disabled || busy} onClick={() => void run('clear')}>清除聊天译文缓存</Button>
        </div>
        <p className="form-description">清缓存仅删除聊天译文，不删除原文。</p>
        <p>当前商家用量：请求 {info.usage.requests} 次，输入 token {info.usage.prompt_tokens}，输出 token {info.usage.completion_tokens}，未知用量请求 {info.usage.unknown_requests} 次，缓存命中 {info.usage.cache_hits} 次。</p>
      </>}
      {message && <Feedback>{message}</Feedback>}
    </>}
  </section>;
}