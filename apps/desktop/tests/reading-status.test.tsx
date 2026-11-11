import { useRef } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { compareMessages, useReadAcknowledgement } from '../src/renderer/hooks/useReadAcknowledgement';
import { effectiveReceptionStatus, receptionLabel } from '../src/renderer/receptionStatus';
import type { ConversationDetail, Message, ReceptionStatus } from '../src/shared/api';
import { createReplyActivityFixture } from './fixtures/replyActivity';

const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 300, height: bottom - top, width: 300, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
let geometry: Record<string, DOMRect>;
let focused = true;
let visible = true;
const step = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(260); }); };
const prepare = () => {
  const f = createReplyActivityFixture();
  const detail = structuredClone(f.details.emma);
  detail.reading = { initialized: true, observed_sequence: 7 };
  detail.conversation.unread_count = 3;
  const original = detail.messages[0];
  detail.messages = ['m1', 'm2', 'm3'].map((message_id, i) => ({ ...original, message_id, timestamp_ms: i + 1 }));
  return detail;
};
function Reading({ detail, active = true, acknowledge }: { detail: ConversationDetail; active?: boolean; acknowledge: (cid: string, mid: string, seq: number) => Promise<void> }) {
  const ref = useRef<HTMLDivElement>(null);
  const result = useReadAcknowledgement({ viewport: ref, detail, conversationId: detail.conversation.conversation_id, messages: detail.messages, active, acknowledge });
  return <><div ref={ref} data-testid="viewport" data-conversation-id={detail.conversation.conversation_id}>{detail.messages.map(message => <article key={message.message_id} data-message-id={message.message_id}>{message.message_id}</article>)}</div>{result.failed && <button onClick={result.retry}>重试阅读确认</button>}</>;
}
beforeEach(() => {
  vi.useFakeTimers();
  focused = visible = true;
  geometry = { m1: box(10, 70), m2: box(80, 160), m3: box(210, 270) };
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden');
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return geometry[this.dataset.testid ?? this.dataset.messageId ?? ''] ?? (this.dataset.testid === 'viewport' ? box(0, 200) : box(0, 0));
  });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('visible reading acknowledgement', () => {
  it('acknowledges the visible boundary with its observed receipt sequence, not the unseen tail', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    expect(acknowledge).toHaveBeenCalledExactlyOnceWith(detail.conversation.conversation_id, 'm2', 7);
    await step();
    expect(acknowledge).toHaveBeenCalledTimes(1);
    geometry.m3 = box(130, 190);
    fireEvent.scroll(screen.getByTestId('viewport'));
    await step();
    expect(acknowledge).toHaveBeenLastCalledWith(detail.conversation.conversation_id, 'm3', 7);
  });
  it('does not acknowledge hidden, unfocused or non-reception views', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    const view = render(<Reading detail={detail} active={false} acknowledge={acknowledge} />);
    await step();
    visible = false;
    view.rerender(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    visible = true; focused = false;
    fireEvent(document, new Event('visibilitychange'));
    await step();
    expect(acknowledge).not.toHaveBeenCalled();
    focused = true;
    fireEvent(window, new Event('focus'));
    await step();
    expect(acknowledge).toHaveBeenCalledTimes(1);
  });
  it('retains counts on failure and retries only the explicit observation', async () => {
    const detail = prepare(), acknowledge = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
    render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    expect(screen.getByRole('button', { name: '重试阅读确认' })).toBeInTheDocument();
    await step();
    expect(acknowledge).toHaveBeenCalledTimes(1);
    expect(detail.conversation.unread_count).toBe(3);
    fireEvent.click(screen.getByRole('button', { name: '重试阅读确认' }));
    await step();
    expect(acknowledge).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('button', { name: '重试阅读确认' })).toBeNull();
  });
  it('binds a pending request to its original customer while observing another', async () => {
    const detail = prepare();
    let finish!: () => void;
    const acknowledge = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; })).mockResolvedValue(undefined);
    const view = render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    const other = structuredClone(detail);
    other.conversation.conversation_id = 'other'; other.reading!.observed_sequence = 99;
    view.rerender(<Reading detail={other} acknowledge={acknowledge} />);
    await step();
    expect(acknowledge.mock.calls).toEqual([[detail.conversation.conversation_id, 'm2', 7], ['other', 'm2', 99]]);
    await act(async () => finish());
    await step();
    expect(acknowledge).toHaveBeenCalledTimes(2);
  });
  it('observes a new sequence after a pending request settles without reusing its old snapshot', async () => {
    const detail = prepare();
    let finish!: () => void;
    const acknowledge = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; })).mockResolvedValue(undefined);
    const view = render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    const next = structuredClone(detail); next.reading!.observed_sequence = 8;
    view.rerender(<Reading detail={next} acknowledge={acknowledge} />);
    await step(); expect(acknowledge).toHaveBeenCalledTimes(1);
    await act(async () => finish()); await step();
    expect(acknowledge).toHaveBeenLastCalledWith(detail.conversation.conversation_id, 'm2', 8);
  });
  it('does not clear unread for clipped messages, legacy responses, or an empty viewport', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    geometry = { m1: box(-50, -10), m2: box(100, 250), m3: box(260, 350) };
    const view = render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step(); expect(acknowledge).not.toHaveBeenCalled();
    const legacy = structuredClone(detail); delete legacy.reading;
    geometry.m1 = box(10, 60);
    view.rerender(<Reading detail={legacy} acknowledge={acknowledge} />);
    fireEvent.scroll(screen.getByTestId('viewport')); await step();
    expect(acknowledge).not.toHaveBeenCalled();
  });
  it('waits for an offscreen message viewport and observes ancestor scrolling', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    geometry.viewport = box(900, 1100);
    geometry.m1 = box(920, 980); geometry.m2 = box(990, 1060); geometry.m3 = box(1110, 1170);
    render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step(); expect(acknowledge).not.toHaveBeenCalled();
    geometry.viewport = box(0, 200);
    geometry.m1 = box(10, 70); geometry.m2 = box(80, 160); geometry.m3 = box(210, 270);
    fireEvent.scroll(document.body); await step();
    expect(acknowledge).toHaveBeenCalledExactlyOnceWith(detail.conversation.conversation_id, 'm2', 7);
  });
  it('clips reading to the visible part of a scrolling parent', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    geometry['clipping-container'] = box(0, 80);
    render(<div data-testid="clipping-container" style={{ overflowY: 'auto' }}><Reading detail={detail} acknowledge={acknowledge} /></div>);
    await step();
    expect(acknowledge).toHaveBeenCalledExactlyOnceWith(detail.conversation.conversation_id, 'm1', 7);
    geometry['clipping-container'] = box(0, 200);
    fireEvent.scroll(screen.getByTestId('clipping-container')); await step();
    expect(acknowledge).toHaveBeenLastCalledWith(detail.conversation.conversation_id, 'm2', 7);
  });
  it('does not acknowledge a message covered by a sticky header', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    const prior = Object.getOwnPropertyDescriptor(document, 'elementFromPoint');
    const hit = vi.fn(() => document.body as Element | null);
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: hit });
    try {
      render(<Reading detail={detail} acknowledge={acknowledge} />);
      await step(); expect(acknowledge).not.toHaveBeenCalled();
      hit.mockImplementation(() => document.querySelector('[data-message-id="m2"]'));
      fireEvent.scroll(document.body); await step();
      expect(acknowledge).toHaveBeenCalledExactlyOnceWith(detail.conversation.conversation_id, 'm2', 7);
    } finally {
      if (prior) Object.defineProperty(document, 'elementFromPoint', prior);
      else Reflect.deleteProperty(document, 'elementFromPoint');
    }
  });
  it('keeps an older cached body bound to the observation that supplied it', async () => {
    const detail = prepare(), acknowledge = vi.fn(async () => {});
    Object.assign(detail.messages[1], { observed_sequence: 3 });
    const view = render(<Reading detail={detail} acknowledge={acknowledge} />);
    await step();
    expect(acknowledge).toHaveBeenCalledExactlyOnceWith(detail.conversation.conversation_id, 'm2', 3);
    const newer = structuredClone(detail); newer.reading!.observed_sequence = 9;
    view.rerender(<Reading detail={newer} acknowledge={acknowledge} />);
    await step(); expect(acknowledge).toHaveBeenCalledTimes(1);
    Object.assign(newer.messages[1], { text: 'Refreshed actual body', observed_sequence: 9 });
    view.rerender(<Reading detail={structuredClone(newer)} acknowledge={acknowledge} />);
    await step();
    expect(acknowledge).toHaveBeenLastCalledWith(detail.conversation.conversation_id, 'm2', 9);
  });
  it('cancels the debounced observation on unmount', async () => {
    const acknowledge = vi.fn(async () => {});
    const view = render(<Reading detail={prepare()} acknowledge={acknowledge} />);
    view.unmount(); await step(); expect(acknowledge).not.toHaveBeenCalled();
  });
  it('uses the backend message ordering for equal timestamps including Unicode IDs', () => {
    const ids = ['z', 'a', '\u{10000}', '￿'];
    expect(ids.map(message_id => ({ message_id, timestamp_ms: 4 }) as Message).sort(compareMessages).map(m => m.message_id)).toEqual(['a', 'z', '￿', '\u{10000}']);
  });
});

describe('authoritative customer reception status', () => {
  it('prefers backend blockers over global running, ownership and config guesses', () => {
    const f = createReplyActivityFixture(), detail = f.details.emma;
    const status: ReceptionStatus = { state: 'send_unknown', reason: '发送结果待核查，请核查原操作', owner: 'human', activity: null, blockers: [{ code: 'unknown', message: '发送结果待核查，请核查原操作' }], observed_at: 10 };
    detail.conversation.reception_status = status;
    detail.conversation.human_owned = true;
    f.snapshot.reception.running = true;
    expect(receptionLabel(f.snapshot, detail, true, false)).toBe(status.reason);
    expect(receptionLabel(f.snapshot, detail, false, false)).toBe('接待服务未连接');
    expect(receptionLabel(f.snapshot, detail, true, true)).toBe('工作台正在关闭');
  });
  it('uses the newer server projection and preserves a paused in-flight activity', () => {
    const f = createReplyActivityFixture(), detail = f.details.emma;
    detail.conversation.reception_status = { state: 'generating', reason: 'AI 正在生成回复', owner: 'automatic', activity: { kind: 'generate_draft', status: 'running', job_id: 'job1' }, blockers: [], observed_at: 10 };
    const listed = f.snapshot.conversations.find(c => c.conversation_id === detail.conversation.conversation_id)!;
    listed.reception_status = { ...detail.conversation.reception_status, state: 'paused', reason: '自动接待已暂停', owner: 'none', blockers: [{ code: 'paused', message: '自动接待已暂停' }], observed_at: 20 };
    expect(effectiveReceptionStatus(f.snapshot, detail.conversation)).toBe(listed.reception_status);
    expect(receptionLabel(f.snapshot, detail, true, false)).toBe('自动接待已暂停');
    expect(effectiveReceptionStatus(f.snapshot, detail.conversation)?.activity?.status).toBe('running');
  });
});
