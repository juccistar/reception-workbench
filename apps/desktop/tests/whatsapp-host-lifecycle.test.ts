// @vitest-environment node
import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import { ChromeWhatsAppHost } from '../src/main/whatsapp/chrome-host';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}

describe('WhatsApp host connection boundaries', () => {
  it('drops history and buffered outgoing events from a previous connection', async () => {
    const events: any[] = [];
    const host: any = Object.create(ChromeWhatsAppHost.prototype);
    Object.assign(host, { options: { onEvent: (event: any) => events.push(event) }, closed: false, generation: 1, status: { status: 'ready', account_id: 'old', connection_epoch: 'old-epoch' }, pending: new Map([['old-epoch', 1]]), outgoing: [] });
    const history = deferred<any[]>();
    const client: any = new EventEmitter();
    client.getChats = () => history.promise;
    host.client = client;
    host.observe(client, 1);
    const reading = host.history(client, 1, 'old', 'old-epoch');
    client.emit('message_create', { id: { _serialized: 'out' }, fromMe: true, to: 'one@lid', body: 'a', timestamp: 1 });
    expect(host.outgoing[0].account).toBe('old');
    host.generation = 2; host.status = { status: 'ready', account_id: 'new', connection_epoch: 'new-epoch' };
    history.resolve([{ id: { _serialized: 'one@lid' }, name: 'contact', fetchMessages: async () => [] }]);
    await reading;
    client.emit('message_create', { id: { _serialized: 'late' }, fromMe: false, from: 'one@lid', body: 'b', timestamp: 1 });
    expect(events).toEqual([]);
  });
  it('disposes a blob handle that arrives after the connection is replaced', async () => {
    const host: any = Object.create(ChromeWhatsAppHost.prototype);
    const pending = deferred<any>();
    const handle = { evaluate: vi.fn(), dispose: vi.fn(async () => {}) };
    const evaluateHandle = vi.fn(() => pending.promise);
    Object.assign(host, { closed: false, generation: 1, status: { status: 'ready', account_id: 'self', connection_epoch: 'epoch' }, lifetime: new AbortController(), tasks: new Set(), client: {
      getMessageById: async () => ({ id: 'image', from: 'chat@lid', type: 'image', hasMedia: true }),
      pupPage: { evaluateHandle },
    } });
    const operation = host.downloadImage('self', 'epoch', 'chat@lid', 'image', async () => {});
    await vi.waitFor(() => expect(evaluateHandle).toHaveBeenCalledOnce());
    host.generation++;
    pending.resolve(handle);
    await expect(operation).rejects.toThrow('source changed');
    expect(handle.dispose).toHaveBeenCalledOnce();
    expect(handle.evaluate).not.toHaveBeenCalled();
  });
  it('disposes the handle when the connection changes during a chunk read', async () => {
    const host: any = Object.create(ChromeWhatsAppHost.prototype);
    const pending = deferred<string>();
    const handle = { evaluate: vi.fn().mockResolvedValueOnce({ size: 5, mime: 'image/png' }).mockReturnValueOnce(pending.promise), dispose: vi.fn(async () => {}) };
    Object.assign(host, { closed: false, generation: 1, status: { status: 'ready', account_id: 'self', connection_epoch: 'epoch' }, lifetime: new AbortController(), tasks: new Set(), client: {
      getMessageById: async () => ({ id: 'image', from: 'chat@lid', type: 'image', hasMedia: true }),
      pupPage: { evaluateHandle: async () => handle },
    } });
    const operation = host.downloadImage('self', 'epoch', 'chat@lid', 'image', async (_mime: string, _size: number, chunks: AsyncIterable<Buffer>) => { for await (const _chunk of chunks) {} });
    await vi.waitFor(() => expect(handle.evaluate).toHaveBeenCalledTimes(2));
    host.generation++;
    pending.resolve(Buffer.from('image').toString('base64'));
    await expect(operation).rejects.toThrow('source changed');
    expect(handle.dispose).toHaveBeenCalledOnce();
  });
  it('disposes a late handle after the page hangs past the deadline', async () => {
    vi.useFakeTimers();
    try {
      const host: any = Object.create(ChromeWhatsAppHost.prototype);
      const pending = deferred<any>();
      const handle = { evaluate: vi.fn(), dispose: vi.fn(async () => {}) };
      const evaluateHandle = vi.fn(() => pending.promise);
      Object.assign(host, { closed: false, generation: 1, status: { status: 'ready', account_id: 'self', connection_epoch: 'epoch' }, lifetime: new AbortController(), tasks: new Set(), client: {
        getMessageById: async () => ({ id: 'image', from: 'chat@lid', type: 'image', hasMedia: true }),
        pupPage: { evaluateHandle },
      } });
      const operation = host.downloadImage('self', 'epoch', 'chat@lid', 'image', async () => {});
      for (let i = 0; i < 10 && !evaluateHandle.mock.calls.length; i++) await Promise.resolve();
      expect(evaluateHandle).toHaveBeenCalledOnce();
      const rejection = expect(operation).rejects.toThrow('source changed');
      await vi.advanceTimersByTimeAsync(60_000);
      await rejection;
      pending.resolve(handle);
      await Promise.resolve();
      expect(handle.dispose).toHaveBeenCalledOnce();
    } finally { vi.useRealTimers(); }
  });
});
