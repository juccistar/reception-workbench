import { useEffect, useRef, useState } from 'react';
import type { ApiMethod, ProductMainImage as ImageState, ReceptionBridge } from '../../shared/api';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

type Invoke = ReceptionBridge['invoke'];
type ImageMetadata = ImageState & { job_id?: string | null; error_code?: string | null; stage?: string | null; available_count?: number; message?: string };
const safePreview = (value: unknown): value is string => typeof value === 'string' && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) && value.length <= 1400000;

export function ProductMainImage({ localProductId, caseId, metadata, invoke, bridge = window.reception, emptyLabel = '暂无商品图片', canRequest = true, presentation = 'gallery' }: { localProductId?: string | null; caseId?: string | null; metadata?: ImageMetadata | null; invoke?: Invoke; bridge?: ReceptionBridge; emptyLabel?: string; canRequest?: boolean; presentation?: 'gallery' | 'requirement' }) {
  const owner = JSON.stringify([caseId, localProductId]);
  const ownerRef = useRef({ owner, generation: 0 });
  if (ownerRef.current.owner !== owner) ownerRef.current = { owner, generation: ownerRef.current.generation + 1 };
  const [loaded, setLoaded] = useState<{ key: string; result: ImageMetadata } | null>(null);
  const [request, setRequest] = useState<{ owner: string; jobId: string | null } | null>(null);
  const [requestError, setRequestError] = useState<{ owner: string; message: string } | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const productId = localProductId || metadata?.local_product_id;
  const requestKey = JSON.stringify([owner, productId, metadata?.status, metadata?.updated_at, metadata?.job_id, metadata?.stage]);
  const result = loaded?.key === requestKey ? loaded.result : null;
  const activeRequest = request?.owner === owner ? request : null;
  const acknowledged = !!activeRequest?.jobId && metadata?.job_id === activeRequest.jobId;
  const waiting = !!activeRequest && (!acknowledged || ['loading', 'queued', 'running', 'not_loaded'].includes(metadata?.status ?? ''));
  const state = waiting ? null : result ?? metadata;
  const candidates = waiting ? [] : (state?.images?.length ? state.images.slice(0, 5) : state ? [state] : []).filter(item => safePreview(item.data_url));
  const [previewIndex, setPreviewIndex] = useState(0);
  const preview = candidates[Math.min(previewIndex, Math.max(0, candidates.length - 1))];
  useEffect(() => { setSelected(null); setPreviewIndex(0); setNotice(''); }, [owner, metadata?.updated_at]);
  useEffect(() => {
    let active = true;
    if (canRequest && productId && invoke && !waiting) void invoke<ImageState>('get_product_main_image', { local_product_id: productId }).then(value => { if (active) setLoaded({ key: requestKey, result: value }); }, () => { if (active) setLoaded({ key: requestKey, result: { status: 'failed' } }); });
    return () => { active = false; };
  }, [productId, requestKey, invoke, waiting, canRequest]);
  const start = async () => {
    if (!invoke || activeRequest && waiting || !canRequest || (!caseId && !localProductId)) return;
    const source = owner; const generation = ownerRef.current.generation;
    setRequest({ owner: source, jobId: null }); setRequestError(null); setSelected(null); setNotice(''); setLoaded(null);
    try {
      const result = await invoke<{ job_id: string }>('request_product_images' as ApiMethod, { ...(caseId ? { case_id: caseId } : { local_product_id: localProductId }), client_request_id: crypto.randomUUID() });
      if (ownerRef.current.owner === source && ownerRef.current.generation === generation) setRequest({ owner: source, jobId: result.job_id });
    } catch {
      if (ownerRef.current.owner === source && ownerRef.current.generation === generation) { setRequest(null); setRequestError({ owner: source, message: '商品图片读取请求失败，请重试' }); }
    }
  };
  const detail = state?.message || metadata?.message;
  const busy = waiting || state?.status === 'loading';
  const loadingLabel = (state?.stage || metadata?.stage) === 'download' ? '正在下载商品图片' : '正在读取商品资料';
  const failureLabels: Record<string, string> = { product_display_not_found: '当前账号无法读取此商品', product_display_empty: '暂未取得商品资料', product_display_invalid_response: '商品图片资料暂不可用', product_image_download_failed: '商品图片下载失败' };
  const message = busy ? loadingLabel : state?.status === 'failed' ? failureLabels[state.error_code || ''] || (detail === '商品图片下载失败' ? detail : '商品图片读取失败') : state?.status === 'unknown' ? '商品图片读取未完成' : state?.status === 'unavailable' && detail === '当前商品未提供图片' ? detail : state?.status === 'not_loaded' ? '尚未读取商品图片' : !state && caseId && !productId ? '商品图片尚未准备' : emptyLabel;
  const statusContent = <span className="image-loading-status">{busy && <span className="tiny-spinner" aria-hidden="true" />}{message}</span>;
  const partial = candidates.length > 0 && state?.status === 'ready' && detail === '部分商品图片读取失败' ? detail : '';
  if (presentation === 'requirement') return <div className="requirement-product-images" aria-busy={busy}>
    {preview ? <><button type="button" className="requirement-product-visual" aria-label="查看需求商品原图" onClick={() => setSelected(Math.min(previewIndex, candidates.length - 1))} onContextMenu={event => { event.preventDefault(); copyImageMenu(bridge, preview.image_ref, setNotice); }}><img src={preview.data_url} alt="需求商品参考图" /></button><div className="requirement-product-thumbnails" aria-label="商品参考图">{candidates.map((item, index) => <button type="button" key={index} className="requirement-product-thumb" aria-label={`查看商品图片 ${index + 1}`} aria-pressed={previewIndex === index} onClick={() => setPreviewIndex(index)}><img src={item.data_url} alt={`商品参考视角 ${index + 1}`} /></button>)}</div></> : <div className="requirement-product-unavailable" role="status" aria-live="polite">{statusContent}{canRequest && (caseId || localProductId) && <button type="button" className="source-link" disabled={!invoke || waiting} onClick={() => void start()}>{waiting ? '正在采集…' : '读取图片'}</button>}</div>}
    {partial && <small>{partial}</small>}{requestError?.owner === owner && <small role="status">{requestError.message}</small>}{notice && <small role="status">{notice}</small>}
    {selected !== null && !waiting && <OriginalImageDialog dialogLabel="商品图片" bridge={bridge} images={candidates.map((item, index) => ({ ref: item.image_ref, label: `商品图片 ${index + 1}` }))} initial={selected} onClose={() => setSelected(null)} />}
  </div>;
  return <div className="product-main-image" aria-busy={busy}><strong>商品图片</strong><div className="product-image-gallery">{candidates.map((item, index) => <button key={`${index}:${item.data_url?.slice(-48)}`} className="requirement-image-button" type="button" aria-label={`放大查看商品图片 ${index + 1}`} onClick={event => { event.currentTarget.focus(); setSelected(index); }} onContextMenu={event => { event.preventDefault(); const source = owner; const generation = ownerRef.current.generation; copyImageMenu(bridge, item.image_ref, value => { if (ownerRef.current.owner === source && ownerRef.current.generation === generation) setNotice(value); }); }}><img src={item.data_url} alt={`商品图片 ${index + 1}`} /></button>)}{!candidates.length && <span role="status" aria-live="polite">{statusContent}</span>}</div>{partial && <small>{partial}</small>}{canRequest && (caseId || localProductId) && <button type="button" className="source-link" disabled={!invoke || waiting} onClick={() => void start()}>{waiting ? '正在采集…' : candidates.length || state?.status === 'failed' || state?.status === 'unavailable' ? '重新采集' : '读取图片'}</button>}{requestError?.owner === owner && <small role="status">{requestError.message}</small>}{notice && <small role="status">{notice}</small>}{selected !== null && !waiting && <OriginalImageDialog dialogLabel="商品图片" bridge={bridge} images={candidates.map((item, index) => ({ ref: item.image_ref, label: `商品图片 ${index + 1}` }))} initial={selected} onClose={() => setSelected(null)} />}</div>;
}
