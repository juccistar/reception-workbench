import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import type { ProductMainImage as ImageState, ReceptionBridge } from '../../shared/api';

type Invoke = ReceptionBridge['invoke'];
const safePreview = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  const match = /^data:image\/(?:png|jpeg);base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match || match[1].length % 4) return false;
  const padding = match[1].endsWith('==') ? 2 : match[1].endsWith('=') ? 1 : 0;
  return match[1].length / 4 * 3 - padding <= 1024 * 1024;
};

/** Local cached gallery only; opening this component never starts an external lookup. */
export function ProductMainImage({ localProductId, metadata, invoke, emptyLabel = '暂无商品图片' }: { localProductId?: string | null; metadata?: ImageState | null; invoke?: Invoke; emptyLabel?: string }) {
  const [loaded, setLoaded] = useState<{ key: string; result: ImageState } | null>(null);
  const requestKey = JSON.stringify([localProductId, metadata?.status, metadata?.updated_at]);
  const result = loaded?.key === requestKey ? loaded.result : null;
  const [selected, setSelected] = useState<number | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const state = result ?? metadata;
  const candidates = result?.images?.length ? result.images.slice(0, 5) : result ? [result] : [];
  const previews = candidates.map(item => item.data_url).filter(safePreview);
  useEffect(() => {
    let active = true; setSelected(null);
    if (localProductId && invoke) void invoke<ImageState>('get_product_main_image', { local_product_id: localProductId }).then(value => { if (active) setLoaded({ key: requestKey, result: value }); }, () => { if (active) setLoaded({ key: requestKey, result: { status: 'failed' } }); });
    return () => { active = false; };
  }, [localProductId, requestKey, invoke]);
  const open = selected !== null && !!previews[selected];
  useEffect(() => {
    if (!open) return;
    close.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setSelected(null); }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setSelected(value => value === null ? null : (value + (event.key === 'ArrowRight' ? 1 : previews.length - 1)) % previews.length); }
      if (event.key === 'Tab') {
        const buttons = [...(dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    document.addEventListener('keydown', key, true);
    return () => { document.removeEventListener('keydown', key, true); trigger.current?.focus(); };
  }, [open, previews.length]);
  const message = state?.status === 'loading' ? '正在读取商品图片' : state?.status === 'failed' ? '商品图片读取失败' : state?.status === 'unknown' ? '商品图片读取未完成' : emptyLabel;
  return <div className="product-main-image"><strong>商品图片</strong><div className="product-image-gallery">{previews.map((preview, index) => <button key={`${index}:${preview.slice(-48)}`} className="requirement-image-button" type="button" aria-label={`放大查看商品图片 ${index + 1}`} onClick={event => { trigger.current = event.currentTarget; setSelected(index); }}><img src={preview} alt={`商品图片 ${index + 1}`} /></button>)}{!previews.length && <span>{message}</span>}</div>{open && <div className="image-dialog-backdrop" onClick={() => setSelected(null)}><div ref={dialog} className="image-dialog product-gallery-dialog" role="dialog" aria-modal="true" aria-label="商品图片" onClick={event => event.stopPropagation()}><button type="button" ref={close} onClick={() => setSelected(null)} aria-label="关闭图片"><X size={18} />关闭</button><img src={previews[selected!]} alt={`商品图片 ${selected! + 1}`} /><div className="gallery-navigation"><button type="button" aria-label="上一张商品图片" disabled={previews.length < 2} onClick={() => setSelected((selected! + previews.length - 1) % previews.length)}><ChevronLeft size={17} /></button><span>{selected! + 1} / {previews.length}</span><button type="button" aria-label="下一张商品图片" disabled={previews.length < 2} onClick={() => setSelected((selected! + 1) % previews.length)}><ChevronRight size={17} /></button></div></div></div>}</div>;
}
