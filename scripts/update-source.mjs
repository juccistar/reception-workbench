// Shared by the update host, settings UI and release builder. No credentials or I/O.
const githubAssetOrigins = new Set([
  'https://release-assets.githubusercontent.com',
  'https://objects.githubusercontent.com',
  'https://github-releases.githubusercontent.com',
]);

export function parseUpdateSource(value) {
  if (typeof value !== 'string' || value.length > 2048) throw new Error('Invalid update source');
  const text = value.trim();
  if (!text) return { kind: 'disabled', url: '' };
  if (/[?#\s\\]/.test(text) || !/^https:\/\//i.test(text) || /^https:\/\/[^/]*@/i.test(text)) throw new Error('Invalid update source');
  const url = new URL(text);
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password) throw new Error('Invalid update source');
  if (url.hostname === 'github.com') {
    if (url.port) throw new Error('Invalid GitHub port');
    const match = /^\/([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9_.-]{1,100})(?:\/releases)?\/?$/.exec(url.pathname);
    if (!match || ['.', '..'].includes(match[2]) || /\.git$/i.test(match[2])) throw new Error('Use a GitHub repository URL');
    const owner = match[1], repo = match[2];
    return { kind: 'github', owner, repo, url: `https://github.com/${owner}/${repo}/` };
  }
  return { kind: 'generic', url: url.href.endsWith('/') ? url.href : `${url.href}/` };
}

export function updateProvider(source) {
  if (source.kind === 'github') return { provider: 'github', owner: source.owner, repo: source.repo, private: false };
  if (source.kind === 'generic') return { provider: 'generic', url: source.url };
  throw new Error('Update source is disabled');
}

export function updateSourceFromConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('Invalid updater configuration');
  if (config.provider === 'github') {
    if (config.private === true || Object.hasOwn(config, 'token') || typeof config.owner !== 'string' || typeof config.repo !== 'string') throw new Error('Use a public GitHub release repository');
    const source = parseUpdateSource(`https://github.com/${config.owner}/${config.repo}/`);
    if (source.kind !== 'github' || source.owner !== config.owner || source.repo !== config.repo) throw new Error('Invalid GitHub configuration');
    return source.url;
  }
  return typeof config.url === 'string' ? parseUpdateSource(config.url).url : '';
}

export function isUpdateRequestAllowed(source, address) {
  try {
    const target = new URL(address);
    if (target.protocol !== 'https:' || target.username || target.password || /^https:\/\/[^/]*@/i.test(address)) return false;
    if (source.kind === 'generic') return target.origin === new URL(source.url).origin;
    if (source.kind !== 'github') return false;
    if (githubAssetOrigins.has(target.origin)) return true;
    if (target.origin !== 'https://github.com') return false;
    const base = `/${source.owner}/${source.repo}/releases`.toLowerCase();
    const location = target.pathname.toLowerCase();
    return location === base || location === `${base}.atom` || location.startsWith(`${base}/`);
  } catch { return false; }
}
