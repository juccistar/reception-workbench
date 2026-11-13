// @vitest-environment node
import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import { WhatsAppHost } from '../src/main/whatsapp/host';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}

describe('WhatsApp host connection boundaries', () => {
  it('drops history and buffered outgoing events from a previous connection', async () => {
    const events: any[] = [];
    const host: any = Object.create(WhatsAppHost.prototype);
    Object.assign(host, { options: { onEvent: (event: any) => events.push(event) }, closing: false, generation: 1, status: 'ready', account: 'old', epoch: 'old-epoch', pendingSends: new Map([['old-epoch', 1]]), outgoing: [] });
    const history = deferred<any[]>();
    const client: any = new EventEmitter();
    client.getChats = () => history.promise;
    host.client = client;
    host.bindEvents(client, 1);
    const reading = host.readHistory(client, 1, 'old', 'old-epoch');
    client.emit('message_create', { id: { _serialized: 'out' }, fromMe: true, to: 'one@lid', body: 'a', timestamp: 1 });
    expect(host.outgoing[0].account).toBe('old');
    host.generation = 2; host.account = 'new'; host.epoch = 'new-epoch';
    history.resolve([{ id: { _serialized: 'one@lid' }, name: 'contact', fetchMessages: async () => [] }]);
    await reading;
    client.emit('message_create', { id: { _serialized: 'late' }, fromMe: false, from: 'one@lid', body: 'b', timestamp: 1 });
    expect(events).toEqual([]);
  });
  it('retires a failed connection, releases transport and permits another attempt', () => {
    const host: any = Object.create(WhatsAppHost.prototype);
    const client: any = new EventEmitter();
    const browser = { disconnect: vi.fn() }, transport = { close: vi.fn() };
    Object.assign(host, { options: { onEvent: vi.fn() }, closing: false, generation: 1, status: 'connecting', account: '', epoch: '', started: true, client, browser, transport, outgoing: [], pendingSends: new Map() });
    host.bindEvents(client, 1);
    client.emit('auth_failure');
    expect(host.started).toBe(false);
    expect(host.status).toBe('error');
    expect(browser.disconnect).toHaveBeenCalledOnce();
    expect(transport.close).toHaveBeenCalledOnce();
    expect(host.generation).toBe(2);
  });
});
