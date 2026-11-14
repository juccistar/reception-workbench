// @vitest-environment node
import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import { ViewTransport } from '../src/main/whatsapp/transport';

function setup() {
  const debuggerApi = Object.assign(new EventEmitter(), { isAttached: vi.fn(() => false), attach: vi.fn(), detach: vi.fn(), sendCommand: vi.fn(async () => ({ protocolVersion: '1.3', product: 'Chrome/156.0.8078.80', revision: 'native', userAgent: 'native-ascii-agent', jsVersion: 'native-v8' })) });
  const transport = new ViewTransport({ debugger: debuggerApi, getURL: () => 'https://web.whatsapp.com/' } as never);
  const messages: any[] = [];
  transport.onmessage = raw => messages.push(JSON.parse(raw));
  return { transport, messages, debuggerApi };
}
describe('single-view CDP boundaries', () => {
  it('returns the controlled native browser version instead of a fixed product string', async () => {
    const { transport, messages, debuggerApi } = setup();
    transport.send(JSON.stringify({ id: 1, method: 'Browser.getVersion' }));
    await vi.waitFor(() => expect(messages).toHaveLength(1));
    expect(debuggerApi.sendCommand).toHaveBeenCalledExactlyOnceWith('Browser.getVersion');
    expect(messages[0].result).toMatchObject({ product: 'Chrome/156.0.8078.80', revision: 'native', jsVersion: 'native-v8' });
    transport.close();
  });
  it('does not expand browser targets or forward commands with an arbitrary page session', async () => {
    const { transport, messages, debuggerApi } = setup();
    transport.send(JSON.stringify({ id: 2, method: 'Target.getTargets' }));
    transport.send(JSON.stringify({ id: 3, method: 'Runtime.evaluate', sessionId: 'other-page', params: { expression: 'private-data' } }));
    await vi.waitFor(() => expect(messages).toHaveLength(2));
    expect(debuggerApi.sendCommand).not.toHaveBeenCalled();
    expect(messages.every(message => message.error?.message === 'Page command failed')).toBe(true);
    expect(JSON.stringify(messages)).not.toContain('private-data');
    transport.close();
  });
  it('does not leak a pending native result after transport close', async () => {
    const { transport, messages, debuggerApi } = setup();
    let finish!: (value: any) => void;
    debuggerApi.sendCommand.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    transport.send(JSON.stringify({ id: 4, method: 'Browser.getVersion' }));
    transport.close();
    finish({ product: 'late' });
    await Promise.resolve(); await Promise.resolve();
    expect(messages).toEqual([]);
    expect(debuggerApi.listenerCount('message')).toBe(0);
  });
});
