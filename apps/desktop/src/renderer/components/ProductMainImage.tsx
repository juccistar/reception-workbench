import { useEffect, useState } from 'react';
import type { ProductMainImage as ImageState, ReceptionBridge } from '../../shared/api';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

type Invoke = ReceptionBridge['invoke'];
const safePreview = (value: unknown): value is string => typeof value === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1400000;

export function ProductMainImage({ localProductId, metadata, invoke, bridge = window.reception, emptyLabel = '暂无商品图片' }: { localProductId?: string | null; metadata?: ImageState | null; invoke?: Invoke; bridge?: ReceptionBridge; emptyLabel?: string }) {
  const [loaded, setLoaded] = useState<{ key: string; result: ImageState } | null>(null);
  const requestKey = JSON.stringify([localProductId, metadata?.status, metadata?.updated_at]);
  const result = loaded?.key === requestKey ? loaded.result : null;
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const state = result ?? metadata;
  const candidates = (result?.images?.length ? result.images.slice(0, 5) : result ? [result] : []).filter(item => safePreview(item.data_url));
  useEffect(() => {
    let active = true; setSelected(null);
    if (localProductId && invoke) void invoke<ImageState>('get_product_main_image', { local_product_id: localProductId }).then(value => { if (active) setLoaded({ key: requestKey, result: value }); }, () => { if (active) setLoaded({ key: requestKey, result: { status: 'failed' } }); });
    return () => { active = false; };
  }, [localProductId, requestKey, invoke]);
  const message = state?.status === 'loading' ? '正在读取商品图片' : state?.status === 'failed' ? '商品图片读取失败' : state?.status === 'unknown' ? '商品图片读取未完成' : emptyLabel;
  return <div className="product-main-image"><strong>商品图片</strong><div className="product-image-gallery">{candidates.map((item, index) => <button key={`${index}:${item.data_url?.slice(-48)}`} className="requirement-image-button" type="button" aria-label={`放大查看商品图片 ${index + 1}`} onClick={event => { event.currentTarget.focus(); setSelected(index); }} onContextMenu={event => { event.preventDefault(); copyImageMenu(bridge, item.image_ref, setNotice); }}><img src={item.data_url} alt={`商品图片 ${index + 1}`} /></button>)}{!candidates.length && <span>{message}</span>}</div>{notice && <small role="status">{notice}</small>}{selected !== null && <OriginalImageDialog dialogLabel="商品图片" bridge={bridge} images={candidates.map((item, index) => ({ ref: item.image_ref, label: `商品图片 ${index + 1}` }))} initial={selected} onClose={() => setSelected(null)} />}</div>;
}
