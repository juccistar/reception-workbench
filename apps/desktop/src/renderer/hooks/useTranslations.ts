import { useEffect, useRef, useState } from 'react';
import type { ReceptionBridge, Translation, TranslationItem, Job } from '../../shared/api';

type Source = { item: TranslationItem; version: string };
const key = (item: TranslationItem) => `${item.kind}:${item.source_id}:${item.field ?? ''}`;
const requestId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;

/** Translation is a read-only view: source text is used only to invalidate local state. */
export function useTranslations(bridge: ReceptionBridge | undefined, conversationId: string, enabled: boolean, sources: Source[], sellerId = '') {
  const signature = JSON.stringify(sources);
  const scope = JSON.stringify([sellerId, conversationId]);
  const [values, setValues] = useState<Record<string, Translation>>({});
  const [revision, setRevision] = useState(0);
  const attempted = useRef(new Set<string>());
  const retries = useRef(new Set<string>());
  const recoveryJobs = useRef(new Set<string>());
  const versions = useRef<Record<string, string>>({});
  const active = useRef('');
  const sequence = useRef(0);
  const current = `${scope}:${signature}`;
  active.current = current;
  useEffect(() => {
    if (!enabled || !bridge || !conversationId) return;
    const runSequence = ++sequence.current;
    const isCurrent = () => active.current === current && sequence.current === runSequence;
    const list = JSON.parse(signature) as Source[];
    const run = async () => {
      for (let start = 0; start < list.length;) {
        if (!isCurrent()) return;
        const batch: Source[] = [];
        let length = 0;
        while (start < list.length && batch.length < 10 && length + list[start].version.length <= 90000) { const source = list[start++]; batch.push(source); length += source.version.length; }
        if (!batch.length) { start++; continue; }
        for (const source of batch) {
          const k = key(source.item);
          if (versions.current[k] !== `${scope}:${source.version}`) {
            versions.current[k] = `${scope}:${source.version}`;
            setValues(previous => { const next = { ...previous }; delete next[k]; return next; });
          }
        }
        try {
          const result = await bridge.invoke<{ items: Translation[] }>('get_translations', { conversation_id: conversationId, items: batch.map(source => source.item) });
          if (!isCurrent()) return;
          if (!Array.isArray(result?.items)) throw new Error('译文服务暂不可用');
          const received = result.items;
          for (const source of batch) {
            if (received.some(item => key(item) === key(source.item) && item.status === 'ready')) retries.current.delete(`${scope}:${key(source.item)}:${source.version}`);
          }
          setValues(previous => ({ ...previous, ...Object.fromEntries(received.map(item => [key(item), item])) }));
          const missing = batch.filter(source => {
            const item = result.items.find(value => key(value) === key(source.item));
            const token = `${scope}:${key(source.item)}:${source.version}`;
            return (item?.status === 'pending' && !attempted.current.has(token)) || (item?.status !== 'ready' && retries.current.has(token));
          });
          if (missing.length) {
            missing.forEach(source => { const token = `${scope}:${key(source.item)}:${source.version}`; attempted.current.add(token); retries.current.delete(token); });
            try { await bridge.invoke('request_translations', { conversation_id: conversationId, items: missing.map(source => source.item), client_request_id: requestId() }); }
            catch (error) {
              if (!isCurrent()) return;
              setValues(previous => ({ ...previous, ...Object.fromEntries(missing.map(source => [key(source.item), { ...source.item, status: 'failed', text: null, error: error instanceof Error ? error.message : '翻译请求失败' }])) }));
            }
          }
        } catch (error) {
          if (!isCurrent()) return;
          setValues(previous => ({ ...previous, ...Object.fromEntries(batch.map(source => [key(source.item), { ...source.item, status: 'failed', text: null, error: error instanceof Error ? error.message : '译文读取失败' }])) }));
        }
      }
    };
    void run();
    return () => { sequence.current++; };
  }, [bridge, sellerId, conversationId, enabled, signature, revision]);
  useEffect(() => bridge?.onEvent(event => {
    if (event.event === 'state.changed') {
      const data = event.data as { reason?: string; conversation_id?: string } | null;
      if (data?.reason === 'translations_updated' && data.conversation_id === conversationId) setRevision(value => value + 1);
      return;
    }
    if (event.event !== 'job.updated') return;
    const job = event.data as Job;
    if (enabled && conversationId && job?.kind === 'check_ai' && job.status === 'succeeded' && job.job_id && !recoveryJobs.current.has(`${scope}:${job.job_id}`)) {
      recoveryJobs.current.add(`${scope}:${job.job_id}`);
      for (const source of JSON.parse(signature) as Source[]) retries.current.add(`${scope}:${key(source.item)}:${source.version}`);
      setRevision(value => value + 1);
      return;
    }
    if (job?.kind === 'translation' && job.conversation_id === conversationId && ['succeeded', 'failed', 'cancelled', 'stale'].includes(job.status)) setRevision(value => value + 1);
  }), [bridge, sellerId, conversationId, enabled, signature]);
  const get = (item: TranslationItem, version: string) => versions.current[key(item)] === `${scope}:${version}` ? values[key(item)] : undefined;
  const retry = (item: TranslationItem, version: string) => { retries.current.add(`${scope}:${key(item)}:${version}`); setValues(previous => { const next = { ...previous }; delete next[key(item)]; return next; }); setRevision(value => value + 1); };
  return { get, retry };
}
