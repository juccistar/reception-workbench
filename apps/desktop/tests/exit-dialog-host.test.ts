// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { createExitDialogHost } from '../src/main/exit-dialog-host';
import type { ExitDialogEvent } from '../src/shared/exit-dialog';

function setup(timeoutMs = 1000) {
  const first = { frame: {} as object | null };
  const second = { frame: {} as object | null };
  const events: ExitDialogEvent[] = [];
  const host = createExitDialogHost({
    currentFrame: (window: typeof first) => window.frame,
    isSource: (source: { window: typeof first; frame: object }, window, frame) => source.window === window && source.frame === frame,
    send: (_window, event) => events.push(event), timeoutMs,
  });
  const source = (window = first) => ({ window, frame: window.frame! });
  const open = () => events.filter(event => event.kind === 'open').at(-1)!.request_id;
  return { first, second, events, host, source, open };
}

describe('exit dialog host', () => {
  it('accepts an exact one-time response from the captured window and frame', async () => {
    const t = setup(); const result = t.host.request(t.first); const id = t.open();
    expect(t.host.respond(t.source(), { request_id: id, choice: 'save' })).toBe(true);
    expect(await result).toBe('save');
    expect(t.host.respond(t.source(), { request_id: id, choice: 'discard' })).toBe(false);
    expect(t.events.at(-1)).toEqual({ kind: 'dismiss', request_id: id });
  });
  it('rejects old responses, forged windows, changed frames and invalid choices', async () => {
    const t = setup(); const old = t.host.request(t.first); const oldId = t.open();
    const current = t.host.request(t.first); const id = t.open();
    expect(await old).toBe('cancel'); expect(id).not.toBe(oldId);
    expect(t.host.respond(t.source(), { request_id: oldId, choice: 'save' })).toBe(false);
    expect(t.host.respond(t.source(t.second), { request_id: id, choice: 'save' })).toBe(false);
    expect(t.host.respond(t.source(), { request_id: id, choice: 'other' })).toBe(false);
    const original = t.source(); t.first.frame = {};
    expect(t.host.respond(original, { request_id: id, choice: 'save' })).toBe(false);
    expect(t.host.respond(t.source(), { request_id: id, choice: 'save' })).toBe(false);
    t.host.cancel(); expect(await current).toBe('cancel');
  });
  it('resolves cancellation and timeout without authorizing exit', async () => {
    vi.useFakeTimers();
    try {
      const t = setup(50); const cancelled = t.host.request(t.first); const id = t.open();
      expect(t.host.respond(t.source(), { request_id: id, choice: 'cancel' })).toBe(true);
      expect(await cancelled).toBe('cancel');
      const timed = t.host.request(t.first); const timedId = t.open();
      await vi.advanceTimersByTimeAsync(50);
      expect(await timed).toBe('cancel');
      expect(t.host.respond(t.source(), { request_id: timedId, choice: 'save' })).toBe(false);
      expect(t.events.at(-1)).toEqual({ kind: 'dismiss', request_id: timedId });
    } finally { vi.useRealTimers(); }
  });
  it('fails closed when delivery or frame lookup fails', async () => {
    const host = createExitDialogHost({ currentFrame: () => ({}), isSource: () => true, send: () => { throw new Error('closed'); } });
    expect(await host.request({})).toBe('cancel');
    const noFrame = createExitDialogHost({ currentFrame: () => null, isSource: () => true, send: () => {} });
    expect(await noFrame.request({})).toBe('cancel');
  });
});
