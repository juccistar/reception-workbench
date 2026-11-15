import { spawn } from 'node:child_process';

export interface ModelCheckProcessOptions { executable: string; prefix: string[]; cwd: string; env: NodeJS.ProcessEnv; timeoutMs?: number }
/** Runs a single isolated check process. Request and result schema are owned by the provider. */
export class ModelCheckHost {
  private readonly active = new Map<string, { promise: Promise<unknown>; cancel: () => void }>();
  constructor(private readonly options: ModelCheckProcessOptions) {}
  check(identity: { profile_id: string; config_revision: number; credential_revision: number; purpose: 'reception' | 'translation' }, request: unknown): Promise<unknown> {
    if (!identity || !/^[A-Za-z0-9_-]{1,128}$/.test(identity.profile_id) || !Number.isSafeInteger(identity.config_revision) || !Number.isSafeInteger(identity.credential_revision) || !['reception', 'translation'].includes(identity.purpose)) return Promise.reject(new Error('model_check_invalid'));
    const id = JSON.stringify(identity); const existing = this.active.get(id); if (existing) return existing.promise;
    let cancel = () => {}; const payload = JSON.stringify(request);
    if (!payload || Buffer.byteLength(payload) > 128 * 1024) return Promise.reject(new Error('model_check_invalid'));
    const promise = new Promise<unknown>((resolve, reject) => {
      const env: NodeJS.ProcessEnv = {};
      for (const [k, v] of Object.entries(this.options.env)) if (['systemroot', 'windir', 'systemdrive', 'temp', 'tmp', 'path', 'pathext', 'comspec', 'userprofile', 'localappdata'].includes(k.toLowerCase())) env[k] = v;
      env.PYTHONIOENCODING = 'utf-8';
      let child: ReturnType<typeof spawn>;
      try { child = spawn(this.options.executable, [...this.options.prefix, '--model-check'], { cwd: this.options.cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] }); } catch { reject(new Error('model_check_unavailable')); return; }
      let finished = false; let total = 0; const chunks: Buffer[] = [];
      const finish = (error?: string, value?: unknown) => { if (finished) return; finished = true; clearTimeout(timer); if (error) reject(new Error(error)); else resolve(value); };
      let stopped: string | undefined;
      const stop = (reason: string) => { if (!stopped) { stopped = reason; child.kill(); } };
      const timer = setTimeout(() => stop('model_check_timeout'), Math.min(Math.max(this.options.timeoutMs ?? 90000, 1000), 180000));
      cancel = () => stop('model_check_cancelled');
      child.stderr?.resume(); child.stdin?.on('error', () => {});
      child.stdout?.on('data', (chunk: Buffer) => { total += chunk.length; if (total > 1024 * 1024) stop('model_check_invalid_result'); else chunks.push(chunk); });
      child.once('error', () => { stopped ??= 'model_check_unavailable'; });
      child.once('close', code => { if (finished) return; if (stopped) { finish(stopped); return; } if (code !== 0) { finish('model_check_failed'); return; } try { const text = Buffer.concat(chunks).toString('utf8').trim(); const value: unknown = JSON.parse(text); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); finish(undefined, value); } catch { finish('model_check_invalid_result'); } });
      child.stdin?.end(payload + '\n');
    });
    this.active.set(id, { promise, cancel: () => cancel() });
    void promise.finally(() => { if (this.active.get(id)?.promise === promise) this.active.delete(id); }).catch(() => {});
    return promise;
  }
  cancel(profileId: string, purpose?: 'reception' | 'translation'): void { for (const [id, task] of this.active) { const identity = JSON.parse(id) as { profile_id: string; purpose: string }; if (identity.profile_id === profileId && (!purpose || identity.purpose === purpose)) task.cancel(); } }
  close(): void { for (const task of this.active.values()) task.cancel(); }
}
