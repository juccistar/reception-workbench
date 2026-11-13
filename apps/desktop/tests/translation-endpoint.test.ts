import { describe, expect, it } from 'vitest';
import { normalizeTranslationBaseUrl, parseTranslationEndpoint, TRANSLATION_ENDPOINT_ERROR } from '../src/shared/translation-endpoint';

describe('translation endpoint policy', () => {
  it('accepts public HTTPS and explicit local HTTP gateways', () => {
    for (const value of ['https://api.deepseek.com/v1', 'https://example.com/custom/chat/completions/',
      'http://127.0.0.1:3425/v1', 'http://127.255.255.255/v1', 'http://LOCALHOST.:3425/v1',
      'http://[::1]:3425/v1', 'https://localhost/v1', 'https://127.0.0.1/v1', 'https://[::1]/v1']) {
      expect(() => parseTranslationEndpoint(value), value).not.toThrow();
    }
  });

  it('compares equivalent service addresses without repeatedly resetting merchant settings', () => {
    expect(normalizeTranslationBaseUrl('http://LOCALHOST.:03425/v1/chat/completions/')).toBe('http://localhost:3425/v1');
    expect(normalizeTranslationBaseUrl('https://EXAMPLE.com:443/v1/')).toBe('https://example.com:443/v1');
    expect(normalizeTranslationBaseUrl('http://[::1]:3425/v1')).toBe('http://[::1]:3425/v1');
  });

  it('rejects remote HTTP and misleading or alternate loopback spellings', () => {
    for (const value of ['http://example.com/v1', 'http://192.168.1.1/v1', 'http://8.8.8.8/v1',
      'http://localhost.example.com/v1', 'http://foo.localhost/v1', 'http://localhost../v1',
      'http://localtest.me/v1', 'http://127.1/v1', 'https://2130706433/v1', 'http://0177.0.0.1/v1',
      'http://0x7f.0.0.1/v1', 'http://0x7f000001/v1', 'http://127.0.0.1./v1', 'http://[::2]/v1']) {
      expect(() => parseTranslationEndpoint(value), value).toThrow(TRANSLATION_ENDPOINT_ERROR);
    }
  });

  it('rejects credentials, query strings, unsupported schemes and unsafe paths', () => {
    for (const value of ['https://user:secret@example.com/v1', 'http://user@localhost/v1',
      'http://localhost/v1?key=secret', 'http://localhost/v1?', 'http://localhost/#',
      'ftp://localhost/v1', ' http://localhost/v1', 'http://localhost:0/v1',
      'http://localhost/a/../v1', 'http://localhost/%2e%2e/v1', 'http://localhost/v1//',
      'http://local%68ost/v1', 'http://localhost\\@example.com/v1']) {
      expect(() => parseTranslationEndpoint(value), value).toThrow(TRANSLATION_ENDPOINT_ERROR);
    }
  });
});
