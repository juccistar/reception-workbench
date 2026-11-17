import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import type { ReceptionBridge } from '../../shared/api';
import type { ImageReference } from '../../shared/media';

export type GalleryImage = { ref?: ImageReference; label: string };

export function copyImageMenu(bridge: ReceptionBridge | undefined, ref: ImageReference | undefined, feedback: (message: string) => void) {
  if (!ref || !bridge?.showImageMenu) { feedback('原图不可用，无法复制图片。'); return; }
  void bridge.showImageMenu(ref).then(result => { if (result === 'copied') feedback('图片已复制'); }, () => feedback('复制原图失败，请重试。'));
}

export function OriginalImageDialog({ images, initial, bridge, onClose, dialogLabel }: { images: GalleryImage[]; initial: number; bridge?: ReceptionBridge; onClose: () => void; dialogLabel?: string }) {
  const [index, setIndex] = useState(initial);
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [originalSize, setOriginalSize] = useState(false);
  const [copying, setCopying] = useState(false);
  const [readRevision, setReadRevision] = useState(0);
  const owner = useRef({ key: '', generation: 0 });
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose); onCloseRef.current = onClose;
  const image = images[index];
  const refKey = JSON.stringify(image?.ref ?? null);
  if (owner.current.key !== refKey) owner.current = { key: refKey, generation: owner.current.generation + 1 };
  const copy = async () => {
    if (!image?.ref || !bridge?.copyImageOriginal || copying) return;
    const generation = owner.current.generation;
    setCopying(true); setError('');
    try { await bridge.copyImageOriginal(image.ref); if (owner.current.generation === generation) setError('图片已复制'); }
    catch { if (owner.current.generation === generation) setError('复制图片失败，请重试'); }
    finally { if (owner.current.generation === generation) setCopying(false); }
  };
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    return () => { owner.current.generation++; previous?.focus(); };
  }, []);
  useEffect(() => {
    let active = true; let objectUrl = '';
    setUrl(''); setError(''); setOriginalSize(false); setCopying(false);
    if (!image?.ref || !bridge?.getImageOriginal) { setError('原图不可用'); return; }
    const requestId = crypto.randomUUID();
    void bridge.getImageOriginal(image.ref, requestId).then(result => {
      if (!active) return;
      objectUrl = URL.createObjectURL(new Blob([new Uint8Array(result.bytes)], { type: result.media_type }));
      setUrl(objectUrl);
    }, () => { if (active) setError('原图读取失败或已变化'); });
    return () => { active = false; void bridge.cancelImageOriginal?.(requestId).catch(() => {}); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [bridge, refKey, readRevision]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onCloseRef.current(); }
      else if (images.length > 1 && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) { event.preventDefault(); setIndex(value => (value + (event.key === 'ArrowRight' ? 1 : images.length - 1)) % images.length); }
      else if (event.key === 'Tab') {
        const buttons = [...(dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
        if (!buttons.length) return;
        if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons[buttons.length - 1].focus(); }
        else if (!event.shiftKey && document.activeElement === buttons[buttons.length - 1]) { event.preventDefault(); buttons[0].focus(); }
      }
    };
    document.addEventListener('keydown', key, true); return () => document.removeEventListener('keydown', key, true);
  }, [images.length]);
  return <div className="image-dialog-backdrop" onClick={onClose}><div ref={dialog} className="image-dialog original-image-dialog" role="dialog" aria-modal="true" aria-label={dialogLabel || image?.label || '图片'} onClick={event => event.stopPropagation()}>
    <header className="original-image-heading"><h2>{dialogLabel || image?.label || '图片'}</h2><button ref={close} type="button" aria-label="关闭图片" onClick={onClose}><X size={18} /></button></header>
    <div className={`original-image-viewport ${originalSize ? 'original-size' : ''}`} aria-busy={!url && !error} onContextMenu={event => { event.preventDefault(); copyImageMenu(bridge, image?.ref, setError); }}>{url ? <img src={url} alt={image?.label || '图片'} /> : <div className="original-image-loading"><p className="image-loading-status" role="status">{!error && <span className="tiny-spinner" aria-hidden="true" />}{error || '正在读取原图…'}</p>{error && image?.ref && bridge?.getImageOriginal && <button type="button" onClick={() => { setError(''); setReadRevision(value => value + 1); }}>重试读取原图</button>}</div>}</div>
    {url && error && <small role="status">{error}</small>}
    {images.length > 1 && <div className="gallery-navigation"><button type="button" aria-label={dialogLabel ? `上一张${dialogLabel}` : '上一张'} onClick={() => setIndex((index + images.length - 1) % images.length)}>上一张</button><span>{index + 1} / {images.length}</span><button type="button" aria-label={dialogLabel ? `下一张${dialogLabel}` : '下一张'} onClick={() => setIndex((index + 1) % images.length)}>下一张</button></div>}
    <footer className="original-image-controls"><button type="button" className="original-image-scale" onClick={() => setOriginalSize(value => !value)}>{originalSize ? '适应窗口' : '原始尺寸'}</button><button type="button" onClick={onClose}>关闭</button><button type="button" className="primary" disabled={!url || copying || !bridge?.copyImageOriginal} onClick={() => void copy()}>{copying ? '正在复制' : '复制图片'}</button></footer>
  </div></div>;
}
