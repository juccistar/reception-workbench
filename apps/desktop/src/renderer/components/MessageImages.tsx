import { useCallback, useEffect, useRef, useState } from 'react';
import type { ImageReference } from '../../shared/media';
import { Paperclip } from 'lucide-react';
import type { MessageImage, ReceptionBridge } from '../../shared/api';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

const safePreview = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  const match = /^data:image\/(?:png|jpeg);base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match || match[1].length % 4 !== 0) return false;
  const padding = match[1].endsWith('==') ? 2 : match[1].endsWith('=') ? 1 : 0;
  return match[1].length / 4 * 3 - padding <= 1024 * 1024;
};

type Props = { attachments: Record<string, unknown>[]; conversationId: string; messageId: string; role: 'buyer' | 'seller'; autoLoadIndices: number[]; revision: number; bridge?: ReceptionBridge };

type RegisteredImage = { index: number; ref: ImageReference };

function ImageCard({ attachment, conversationId, messageId, role, autoLoad, revision, bridge, index, gallery, register }: Omit<Props, 'attachments' | 'autoLoadIndices'> & { attachment: Record<string, unknown>; autoLoad: boolean; index: number; gallery: RegisteredImage[]; register: (index: number, ref?: ImageReference) => void }) {
  const sourceVersion = typeof attachment.source_version === 'string' ? attachment.source_version : '';
  const [image, setImage] = useState<MessageImage>({ status: 'not_loaded', source_version: sourceVersion, message: '' });
  const [dialog, setDialog] = useState<{ images: { ref?: ImageReference; label: string }[]; initial: number } | null>(null);
  const [copyStatus, setCopyStatus] = useState('');
  const refreshRef = useRef<() => void>(() => {});
  const loadRef = useRef<(retry: boolean) => void>(() => {});
  const autoRef = useRef(autoLoad);
  autoRef.current = autoLoad;

  useEffect(() => {
    let active = true, fetching = false, dirty = false, loading = false, autoAttempted = false;
    const params = { conversation_id: conversationId, message_id: messageId, source_version: sourceVersion };
    setDialog(null);
    setImage({ status: 'not_loaded', source_version: sourceVersion, message: '' });
    const load = (retry: boolean) => {
      if (!active || loading || !bridge || !sourceVersion) return;
      loading = true;
      if (fetching) dirty = true;
      setImage({ status: 'loading', source_version: sourceVersion, message: '' });
      void bridge.invoke('load_message_image', { ...params, ...(retry ? { retry: true } : {}) }).then(() => {
        if (active) { loading = false; refresh(); }
      }).catch(() => {
        if (active) { loading = false; setImage({ status: 'failed', source_version: sourceVersion, message: '' }); }
      });
    };
    const refresh = () => {
      if (!active) return;
      if (!bridge || !sourceVersion) { setImage({ status: 'unavailable', source_version: sourceVersion, message: '' }); return; }
      if (fetching) { dirty = true; return; }
      fetching = true;
      void bridge.invoke<MessageImage>('get_message_image', params).then(result => {
        if (!active || dirty) return;
        if (!result || result.source_version !== sourceVersion) {
          setImage({ status: 'unavailable', source_version: sourceVersion, message: '' });
          return;
        }
        if (loading && result.status === 'not_loaded') return;
        setImage(result);
        if (result.status === 'failed' || result.status === 'unavailable') autoAttempted = true;
        if (result.status === 'not_loaded' && autoRef.current && !autoAttempted) { autoAttempted = true; load(false); }
      }).catch(() => {
        if (active && !dirty) { autoAttempted = true; setImage({ status: 'failed', source_version: sourceVersion, message: '' }); }
      }).finally(() => {
        fetching = false;
        if (active && dirty) { dirty = false; refresh(); }
      });
    };
    refreshRef.current = refresh;
    loadRef.current = load;
    refresh();
    return () => { active = false; refreshRef.current = () => {}; loadRef.current = () => {}; };
  }, [bridge, conversationId, messageId, sourceVersion]);

  useEffect(() => { refreshRef.current(); }, [revision, autoLoad]);

  const preview = image.status === 'ready' && image.source_version === sourceVersion && safePreview(image.data_url) ? image.data_url : null;
  const label = role === 'buyer' ? '客户发送的照片' : '商家发送的照片';
  const imageRefKey = preview && image.image_ref ? JSON.stringify(image.image_ref) : '';
  useEffect(() => {
    register(index, imageRefKey ? image.image_ref : undefined);
    return () => register(index);
  }, [register, index, imageRefKey]);
  const status = image.status === 'ready' ? (preview ? '图片已加载' : '图片预览不可用') : ({ not_loaded: '图片尚未加载', loading: '图片加载中…', failed: '图片加载失败', unavailable: '图片暂不可用' } as const)[image.status];
  const observation = image.input_mode !== 'direct' && image.vision_status === 'ready' && typeof image.vision_description === 'string' && image.vision_description.trim() && image.vision_description.length <= 2000 ? image.vision_description : null;
  const visionLabel = image.input_mode === 'direct' ? '生成回复时结合此图片' : image.vision_status === 'processing' ? 'AI 正在识别照片…' : observation ? '图片观察 · 待客户确认' : image.vision_status === 'failed' || image.vision_status === 'unavailable' ? '暂未识别，可结合文字或人工确认' : '尚未识别';
  return <div className="message-image-notice"><strong>{label}</strong><small aria-live="polite">{status}</small>
    {preview && (role === 'buyer' || image.input_mode === 'direct') && <div className="message-image-observation"><small aria-live="polite">{visionLabel}</small>{observation && <p>{observation}</p>}</div>}
    {preview && <><button type="button" className="message-image-preview" onClick={event => {
      event.currentTarget.focus();
      const initial = image.image_ref ? gallery.findIndex(item => item.ref.sha256 === image.image_ref?.sha256) : -1;
      setDialog(initial < 0 ? { images: [{ ref: image.image_ref, label }], initial: 0 } : { images: gallery.map(item => ({ ref: item.ref, label })), initial });
    }} onContextMenu={event => { event.preventDefault(); copyImageMenu(bridge, image.image_ref, setCopyStatus); }} aria-label={`放大查看${label}`}><img src={preview} alt={label} /></button>{dialog && <OriginalImageDialog bridge={bridge} images={dialog.images} initial={dialog.initial} onClose={() => setDialog(null)} />}</>}{copyStatus && <small role="status">{copyStatus}</small>}
    {(image.status === 'not_loaded' || image.status === 'failed') && <button type="button" className="message-image-load" onClick={() => loadRef.current(true)}>{image.status === 'failed' ? '重试加载图片' : '加载图片'}</button>}
  </div>;
}

export function MessageImages({ attachments, autoLoadIndices, ...props }: Props) {
  const [registered, setRegistered] = useState<Record<number, ImageReference>>({});
  const register = useCallback((index: number, ref?: ImageReference) => setRegistered(previous => {
    if (JSON.stringify(previous[index] ?? null) === JSON.stringify(ref ?? null)) return previous;
    const next = { ...previous };
    if (ref) next[index] = ref; else delete next[index];
    return next;
  }), []);
  const seen = new Set<string>();
  const gallery = Object.entries(registered).map(([index, ref]) => ({ index: Number(index), ref })).sort((a, b) => a.index - b.index).filter(item => {
    if (seen.has(item.ref.sha256)) return false;
    seen.add(item.ref.sha256); return true;
  });
  return <div className="message-attachments">{attachments.map((attachment, index) => {
    if (attachment.kind === 'image') return <ImageCard key={`${String(attachment.attachment_id ?? index)}:${String(attachment.source_version ?? '')}`} index={index} gallery={gallery} register={register} attachment={attachment} autoLoad={autoLoadIndices.includes(index)} {...props} />;
    const name = typeof attachment.name === 'string' ? attachment.name : typeof attachment.filename === 'string' ? attachment.filename : '附件';
    return <div key={index}><Paperclip size={14} /><span>{name}</span><small>仅附件记录 · 未下载</small></div>;
  })}</div>;
}
