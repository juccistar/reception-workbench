import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { EventEmitter } from 'node:events';
import type { ServiceEvent } from '../shared/api';
import { SUPPLEMENT_UPLOAD_ENVELOPE_BYTES } from '../shared/media-limits';

export class BackendError extends Error {
  constructor(public code: string, message: string) { super(message); this.name = 'BackendError'; }
}
interface Pending { resolve(value: unknown): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }

/** One owned Python process; stdout is a framed protocol, stderr is never forwarded. */
export class ServiceClient extends EventEmitter {
  private child: ChildProcessWithoutNullStreams;
  private pending = new Map<string, Pending>();
  private exited = false;
  private closing = false;
  private exitPromise: Promise<void>;
  private resolveExit!: () => void;
  constructor(executable: string, args: string[], cwd: string, env: NodeJS.ProcessEnv) {
    super();
    this.exitPromise = new Promise(resolve => { this.resolveExit = resolve; });
    this.child = spawn(executable, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    this.child.stdin.on('error', () => { /* exit/error paths settle pending calls */ });
    this.child.stderr.on('data', () => { /* Never copy native content into UI or diagnostics. */ });
    const lines = createInterface({ input: this.child.stdout, crlfDelay: Infinity });
    lines.on('line', line => this.receive(line));
    this.child.on('error', () => this.disconnected('服务未能启动，请检查 Python 运行环境或程序文件。'));
    this.child.on('exit', () => this.disconnected(this.closing ? '后台服务已结束。' : '后台服务已断开；未确认的操作需要核查。'));
    // An error makes RPC unavailable, but only close proves that the owned
    // process and its stdio have ended (including a failed spawn).
    this.child.once('close', () => this.resolveExit());
  }
  private receive(line: string) {
    // stdout can drain after the child has reported exit or a spawn error.
    if (this.exited) return;
    if (line.length > 8 * 1024 * 1024) { this.emit('protocol-error', '后台返回超过通信预算。'); return; }
    let value: Record<string, unknown>;
    try { value = JSON.parse(line); } catch { this.emit('protocol-error', '后台返回格式异常。'); return; }
    if (!value || typeof value !== 'object') return;
    if (typeof value.event === 'string') { this.emit('event', { event: value.event, data: value.data } satisfies ServiceEvent); return; }
    if (typeof value.id !== 'string') return;
    const pending = this.pending.get(value.id);
    if (!pending) return;
    this.pending.delete(value.id); clearTimeout(pending.timer);
    if (value.error && typeof value.error === 'object') {
      const error = value.error as Record<string, unknown>;
      pending.reject(new BackendError(typeof error.code === 'string' ? error.code : 'backend_error', typeof error.message === 'string' ? error.message.slice(0, 2000) : '操作未完成。'));
    } else pending.resolve(value.result);
  }
  private disconnected(message: string) {
    if (this.exited) return;
    this.exited = true;
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(new BackendError('backend_unavailable', message)); }
    this.pending.clear();
    this.emit('event', { event: 'backend.disconnected', data: { message, expected: this.closing } });
  }
  invoke<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (this.exited || (this.closing && method !== 'shutdown')) return Promise.reject(new BackendError('backend_unavailable', '后台服务不可用。'));
    const id = randomUUID();
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new BackendError('rpc_timeout', '后台尚未确认此操作；请核查任务记录，不要重复提交。')); }, 30000);
      this.pending.set(id, { resolve: value => resolve(value as T), reject, timer });
      const line = JSON.stringify({ id, method, params }) + '\n';
      if (method === 'upload_supplement_attachment' && Buffer.byteLength(line, 'utf8') > SUPPLEMENT_UPLOAD_ENVELOPE_BYTES) {
        this.pending.delete(id); clearTimeout(timer);
        reject(new BackendError('request_too_large', '请求超过大小限制。'));
        return;
      }
      this.child.stdin.write(line, 'utf8', error => {
        if (error) { const item = this.pending.get(id); this.pending.delete(id); clearTimeout(timer); item?.reject(new BackendError('backend_unavailable', '后台通信不可用。')); }
      });
    });
  }
  async shutdown(): Promise<void> {
    if (this.exited) return this.exitPromise;
    if (!this.closing) {
      this.closing = true;
      try { await this.invoke('shutdown'); } catch { /* stdin EOF also requests revocation and shutdown */ }
      this.child.stdin.end();
    }
    await this.exitPromise;
  }
  hasExited() { return this.exited; }
  /** Only call after a separate, explicit native force-exit confirmation. */
  forceExit() { if (!this.exited) this.child.kill(); }
}
