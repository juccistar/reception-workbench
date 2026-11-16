import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Copy, ExternalLink, Package } from 'lucide-react';
import type { Case, RequirementMedia, ReceptionBridge } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { ProductMainImage } from './ProductMainImage';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

const fields = [ ['quantity', '数量'], ['specifications', '规格'], ['customization', '定制'], ['destination', '收货地'], ['delivery', '交期'], ['budget', '预算'], ['packaging', '包装'], ['certifications', '认证'], ['contact', '联系方式'], ['product_images', '产品图片'], ['product_url', '完整链接'] ] as const;
const mainFields = new Set(['quantity', 'specifications', 'customization', 'destination', 'delivery']);
const safeImage = (value: unknown): value is string => typeof value === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1400000;
type CopyBridge = ReceptionBridge & { copyProductText?: (params: { field: 'product_id' | 'product_url'; case_id: string }) => Promise<void> };

export function SharedRequirementCard({ model, current, locate, actions = false, onOpenProduct }: { model: Workbench; current: Case; locate?: (id: string) => void; actions?: boolean; onOpenProduct?: (id: string) => void }) {
  const [media, setMedia] = useState<RequirementMedia | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [feedback, setFeedback] = useState<{ owner: string; field: string; message: string } | null>(null);
  const owner = `${current.conversation_id}:${current.case_id}`;
  const ownerRef = useRef(owner); ownerRef.current = owner;
  const operation = useRef(0);
  const report = (field: string, action: () => Promise<void>, success: string, failure: string) => {
    const source = owner; const serial = ++operation.current;
    void action().then(() => { if (ownerRef.current === source && serial === operation.current) setFeedback({ owner: source, field, message: success }); }, () => { if (ownerRef.current === source && serial === operation.current) setFeedback({ owner: source, field, message: failure }); });
  };
  const requirements = current.requirements_format === 'ai-v1' ? current.requirements : undefined;
  const text = (field: string) => typeof requirements?.[field]?.value === 'string' ? requirements[field].value as string : '';
  const refs = Array.isArray(requirements?.product_images?.value) ? requirements.product_images.value : [];
  const refsKey = JSON.stringify(refs);
  useEffect(() => {
    let active = true; setMedia(null); setSelected(null); setExpanded(false); setFeedback(null); operation.current++;
    if (refs.length && model.connected) void model.invoke<RequirementMedia>('get_requirement_media', { case_id: current.case_id }).then(result => { if (active && result.case_id === current.case_id) setMedia(result); }).catch(() => {});
    return () => { active = false; };
  }, [current.case_id, current.conversation_id, current.requirements_revision, refsKey, model.connected, model.invoke]);
  const images = media?.items.filter(item => refs.includes(item.media_id) && item.status === 'ready' && safeImage(item.data_url)) ?? [];
  const present = fields.filter(([key]) => { const value = requirements?.[key]?.value; return Array.isArray(value) ? value.length > 0 : typeof value === 'string' && !!value.trim(); });
  if (!text('product') && !present.length) return <p className="requirements-empty">当前没有采购需求</p>;
  const status = (field: string) => feedback?.owner === owner && feedback.field === field && <small role="status">{feedback.message}</small>;
  const sourceButtons = (field: string) => locate && requirements?.[field]?.evidence_ids?.map(id => <button type="button" className="source-link" key={id} onClick={() => locate(id)}>来源</button>);
  const copy = (field: 'product_id' | 'product_url') => report(field, () => {
    const bridge = model.bridge as CopyBridge | undefined;
    return bridge?.copyProductText ? bridge.copyProductText({ field, case_id: current.case_id }) : Promise.reject();
  }, field === 'product_id' ? '商品编号已复制' : '商品链接已复制', '复制失败，请选择文字复制');
  const hasPlatformImages = !!current.product_main_image || !!text('product_id').trim();
  return <div className="shared-requirement-card">
    {hasPlatformImages && <ProductMainImage key={owner} presentation="requirement" caseId={current.case_id} localProductId={current.product_main_image?.local_product_id ?? current.local_product_id} metadata={current.product_main_image} invoke={model.invoke} bridge={model.bridge} canRequest={/^[0-9]+$/.test(text('product_id'))} emptyLabel={/^[0-9]+$/.test(text('product_id')) ? '暂无商品图片' : '无平台主图'} />}
    {text('product') && <h3 className="requirement-product-title">{text('product')}</h3>}
    {text('specifications') && <p className="requirement-product-subtitle">{text('specifications')}</p>}
    {(text('product_url') || text('product_id')) && <div className="requirement-product-links">
      {text('product_url') && <button type="button" className="text-button" title={text('product_url')} onClick={() => report('product_url', () => model.bridge?.openProductLink ? model.bridge.openProductLink({ case_id: current.case_id }) : Promise.reject(), '', '打开商品链接失败')}><ExternalLink size={11} />商品链接</button>}
      {text('product_id') && <button type="button" className="text-button product-id-copy" aria-label="复制商品编号" onClick={() => copy('product_id')}>{text('product_id')}<Copy size={11} /></button>}
    </div>}
    {status('product_id')}{status('product_url')}
    <div className="canonical-requirements">{present.filter(([key]) => expanded || mainFields.has(key)).map(([key, label]) => {
      const value = requirements![key].value;
      if (key === 'product_images') return <div className="canonical-field" key={key}><strong>{label}</strong><div>{images.map((item, index) => <button className="requirement-image-button" type="button" key={item.media_id} onClick={event => { event.currentTarget.focus(); setSelected(index); }} onContextMenu={event => { event.preventDefault(); const source = owner; copyImageMenu(model.bridge, item.image_ref, message => { if (ownerRef.current === source) setFeedback({ owner: source, field: key, message }); }); }} aria-label="放大查看产品图片"><img src={item.data_url} alt="产品图片" /></button>)}{!images.length && <span>图片暂不可用</span>}{sourceButtons(key)}{status(key)}</div></div>;
      if (typeof value !== 'string') return null;
      return <div className="canonical-field" key={key}><strong>{label}</strong><div>{key === 'product_url' ? <button type="button" className="product-url-text" onClick={() => copy('product_url')}>{value}</button> : <span className={key === 'quantity' ? 'requirement-quantity' : undefined}>{value}</span>}{sourceButtons(key)}</div></div>;
    })}</div>
    <button type="button" className="requirement-more-fields" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起更多需求' : '更多需求'}<ChevronDown size={12} /></button>
    {actions && <button type="button" className="requirement-procurement-link" onClick={() => { const id = current.local_product_id || current.product_main_image?.local_product_id; if (id && onOpenProduct) onOpenProduct(id); else void model.navigate('products'); }}><Package size={13} />查看商品与供货方案<ChevronRight size={12} /></button>}
    {selected !== null && <OriginalImageDialog bridge={model.bridge} images={images.map(item => ({ ref: item.image_ref, label: '产品图片' }))} initial={selected} onClose={() => setSelected(null)} />}
  </div>;
}
