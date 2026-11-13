import { describe, expect, it } from 'vitest';
import { validateChatParameters } from '../src/shared/chat-protocol';

describe('policy and assistance bridge boundaries', () => {
  it('accepts explicit internal usage without converting it to public material', () => {
    expect(() => validateChatParameters('save_policy', { kind: 'policy', title: 'Reception', content: 'Local guidance', product_scope: [], confirmed: true, public: false, enabled: true, usage: 'internal_guidance', category: 'reception', customer_wording: '', internal_notes: '' })).not.toThrow();
  });
  it('rejects arbitrary template metadata and unknown policy operations', () => {
    expect(() => validateChatParameters('adopt_builtin_policy', { template_id: 'B01', enabled: true, template_version: 100, update: false })).toThrow();
    expect(() => validateChatParameters('list_builtin_policies', { execute: 'send' })).toThrow();
  });
  it('accepts the priority sent by every policy form and rejects invalid priorities', () => {
    const form = { kind: 'policy', title: '接待规则', content: '1111', product_scope: [], confirmed: true, public: false, enabled: true, usage: 'internal_guidance', category: 'reception', customer_wording: '', internal_notes: '', valid_until: null };
    for (const priority of [0, 9, 100]) expect(() => validateChatParameters('save_policy', { ...form, priority })).not.toThrow();
    for (const priority of [-1, 101, 1.5, '9', null, NaN]) expect(() => validateChatParameters('save_policy', { ...form, priority })).toThrow();
    expect(() => validateChatParameters('save_policy', { ...form, priority: 0, policy_id: 'p1', expected_revision: 3 })).not.toThrow();
    expect(() => validateChatParameters('save_policy', { ...form, priority: 0, expected_revision: 0 })).toThrow();
  });
});
