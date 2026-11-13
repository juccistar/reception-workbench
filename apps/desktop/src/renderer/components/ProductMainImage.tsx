import { useEffect, useRef, useState } from 'react';
import type { ProductMainImage as ImageState, ReceptionBridge } from '../../shared/api';

type Invoke = ReceptionBridge['invoke'];
const safePreview = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  const match = /^data:image\/(?:png|jpeg);base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match || match[1].length % 4) return false;
  const padding = match[1].endsWith('==') ? 2 : match[1].endsWith('=') ? 1 : 0;
  return match[1].length / 4 * 3 - padding <= 1024 * 1024;
};

/** Reads an already cached local preview. This component never starts an external lookup. */
export function ProductMainImage({ localProductId, metadata, invoke, emptyLabel = '暂无可用商品主图' }: { localProductId?: string | null; metadata?: ImageState | null; invoke?: Invoke; emptyLabel?: string }) {
  const [loaded, setLoaded] = useState<{ key: string; result: ImageState } | null>(null);
  const requestKey = JSON.stringify([localProductId, metadata?.status, metadata?.updated_at]);
  const result = loaded?.key === requestKey ? loaded.result : null;
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null); const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let active = true; setOpen(false);
    if (localProductId && invoke) void invoke<ImageState>('get_product_main_image', { local_product_id: localProductId }).then(value => { if (active) setLoaded({ key: requestKey, result: value }); }, () => { if (active) setLoaded({ key: requestKey, result: { status: 'failed' } }); });
    return () => { active = false; };
  }, [localProductId, requestKey, invoke]);
  useEffect(() => {
    if (!open) return;
    close.current?.focus();
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); } if (event.key === 'Tab') { event.preventDefault(); close.current?.focus(); } };
    document.addEventListener('keydown', key, true);
    return () => { document.removeEventListener('keydown', key, true); trigger.current?.focus(); };
  }, [open]);
  const state = result ?? metadata;
  const preview = result?.status === 'ready' && safePreview(result.data_url) ? result.data_url : null;
  const message = state?.status === 'loading' ? '正在读取商品主图' : state?.status === 'failed' ? '商品主图读取失败' : state?.status === 'unknown' ? '商品主图读取结果未知' : emptyLabel;
  return <div className="product-main-image"><strong>商品主图</strong><div>{preview ? <button ref={trigger} className="requirement-image-button" type="button" aria-label="放大查看商品主图" onClick={() => setOpen(true)}><img src={preview} alt="商品主图" /></button> : <span>{message}</span>}{preview && state?.message && <span>{state.message}</span>}{open && preview && <div className="image-dialog-backdrop" onClick={() => setOpen(false)}><div className="image-dialog" role="dialog" aria-modal="true" aria-label="商品主图" onClick={event => event.stopPropagation()}><button type="button" ref={close} onClick={() => setOpen(false)}>关闭图片</button><img src={preview} alt="商品主图" /></div></div>}</div></div>;
}
