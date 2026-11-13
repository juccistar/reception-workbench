import { useEffect, useRef, useState } from 'react';
import type { ReceptionBridge, Translation, TranslationItem, Job } from '../../shared/api';

type Source = { item: TranslationItem; version: string; requestable?: boolean };
const key = (item: TranslationItem) => `${item.kind}:${item.source_id}:${item.field ?? ''}`;
const requestId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
async function identity(source: Source): Promise<TranslationItem> {
  if (!globalThis.crypto?.subtle) return source.item;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source.version));
  return { ...source.item, source_hash: [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('') };
}

/** Reads persistent translations even without a key; network requests need separate permission. */
export function useTranslations(bridge: ReceptionBridge | undefined, conversationId: string, enabled: boolean,
  sources: Source[], sellerId = '', configRevision = 0, canRequest = true) {
  const signature = JSON.stringify(sources);
  const scope = JSON.stringify([sellerId, conversationId, configRevision]);
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
        while (start < list.length && batch.length < 6 && length + list[start].version.length <= 90000) {
          const source = list[start++]; batch.push(source); length += source.version.length;
        }
        if (!batch.length) { start++; continue; }
        for (const source of batch) {
          const k = key(source.item);
          if (versions.current[k] !== `${scope}:${source.version}`) {
            versions.current[k] = `${scope}:${source.version}`;
            setValues(previous => { const next = { ...previous }; delete next[k]; return next; });
          }
        }
        try {
          const identities = await Promise.all(batch.map(identity));
          if (!isCurrent()) return;
          const result = await bridge.invoke<{ items: Translation[] }>('get_translations', { conversation_id: conversationId, items: identities });
          if (!isCurrent()) return;
          if (!Array.isArray(result?.items)) throw new Error('译文服务暂不可用');
          const received = result.items;
          setValues(previous => ({ ...previous, ...Object.fromEntries(received.map(item => [key(item), item])) }));
          const missing = batch.flatMap((source, index) => {
            const item = received.find(value => key(value) === key(source.item));
            const token = `${scope}:${key(source.item)}:${source.version}`;
            const attemptToken = `${token}:${item?.cache_key ?? ''}`;
            const shouldRequest = (item?.status === 'pending' || item?.legacy) && !attempted.current.has(attemptToken)
              || item?.status !== 'ready' && retries.current.has(token);
            if (!canRequest || source.requestable === false || !shouldRequest) return [];
            attempted.current.add(attemptToken); retries.current.delete(token);
            return [identities[index]];
          });
          if (missing.length) {
            try { await bridge.invoke('request_translations', { conversation_id: conversationId, items: missing, client_request_id: requestId() }); }
            catch (error) {
              if (!isCurrent()) return;
              setValues(previous => ({ ...previous, ...Object.fromEntries(missing.map(item => [key(item), {
                ...item, status: 'failed', text: previous[key(item)]?.text ?? null,
                error: error instanceof Error ? error.message : '翻译请求失败',
              }])) }));
            }
          }
        } catch (error) {
          if (!isCurrent()) return;
          setValues(previous => ({ ...previous, ...Object.fromEntries(batch.map(source => [key(source.item), {
            ...source.item, status: 'failed', text: null, error: error instanceof Error ? error.message : '译文读取失败',
          }])) }));
        }
      }
    };
    void run();
    return () => { sequence.current++; };
  }, [bridge, scope, conversationId, enabled, canRequest, signature, revision]);
  useEffect(() => bridge?.onEvent(event => {
    if (event.event === 'translation.settings_changed' || event.event === 'translation.cache_cleared') {
      attempted.current.clear(); retries.current.clear(); setRevision(value => value + 1); return;
    }
    if (event.event === 'state.changed') {
      const data = event.data as { reason?: string; conversation_id?: string } | null;
      if (data?.reason === 'translations_updated' && data.conversation_id === conversationId) setRevision(value => value + 1);
      return;
    }
    if (event.event !== 'job.updated') return;
    const job = event.data as Job;
    if (enabled && canRequest && conversationId && job?.kind === 'translation_check' && job.status === 'succeeded'
        && job.job_id && !recoveryJobs.current.has(`${scope}:${job.job_id}`)) {
      recoveryJobs.current.add(`${scope}:${job.job_id}`);
      attempted.current.clear();
      for (const source of JSON.parse(signature) as Source[]) retries.current.add(`${scope}:${key(source.item)}:${source.version}`);
      setRevision(value => value + 1); return;
    }
    if (job?.kind === 'translation' && job.conversation_id === conversationId
        && ['succeeded', 'failed', 'cancelled', 'stale'].includes(job.status)) setRevision(value => value + 1);
  }), [bridge, scope, conversationId, enabled, canRequest, signature]);
  const get = (item: TranslationItem, version: string) => versions.current[key(item)] === `${scope}:${version}` ? values[key(item)] : undefined;
  const retry = (item: TranslationItem, version: string) => {
    retries.current.add(`${scope}:${key(item)}:${version}`);
    setValues(previous => { const next = { ...previous }; delete next[key(item)]; return next; });
    setRevision(value => value + 1);
  };
  return { get, retry };
}
