import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { FeishuBridge, FeishuConfiguration } from '../../shared/feishu';
import type { Workbench } from '../hooks/useWorkbench';
import { Button, Drawer, Feedback, checkLabels } from './ui';
import './feishu-form.css';

const roles = [
  ['buyer_id', '买家编号', 'text'], ['account', '联系账号', 'select'],
  ['status', '接待状态', 'select'], ['product_name', '采购需求字段', 'text'],
  ['number', '自动编号', 'auto_number'], ['customer_name', '客户字段', 'text'],
  ['quantity', '采购数量', 'number'], ['customer_email', '客户邮箱', 'text'],
  ['customer_company', '客户公司', 'text'], ['phone', '联系方式', 'text'],
  ['product_url', '产品链接', 'text'], ['inquiry_time', '询盘时间', 'datetime'],
] as const;
type Form = FeishuConfiguration & { app_secret: string; names: Record<string, string> };
function fromSettings(value: Record<string, unknown>): Form {
  const text = (key: string) => typeof value[key] === 'string' ? value[key] as string : '';
  const fields = value.fields as Record<string, { name?: string }> | undefined;
  return { enabled: value.enabled === true, app_id: text('app_id'), table_url: text('table_url'),
    app_secret: '', account_option: text('account_option'), initial_status: text('initial_status'), fields: {},
    names: Object.fromEntries(roles.map(([role]) => [role, fields?.[role]?.name ?? ''])) };
}
export function FeishuForm({ model }: { model: Workbench }) {
  const settings = model.snapshot?.settings.feishu ?? {};
  const [form, setForm] = useState<Form>(() => fromSettings(settings));
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const [grantOpen, setGrantOpen] = useState(false);
  const [granting, setGranting] = useState(false);
  const revision = useRef(0);
  const savingRef = useRef<Promise<boolean> | null>(null);
  useEffect(() => { if (!dirty) setForm(fromSettings(settings)); }, [model.snapshot?.settings.feishu, dirty]);
  useEffect(() => { model.setFormDirty('feishu', dirty); return () => model.setFormDirty('feishu', false); }, [dirty, model.setFormDirty]);
  const update = (changes: Partial<Form>) => { revision.current++; setForm(previous => ({ ...previous, ...changes })); setDirty(true); setMessage(''); };
  const save = (): Promise<boolean> => {
    if (savingRef.current) return savingRef.current;
    if (!dirty) return Promise.resolve(true);
    const bridge = (model.bridge ?? window.reception) as typeof window.reception & Partial<FeishuBridge>;
    if (!bridge?.saveFeishuConfiguration) { setMessage('飞书配置服务尚未就绪，设置未保存。'); return Promise.resolve(false); }
    const version = revision.current;
    const fields = Object.fromEntries(roles.filter(([role]) => form.names[role]?.trim()).map(([role, , type]) => [role, { name: form.names[role].trim(), type }]));
    const { names: _names, app_secret, ...values } = form;
    setSaving(true);
    const pending = (async () => {
      try {
        const saved = await bridge.saveFeishuConfiguration!({ settings: { ...values, fields }, app_secret, expected_revision: Number(settings.revision ?? 0) });
        if (!saved) { setMessage('飞书设置保存失败，请重试。'); return false; }
        if (revision.current === version) { setForm(previous => ({ ...previous, app_secret: '' })); setDirty(false); setMessage('飞书设置已保存。'); }
        else setMessage('前一版已保存，刚输入的修改仍待保存。');
        return revision.current === version;
      } catch { setMessage('飞书设置保存失败，请重试。'); return false; }
      finally { setSaving(false); savingRef.current = null; }
    })();
    savingRef.current = pending;
    return pending;
  };
  const saverModel = model as Workbench & { registerFormSaver?: (id: string, save: () => Promise<boolean>) => () => void };
  useEffect(() => saverModel.registerFormSaver?.('feishu', save), [saverModel.registerFormSaver, form, dirty]);
  const submit = (event: FormEvent) => { event.preventDefault(); void save(); };
  const grant = async (enabled: boolean) => {
    const bridge = (model.bridge ?? window.reception) as typeof window.reception & Partial<FeishuBridge>;
    if (!bridge?.grantFeishuWrite || granting) { setMessage('飞书授权服务尚未就绪。'); return; }
    setGranting(true);
    try {
      const granted = await bridge.grantFeishuWrite({ enabled, expected_revision: Number(settings.revision ?? 0) });
      setMessage(granted ? enabled ? '本次飞书写入已授权，重启后关闭。' : '本次写入授权已取消。' : '未授权飞书写入。');
      if (granted) setGrantOpen(false);
    } catch { setMessage('授权失败，请检查配置和表格结构。'); }
    finally { setGranting(false); }
  };
  const check = model.snapshot?.checks?.feishu;
  return <form className="feishu-form" onSubmit={submit}>
    <div className="feishu-form-body">
      <header><h2>飞书登记</h2><p>可选的异步登记渠道，不影响普通人工聊天。</p></header>
      <div className="feishu-form-toggle"><span>飞书登记<small>写入需要独立授权。</small></span><label className="feishu-switch"><input aria-label="启用飞书登记" type="checkbox" checked={form.enabled} onChange={event => update({ enabled: event.target.checked })} /><span /></label></div>
      <label>应用 ID<input value={form.app_id} autoComplete="off" onChange={event => update({ app_id: event.target.value })} placeholder="填写应用 ID" /></label>
      <label>应用密钥<input type="password" value={form.app_secret} autoComplete="new-password" onChange={event => update({ app_secret: event.target.value })} placeholder="留空保留已有密钥" /></label>
      <label>多维表格链接<input value={form.table_url} onChange={event => update({ table_url: event.target.value })} placeholder="https://example.feishu.cn/base/..." /></label>
      <div className="feishu-form-actions"><Button disabled={!model.connected || dirty || saving} loading={model.busy('check_feishu')} onClick={() => void model.runJob('check_feishu')}>检查表格结构</Button><Button variant="primary" disabled={!form.enabled || !model.connected || dirty || saving || granting} onClick={() => settings.write_enabled === true ? void grant(false) : setGrantOpen(true)}>{settings.write_enabled === true ? '取消本次写入授权' : '授权本次写入'}</Button></div>
      <p className="feishu-form-status" role="status">{dirty ? '请先保存设置，再检查表格结构。' : check ? `${checkLabels[check.status]}${check.message ? `：${check.message}` : ''}` : '尚未检查表格结构。'}</p>
      <details className="feishu-form-mapping"><summary>字段映射</summary><div className="feishu-form-grid"><label>联系账号选项<input value={form.account_option} onChange={event => update({ account_option: event.target.value })} /></label><label>初始接待状态<input value={form.initial_status} onChange={event => update({ initial_status: event.target.value })} /></label>{roles.map(([role, label]) => <label key={role}>{label}<input value={form.names[role] ?? ''} onChange={event => update({ names: { ...form.names, [role]: event.target.value } })} /></label>)}</div></details>
      {message && <Feedback>{message}</Feedback>}
    </div>
    <footer className="feishu-form-footer"><span>{dirty ? '有未保存的修改。保存设置会暂停接待。' : '保存配置会暂停接待。'}</span><div>{dirty && <Button disabled={saving} onClick={() => { revision.current++; setForm(fromSettings(settings)); setDirty(false); setMessage('已放弃本次飞书修改。'); }}>放弃修改</Button>}<Button type="submit" variant="primary" disabled={!dirty || !model.connected || saving} loading={saving}>保存设置</Button></div></footer>
    {grantOpen && <Drawer side="center" title="授权本次飞书登记" onClose={() => { if (!granting) setGrantOpen(false); }}><div className="ai-failure"><p>仅允许当前商家的待登记内容写入所配置的表格。登记队列仍需单独处理，重启后写入关闭。</p><div className="dialog-actions"><Button disabled={granting} onClick={() => setGrantOpen(false)}>取消</Button><Button variant="primary" loading={granting} onClick={() => void grant(true)}>确认授权</Button></div></div></Drawer>}
  </form>;
}
