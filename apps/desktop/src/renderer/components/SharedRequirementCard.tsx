import { useEffect, useRef, useState } from 'react';
import type { Case, RequirementMedia } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { ProductMainImage } from './ProductMainImage';

const fields = [ ['product', '产品'], ['product_id', '产品ID'], ['product_url', '产品链接'], ['product_images', '产品图片'], ['quantity', '数量与单位'], ['specifications', '关键规格'], ['customization', '定制要求'], ['destination', '收货地'], ['delivery', '客户期望交期'], ['budget', '客户预算'], ['packaging', '包装要求'], ['certifications', '认证要求'], ['contact', '联系方式'] ] as const;
const safeImage = (value: unknown): value is string => typeof value === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1400000;
const safeLink = (value: string) => { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password; } catch { return false; } };

export function SharedRequirementCard({ model, current }: { model: Workbench; current: Case; locate?: (id: string) => void; actions?: boolean }) {
  const [media, setMedia] = useState<RequirementMedia | null>(null);
  const [image, setImage] = useState<{ url: string; label: string } | null>(null);
  const [copyStatus, setCopyStatus] = useState('');
  const trigger = useRef<HTMLButtonElement | null>(null);
  const close = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!image) return;
    close.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setImage(null); }
      if (event.key === 'Tab') { event.preventDefault(); close.current?.focus(); }
    };
    document.addEventListener('keydown', key, true);
    return () => { document.removeEventListener('keydown', key, true); trigger.current?.focus(); };
  }, [image]);
  const requirements = current.requirements_format === 'ai-v1' ? current.requirements : undefined;
  const refs = Array.isArray(requirements?.product_images?.value) ? requirements.product_images.value : [];
  useEffect(() => {
    let active = true; setMedia(null); setImage(null);
    if (refs.length && model.connected) void model.invoke<RequirementMedia>('get_requirement_media', { case_id: current.case_id }).then(result => { if (active && result.case_id === current.case_id) setMedia(result); }).catch(() => {});
    return () => { active = false; };
  }, [current.case_id, current.revision, model.connected, model.invoke]);
  const present = fields.filter(([key]) => { const value = requirements?.[key]?.value; return Array.isArray(value) ? value.length > 0 : typeof value === 'string' && !!value.trim(); });
  if (!present.length) return <p className="requirements-empty">当前没有采购需求</p>;
  return <div className="canonical-requirements">{present.map(([key, label]) => {
    const value = requirements![key].value;
    if (key === 'product_images') return <div className="canonical-field" key={key}><strong>{label}</strong><div>{media?.items.filter(item => refs.includes(item.media_id) && item.status === 'ready' && safeImage(item.data_url)).map(item => <button className="requirement-image-button" type="button" key={item.media_id} onClick={event => { trigger.current = event.currentTarget; setImage({ url: item.data_url!, label: '产品图片' }); }} aria-label="放大查看产品图片"><img src={item.data_url} alt="产品图片" /></button>)}{!media?.items.some(item => refs.includes(item.media_id) && item.status === 'ready' && safeImage(item.data_url)) && <span>图片暂不可用</span>}</div></div>;
    if (typeof value !== 'string') return null;
    return <div className="canonical-field" key={key}><strong>{label}</strong><div><span>{value}</span>{(key === 'product_id' || key === 'product_url' && safeLink(value)) && <button type="button" className="source-link" onClick={() => { if (!navigator.clipboard) { setCopyStatus('复制失败，请选择文字复制'); return; } void navigator.clipboard.writeText(value).then(() => setCopyStatus(`${label}已复制`), () => setCopyStatus('复制失败，请选择文字复制')); }}>复制{label}</button>}</div></div>;
  })}{(current.product_main_image || typeof requirements?.product_id?.value === 'string' && requirements.product_id.value.trim()) && <ProductMainImage localProductId={/^[0-9]+$/.test(String(requirements?.product_id?.value ?? '')) ? (current.product_main_image?.local_product_id ?? current.local_product_id) : null} metadata={current.product_main_image} invoke={model.invoke} emptyLabel={/^[0-9]+$/.test(String(requirements?.product_id?.value ?? '')) ? '暂无可用商品主图' : '无平台主图'} />}{copyStatus && <small role="status">{copyStatus}</small>}{image && <div className="image-dialog-backdrop" onClick={() => setImage(null)}><div className="image-dialog" role="dialog" aria-modal="true" aria-label={image.label} onClick={event => event.stopPropagation()}><button type="button" ref={close} onClick={() => setImage(null)}>关闭图片</button><img src={image.url} alt={image.label} /></div></div>}</div>;
}
