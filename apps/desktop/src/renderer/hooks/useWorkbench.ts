import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import type { ApiMethod, ChatSendParams, ConversationDetail, Draft, ExecutorHealth, Job, JobAck, Outbox, Preferences, ReceptionBridge, ServiceEvent, Snapshot, Supplement, SupplementAttachment, SupplementAttachmentBatch, SupplementUpload } from '../../shared/api';
import { executorHealth } from '../executorHealth';

export type Page = 'home' | 'reception' | 'whatsapp' | 'handoff' | 'customers' | 'policies' | 'settings' | 'activity' | 'products' | 'suppliers' | 'procurement';
type SendStatus = Job['status'] | 'requesting' | 'retry' | 'accepted' | 'rejected';
export interface ChatSend {
  params: ChatSendParams;
  body: string;
  editVersion: number;
  status: SendStatus;
  jobId: string | null;
  message: string;
  cleared: boolean;
  batchId?: string;
}
export interface EditorState {
  body: string;
  server: Draft | null;
  dirty: boolean;
  saving: boolean;
  edited: boolean;
  editVersion: number;
  saveState: 'saved' | 'pending' | 'saving' | 'error';
  error: string;
  manualState: 'idle' | 'pending' | 'ready' | 'error';
  manualError: string;
  send: ChatSend | null;
}
const defaults: Preferences = { nav_preference: 'expanded', details_preference: 'expanded', list_width: 272, details_width: 360, requirements_ratio: 0.68 };
const isActive = (job: Job) => job.status === 'queued' || job.status === 'running';
const sendUnsettled = (send: ChatSend | null) => !!send && ['requesting', 'retry', 'queued', 'running', 'unknown'].includes(send.status);
const isSendJob = (job: Job) => ['send_chat', 'confirm_send', 'automatic_send'].includes(job.kind);
const isSendOperation = (operation: Outbox) => ['send', 'send_chat'].includes(operation.kind);
export const errorText = (error: unknown): string => {
  // Only service-approved messages cross the bridge. Never render raw diagnostics or exception objects.
  if (error instanceof Error) return error.message;
  if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') return error.message;
  return '操作未完成，请重试或查看运行记录。';
};
const draftBody = (draft: Draft | null, batches: ConversationDetail['reply_batches'] = []) => {
  const batchId = draft?.blocked_batch_id ?? draft?.batch_id;
  const partial = batches?.some(batch => batch.batch_id === batchId && batch.status === 'stopped' && batch.accepted > 0);
  return draft?.status === 'accepted' || partial && !!draft?.message_parts ? '' : draft?.body ?? '';
};
const createEditor = (draft: Draft | null, batches?: ConversationDetail['reply_batches']): EditorState => ({ body: draftBody(draft, batches), server: draft, dirty: false, saving: false, edited: false, editVersion: 0, saveState: 'saved', error: '', manualState: 'idle', manualError: '', send: null });
const mutationId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const checksReady = (snapshot: Snapshot | null, keys: (keyof Snapshot['checks'])[]) => !!snapshot && keys.every(key => snapshot.app.mode === 'demo' ? ['passed', 'demo'].includes(snapshot.checks[key].status) : snapshot.checks[key].status === 'passed');
const generationBlocker = (snapshot: Snapshot | null, detail: ConversationDetail | null): string => {
  if (!executorHealth(snapshot).ready) return `${executorHealth(snapshot).message}，正文仍可编辑保存。`;
  if (!detail) return '请先选择客户。';
  if (!snapshot || (snapshot.app.mode !== 'demo' && !snapshot.app.account_verified)) return '请先核验当前商家身份。';
  if (!checksReady(snapshot, ['ai'])) {
    const status = snapshot?.checks.ai.status;
    if (status === 'running') return '正在检查 AI 回复能力；您仍可直接手写发送。';
    if (status === 'not_checked' || !status) return '当前工作区尚未检查 AI 回复能力，可在连接与设置中检查；您仍可直接手写发送。';
    return '本次 AI 回复能力检查未通过，可在连接与设置中查看原因；您仍可直接手写发送。';
  }
  return '';
};

export function useWorkbench(bridge: ReceptionBridge | undefined) {
  const [baseSnapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [healthProjection, setHealthProjection] = useState<ExecutorHealth | null>(null);
  const healthRef = useRef<ExecutorHealth | null>(null);
  const snapshot = baseSnapshot && healthProjection ? { ...baseSnapshot, executor_health: healthProjection } : baseSnapshot;
  const observeHealth = useCallback((health: ExecutorHealth | null | undefined) => {
    if (!health || !['idle', 'running', 'waiting_external', 'recovering', 'stalled', 'stopped', 'failed'].includes(health.state) || typeof health.worker_alive !== 'boolean' || !Number.isFinite(health.observed_at_ms) || (healthRef.current && health.observed_at_ms < healthRef.current.observed_at_ms)) return;
    const previous = healthRef.current;
    const severity = (value: ExecutorHealth) => !value.worker_alive || ['failed', 'stalled', 'stopped'].includes(value.state) ? 2 : value.state === 'recovering' ? 1 : 0;
    if (previous && health.observed_at_ms === previous.observed_at_ms && severity(health) < severity(previous)) return;
    healthRef.current = health;
    setHealthProjection(health);
  }, []);
  const withLatestHealth = useCallback((value: Snapshot): Snapshot => {
    observeHealth(value.executor_health);
    return healthRef.current ? { ...value, executor_health: healthRef.current } : value;
  }, [observeHealth]);
  const [connected, setConnected] = useState(false);
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => { if (!notice || /失败|未完成|未连接|断开|异常|未知|核查|重试|未保存|尚未确认/.test(notice)) return; const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer); }, [notice]);
  const [page, setPage] = useState<Page>('reception');
  const [selection, setSelection] = useState<{ conversationId: string; caseId: string }>({ conversationId: '', caseId: '' });
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [preferences, setPreferences] = useState<Preferences>(defaults);
  const [jobs, setJobs] = useState<Job[]>([]);
  const jobsRef = useRef<Job[]>([]);
  const [pending, setPending] = useState<string[]>([]);
  const [generationRequests, setGenerationRequests] = useState<{ job_id: string; conversation_id: string; case_id: string }[]>([]);
  const [historyRequests, setHistoryRequests] = useState<{ job_id: string; conversation_id: string }[]>([]);
  const [, redraw] = useReducer((value: number) => value + 1, 0);
  // A procurement case is only context. Chat text, ownership and send attempts belong to the customer conversation.
  const editors = useRef(new Map<string, EditorState>());
  const supplements = useRef(new Map<string, { body: string; revision: number | null; dirty: boolean; version: number; error: string; saving: Promise<boolean> | null; attachments: SupplementAttachment[]; attachmentBusy: boolean }>());
  const caseOwners = useRef(new Map<string, string>());
  const dirtyForms = useRef(new Set<string>());
  const setFormDirty = useCallback((id: string, dirty: boolean) => { if (dirty) dirtyForms.current.add(id); else dirtyForms.current.delete(id); }, []);
  const canLeaveForms = useCallback(() => { if (!dirtyForms.current.size) return true; setNotice('当前表单有未保存修改，请先保存或放弃修改后再切换。'); return false; }, []);
  const saves = useRef(new Map<string, Promise<boolean>>());
  const manualRequests = useRef(new Map<string, Promise<boolean>>());
  const selectionRef = useRef(selection);
  const pinnedCase = useRef<string | null>(null);
  const detailRef = useRef(detail);
  const connectedRef = useRef(false);
  const snapshotRef = useRef(snapshot);
  const snapshotSequence = useRef(0);
  const detailSequence = useRef(0);
  const pendingRef = useRef(new Set<string>());
  const receptionIntent = useRef(0);
  const mounted = useRef(true);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scrollPositions = useRef(new Map<string, number>());
  selectionRef.current = selection;
  detailRef.current = detail;
  snapshotRef.current = snapshot;
  const editor = editors.current.get(selection.conversationId);

  const settleSend = useCallback((entry: EditorState, status: SendStatus, message = '') => {
    const attempt = entry.send;
    if (!attempt) return;
    attempt.status = status;
    attempt.message = message;
    if (entry.server?.draft_id === attempt.params.draft_id && entry.server.revision === attempt.params.expected_revision && ['accepted', 'succeeded', 'rejected', 'unknown'].includes(status)) entry.server = { ...entry.server, status: status === 'succeeded' ? 'accepted' : status as Draft['status'] };
    if ((status === 'accepted' || status === 'succeeded') && !attempt.cleared) {
      attempt.cleared = true;
      // A receipt only consumes the exact input that was sent. Typing during a send remains a new draft.
      if (entry.editVersion === attempt.editVersion && entry.body === attempt.body) {
        entry.body = ''; entry.dirty = false; entry.edited = false; entry.saveState = 'saved'; entry.error = '';
        ++entry.editVersion;
      }
    }
  }, []);

  const syncSends = useCallback((state: Snapshot | null = snapshotRef.current) => {
    for (const entry of editors.current.values()) {
      const attempt = entry.send;
      if (!attempt) continue;
      if (attempt.batchId) {
        const batch = detailRef.current?.reply_batches?.find(item => item.batch_id === attempt.batchId);
        if (batch?.status === 'accepted') settleSend(entry, 'accepted');
        else if (batch?.status === 'stopped') settleSend(entry, batch.pending_operation_ids.length ? 'unknown' : 'rejected', '分条发送已停止，请查看发送进度。');
        continue;
      }
      const job = jobsRef.current.find(item => item.job_id === attempt.jobId);
      // An operation receipt is more specific than the completion status of its job.
      const operation = state?.outbox.find(item => isSendOperation(item) && (item.client_request_id
        ? item.client_request_id === attempt.params.client_request_id
        : item.job_id ? item.job_id === attempt.jobId
          : item.draft_id === attempt.params.draft_id && item.draft_revision === attempt.params.expected_revision));
      if (operation && ['accepted', 'rejected', 'unknown'].includes(operation.status)) settleSend(entry, operation.status as SendStatus, operation.message);
      else if (job && !['accepted', 'rejected'].includes(attempt.status)) settleSend(entry, job.result?.status === 'accepted' ? 'accepted' : job.result?.status === 'rejected' ? 'rejected' : job.status, job.message);
    }
    if (mounted.current) redraw();
  }, [settleSend]);

  const updateJobs = useCallback((incoming: Job[]) => {
    const combined = new Map(jobsRef.current.map(job => [job.job_id, job]));
    for (const job of incoming) {
      const current = combined.get(job.job_id);
      if (!current || !isActive(job) || (isActive(current) && !(current.status === 'running' && job.status === 'queued'))) combined.set(job.job_id, job);
      if (job.case_id && job.conversation_id) caseOwners.current.set(job.case_id, job.conversation_id);
    }
    jobsRef.current = [...combined.values()].sort((a, b) => b.created_at - a.created_at);
    setGenerationRequests(previous => previous.filter(request => !combined.has(request.job_id)));
    setHistoryRequests(previous => previous.filter(request => !combined.has(request.job_id)));
    setJobs(jobsRef.current);
    syncSends();
  }, [syncSends]);

  const mergeDraft = useCallback((result: ConversationDetail) => {
    const key = result.conversation.conversation_id;
    for (const item of result.cases) caseOwners.current.set(item.case_id, key);
    const note = result.supplement;
    const oldNote = supplements.current.get(key);
    if (!oldNote) supplements.current.set(key, { body: note?.body ?? '', revision: note?.revision ?? null, dirty: false, version: 0, error: '', saving: null, attachments: note?.attachments ?? [], attachmentBusy: false });
    else if (!oldNote.dirty && !oldNote.saving && !oldNote.attachmentBusy && note && (note.revision ?? -1) >= (oldNote.revision ?? -1)) { oldNote.body = note.body; oldNote.revision = note.revision; oldNote.attachments = note.attachments ?? []; }
    const incoming = result.chat_draft;
    let current = editors.current.get(key);
    if (!current) { current = createEditor(incoming, result.reply_batches); editors.current.set(key, current); }
    else if (incoming && !current.saving && (!current.server || incoming.revision >= current.server.revision)) {
      if (!current.dirty && !current.edited && !current.send) { current.body = draftBody(incoming, result.reply_batches); }
      current.server = incoming;
    }
    const attempt = current.send;
    if (attempt && !attempt.batchId && incoming?.draft_id === attempt.params.draft_id && incoming.revision === attempt.params.expected_revision && ['accepted', 'rejected', 'unknown'].includes(incoming.status)) settleSend(current, incoming.status as SendStatus);
    syncSends();
  }, [settleSend, syncSends]);

  const refreshSnapshot = useCallback(async () => {
    if (closingRef.current) return null;
    const sequence = ++snapshotSequence.current;
    if (!bridge) {
      setError('未找到桌面服务桥。请从桌面应用启动此工作台，浏览器页面不能连接本地接待服务。');
      setLoading(false); return null;
    }
    try {
      const received = await bridge.invoke<Snapshot>('get_snapshot');
      if (!mounted.current || sequence !== snapshotSequence.current) return null;
      const result = withLatestHealth(received);
      connectedRef.current = true; setConnected(true);
      setSnapshot(result); snapshotRef.current = result;
      for (const item of [...result.conversations, ...(result.handoff_cases ?? [])]) if (item.case_id) caseOwners.current.set(item.case_id, item.conversation_id);
      updateJobs(result.jobs);
      setError(''); return result;
    } catch (failure) {
      if (sequence === snapshotSequence.current && mounted.current) { connectedRef.current = false; setConnected(false); setError(errorText(failure)); }
      return null;
    } finally { if (sequence === snapshotSequence.current && mounted.current) setLoading(false); }
  }, [bridge, updateJobs, withLatestHealth]);

  const loadDetail = useCallback(async (conversationId: string, caseId = '', quiet = false, refreshEmpty = false) => {
    if (!bridge || !conversationId || closingRef.current) return false;
    if (quiet && selectionRef.current.conversationId === conversationId) caseId = pinnedCase.current ?? '';
    const sequence = ++detailSequence.current;
    if (!quiet) { setDetailLoading(true); setDetailError(''); }
    try {
      const result = await bridge.invoke<ConversationDetail>('get_conversation', { conversation_id: conversationId, ...(caseId ? { case_id: caseId } : {}), ...(refreshEmpty ? { refresh_empty: true } : {}) });
      if (!mounted.current || sequence !== detailSequence.current || selectionRef.current.conversationId !== conversationId) return false;
      if (caseId && result.case?.case_id !== caseId) throw new Error('此采购事项暂时无法读取，当前正文已保留。');
      mergeDraft(result);
      setDetail(result); detailRef.current = result; syncSends();
      const nextSelection = { conversationId, caseId: result.case?.case_id ?? '' };
      selectionRef.current = nextSelection; setSelection(nextSelection); setDetailError(''); return true;
    } catch (failure) {
      if (sequence === detailSequence.current && mounted.current) setDetailError(errorText(failure));
      return false;
    } finally { if (sequence === detailSequence.current && mounted.current) setDetailLoading(false); }
  }, [bridge, mergeDraft, syncSends]);

  const markMessagesRead = useCallback(async (conversationId: string, throughMessageId: string, observedSequence: number) => {
    if (!bridge || !connectedRef.current || closingRef.current) throw new Error('阅读进度尚未保存');
    await bridge.invoke('mark_messages_read', { conversation_id: conversationId, through_message_id: throughMessageId, observed_sequence: observedSequence });
    // Fetch fresh counts instead of applying a possibly delayed acknowledgement over newer arrivals.
    if (!mounted.current || closingRef.current) return;
    void refreshSnapshot();
    if (selectionRef.current.conversationId === conversationId) void loadDetail(conversationId, selectionRef.current.caseId, true);
  }, [bridge, refreshSnapshot, loadDetail]);

  useEffect(() => {
    mounted.current = true; void refreshSnapshot();
    if (bridge) void bridge.invoke<ExecutorHealth>('get_executor_health').then(health => { if (mounted.current) observeHealth(health); }).catch(() => {});
    if (bridge) void bridge.getPreferences().then(result => { if (mounted.current) setPreferences({ ...defaults, ...result }); }).catch(() => { if (mounted.current) setNotice('布局偏好读取失败，已使用默认布局。'); });
    return () => { mounted.current = false; if (refreshTimer.current) clearTimeout(refreshTimer.current); };
  }, [bridge, refreshSnapshot, observeHealth]);

  useEffect(() => {
    if (!selection.conversationId && snapshot?.conversations.length) {
      const first = snapshot.conversations[0];
      const next = { conversationId: first.conversation_id, caseId: first.case_id ?? '' };
      selectionRef.current = next; setSelection(next); void loadDetail(next.conversationId, next.caseId);
    }
  }, [snapshot, selection.conversationId, loadDetail]);

  useEffect(() => {
    if (!bridge) return;
    return bridge.onEvent((event: ServiceEvent) => {
      const expectedDisconnect = event.event === 'backend.disconnected' && event.data !== null && typeof event.data === 'object' && 'expected' in event.data && event.data.expected === true;
      if (event.event === 'backend.closing' || expectedDisconnect) {
        closingRef.current = true; setClosing(true); ++snapshotSequence.current; ++detailSequence.current;
        if (refreshTimer.current) { clearTimeout(refreshTimer.current); refreshTimer.current = null; }
        setLoading(false); setDetailLoading(false); setError(''); setNotice(''); return;
      }
      if (closingRef.current) return;
      if (event.event === 'executor.health') { observeHealth(event.data as ExecutorHealth); return; }
      if (event.event === 'translation.settings_changed') { void refreshSnapshot(); return; }
      if (event.event === 'job.updated' && (event.data as Job)?.kind === 'translation') return;
      if (event.event === 'state.changed' && (event.data as { reason?: string } | null)?.reason === 'translations_updated') return;
      if (event.event === 'backend.disconnected') {
        ++snapshotSequence.current; ++detailSequence.current; connectedRef.current = false; setConnected(false); setDetailLoading(false);
        setError('接待服务已断开。消息和检查状态可能已过期，正文仍保留在当前窗口；请恢复连接后保存。'); return;
      }
      if (event.event === 'job.updated') {
        const job = event.data as Job;
        if (job?.job_id) {
          updateJobs([job]);
          if (job.status === 'failed' && job.conversation_id === selectionRef.current.conversationId) setNotice(job.message || '本次任务未完成，请查看运行记录。');
        }
      }
      if (event.event === 'state.changed' || (event.event === 'job.updated'
          && (!isActive(event.data as Job) || ['generate_draft', 'send_chat', 'confirm_send', 'automatic_send'].includes((event.data as Job).kind)))) {
        if (refreshTimer.current) clearTimeout(refreshTimer.current);
        refreshTimer.current = setTimeout(() => {
          void refreshSnapshot();
          const current = selectionRef.current;
          if (current.conversationId) void loadDetail(current.conversationId, current.caseId, true);
        }, 70);
      }
    });
  }, [bridge, loadDetail, refreshSnapshot, updateJobs, observeHealth]);

  const beginManualEdit = useCallback((conversationId: string): Promise<boolean> => {
    const entry = editors.current.get(conversationId);
    if (!entry) return Promise.resolve(false);
    const existing = manualRequests.current.get(conversationId);
    if (existing) return existing;
    if (entry.manualState === 'ready') return Promise.resolve(true);
    if (!bridge || !connectedRef.current || closingRef.current) {
      entry.manualState = 'error'; entry.manualError = '服务未连接，尚未暂停此客户自动接待。'; redraw(); return Promise.resolve(false);
    }
    entry.manualState = 'pending'; entry.manualError = ''; redraw();
    // Invoke immediately on input, before the autosave debounce or any other awaited work.
    const operation = (async () => {
      try {
        const result = await bridge.invoke<Snapshot>('begin_manual_edit', { conversation_id: conversationId });
        const owner = result.conversations.find(item => item.conversation_id === conversationId);
        if (!owner?.human_owned) throw new Error('尚未确认暂停此客户自动接待，请重试。');
        entry.manualState = 'ready';
        // This FAST acknowledgement owns only this customer's takeover, not a possibly newer global toggle.
        const latest = snapshotRef.current;
        if (latest) { const next = { ...latest, conversations: latest.conversations.map(item => item.conversation_id === conversationId ? { ...item, human_owned: true } : item) }; snapshotRef.current = next; if (mounted.current) setSnapshot(next); }
        if (detailRef.current?.conversation.conversation_id === conversationId) {
          const next = { ...detailRef.current, conversation: { ...detailRef.current.conversation, human_owned: true } };
          detailRef.current = next; if (mounted.current) setDetail(next);
        }
        return true;
      } catch (failure) { entry.manualState = 'error'; entry.manualError = errorText(failure); return false; }
      finally { manualRequests.current.delete(conversationId); if (mounted.current) redraw(); }
    })();
    manualRequests.current.set(conversationId, operation);
    return operation;
  }, [bridge]);

  const saveDraft = useCallback(async (conversationId: string): Promise<boolean> => {
    const existing = saves.current.get(conversationId);
    if (existing) return existing;
    const entry = editors.current.get(conversationId);
    if (!entry || !entry.dirty) return true;
    if (!bridge || !connectedRef.current || closingRef.current) { entry.saveState = 'error'; entry.error = '服务未连接，尚未保存。恢复连接后可重试。'; redraw(); return false; }
    const capturedVersion = entry.editVersion;
    const capturedBody = entry.body;
    entry.saving = true; entry.saveState = 'saving'; entry.error = ''; redraw();
    const operation = (async () => {
      try {
        if (!(await beginManualEdit(conversationId))) throw new Error(entry.manualError || '尚未暂停此客户自动接待。');
        const result = await bridge.invoke<Draft>('save_chat_draft', { conversation_id: conversationId, body: capturedBody, expected_revision: entry.server?.revision ?? null, client_mutation_id: mutationId() });
        if (result.conversation_id !== conversationId || result.purpose !== 'chat' || result.body !== capturedBody) throw new Error('服务返回的聊天草稿与保存内容不一致，请刷新核对。');
        entry.server = result;
        entry.dirty = entry.editVersion !== capturedVersion;
        entry.saveState = entry.dirty ? 'pending' : 'saved';
        // Do not assign result.body: the user may have continued typing.
        return !entry.dirty;
      } catch (failure) { entry.saveState = 'error'; entry.error = errorText(failure); entry.dirty = true; return false; }
      finally { entry.saving = false; saves.current.delete(conversationId); if (mounted.current) redraw(); }
    })();
    saves.current.set(conversationId, operation);
    return operation;
  }, [bridge, beginManualEdit]);

  const saveSupplement = useCallback(async (conversationId: string): Promise<boolean> => {
    const entry = supplements.current.get(conversationId);
    if (!entry) return true;
    if (entry.attachmentBusy) return false;
    if (entry.saving) { if (!(await entry.saving)) return false; return entry.dirty ? saveSupplement(conversationId) : true; }
    if (!entry.dirty) return true;
    if (!bridge || !connectedRef.current || closingRef.current) { entry.error = '服务未连接，补充内容尚未保存。'; redraw(); return false; }
    const body = entry.body, version = entry.version;
    const operation = (async () => {
      try {
        const saved = await bridge.invoke<Supplement>('save_conversation_supplement', { conversation_id: conversationId, body, expected_revision: entry.revision, client_mutation_id: mutationId() });
        if (saved.conversation_id !== conversationId || saved.body !== body) throw new Error('保存结果不匹配，请重试。');
        entry.revision = saved.revision; entry.dirty = entry.version !== version; entry.error = ''; return true;
      } catch (failure) { entry.error = errorText(failure); return false; }
      finally { entry.saving = null; if (mounted.current) redraw(); }
    })();
    entry.saving = operation; redraw();
    if (!(await operation)) return false;
    return entry.dirty ? saveSupplement(conversationId) : true;
  }, [bridge]);
  const updateSupplementAttachments = useCallback(async (conversationId: string, operation: () => Promise<SupplementAttachmentBatch | Supplement | null | undefined>): Promise<boolean> => {
    const entry = supplements.current.get(conversationId);
    if (!entry || !bridge || !connectedRef.current || closingRef.current || entry.attachmentBusy || !(await saveSupplement(conversationId)) || entry.attachmentBusy) return false;
    entry.attachmentBusy = true; entry.error = ''; redraw();
    try {
      const result = await operation();
      const batch = result && 'supplement' in result ? result : { supplement: result, errors: [] };
      const saved = batch.supplement;
      if (saved) {
        if (saved.conversation_id !== conversationId) throw new Error('附件结果与当前客户不匹配。');
        entry.revision = saved.revision;
        entry.attachments = saved.attachments ?? [];
      }
      entry.error = batch.errors.join('；');
      return !!saved || batch.errors.length > 0;
    } catch (failure) { entry.error = errorText(failure); return true; }
    finally { entry.attachmentBusy = false; if (mounted.current) redraw(); }
  }, [bridge, saveSupplement]);
  const changeSupplementAttachment = useCallback(async (conversationId: string, attachmentId?: string) => updateSupplementAttachments(conversationId, async () => {
    if (attachmentId) return bridge!.invoke<Supplement>('remove_supplement_attachment', { conversation_id: conversationId, attachment_id: attachmentId });
    return bridge!.chooseSupplementAttachments ? bridge!.chooseSupplementAttachments(conversationId) : bridge!.chooseSupplementAttachment?.(conversationId);
  }), [bridge, updateSupplementAttachments]);
  const uploadSupplementFiles = useCallback(async (conversationId: string, files: File[]) => updateSupplementAttachments(conversationId, async () => {
    if (!bridge?.uploadSupplementAttachments) throw new Error('当前程序不支持粘贴或拖入附件，请更新程序。');
    const existing = supplements.current.get(conversationId)!.attachments;
    if (existing.length >= 5) throw new Error('业务补充最多添加 5 个附件。');
    const uploads: SupplementUpload[] = [], errors: string[] = [];
    let total = existing.reduce((sum, file) => sum + file.size, 0);
    for (const file of files) {
      if (uploads.length + existing.length >= 5) { errors.push('业务补充最多添加 5 个附件。'); break; }
      const name = file.name || `粘贴图片-${Date.now()}.png`;
      if (!/\.(png|jpe?g|webp|pdf|txt|md|csv)$/i.test(name)) { errors.push('附件格式不支持，请使用图片、PDF 或 UTF-8 文本。'); continue; }
      if (file.size < 1 || file.size > 6 * 1024 * 1024) { errors.push('单个附件须大于零且不超过 6 MiB。'); continue; }
      if (total + file.size > 20 * 1024 * 1024) { errors.push('附件合计不能超过 20 MiB。'); continue; }
      try {
        const content_b64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error('附件读取失败，请重新选择。'));
          reader.onload = () => { const value = String(reader.result); const comma = value.indexOf(','); if (comma < 0) reject(new Error('附件读取失败。')); else resolve(value.slice(comma + 1)); };
          reader.readAsDataURL(file);
        });
        uploads.push({ name, content_b64 }); total += file.size;
      } catch { errors.push('附件读取失败，请重新选择。'); }
    }
    const result = uploads.length ? await bridge.uploadSupplementAttachments(conversationId, uploads) : { supplement: null, errors: [] };
    return { ...result, errors: [...new Set([...errors, ...result.errors])] };
  }), [bridge, updateSupplementAttachments]);
  const pasteSupplementAttachments = useCallback(async (conversationId: string) => updateSupplementAttachments(conversationId, async () => bridge?.pasteSupplementAttachments?.(conversationId)), [bridge, updateSupplementAttachments]);
  const editSupplement = useCallback((body: string) => {
    const entry = supplements.current.get(selectionRef.current.conversationId);
    if (!entry || closingRef.current) return;
    entry.body = body; entry.dirty = true; entry.version++; entry.error = ''; redraw();
  }, []);
  const supplement = supplements.current.get(selection.conversationId);
  useEffect(() => {
    if (!supplement?.dirty || supplement.saving || supplement.attachmentBusy || supplement.error) return;
    const timer = setTimeout(() => void saveSupplement(selection.conversationId), 800);
    return () => clearTimeout(timer);
  }, [supplement?.body, supplement?.dirty, supplement?.saving, supplement?.attachmentBusy, supplement?.error, selection.conversationId, saveSupplement]);

  useEffect(() => {
    const saveForClose = (event: Event) => {
      const finish = (event as CustomEvent<{ finish?: (saved: boolean) => void }>).detail?.finish;
      if (typeof finish !== 'function') return;
      void (async () => {
        if (dirtyForms.current.size) { finish(false); return; }
        for (const [conversationId, entry] of supplements.current) if (entry.attachmentBusy || ((entry.dirty || entry.saving) && !(await saveSupplement(conversationId)))) { finish(false); return; }
        for (const [conversationId, entry] of editors.current) if ((entry.dirty || entry.saving) && !(await saveDraft(conversationId))) { finish(false); return; }
        finish(!dirtyForms.current.size && ![...editors.current.values()].some(entry => entry.dirty || entry.saving));
      })().catch(() => finish(false));
    };
    window.addEventListener('reception:save-before-close', saveForClose);
    return () => window.removeEventListener('reception:save-before-close', saveForClose);
  }, [saveDraft, saveSupplement]);

  const editBody = useCallback((body: string) => {
    const key = selectionRef.current.conversationId;
    if (!key || closingRef.current) return;
    let current = editors.current.get(key);
    if (!current) { current = createEditor(null); editors.current.set(key, current); }
    current.body = body; current.dirty = true; current.edited = true; ++current.editVersion;
    current.saveState = current.saving ? 'saving' : 'pending'; current.error = '';
    void beginManualEdit(key); redraw();
  }, [beginManualEdit]);

  useEffect(() => {
    if (!editor?.dirty || editor.saving || editor.saveState === 'error' || editor.manualState !== 'ready') return;
    const timer = setTimeout(() => { void saveDraft(selection.conversationId); }, 800);
    return () => clearTimeout(timer);
  }, [editor?.body, editor?.dirty, editor?.saving, editor?.saveState, editor?.server?.revision, editor?.manualState, editor?.send?.status, selection.conversationId, saveDraft]);

  const flush = useCallback(async () => {
    const current = selectionRef.current;
    const entry = editors.current.get(current.conversationId);
    if (!entry?.dirty && !entry?.saving) return true;
    const version = entry.editVersion;
    const saved = await saveDraft(current.conversationId);
    if (!saved || entry.dirty || entry.editVersion !== version) {
      setNotice(entry.error || '正文尚未保存，已留在当前客户。请等待保存或发送回执后再切换。'); return false;
    }
    return true;
  }, [saveDraft]);

  const selectConversation = useCallback(async (conversationId: string, caseId: string | null = '') => {
    caseId ??= '';
    // A manual choice of an older case pins the view; choosing the current case resumes following.
    const sameConversation = selectionRef.current.conversationId === conversationId;
    if (sameConversation && caseId) pinnedCase.current = caseId === detailRef.current?.conversation.case_id ? null : caseId;
    if (!sameConversation) { pinnedCase.current = null; caseId = ''; }
    // Only an explicit customer selection may retry empty history; passive detail refreshes stay local.
    if (sameConversation && (!caseId || selectionRef.current.caseId === caseId)) {
      setPage('reception'); return loadDetail(conversationId, selectionRef.current.caseId, true, true);
    }
    if (!canLeaveForms() || !(await flush())) return false;
    void saveSupplement(selectionRef.current.conversationId);
    const next = { conversationId, caseId };
    selectionRef.current = next; setSelection(next); setDetail(null); detailRef.current = null; setPage('reception');
    return loadDetail(conversationId, caseId, false, true);
  }, [flush, loadDetail, canLeaveForms, saveSupplement]);

  const navigate = useCallback(async (target: Page) => {
    if (!canLeaveForms() || (target !== 'reception' && !(await flush()))) return false;
    setPage(target); return true;
  }, [flush, canLeaveForms]);

  const invoke = useCallback(async <T,>(method: ApiMethod, params: Record<string, unknown> = {}): Promise<T> => {
    if (!bridge || !connectedRef.current || closingRef.current) throw new Error('接待服务未连接，请先恢复连接。');
    return bridge.invoke<T>(method, params);
  }, [bridge]);

  const action = useCallback(async <T,>(key: string, operation: () => Promise<T>): Promise<T | null> => {
    if (pendingRef.current.has(key)) return null;
    pendingRef.current.add(key); setPending([...pendingRef.current]); setError('');
    try { return await operation(); }
    catch (failure) { setError(errorText(failure)); return null; }
    finally { pendingRef.current.delete(key); if (mounted.current) setPending([...pendingRef.current]); }
  }, []);

  const runJob = useCallback(async (method: ApiMethod, params: Record<string, unknown> = {}) => action(`${method}:${String(method === 'generate_manual_suggestion' ? params.conversation_id ?? '' : params.case_id ?? params.conversation_id ?? '')}`, async () => {
    const health = executorHealth(snapshotRef.current);
    if (!health.ready) throw new Error(`${health.message}，此任务暂未就绪。`);
    const ack = await invoke<JobAck & { cancelled?: boolean }>(method, params);
    if (ack?.cancelled) { setNotice('已取消本次操作。'); return null; }
    if (!ack?.job_id) throw new Error('服务未返回任务编号，不能确认任务已创建。');
    setNotice('任务已提交，处理结果会在运行记录中更新。');
    if (method === 'fill_history' && !jobsRef.current.some(job => job.job_id === ack.job_id)) setHistoryRequests(previous => [...previous, { job_id: ack.job_id, conversation_id: String(params.conversation_id ?? '') }]);
    if ((method === 'generate_draft' || method === 'generate_manual_suggestion') && !jobsRef.current.some(job => job.job_id === ack.job_id)) {
      const caseId = String(params.case_id ?? '');
      const conversationId = method === 'generate_manual_suggestion' ? String(params.conversation_id ?? '') : caseOwners.current.get(caseId);
      if (conversationId) setGenerationRequests(previous => [...previous.filter(item => item.conversation_id !== conversationId), { job_id: ack.job_id, conversation_id: conversationId, case_id: caseId }]);
    }
    try { updateJobs(await invoke<Job[]>('list_jobs')); }
    catch (failure) {
      if (method !== 'generate_draft' && method !== 'generate_manual_suggestion') throw failure;
      setNotice('生成任务已受理，状态同步暂未完成。可刷新运行记录核对原任务，请勿重复提交。');
    }
    return ack;
  }), [action, invoke, updateJobs]);

  const changePreferences = useCallback((value: Partial<Preferences>) => {
    setPreferences(previous => ({ ...previous, ...value }));
    if (bridge) void bridge.savePreferences(value).catch(() => setNotice('布局已调整，但偏好未能保存。'));
  }, [bridge]);

  const mutate = useCallback(async (method: ApiMethod, params: Record<string, unknown>, message?: string) => {
    const intent = method === 'set_reception' ? ++receptionIntent.current : null;
    const key = method === 'set_reception' && params.enabled === false ? 'pause_reception' : method;
    return action(key, async () => {
      const result = await invoke<Snapshot>(method, params);
      const latestIntent = intent === null || intent === receptionIntent.current;
      if (method === 'takeover') {
        const entry = editors.current.get(String(params.conversation_id));
        if (entry) { entry.manualState = params.enabled === false ? 'idle' : 'ready'; entry.manualError = ''; }
      }
      if (result?.app && latestIntent) { const merged = withLatestHealth(result); setSnapshot(merged); snapshotRef.current = merged; updateJobs(merged.jobs); }
      else await refreshSnapshot();
      const current = selectionRef.current;
      if (current.conversationId) await loadDetail(current.conversationId, current.caseId, true);
      if (message && latestIntent) setNotice(message);
      return result;
    });
  }, [action, invoke, loadDetail, refreshSnapshot, updateJobs, withLatestHealth]);

  const reconcileOperation = useCallback(async (operationId: string) => action(`reconcile:${operationId}`, async () => {
    if (!executorHealth(snapshotRef.current).ready) throw new Error('后台执行暂不可用，原操作核查需要后台任务；回执已保留，请恢复后核查。');
    const result = await invoke<{ status?: string; message?: string; job_id?: string }>('reconcile_operation', { operation_id: operationId });
    setNotice(result.job_id ? '原操作核查已排队，不会重新发送；完成后更新回执。' : result.message || '原操作核查已完成，请查看回执。');
    await refreshSnapshot();
    const current = selectionRef.current;
    if (current.conversationId) await loadDetail(current.conversationId, current.caseId, true);
    return result;
  }), [action, invoke, refreshSnapshot, loadDetail]);

  const generate = useCallback(async () => {
    const blocked = generationBlocker(snapshotRef.current, detailRef.current);
    if (blocked) { setNotice(blocked); return; }
    const conversationId = selectionRef.current.conversationId;
    const selectedCaseId = selectionRef.current.caseId;
    const entry = editors.current.get(conversationId);
    const frozen = entry?.body.trim() ? { body: entry.body, version: entry.editVersion } : null;
    if (!(await saveSupplement(conversationId))) return;
    if (frozen && !(await saveDraft(conversationId))) return;
    const current = detailRef.current;
    if (!current || selectionRef.current.conversationId !== conversationId || selectionRef.current.caseId !== selectedCaseId) return;
    const latestBlocker = generationBlocker(snapshotRef.current, current);
    if (latestBlocker) { setNotice(latestBlocker); return; }
    const params: Record<string, unknown> = { conversation_id: conversationId, case_id: selectedCaseId || null, client_request_id: mutationId() };
    if (frozen) {
      const saved = entry?.server;
      if (!entry || entry.editVersion !== frozen.version || entry.body !== frozen.body || entry.dirty || entry.saving ||
          saved?.body !== frozen.body || saved?.conversation_id !== conversationId || !saved.revision) {
        setNotice('润色期间正文已变化，请以当前输入重新点击润色。'); return;
      }
      if (!globalThis.crypto?.subtle) { setNotice('无法核对当前回复正文摘要，请稍后重试。'); return; }
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(frozen.body));
      if (entry.editVersion !== frozen.version || entry.body !== frozen.body || selectionRef.current.conversationId !== conversationId) return;
      Object.assign(params, { input_body: frozen.body, input_revision: saved.revision,
        input_hash: [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('') });
    }
    await runJob('generate_manual_suggestion', params);
  }, [saveSupplement, saveDraft, runJob]);

  const reviewedSuggestions = useRef(new Map<string, string>());
  const reviewKey = (conversationId: string) => `reception.suggestion.review.${snapshotRef.current?.app.mode ?? 'unknown'}.${snapshotRef.current?.app.seller_id ?? ''}.${conversationId}`;
  const finishSuggestionReview = useCallback(() => {
    const current = detailRef.current;
    const suggestion = current?.manual_suggestion;
    if (!current || !suggestion) return;
    const key = `reception.suggestion.review.${snapshotRef.current?.app.mode ?? 'unknown'}.${snapshotRef.current?.app.seller_id ?? ''}.${current.conversation.conversation_id}`;
    reviewedSuggestions.current.set(key, suggestion.suggestion_id);
    try { localStorage.setItem(key, suggestion.suggestion_id); } catch { /* keep the current-window review state */ }
    redraw();
  }, []);
  const suggestionKey = reviewKey(selection.conversationId);
  if (!reviewedSuggestions.current.has(suggestionKey)) {
    try { reviewedSuggestions.current.set(suggestionKey, localStorage.getItem(suggestionKey) ?? ''); } catch { reviewedSuggestions.current.set(suggestionKey, ''); }
  }
  const pendingSuggestion = !!detail?.manual_suggestion && reviewedSuggestions.current.get(suggestionKey) !== detail.manual_suggestion.suggestion_id;

  // The optional native preview belongs to the AI suggestion, never to manual chat.
  const preview = useCallback(async () => {
    const draft = detailRef.current?.draft;
    if (draft) await runJob('preview_send', { draft_id: draft.draft_id, expected_revision: draft.revision });
  }, [runJob]);

  const adoptSuggestion = useCallback(async () => {
    const suggestion = detailRef.current?.manual_suggestion;
    const cid = selectionRef.current.conversationId;
    const entry = editors.current.get(cid);
    if (!suggestion || suggestion.status !== 'ready' || !entry) return;
    if (suggestion.reply_messages && suggestion.reply_messages.length !== 1 || suggestion.input_part_index != null) {
      setNotice('旧版多条建议不可采用，请重新生成单条建议。'); return;
    }
    if (suggestion.mode === 'polish') {
      const body = entry.body;
      const version = entry.editVersion;
      if (!body || entry.dirty || entry.server?.revision !== suggestion.input_revision || !crypto.subtle) { setNotice('润色依据已变化，请重新生成。'); return; }
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body));
      const hash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
      if (hash !== suggestion.input_hash || entry.editVersion !== version || selectionRef.current.conversationId !== cid || detailRef.current?.manual_suggestion?.suggestion_id !== suggestion.suggestion_id) { setNotice('润色依据已变化，请重新生成。'); return; }
    }
    editBody(suggestion.body);
    finishSuggestionReview();
  }, [editBody, finishSuggestionReview]);

  const conversationFence = useCallback((conversationId: string, entry?: EditorState): string => {
    const state = snapshotRef.current;
    const unknownOperation = state?.outbox.some(item => isSendOperation(item) && item.status === 'unknown' && (item.conversation_id === conversationId || caseOwners.current.get(item.case_id) === conversationId));
    const unknownJob = jobsRef.current.some(job => isSendJob(job) && job.status === 'unknown' && job.conversation_id === conversationId &&
      !(entry?.send?.jobId === job.job_id && ['accepted', 'rejected'].includes(entry.send.status)) &&
      !state?.outbox.some(item => isSendOperation(item) && (item.job_id === job.job_id || item.operation_id === job.operation_id || item.operation_id === job.result?.operation_id) && ['accepted', 'rejected'].includes(item.status)));
    if (entry?.server?.status === 'unknown' || entry?.send?.status === 'unknown' || unknownOperation || unknownJob) return '此客户上次发送结果未知，请先在运行记录核查原操作，避免重复发送。';
    if (entry?.send?.status !== 'retry' && (entry?.server?.status === 'sending' || jobsRef.current.some(job => isSendJob(job) && isActive(job) && job.conversation_id === conversationId))) return '此客户有发送任务正在处理，请等待回执。';
    return '';
  }, []);

  const chatBlocker = useCallback((conversationId: string, entry?: EditorState): string => {
    if (!connectedRef.current) return '服务未连接，暂不能发送。';
    if (closingRef.current) return '正在关闭工作台。';
    if (!entry) return '请先选择客户。';
    const fence = conversationFence(conversationId, entry);
    if (fence) return fence;
    const health = executorHealth(snapshotRef.current);
    if (!health.ready) return `${health.message}，暂不能发送，正文已保留。`;
    if (entry.manualState === 'pending') return '正在暂停此客户自动接待，确认成功后才能发送。';
    if (entry.manualState === 'error') return '尚未确认暂停此客户自动接待，请先重试。';
    const linkedBatch = entry.server?.blocked_batch_id ?? entry.server?.batch_id ?? (entry.server?.draft_id === entry.send?.params.draft_id && entry.server?.revision === entry.send?.params.expected_revision ? entry.send?.batchId : undefined);
    const stoppedBatch = detailRef.current?.reply_batches?.find(batch => batch.batch_id === linkedBatch && batch.status === 'stopped');
    if (stoppedBatch && stoppedBatch.accepted > 0) return '旧批次已有消息受理，请先开启空白新草稿。';
    if (sendUnsettled(entry.send) && entry.send?.status !== 'retry') return '发送请求正在处理，请等待回执。';
    if (entry.send?.status !== 'retry' && !entry.body.trim()) return '请输入每条回复正文。';
    return '';
  }, [conversationFence]);

  const send = useCallback(async () => {
    const conversationId = selectionRef.current.conversationId;
    return action(`send:${conversationId}`, async () => {
      const entry = editors.current.get(conversationId);
      const blocked = chatBlocker(conversationId, entry);
      if (blocked) throw new Error(blocked);
      if (!entry) return;
      let attempt = entry.send?.status === 'retry' ? entry.send : null;
      if (!attempt) {
        if (!(await beginManualEdit(conversationId))) return;
        if ((entry.server?.status === 'rejected' || entry.server?.message_parts) && !entry.dirty) { entry.dirty = true; entry.saveState = 'pending'; }
        if (!(await flush())) return;
        if (conversationId !== selectionRef.current.conversationId) throw new Error('客户已切换，请重新发送。');
        const latestBlocker = chatBlocker(conversationId, entry);
        if (latestBlocker) throw new Error(latestBlocker);
        const saved = entry.server;
        if (!saved || saved.purpose !== 'chat' || saved.conversation_id !== conversationId || saved.body !== entry.body || entry.dirty || entry.saving) throw new Error('尚未确认保存确切正文，请保存后重试。');
        attempt = { params: { conversation_id: conversationId, draft_id: saved.draft_id, expected_revision: saved.revision, client_request_id: mutationId() }, body: saved.body, editVersion: entry.editVersion, status: 'requesting', jobId: null, message: '', cleared: false };
        entry.send = attempt;
      }
      // A communication failure retains every argument, including the original request ID and revision.
      attempt.status = 'requesting'; attempt.message = ''; redraw();
      try {
        const ack = await invoke<JobAck & { batch_id?: string }>('send_chat', { ...attempt.params });
        if (!ack?.job_id || (attempt.batchId && !ack.batch_id)) throw new Error('服务未返回任务编号，请重试原发送请求以查询同一任务。');
        if (ack.batch_id) attempt.batchId = ack.batch_id;
        attempt.jobId = ack.job_id; attempt.status = 'queued';
        setNotice('发送请求已提交，执行状态与平台回执会在本条回复下方更新。');
        syncSends();
      } catch (failure) {
        if (['accepted', 'rejected', 'unknown'].includes(attempt.status)) { redraw(); return; }
        attempt.status = 'retry'; attempt.message = errorText(failure);
        setNotice('发送请求尚未确认。重试将使用原请求编号和原正文，不会提交新的一条。'); redraw(); return;
      }
      // Failure to refresh records after a known acknowledgement must not turn it into a new submission.
      try { updateJobs(await invoke<Job[]>('list_jobs')); } catch { setNotice('发送请求已提交，回执读取暂未完成，可刷新运行记录。'); }
      void refreshSnapshot();
      if (selectionRef.current.conversationId === conversationId) void loadDetail(conversationId, selectionRef.current.caseId, true);
    });
  }, [action, chatBlocker, beginManualEdit, flush, invoke, syncSends, updateJobs, refreshSnapshot, loadDetail]);

  const startNewDraft = useCallback(async () => {
    const cid = selectionRef.current.conversationId;
    const entry = editors.current.get(cid);
    const batchId = entry?.server?.blocked_batch_id ?? entry?.server?.batch_id;
    const batch = detailRef.current?.reply_batches?.find(item => item.batch_id === batchId);
    if (!entry || !bridge || !batch || batch.status !== 'stopped' || !batch.accepted || batch.pending_operation_ids.length || conversationFence(cid, entry) || entry.saving || entry.dirty) return;
    const version = entry.editVersion;
    entry.saving = true; entry.saveState = 'saving'; redraw();
    try {
      const result = await bridge.invoke<Draft>('save_chat_draft', { conversation_id: cid, body: '', expected_revision: entry.server?.revision ?? null, client_mutation_id: mutationId() });
      if (result.conversation_id !== cid || result.body !== '' || result.blocked_batch_id || result.batch_id) throw new Error('新草稿尚未确认，请刷新后重试。');
      entry.server = result;
      if (entry.editVersion === version) { entry.body = ''; entry.edited = false; entry.dirty = false; }
      entry.send = null; entry.error = ''; entry.saveState = 'saved';
      if (selectionRef.current.conversationId === cid) void loadDetail(cid, selectionRef.current.caseId, true);
    } catch (failure) { entry.error = errorText(failure); entry.saveState = 'error'; }
    finally { entry.saving = false; if (mounted.current) redraw(); }
  }, [bridge, conversationFence, loadDetail]);

  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => {
      if (dirtyForms.current.size || [...supplements.current.values()].some(entry => entry.dirty || entry.saving || entry.attachmentBusy) || [...editors.current.values()].some(entry => entry.dirty || entry.saving)) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', protect);
    return () => window.removeEventListener('beforeunload', protect);
  }, []);

  const identityReady = !!snapshot && (snapshot.app.mode === 'demo' || snapshot.app.account_verified);
  const aiReady = checksReady(snapshot, ['ai']);
  const health = executorHealth(snapshot);
  const aiReceptionReady = health.ready && identityReady && checksReady(snapshot, ['identity', 'messages', 'ai']);
  const sendIssue = chatBlocker(selection.conversationId, editor);
  const generationIssue = generationBlocker(snapshot, detail);

  const legacyBatchId = editor?.server?.blocked_batch_id ?? editor?.server?.batch_id;
  const legacyPartialBatch = detail?.reply_batches?.find(batch => batch.batch_id === legacyBatchId && batch.status === 'stopped' && batch.accepted > 0) ?? null;

  return { bridge, snapshot, connected, closing, loading, error, notice, setError, setNotice, page, selection, detail, detailLoading, detailError, editor, supplement, editSupplement, saveSupplement, changeSupplementAttachment, uploadSupplementFiles, pasteSupplementAttachments, jobs, pending, generationRequests, historyRequests, preferences, setFormDirty, changePreferences, refreshSnapshot, loadDetail, markMessagesRead, selectConversation, navigate, invoke, action, mutate, runJob, generate, preview, send, editBody, legacyPartialBatch, startNewDraft, saveDraft, flush, adoptSuggestion, pendingSuggestion, dismissSuggestion: finishSuggestionReview, beginManualEdit, scrollPositions,
    aiReady, aiReceptionReady, executorReady: health.ready, executorMessage: health.message, sendIssue, generationIssue, reconcileOperation,
    unknownOperations: snapshot?.outbox.filter(item => isSendOperation(item) && item.status === 'unknown' && (item.conversation_id === selection.conversationId || caseOwners.current.get(item.case_id) === selection.conversationId)) ?? [],
    canSave: connected && !closing && !!editor?.dirty,
    canSend: connected && !closing && !sendIssue,
    canPreview: connected && !closing && health.ready && !!detail?.draft,
    canGenerate: connected && !closing && !!detail && !generationIssue,
    busy: (kind: string, caseId?: string) => (kind === 'generate_draft' && generationRequests.some(item => !caseId || item.case_id === caseId)) || pending.some(key => caseId ? key === `${kind}:${caseId}` : key === kind || key.startsWith(`${kind}:`)) || jobs.some(job => job.kind === kind && isActive(job) && (!caseId || job.case_id === caseId)),
  };
}
export type Workbench = ReturnType<typeof useWorkbench>;
