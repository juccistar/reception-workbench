import { describe, expect, it, vi } from 'vitest';
import { createEventRefresh } from '../src/renderer/hooks/eventRefresh';

describe('event refresh', () => {
  it('does not postpone a sustained burst and reruns once for changes during a read', async () => {
    vi.useFakeTimers();
    try {
      let release!: () => void;
      const held = new Promise<void>(resolve => { release = resolve; });
      const run = vi.fn().mockImplementationOnce(() => held).mockResolvedValue(undefined);
      const refresh = createEventRefresh(run, 70);
      refresh.request();
      await vi.advanceTimersByTimeAsync(60);
      refresh.request();
      await vi.advanceTimersByTimeAsync(10);
      expect(run).toHaveBeenCalledTimes(1);
      refresh.request(); refresh.request();
      expect(run).toHaveBeenCalledTimes(1);
      release();
      await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(2));
      refresh.stop();
    } finally { vi.useRealTimers(); }
  });

  it('does not block a different conversation behind an in-flight read', async () => {
    vi.useFakeTimers();
    try {
      let release!: () => void;
      const held = new Promise<void>(resolve => { release = resolve; });
      const run = vi.fn((key: string) => key === 'old' ? held : Promise.resolve());
      const refresh = createEventRefresh(run, 10);
      refresh.request('old');
      await vi.advanceTimersByTimeAsync(10);
      refresh.request('new');
      await vi.advanceTimersByTimeAsync(10);
      expect(run).toHaveBeenCalledWith('new');
      release(); refresh.stop();
    } finally { vi.useRealTimers(); }
  });

  it('stops pending work and does not schedule further passes', async () => {
    vi.useFakeTimers();
    try {
      const run = vi.fn().mockResolvedValue(undefined);
      const refresh = createEventRefresh(run, 70);
      refresh.request(); refresh.stop(); refresh.request();
      await vi.advanceTimersByTimeAsync(100);
      expect(run).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
});
