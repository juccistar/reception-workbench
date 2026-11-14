import type { WebContents } from 'electron';

/** A CDP connection scoped to one WebContents. Browser/Target operations are virtual. */
export class ViewTransport {
  onmessage?: (message: string) => void;
  onclose?: () => void;
  private closed = false;
  private readonly target = 'whatsapp-view';
  private readonly session = 'whatsapp-page';
  private readonly listener: (_event: Electron.Event, method: string, params: unknown, sessionId?: string) => void;
  constructor(private readonly contents: WebContents) {
    if (contents.debugger.isAttached()) throw new Error('View debugger already attached');
    contents.debugger.attach('1.3');
    this.listener = (_event, method, params, sessionId) => {
      if (!sessionId && !method.startsWith('Target.')) this.emit({ method, params, sessionId: this.session });
    };
    contents.debugger.on('message', this.listener);
    contents.debugger.once('detach', () => this.close());
  }
  private emit(value: unknown): void { if (!this.closed) queueMicrotask(() => this.onmessage?.(JSON.stringify(value))); }
  send(raw: string): void {
    const command = JSON.parse(raw) as { id: number; method: string; params?: Record<string, unknown>; sessionId?: string };
    void this.dispatch(command).catch(() => this.emit({ id: command.id, sessionId: command.sessionId, error: { code: -32000, message: 'Page command failed' } }));
  }
  private async dispatch(c: { id: number; method: string; params?: Record<string, unknown>; sessionId?: string }): Promise<void> {
    const info = { targetId: this.target, type: 'page', title: '', url: this.contents.getURL() || 'about:blank', attached: true, canAccessOpener: false };
    let result: unknown = {};
    if (!c.sessionId) {
      switch (c.method) {
        case 'Target.getBrowserContexts': result = { browserContextIds: [] }; break;
        case 'Target.setDiscoverTargets': this.emit({ method: 'Target.targetCreated', params: { targetInfo: info } }); break;
        case 'Target.setAutoAttach':
          this.emit({ method: 'Target.attachedToTarget', params: { sessionId: this.session, targetInfo: info, waitingForDebugger: false } }); break;
        case 'Target.createTarget': result = { targetId: this.target }; break;
        case 'Browser.getVersion': result = await this.contents.debugger.sendCommand('Browser.getVersion'); break;
        default: throw new Error('Unsupported browser command');
      }
    } else {
      if (c.sessionId !== this.session) throw new Error('Invalid session');
      if (c.method === 'Target.setAutoAttach' || c.method === 'Runtime.runIfWaitingForDebugger') result = {};
      else if (c.method.startsWith('Target.') || c.method.startsWith('Browser.')) throw new Error('Unsupported target command');
      else result = await this.contents.debugger.sendCommand(c.method, c.params);
    }
    this.emit({ id: c.id, sessionId: c.sessionId, result });
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.contents.debugger.removeListener('message', this.listener);
    if (this.contents.debugger.isAttached()) this.contents.debugger.detach();
    this.onclose?.();
  }
}
