import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { FileText, Plus, X } from 'lucide-react';
import type { SupplementAttachment } from '../../shared/api';
import type { Workbench } from '../hooks/useWorkbench';
import { Button, Drawer, IconButton } from './ui';
import { copyImageMenu, OriginalImageDialog } from './OriginalImageDialog';

export function SupplementPanel({ model, conversationId: suppliedConversationId }: { model: Workbench; conversationId?: string }) {
  const conversationId = suppliedConversationId ?? model.selection.conversationId;
  const note = suppliedConversationId ? model.supplementFor(conversationId) : model.supplement;
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState<SupplementAttachment | null>(null);
  const [help, setHelp] = useState(false);
  const [notice, setNotice] = useState('');
  const helpRef = useRef<HTMLDivElement>(null);
  const depth = useRef(0);
  const text = useRef<HTMLTextAreaElement>(null);
  const available = suppliedConversationId ? !!note : !!model.detail;
  const disabled = !available || !model.connected || model.closing || !!note?.attachmentBusy;
  useEffect(() => { setPreview(null); setHelp(false); setDragging(false); depth.current = 0; }, [conversationId, model.page]);
  useEffect(() => {
    if (!help) return;
    const outside = (event: PointerEvent) => { if (!helpRef.current?.contains(event.target as Node)) setHelp(false); };
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { setHelp(false); helpRef.current?.querySelector('button')?.focus(); } };
    const peer = (event: Event) => { if ((event as CustomEvent<string>).detail !== 'attachment-help') setHelp(false); };
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', key); document.addEventListener('reception:popover', peer);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', key); document.removeEventListener('reception:popover', peer); };
  }, [help]);
  useEffect(() => {
    const preventFileNavigation = (event: DragEvent) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); };
    window.addEventListener('dragover', preventFileNavigation); window.addEventListener('drop', preventFileNavigation);
    return () => { window.removeEventListener('dragover', preventFileNavigation); window.removeEventListener('drop', preventFileNavigation); };
  }, []);
  useLayoutEffect(() => {
    if (!text.current) return;
    text.current.style.height = '0px';
    text.current.style.height = `${Math.max(86, Math.min(200, text.current.scrollHeight))}px`;
  }, [note?.body, conversationId]);
  const upload = (files: File[]) => { if (!disabled && files.length) void model.uploadSupplementFiles(conversationId, files); };
  return <section className="supplement-panel" aria-label="业务补充">
    <div className="supplement-heading"><h2>业务补充</h2>{!!note?.attachments.length && <small>{note.attachments.length} 个附件</small>}</div>
    <div className={`supplement-composer ${dragging ? 'dragging' : ''}`} onDragEnter={event => { if (!disabled && event.dataTransfer.types.includes('Files')) { event.preventDefault(); depth.current++; setDragging(true); } }} onDragOver={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = disabled ? 'none' : 'copy'; } }} onDragLeave={event => { if (event.dataTransfer.types.includes('Files')) { depth.current = Math.max(0, depth.current - 1); if (!depth.current) setDragging(false); } }} onDrop={event => { event.preventDefault(); depth.current = 0; setDragging(false); upload([...event.dataTransfer.files]); }}>
      <div className="supplement-scroll">{!!note?.attachments.length && <ul className="supplement-files">{note.attachments.map(item => <li key={item.id} className={item.status === 'failed' ? 'attachment-failed' : ''}><button type="button" className="attachment-preview" onClick={event => { event.currentTarget.focus(); setPreview(item); }} onContextMenu={event => { if (item.media_type.startsWith('image/')) { event.preventDefault(); copyImageMenu(model.bridge, item.image_ref, setNotice); } }} title={item.name}>{item.preview_data_url ? <img src={item.preview_data_url} alt={`${item.name} 预览`} /> : <FileText size={17} />}<span>{item.name}</span></button><button type="button" className="attachment-remove" disabled={disabled} aria-label={`移除 ${item.name}`} onClick={() => void model.changeSupplementAttachment(conversationId, item.id)}><X size={12} /></button>{item.error && <span className="attachment-error" role="alert">{item.error}</span>}</li>)}</ul>}
      <textarea ref={text} aria-label={suppliedConversationId ? '报价业务补充' : '业务补充内容'} disabled={!available || model.closing} value={note?.body ?? ''} onChange={event => suppliedConversationId ? model.editSupplementFor(conversationId, event.target.value) : model.editSupplement(event.target.value)} placeholder="补充报价、交期等信息，可粘贴或拖入文件…" onPaste={event => {
        const files = [...event.clipboardData.files];
        if (files.length) { event.preventDefault(); upload(files); }
        else if (!event.clipboardData.getData('text/plain') && !disabled) { event.preventDefault(); void model.pasteSupplementAttachments(conversationId); }
      }} /></div>
      <div className="supplement-toolbar"><IconButton label="添加附件" disabled={disabled} onClick={() => void model.changeSupplementAttachment(conversationId)}><Plus size={19} /></IconButton><div ref={helpRef} className="attachment-help"><button type="button" aria-label="附件格式与限制" aria-expanded={help} onClick={() => { if (!help) document.dispatchEvent(new CustomEvent('reception:popover', { detail: 'attachment-help' })); setHelp(value => !value); }}>?</button>{help && <p>图片、PDF、UTF-8 文本；最多 5 个，单个 6 MiB、合计 20 MiB。PDF 最多 10 页，须有可提取文字；单附件文字最多 16000 字。</p>}</div><small role="status">{notice || (note?.attachmentBusy ? '正在处理附件' : note?.saving ? '正在保存' : note?.dirty ? '等待保存' : note?.error ? '操作未完成' : '已保存')}</small></div>
    </div>
    {note?.error && <div className="supplement-error" role="alert"><span>{note.error}</span>{note.conflict ? <><p>最新保存内容：{note.conflict.body || '（空）'}</p><Button variant="ghost" onClick={() => model.reapplySupplement(conversationId)}>重新应用我的内容</Button></> : note.dirty && <Button variant="ghost" onClick={() => void model.saveSupplement(conversationId)}>重试保存</Button>}</div>}
    {preview && (preview.media_type.startsWith('image/') ? <OriginalImageDialog bridge={model.bridge} images={(note?.attachments ?? []).filter(item => item.media_type.startsWith('image/')).map(item => ({ ref: item.image_ref, label: item.name }))} initial={(note?.attachments ?? []).filter(item => item.media_type.startsWith('image/')).findIndex(item => item.id === preview.id)} onClose={() => setPreview(null)} /> : <Drawer title={preview.name} onClose={() => setPreview(null)} className="supplement-preview"><p>{preview.media_type} · {(preview.size / 1024 / 1024).toFixed(2)} MiB</p>{preview.error ? <p role="alert">{preview.error}</p> : <pre>{preview.text_preview || '当前附件没有可显示的文字预览。'}</pre>}{preview.text_preview && <small>提取文字预览（前 500 字）</small>}</Drawer>)}
  </section>;
}
