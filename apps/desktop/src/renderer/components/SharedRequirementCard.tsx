import { useEffect, useRef, useState } from 'react';
import { ExternalLink } from 'lucide-react';
import type { Case, RequirementMedia, ReceptionBridge } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { ProductMainImage } from './ProductMainImage';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

const fields = [ ['product', '产品'], ['product_id', '产品ID'], ['product_url', '产品链接'], ['product_images', '产品图片'], ['quantity', '数量与单位'], ['specifications', '关键规格'], ['customization', '定制要求'], ['destination', '收货地'], ['delivery', '客户期望交期'], ['budget', '客户预算'], ['packaging', '包装要求'], ['certifications', '认证要求'], ['contact', '联系方式'] ] as const;
const safeImage = (value: unknown): value is string => typeof value === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1400000;
type CopyBridge = ReceptionBridge & { copyProductText?: (params: { field: 'product_id' | 'product_url'; case_id: string }) => Promise<void> };

export function SharedRequirementCard({ model, current }: { model: Workbench; current: Case; locate?: (id: string) => void; actions?: boolean }) {
  const [media, setMedia] = useState<RequirementMedia | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<{ owner: string; field: string; message: string } | null>(null);
  const owner = `${current.conversation_id}:${current.case_id}`;
  const ownerRef = useRef(owner); ownerRef.current = owner;
  const operation = useRef(0);
  const report = (field: string, action: () => Promise<void>, success: string, failure: string) => {
    const source = owner; const serial = ++operation.current;
    void action().then(() => { if (ownerRef.current === source && serial === operation.current) setFeedback({ owner: source, field, message: success }); }, () => { if (ownerRef.current === source && serial === operation.current) setFeedback({ owner: source, field, message: failure }); });
  };
  const requirements = current.requirements_format === 'ai-v1' ? current.requirements : undefined;
  const refs = Array.isArray(requirements?.product_images?.value) ? requirements.product_images.value : [];
  const refsKey = JSON.stringify(refs);
  useEffect(() => {
    let active = true; setMedia(null); setSelected(null); setFeedback(null); operation.current++;
    if (refs.length && model.connected) void model.invoke<RequirementMedia>('get_requirement_media', { case_id: current.case_id }).then(result => { if (active && result.case_id === current.case_id) setMedia(result); }).catch(() => {});
    return () => { active = false; };
  }, [current.case_id, current.conversation_id, current.requirements_revision, refsKey, model.connected, model.invoke]);
  const images = media?.items.filter(item => refs.includes(item.media_id) && item.status === 'ready' && safeImage(item.data_url)) ?? [];
  const present = fields.filter(([key]) => { const value = requirements?.[key]?.value; return Array.isArray(value) ? value.length > 0 : typeof value === 'string' && !!value.trim(); });
  if (!present.length) return <p className="requirements-empty">当前没有采购需求</p>;
  const status = (field: string) => feedback?.owner === owner && feedback.field === field && <small role="status">{feedback.message}</small>;
  return <div className="canonical-requirements">{present.map(([key, label]) => {
    const value = requirements![key].value;
    if (key === 'product_images') return <div className="canonical-field" key={key}><strong>{label}</strong><div>{images.map((item, index) => <button className="requirement-image-button" type="button" key={item.media_id} onClick={event => { event.currentTarget.focus(); setSelected(index); }} onContextMenu={event => { event.preventDefault(); const source = owner; copyImageMenu(model.bridge, item.image_ref, message => { if (ownerRef.current === source) setFeedback({ owner: source, field: key, message }); }); }} aria-label="放大查看产品图片"><img src={item.data_url} alt="产品图片" /></button>)}{!images.length && <span>图片暂不可用</span>}{status(key)}</div></div>;
    if (typeof value !== 'string') return null;
    const copy = () => report(key, () => { const bridge = model.bridge as CopyBridge | undefined; if (!bridge?.copyProductText) return Promise.reject(); return bridge.copyProductText({ field: key as 'product_id' | 'product_url', case_id: current.case_id }); }, `${label}已复制`, '复制失败，请选择文字复制');
    return <div className="canonical-field" key={key}><strong>{label}</strong><div>{key === 'product_url' ? <><button type="button" className="product-url-text" onClick={copy}>{value}</button><button type="button" className="source-link" title="在浏览器打开产品链接" aria-label="在浏览器打开产品链接" onClick={() => report(key, () => model.bridge?.openProductLink ? model.bridge.openProductLink({ case_id: current.case_id }) : Promise.reject(), '', '打开产品链接失败')}><ExternalLink size={14} /></button></> : <span>{value}</span>}{key === 'product_id' && <button type="button" className="source-link" onClick={copy}>复制{label}</button>}{status(key)}</div></div>;
  })}{(current.product_main_image || typeof requirements?.product_id?.value === 'string' && requirements.product_id.value.trim()) && <ProductMainImage key={owner} caseId={current.case_id} localProductId={current.product_main_image?.local_product_id ?? current.local_product_id} metadata={current.product_main_image} invoke={model.invoke} bridge={model.bridge} canRequest={/^[0-9]+$/.test(String(requirements?.product_id?.value ?? ''))} emptyLabel={/^[0-9]+$/.test(String(requirements?.product_id?.value ?? '')) ? '暂无商品图片' : '无平台主图'} />}{selected !== null && <OriginalImageDialog bridge={model.bridge} images={images.map(item => ({ ref: item.image_ref, label: '产品图片' }))} initial={selected} onClose={() => setSelected(null)} />}</div>;
}
