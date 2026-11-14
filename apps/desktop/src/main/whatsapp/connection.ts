import type { WhatsAppHostErrorCode, WhatsAppPhase } from '../../shared/whatsapp';

/** Only these codes cross the host boundary; SDK messages can contain private data. */
export class WhatsAppConnectionError extends Error {
  constructor(readonly error_code: WhatsAppHostErrorCode, readonly http_status?: number) {
    super(error_code);
  }
}

export function whatsappUserAgent(original: string, applicationName: string, version: string): string {
  if (!/^[0-9A-Za-z.+-]+$/.test(version)) throw new WhatsAppConnectionError('sdk_error');
  const escaped = applicationName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // Use the actual Chromium browser agent. Optional app/Electron product tags
  // can make WhatsApp's browser parser read the host version as Chrome's.
  const normalized = original.replace(new RegExp(`(?:^|\\s)${escaped}/[^\\s]+`, 'g'), ' ')
    .replace(/\sElectron\/[^\s]+/g, '').replace(/\s{2,}/g, ' ').trim();
  if (!normalized || /[^\x20-\x7e]/.test(normalized)) throw new WhatsAppConnectionError('sdk_error');
  return normalized;
}

export function retryAfterDelay(value: string | undefined, now = Date.now()): number {
  if (value && /^\d+$/.test(value.trim())) {
    const delay = Number(value.trim()) * 1000;
    if (Number.isFinite(delay)) return delay;
  }
  const date = value ? Date.parse(value) : NaN;
  return Number.isFinite(date) ? Math.max(0, date - now) : 60_000;
}

export class WhatsAppStepFailure extends Error {
  constructor(readonly phase: WhatsAppPhase, readonly original: unknown) { super('WhatsApp connection step failed'); }
}

export function connectionFailure(error: unknown, timeout: 'navigation_timeout' | 'sdk_timeout' = 'sdk_timeout', phase?: WhatsAppPhase): WhatsAppConnectionError {
  if (error instanceof WhatsAppStepFailure) return connectionFailure(error.original, timeout, error.phase);
  if (error instanceof WhatsAppConnectionError) return error;
  // Inspect only to select a fixed code; never expose or persist the source text.
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : '';
  if (/ERR_CERT_|ERR_SSL_|TLS|certificate/i.test(text)) return new WhatsAppConnectionError('tls_error');
  if (/ERR_TIMED_OUT/i.test(text)) return new WhatsAppConnectionError('navigation_timeout');
  if (error instanceof Error && error.name === 'TimeoutError') return new WhatsAppConnectionError(timeout);
  if (/ERR_[A-Z_]+/.test(text)) return new WhatsAppConnectionError('network_error');
  if (/ready timeout/i.test(text)) return new WhatsAppConnectionError('ready_timeout');
  if (/auth timeout/i.test(text)) return new WhatsAppConnectionError('sdk_timeout');
  if (/ENOENT|Cannot find module|no such file/i.test(text)) return new WhatsAppConnectionError('sdk_resource_missing');
  if (/Execution context (?:was destroyed|is not available)|Cannot find context|Cannot find execution context|Inspected target navigated/i.test(text)) return new WhatsAppConnectionError('page_context_lost');
  if (phase === 'cdp_connect' || /Protocol error|Page command failed|Session closed|Target closed/i.test(text)) return new WhatsAppConnectionError('cdp_error');
  if (phase === 'app_modules') return new WhatsAppConnectionError('app_module_error');
  return new WhatsAppConnectionError('sdk_error');
}
