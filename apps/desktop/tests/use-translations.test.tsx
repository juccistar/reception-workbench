import { act, renderHook, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { ReceptionBridge, ServiceEvent, TranslationItem } from '../src/shared/api';
import { useTranslations } from '../src/renderer/hooks/useTranslations';

const item: TranslationItem = { kind: 'message', source_id: 'm1' };
const sources = [{ item, version: 'hello' }];

it('resumes failed loaded translations once after a successful AI check', async () => {
  let listener: (event: ServiceEvent) => void = () => {};
  let status = 'pending';
  const calls: string[] = [];
  const bridge = {
    onEvent(callback: typeof listener) { listener = callback; return () => {}; },
    async invoke(method: string) {
      calls.push(method);
      if (method === 'get_translations') return { items: [{ ...item, status, text: status === 'ready' ? '你好' : null }] };
      if (method === 'request_translations') { status = 'failed'; return { job_id: 'translation' }; }
      throw new Error('unexpected method');
    },
  } as unknown as ReceptionBridge;
  const hook = renderHook(() => useTranslations(bridge, 'conversation', true, sources));
  await waitFor(() => expect(calls.filter(call => call === 'request_translations')).toHaveLength(1));
  act(() => listener({ event: 'job.updated', data: { kind: 'translation', conversation_id: 'conversation', status: 'failed' } }));
  await waitFor(() => expect(hook.result.current.get(item, 'hello')?.status).toBe('failed'));
  const check = { event: 'job.updated', data: { kind: 'check_ai', status: 'succeeded', job_id: 'check-1' } };
  act(() => listener(check));
  await waitFor(() => expect(calls.filter(call => call === 'request_translations')).toHaveLength(2));
  act(() => { listener(check); listener({ event: 'state.changed', data: { reason: 'translations_updated', conversation_id: 'conversation' } }); });
  await waitFor(() => expect(hook.result.current.get(item, 'hello')?.status).toBe('failed'));
  expect(calls.filter(call => call === 'request_translations')).toHaveLength(2);
  expect(new Set(calls)).toEqual(new Set(['get_translations', 'request_translations']));
});

it('isolates identical source identifiers across sellers and ignores a late response', async () => {
  let resolveOld!: (value: unknown) => void;
  let seller = 'seller-a';
  const invoke = vi.fn((method: string) => {
    if (method === 'get_translations' && seller === 'seller-a') return new Promise(resolve => { resolveOld = resolve; });
    if (method === 'get_translations') return Promise.resolve({ items: [{ ...item, status: 'ready', text: '商家乙的译文' }] });
    return Promise.resolve({ job_id: 'job' });
  });
  const bridge = { invoke, onEvent: () => () => {} } as unknown as ReceptionBridge;
  const hook = renderHook(({ identity }) => useTranslations(bridge, 'same-conversation', true, sources, identity), { initialProps: { identity: seller } });
  await waitFor(() => expect(resolveOld).toBeTypeOf('function'));
  seller = 'seller-b';
  hook.rerender({ identity: seller });
  expect(hook.result.current.get(item, 'hello')?.text).toBeUndefined();
  await waitFor(() => expect(hook.result.current.get(item, 'hello')?.text).toBe('商家乙的译文'));
  await act(async () => { resolveOld({ items: [{ ...item, status: 'ready', text: '商家甲的迟到译文' }] }); });
  expect(hook.result.current.get(item, 'hello')?.text).toBe('商家乙的译文');
  expect(invoke.mock.calls.filter(([method]) => method === 'request_translations')).toHaveLength(0);
});

it('ignores late reads after changing conversation and does not request old sources', async () => {
  let resolveFirst!: (value: unknown) => void;
  const invoke = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'get_translations' && params.conversation_id === 'old') return new Promise(resolve => { resolveFirst = resolve; });
    if (method === 'get_translations') return Promise.resolve({ items: [{ ...item, status: 'ready', text: '新' }] });
    return Promise.resolve({ job_id: 'job' });
  });
  const bridge = { invoke, onEvent: () => () => {} } as unknown as ReceptionBridge;
  const hook = renderHook(({ conversation }) => useTranslations(bridge, conversation, true, sources), { initialProps: { conversation: 'old' } });
  hook.rerender({ conversation: 'new' });
  await waitFor(() => expect(hook.result.current.get(item, 'hello')?.text).toBe('新'));
  await act(async () => { resolveFirst({ items: [{ ...item, status: 'pending', text: null }] }); });
  expect(hook.result.current.get(item, 'hello')?.text).toBe('新');
  expect(invoke.mock.calls.filter(([method]) => method === 'request_translations')).toHaveLength(0);
});
