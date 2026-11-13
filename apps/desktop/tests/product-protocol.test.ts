import { describe, expect, it } from 'vitest';
import { API_METHODS, type ApiMethod } from '../src/shared/api';
import { validateChatParameters } from '../src/shared/chat-protocol';

describe('product and procurement bridge protocol', () => {
  const requests: [ApiMethod, Record<string, unknown>][] = [
    ['list_products', {}], ['get_product', { local_product_id: 'p' }], ['get_product_main_image', { local_product_id: 'p' }],
    ['list_suppliers', {}], ['get_supplier', { supplier_id: 's' }],
    ['save_supplier', { name: '供应商', platform: 'alibaba', platform_supplier_id: null, product_url: null, contact: '', notes: '', client_mutation_id: 'm' }],
    ['save_product_offer', { local_product_id: 'p', supplier_id: 's', data: { unit_price: '12.50', tax_included: null, source_type: 'page_reference', valid_until: null }, client_mutation_id: 'm' }],
    ['select_supplier_offer', { case_id: 'c', offer_id: null, expected_case_revision: 0, client_mutation_id: 'm' }],
    ['bind_requirement_product', { case_id: 'c', local_product_id: 'p', expected_case_revision: 1, client_mutation_id: 'm' }],
    ['set_product_default_offer', { local_product_id: 'p', offer_id: null, expected_revision: 1, client_mutation_id: 'm' }],
    ['get_procurement_settings', {}], ['save_procurement_settings', { auto_find: true, auto_inquiry: false, supplier_limit: 3 }],
    ['set_procurement_enabled', { enabled: false }], ['list_procurement_tasks', {}],
    ['request_procurement', { local_product_id: 'p', client_request_id: 'r' }], ['reconcile_procurement', { task_id: 't' }],
  ];

  it('allows each contract method and rejects unknown or missing parameters', () => {
    for (const [method, params] of requests) {
      expect(API_METHODS).toContain(method);
      expect(() => validateChatParameters(method, params)).not.toThrow();
      expect(() => validateChatParameters(method, { ...params, extra: true })).toThrow();
      for (const key of Object.keys(params)) {
        const missing = { ...params };
        delete missing[key];
        expect(() => validateChatParameters(method, missing)).toThrow();
      }
    }
    for (const retired of ['source_product', 'request_product_lookup', 'list_assistance_tasks', 'start_assistance_task', 'save_assistance_result', 'organize_requirements', 'apply_requirement_proposal']) expect(API_METHODS).not.toContain(retired);
    for (const retained of ['send_chat', 'reconcile_operation', 'get_requirement_media']) expect(API_METHODS).toContain(retained);
    expect(() => validateChatParameters('list_procurement_tasks', { local_product_id: 'p' })).not.toThrow();
  });

  it('checks selection, revision, settings and optional identifiers', () => {
    const selection = { case_id: 'c', offer_id: null, expected_case_revision: 1, client_mutation_id: 'm' };
    for (const revision of [null, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => validateChatParameters('select_supplier_offer', { ...selection, expected_case_revision: revision })).toThrow();
    }
    expect(() => validateChatParameters('select_supplier_offer', { ...selection, offer_id: undefined })).toThrow();
    expect(() => validateChatParameters('list_procurement_tasks', { local_product_id: null })).toThrow();
    for (const supplier_limit of [0, -1, 1.5, 4, '3']) {
      expect(() => validateChatParameters('save_procurement_settings', { auto_find: true, auto_inquiry: false, supplier_limit })).toThrow();
    }
  });

  it('accepts only offer business fields with their defined types', () => {
    const params = { local_product_id: 'p', supplier_id: 's', client_mutation_id: 'm' };
    expect(() => validateChatParameters('save_product_offer', { ...params, offer_id: 'o', expected_revision: 1, data: {} })).not.toThrow();
    for (const data of [null, [], { seller_id: 'other' }, { unit_price: 12 }, { unit_price: '1e3' }, { tax_included: 'yes' }, { valid_until: -1 }, { source_type: 'verified' }, { enabled: null }]) {
      expect(() => validateChatParameters('save_product_offer', { ...params, data })).toThrow();
    }
  });
});
