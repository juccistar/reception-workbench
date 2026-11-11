import { useEffect, useRef, useState, type FormEvent } from 'react';
import { FileCheck2, Save } from 'lucide-react';
import type { Workbench } from '../hooks/useWorkbench';
import { Button, Feedback } from './ui';

const roles = [
  ['buyer_id', '买家编号', 'text', true], ['account', '联系账号', 'select', true],
  ['status', '接待状态', 'select', true], ['product_name', '产品名称', 'text', true],
  ['number', '自动编号', 'auto_number', true], ['customer_name', '客户名称', 'text', false],
  ['quantity', '采购数量', 'number', false], ['customer_email', '客户邮箱', 'text', false],
  ['customer_company', '客户公司', 'text', false], ['phone', '联系方式', 'text', false],
  ['product_url', '产品链接', 'text', false], ['inquiry_time', '询盘时间', 'datetime', false],
] as const;
interface Form {
  enabled: boolean; write_enabled: boolean; cli_path: string; base_token: string;
  table_id: string; form_share_token: string; account_option: string; initial_status: string;
  names: Record<string, string>;
}
function fromSettings(value: Record<string, unknown>): Form {
  const text = (key: string) => typeof value[key] === 'string' ? value[key] as string : '';
  const fields = value.fields as Record<string, { name?: string }> | undefined;
  return { enabled: value.enabled === true, write_enabled: value.write_enabled === true,
    cli_path: text('cli_path'), base_token: text('base_token'), table_id: text('table_id') || text('table'),
    form_share_token: text('form_share_token'), account_option: text('account_option'), initial_status: text('initial_status'),
    names: Object.fromEntries(roles.map(([role]) => [role, fields?.[role]?.name ?? ''])) };
}
export function FeishuForm({ model }: { model: Workbench }) {
  const settings = model.snapshot?.settings.feishu ?? {};
  const [form, setForm] = useState<Form>(() => fromSettings(settings));
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState('');
  const revision = useRef(0);
  useEffect(() => { if (!dirty) setForm(fromSettings(settings)); }, [model.snapshot?.settings.feishu, dirty]);
  useEffect(() => { model.setFormDirty('feishu', dirty); return () => model.setFormDirty('feishu', false); }, [dirty, model.setFormDirty]);
  const update = (changes: Partial<Form>) => { revision.current++; setForm(previous => ({ ...previous, ...changes })); setDirty(true); setMessage(''); };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const fields = Object.fromEntries(roles.filter(([role]) => form.names[role]?.trim()).map(([role, , type]) => [role, { name: form.names[role].trim(), type }]));
    if (form.write_enabled && (!form.enabled || !form.cli_path || !form.base_token || !form.table_id || !form.form_share_token || !form.account_option || !form.initial_status || roles.some(([role, , , required]) => required && !fields[role]))) {
      setMessage('开启写入前请完整填写目标、选项和五个必需字段，并执行结构检查。'); return;
    }
    const version = revision.current;
    const { names: _names, ...values } = form;
    const saved = await model.mutate('save_settings', { feishu: { ...values, fields } });
    if (saved) { setDirty(revision.current !== version); setMessage(revision.current === version ? '飞书配置已保存。请单独检查结构，再明确处理同步队列。' : '前一版已保存，刚输入的修改仍待保存。'); }
  };
  return <form className="form-card" onSubmit={event => void submit(event)}>
    <div className="form-card-heading"><h2><FileCheck2 size={18} />飞书交接</h2></div>
    <p className="form-description">使用已授权的原生飞书 CLI。以下填写表格与表单标识；访问令牌和应用密钥不进入页面。保存配置会暂停接待。</p>
    <label>飞书 CLI 完整路径<input value={form.cli_path} onChange={event => update({ cli_path: event.target.value })} placeholder="例如 C:\\Tools\\lark-cli.exe" /></label>
    <div className="form-row"><label>多维表格标识<input value={form.base_token} onChange={event => update({ base_token: event.target.value })} /></label><label>数据表 ID<input value={form.table_id} onChange={event => update({ table_id: event.target.value })} /></label></div>
    <label>原表单分享标识<input value={form.form_share_token} onChange={event => update({ form_share_token: event.target.value })} placeholder="与目标表匹配的原表单标识" /></label>
    <div className="form-row"><label>联系账号选项<input value={form.account_option} onChange={event => update({ account_option: event.target.value })} /></label><label>初始接待状态<input value={form.initial_status} onChange={event => update({ initial_status: event.target.value })} /></label></div>
    <details><summary>数据表字段映射（首次配置必填）</summary><p className="form-description">填写实际列名，类型由业务角色确定。自动编号仅由表格生成，不向报价、成本或审批列写入。</p><div className="form-row">{roles.map(([role, label, type, required]) => <label key={role}>{label}{required ? ' *' : '（可选）'}<input value={form.names[role] ?? ''} onChange={event => update({ names: { ...form.names, [role]: event.target.value } })} placeholder={type} /></label>)}</div></details>
    <label className="checkbox-label"><input type="checkbox" checked={form.enabled} onChange={event => update({ enabled: event.target.checked, write_enabled: event.target.checked && form.write_enabled })} /><span><strong>启用飞书交接</strong><small>本地保存始终独立于飞书。</small></span></label>
    <label className="checkbox-label"><input type="checkbox" checked={form.write_enabled} disabled={!form.enabled} onChange={event => update({ write_enabled: event.target.checked })} /><span><strong>允许本次运行写入交接记录</strong><small>重启后关闭写入；处理队列前仍会显示系统确认。</small></span></label>
    {message && <Feedback>{message}</Feedback>}
    <div className="form-actions"><Button type="submit" disabled={!dirty || !model.connected} loading={model.busy('save_settings')}><Save size={16} />保存飞书配置</Button>{dirty && <Button onClick={() => { setForm(fromSettings(settings)); setDirty(false); setMessage('已放弃本次飞书修改。'); }}>放弃飞书修改</Button>}</div>
  </form>;
}
