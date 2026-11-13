export const TRANSLATION_ENDPOINT_ERROR = '服务地址须为有效的 HTTPS API 地址；本机网关可使用 HTTP（127.0.0.1、localhost 或 [::1]）。';

/** Validate the raw authority before URL can rewrite alternate numeric IPs. */
export function parseTranslationEndpoint(value: string): URL {
  try {
    if (value !== value.trim() || /[\u0000- \u007f\\]/.test(value)) throw new Error();
    const match = /^(https?):\/\/([^/?#]+)(\/[^?#]*)?$/i.exec(value);
    if (!match) throw new Error();
    const authority = /^(\[[^\]]+\]|[^:@]+)(?::([0-9]+))?$/.exec(match[2]);
    if (!authority || match[2].includes('%')) throw new Error();
    const rawHostname = authority[1].toLowerCase();
    const rawHost = rawHostname.replace(/\.$/, '');
    if (!rawHost.startsWith('[') && (!/^[a-z0-9-]+(?:\.[a-z0-9-]+)*$/.test(rawHost)
        || rawHost.split('.').some(label => label.startsWith('-') || label.endsWith('-') || /^0x[0-9a-f]+$/.test(label)))) throw new Error();
    const rawPath = match[3] || '';
    if (rawPath.includes('%') || rawPath.includes('//') || rawPath.split('/').some(part => part === '.' || part === '..')) throw new Error();
    const endpoint = new URL(value);
    const host = endpoint.hostname.toLowerCase().replace(/\.+$/, '');
    if (!host || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || authority[2] && Number(authority[2]) === 0) throw new Error();
    if (/^[0-9.]+$/.test(host) && (host !== rawHost || rawHostname.endsWith('.'))) throw new Error();
    if (host.endsWith('.localhost')) throw new Error();
    const ipv4 = /^(127)\.([0-9]+)\.([0-9]+)\.([0-9]+)$/.exec(rawHost);
    const loopback = rawHost === 'localhost' || host === '[::1]' || !!ipv4;
    if (endpoint.protocol !== 'https:' && (endpoint.protocol !== 'http:' || !loopback)) throw new Error();
    return endpoint;
  } catch {
    throw new Error(TRANSLATION_ENDPOINT_ERROR);
  }
}

/** Match the backend base URL spelling before comparing shared connections. */
export function normalizeTranslationBaseUrl(value: string): string {
  const endpoint = parseTranslationEndpoint(value);
  const match = /^https?:\/\/(\[[^\]]+\]|[^:/?#]+)(?::([0-9]+))?(\/[^?#]*)?$/i.exec(value)!;
  let prefix = (match[3] || '').replace(/\/$/, '');
  if (prefix.endsWith('/chat/completions')) prefix = prefix.slice(0, -'/chat/completions'.length);
  const host = endpoint.hostname.toLowerCase().replace(/\.$/, '');
  const port = match[2] ? `:${Number(match[2])}` : '';
  return `${endpoint.protocol}//${host}${port}${prefix}`;
}
