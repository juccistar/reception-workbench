import { useEffect, useRef, useState } from 'react';
import { Paperclip } from 'lucide-react';
import type { MessageImage, ReceptionBridge } from '../../shared/api';

const safePreview = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  const match = /^data:image\/(?:png|jpeg);base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match || match[1].length % 4 !== 0) return false;
  const padding = match[1].endsWith('==') ? 2 : match[1].endsWith('=') ? 1 : 0;
  return match[1].length / 4 * 3 - padding <= 1024 * 1024;
};

type Props = { attachments: Record<string, unknown>[]; conversationId: string; messageId: string; role: 'buyer' | 'seller'; autoLoad: boolean; revision: number; bridge?: ReceptionBridge };

function ImageCard({ attachment, conversationId, messageId, role, autoLoad, revision, bridge }: Omit<Props, 'attachments'> & { attachment: Record<string, unknown> }) {
  const sourceVersion = typeof attachment.source_version === 'string' ? attachment.source_version : '';
  const [image, setImage] = useState<MessageImage>({ status: 'not_loaded', source_version: sourceVersion, message: '' });
  const [dialog, setDialog] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const refreshRef = useRef<() => void>(() => {});
  const loadRef = useRef<(retry: boolean) => void>(() => {});
  const autoRef = useRef(autoLoad);
  autoRef.current = autoLoad;

  useEffect(() => {
    let active = true, fetching = false, dirty = false, loading = false, autoAttempted = false;
    const params = { conversation_id: conversationId, message_id: messageId, source_version: sourceVersion };
    setDialog(false);
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
  useEffect(() => {
    if (!dialog) return;
    closeRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setDialog(false); }
      if (event.key === 'Tab') { event.preventDefault(); closeRef.current?.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); triggerRef.current?.focus(); };
  }, [dialog]);

  const preview = image.status === 'ready' && image.source_version === sourceVersion && safePreview(image.data_url) ? image.data_url : null;
  const label = role === 'buyer' ? '客户发送的照片' : '商家发送的照片';
  const status = image.status === 'ready' ? (preview ? '图片已加载' : '图片预览不可用') : ({ not_loaded: '图片尚未加载', loading: '图片加载中…', failed: '图片加载失败', unavailable: '图片暂不可用' } as const)[image.status];
  const observation = image.vision_status === 'ready' && typeof image.vision_description === 'string' && image.vision_description.trim() && image.vision_description.length <= 2000 ? image.vision_description : null;
  const visionLabel = image.vision_status === 'processing' ? 'AI 正在识别照片…' : observation ? '图片观察 · 待客户确认' : image.vision_status === 'failed' || image.vision_status === 'unavailable' ? '暂未识别，可结合文字或人工确认' : '尚未识别';
  return <div className="message-image-notice"><strong>{label}</strong><small aria-live="polite">{status}</small>
    {role === 'buyer' && preview && <div className="message-image-observation"><small aria-live="polite">{visionLabel}</small>{observation && <p>{observation}</p>}</div>}
    {preview && <><button type="button" ref={triggerRef} className="message-image-preview" onClick={() => setDialog(true)} aria-label={`放大查看${label}`}><img src={preview} alt={label} /></button>{dialog && <div className="image-dialog-backdrop"><div className="image-dialog" role="dialog" aria-modal="true" aria-label={label}><button type="button" ref={closeRef} onClick={() => setDialog(false)}>关闭图片</button><img src={preview} alt={label} /></div></div>}</>}
    {(image.status === 'not_loaded' || image.status === 'failed') && <button type="button" className="message-image-load" onClick={() => loadRef.current(true)}>{image.status === 'failed' ? '重试加载图片' : '加载图片'}</button>}
  </div>;
}

export function MessageImages({ attachments, ...props }: Props) {
  return <div className="message-attachments">{attachments.map((attachment, index) => {
    if (attachment.kind === 'image') return <ImageCard key={`${String(attachment.attachment_id ?? index)}:${String(attachment.source_version ?? '')}`} attachment={attachment} {...props} />;
    const name = typeof attachment.name === 'string' ? attachment.name : typeof attachment.filename === 'string' ? attachment.filename : '附件';
    return <div key={index}><Paperclip size={14} /><span>{name}</span><small>仅附件记录 · 未下载</small></div>;
  })}</div>;
}
