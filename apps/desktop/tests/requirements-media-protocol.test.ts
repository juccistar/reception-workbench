import { describe, expect, it } from 'vitest';
import { validateChatParameters } from '../src/shared/chat-protocol';

describe('requirement media bridge parameters', () => {
  const cover = { case_id: 'case-a', media_id: 'media-a', expected_revision: 2 ** 52 - 1, client_mutation_id: 'cover-1' };
  const source = { case_id: 'case-a', message_id: 'buyer-image', source_version: 'a'.repeat(64), enabled: true, expected_revision: 0, client_mutation_id: 'source-1' };

  it('accepts a local case read and bounded source-bound load', () => {
    expect(() => validateChatParameters('get_requirement_media', { case_id: 'case-a' })).not.toThrow();
    expect(() => validateChatParameters('load_requirement_media', { case_id: 'case-a', media_id: 'media-a', client_request_id: 'load-1' })).not.toThrow();
  });

  it('accepts the full display revision and explicit cover clearing', () => {
    expect(() => validateChatParameters('set_requirement_media_cover', cover)).not.toThrow();
    expect(() => validateChatParameters('set_requirement_media_cover', { ...cover, media_id: null })).not.toThrow();
    expect(() => validateChatParameters('set_product_image_source', source)).not.toThrow();
  });

  it('rejects URL, path and execution parameters before forwarding', () => {
    for (const extra of [{ url: 'https://example.invalid/p.png' }, { file_path: 'C:/image.png' }, { execute: 'send' }]) {
      expect(() => validateChatParameters('get_requirement_media', { case_id: 'case-a', ...extra })).toThrow();
      expect(() => validateChatParameters('load_requirement_media', { case_id: 'case-a', media_id: 'media-a', client_request_id: 'load-1', ...extra })).toThrow();
    }
  });

  it('requires exact image versions and a boolean source decision', () => {
    for (const update of [{ source_version: 'old' }, { source_version: 'A'.repeat(64) }, { enabled: 'true' }, { enabled: null }]) {
      expect(() => validateChatParameters('set_product_image_source', { ...source, ...update })).toThrow();
    }
  });

  it('rejects missing identities and unsafe CAS revisions', () => {
    expect(() => validateChatParameters('load_requirement_media', { case_id: 'case-a', media_id: 'media-a' })).toThrow();
    for (const revision of [null, -1, 0.5, Number.NaN, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => validateChatParameters('set_requirement_media_cover', { ...cover, expected_revision: revision })).toThrow();
    }
    expect(() => validateChatParameters('set_requirement_media_cover', { ...cover, media_id: undefined })).toThrow();
    expect(() => validateChatParameters('set_product_image_source', { ...source, case_id: ' '.repeat(2) })).toThrow();
  });
});
