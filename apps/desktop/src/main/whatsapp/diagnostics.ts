import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, renameSync, statSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import type { WhatsAppHostErrorCode, WhatsAppPhase } from '../../shared/whatsapp';

const LIMIT = 2 * 1024 * 1024;
const exceptionNames = new Set(['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'URIError', 'EvalError', 'AggregateError', 'TimeoutError', 'ProtocolError', 'TargetCloseError', 'AbortError', 'DOMException', 'WhatsAppConnectionError']);
const phases = new Set<WhatsAppPhase>(['page_reload', 'view_setup', 'cdp_connect', 'auth_setup', 'version_cache', 'page_navigation', 'app_modules', 'sdk_inject', 'sdk_callbacks', 'qr_wait', 'authenticated', 'ready']);
const token = (value: string | undefined) => value && /^[0-9A-Za-z.+-]{1,50}$/.test(value) ? value : 'unknown';

function exceptionType(error: unknown): string {
  const name = error instanceof Error ? error.name : '';
  return exceptionNames.has(name) ? name : 'Error';
}

function frames(error: unknown): Array<{ file: string; line: number; column: number; function?: string }> {
  if (!(error instanceof Error) || typeof error.stack !== 'string') return [];
  return error.stack.slice(0, 16_000).split('\n').slice(1, 18).flatMap(row => {
    const match = /^\s*at (?:(.*?) \()?(.+):(\d+):(\d+)\)?$/.exec(row);
    if (!match) return [];
    const source = match[2].replaceAll('\\', '/');
    const file = /(?:^|\/)(apps\/desktop\/src\/[A-Za-z0-9_./-]+\.(?:ts|js))$/.exec(source)?.[1]
      ?? /(?:^|\/)((?:app\.asar\/)?main\/[A-Za-z0-9_./-]+\.(?:js|cjs))$/.exec(source)?.[1]
      ?? /(?:^|\/)(whatsapp-runtime\.asar\/node_modules\/[A-Za-z0-9@_./-]+\.(?:js|cjs))$/.exec(source)?.[1];
    if (!file || file.split('/').includes('..')) return [];
    const name = match[1];
    return [{ file, line: Number(match[3]), column: Number(match[4]), ...(name && /^[A-Za-z_$][A-Za-z0-9_.$ <>]{0,95}$/.test(name) ? { function: name } : {}) }];
  }).slice(0, 10);
}

/** Host-owned fixed-schema diagnostics. Never serializes an SDK error or its message. */
export class WhatsAppDiagnostics {
  private readonly recent = new Map<string, { at: number; id?: string }>();
  private readonly versions: Record<string, string>;
  private context: Record<string, string | number | boolean> = {};
  setContext(value: { webVersion?: unknown; documentGeneration?: number; postLogout?: boolean; databaseError?: boolean; sessionGeneration?: number; recoveryStage?: 'committed'; previousPhase?: WhatsAppPhase }): void {
    const context: Record<string, string | number | boolean> = {};
    if (typeof value.webVersion === 'string' && /^[0-9]+(?:\.[0-9]+){1,5}$/.test(value.webVersion) && value.webVersion.length <= 50) context.web_version = value.webVersion;
    if (Number.isSafeInteger(value.documentGeneration) && value.documentGeneration! >= 0) context.document_generation = value.documentGeneration!;
    if (typeof value.postLogout === 'boolean') context.post_logout = value.postLogout;
    if (typeof value.databaseError === 'boolean') context.database_error = value.databaseError;
    if (Number.isSafeInteger(value.sessionGeneration) && value.sessionGeneration! >= 0) context.session_generation = value.sessionGeneration!;
    if (value.recoveryStage === 'committed') context.recovery_stage = value.recoveryStage;
    if (value.previousPhase && phases.has(value.previousPhase)) context.previous_phase = value.previousPhase;
    this.context = context;
  }
  constructor(private readonly directory: string | undefined, version: string) {
    this.versions = { app: token(version), electron: token(process.versions.electron), chromium: token(process.versions.chrome), whatsapp_web_js: '1.34.8', puppeteer: '24.38.0' };
  }
  private write(row: Record<string, unknown>): boolean {
    if (!this.directory) return false;
    try {
      mkdirSync(this.directory, { recursive: true });
      const file = join(this.directory, 'whatsapp-host.jsonl');
      const bytes = JSON.stringify(row) + '\n';
      if (existsSync(file) && statSync(file).size + Buffer.byteLength(bytes) > LIMIT) {
        if (existsSync(file + '.2')) unlinkSync(file + '.2');
        if (existsSync(file + '.1')) renameSync(file + '.1', file + '.2');
        renameSync(file, file + '.1');
      }
      appendFileSync(file, bytes, { encoding: 'utf8', mode: 0o600 });
      return true;
    } catch { return false; }
  }
  event(attempt: number, phase: WhatsAppPhase, event: 'started' | 'completed', elapsedMs?: number): void {
    if (!phases.has(phase) || !Number.isSafeInteger(attempt) || attempt < 0) return;
    this.write({ time_ms: Date.now(), attempt, phase, event, versions: this.versions, ...this.context,
      ...(Number.isFinite(elapsedMs) && elapsedMs! >= 0 ? { elapsed_ms: Math.round(elapsedMs!) } : {}) });
  }
  failure(attempt: number, phase: WhatsAppPhase, error: unknown, code: WhatsAppHostErrorCode, httpStatus?: number, fatal = true): string | undefined {
    if (!phases.has(phase) || !Number.isSafeInteger(attempt) || attempt < 0) return;
    const type = exceptionType(error), now = Date.now();
    const key = attempt + ':' + phase + ':' + type + ':' + code + ':' + fatal;
    for (const [entry, value] of this.recent) if (now - value.at >= 10_000) this.recent.delete(entry);
    if (this.recent.size >= 128) this.recent.delete(this.recent.keys().next().value!);
    const previous = this.recent.get(key);
    if (previous && now - previous.at < 10_000) return previous.id;
    const id = 'wa_diag_' + randomUUID();
    const written = this.write({ time_ms: now, diagnostic_id: id, attempt, phase, event: fatal ? 'failed' : 'page_notice', error_code: code,
      exception_type: type, frames: frames(error), versions: this.versions, ...this.context,
      ...(Number.isInteger(httpStatus) && httpStatus! >= 100 && httpStatus! <= 599 ? { http_status: httpStatus } : {}) });
    this.recent.set(key, { at: now, ...(written ? { id } : {}) });
    return written ? id : undefined;
  }
}
