import { useState } from 'react';
import type { Settings } from '../../shared/api';
import { Button, fieldLabels } from './ui';

const defaults = {
  image_batch_mib: 64, image_prepare_seconds: 60, image_parallelism: 2,
  reception_images_enabled: true, context_message_count: 50,
  generation_retry_limit: 3, translation_parallelism: 2,
  translation_context_messages: 2, translation_context_bytes: 300,
  max_questions_per_reply: 2, max_questions_per_field: 2,
  required_fields_by_category: {} as Record<string, string[]>,
};
export type ProcessingParameters = typeof defaults;
export function processingParameters(settings?: Settings): ProcessingParameters {
  return Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
    [key, settings?.[key as keyof Settings] ?? value])) as ProcessingParameters;
}
const numbers: { key: 'generation_retry_limit' | 'max_questions_per_reply' | 'max_questions_per_field'; label: string; min: number; max: number }[] = [
  { key: 'generation_retry_limit', label: '同一来源连续生成失败上限', min: 1, max: 10 },
  { key: 'max_questions_per_reply', label: '每轮最多追问字段数', min: 1, max: 2 },
  { key: 'max_questions_per_field', label: '同一字段最多受理追问次数', min: 1, max: 10 },
];
export function ProcessingSettings({ value, onChange }: {
  value: ProcessingParameters;
  onChange: (parameters: ProcessingParameters) => void;
}) {
  const update = <K extends keyof ProcessingParameters>(key: K, next: ProcessingParameters[K]) => onChange({ ...value, [key]: next });
  const [category, setCategory] = useState('');
  const [error, setError] = useState('');
  const templates = value.required_fields_by_category;
  const add = () => {
    const label = category.trim();
    if (!label || [...label].length > 8 || templates[label]) {
      setError('请填写未添加的简短品类，最多8个字符。'); return;
    }
    update('required_fields_by_category', { ...templates, [label]: ['product'] });
    setCategory(''); setError('');
  };
  return <details className="processing-settings"><summary>高级处理参数</summary>
    <p className="form-description">按当前商家保存，与连接参数一起保存后撤销自动发送授权。图片按本轮清单准备，无固定张数上限；原图准备就绪不代表模型已读取。</p>
    <div className="processing-fields">{numbers.map(item => <label key={item.key}>{item.label}<input
      type="number" step={1} min={item.min} max={item.max} required value={value[item.key]}
      onChange={event => update(item.key, Number(event.target.value))} /></label>)}</div>
    <p className="form-description">图片准备时限与 AI 生成超时分别计算。追问限制作用于指令和已记录的受理提问次数，正文超问仍显示质量提醒。</p>
    <fieldset><legend>按品类设置待报价必备条件</legend><p className="form-description">未设置的品类使用上方通用条件。条件只用于需求交接判断，不限制普通聊天发送。</p>
      {Object.entries(templates).map(([label, fields]) => <div className="category-template" key={label}>
        <div className="form-card-heading"><strong>{label}</strong><Button onClick={() => {
          const next = { ...templates }; delete next[label]; update('required_fields_by_category', next);
        }}>移除品类条件</Button></div>
        <div className="field-checks">{Object.entries(fieldLabels).filter(([key]) => !key.startsWith('sample')).map(([key, name]) =>
          <label className="checkbox-label compact" key={key}><input type="checkbox" disabled={key === 'product'}
            checked={key === 'product' || fields.includes(key)} onChange={event => update('required_fields_by_category', {
              ...templates, [label]: event.target.checked ? [...new Set([...fields, key, 'product'])] : fields.filter(field => field !== key),
            })} />{name}</label>)}</div>
      </div>)}
      <label>新增品类<input value={category} maxLength={8} placeholder="例如：帽子" onChange={event => { setCategory(event.target.value); setError(''); }} /></label>
      <Button onClick={add} disabled={!category.trim() || Object.keys(templates).length >= 100}>添加品类条件</Button>
      {error && <p role="alert">{error}</p>}
    </fieldset>
  </details>;
}
