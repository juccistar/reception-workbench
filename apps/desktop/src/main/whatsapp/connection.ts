import type { WhatsAppHostErrorCode } from '../../shared/whatsapp';

/** Only these codes cross the host boundary; SDK messages can contain private data. */
export class WhatsAppConnectionError extends Error {
  constructor(readonly error_code: WhatsAppHostErrorCode, readonly http_status?: number) {
    super(error_code);
  }
}

export function whatsappUserAgent(original: string, applicationName: string, version: string): string {
  if (!/^[0-9A-Za-z.+-]+$/.test(version)) throw new WhatsAppConnectionError('sdk_error');
  const escaped = applicationName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const normalized = original.replace(new RegExp(`${escaped}/[^\\s]+`, 'g'), `ReceptionWorkbench/${version}`);
  if (!normalized || /[^\x20-\x7e]/.test(normalized)) throw new WhatsAppConnectionError('sdk_error');
  return normalized;
}

export function connectionFailure(error: unknown, timeout: 'navigation_timeout' | 'sdk_timeout' = 'sdk_timeout'): WhatsAppConnectionError {
  if (error instanceof WhatsAppConnectionError) return error;
  // Inspect only to select a fixed code; never expose or persist the source text.
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : '';
  if (/ERR_CERT_|ERR_SSL_|TLS|certificate/i.test(text)) return new WhatsAppConnectionError('tls_error');
  if (/ERR_TIMED_OUT/i.test(text)) return new WhatsAppConnectionError('navigation_timeout');
  if (error instanceof Error && error.name === 'TimeoutError') return new WhatsAppConnectionError(timeout);
  if (/ERR_[A-Z_]+/.test(text)) return new WhatsAppConnectionError('network_error');
  if (/ready timeout/i.test(text)) return new WhatsAppConnectionError('ready_timeout');
  if (/auth timeout/i.test(text)) return new WhatsAppConnectionError('sdk_timeout');
  return new WhatsAppConnectionError('sdk_error');
}
