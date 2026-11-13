import { useEffect, useRef, useState } from 'react';
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
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose); onCloseRef.current = onClose;
  const image = images[index];
  const refKey = JSON.stringify(image?.ref ?? null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    return () => { previous?.focus(); };
  }, []);
  useEffect(() => {
    let active = true; let objectUrl = '';
    setUrl(''); setError(''); setOriginalSize(false);
    if (!image?.ref || !bridge?.getImageOriginal) { setError('原图不可用'); return; }
    void bridge.getImageOriginal(image.ref).then(result => {
      if (!active) return;
      objectUrl = URL.createObjectURL(new Blob([new Uint8Array(result.bytes)], { type: result.media_type }));
      setUrl(objectUrl);
    }, () => { if (active) setError('原图读取失败或已变化'); });
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [bridge, refKey]);
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
    <div className="original-image-controls"><button ref={close} type="button" onClick={onClose}>关闭图片</button><button type="button" onClick={() => setOriginalSize(value => !value)}>{originalSize ? '适应窗口' : '原始尺寸'}</button></div>
    <div className={`original-image-viewport ${originalSize ? 'original-size' : ''}`} onContextMenu={event => { event.preventDefault(); copyImageMenu(bridge, image?.ref, setError); }}>{url ? <img src={url} alt={image?.label || '图片'} /> : <p role="status">{error || '正在读取原图…'}</p>}</div>
    {url && error && <small role="status">{error}</small>}
    {images.length > 1 && <div className="gallery-navigation"><button type="button" aria-label={dialogLabel ? `上一张${dialogLabel}` : '上一张'} onClick={() => setIndex((index + images.length - 1) % images.length)}>上一张</button><span>{index + 1} / {images.length}</span><button type="button" aria-label={dialogLabel ? `下一张${dialogLabel}` : '下一张'} onClick={() => setIndex((index + 1) % images.length)}>下一张</button></div>}
  </div></div>;
}
