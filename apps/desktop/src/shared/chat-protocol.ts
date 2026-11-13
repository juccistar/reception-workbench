import type { ApiMethod } from './api';

/** Validate the narrow chat protocol in both preload and main. The service binds and checks the exact saved version. */
export function validateChatParameters(method: ApiMethod, params: Record<string, unknown>): void {
  const productFields: Partial<Record<ApiMethod, { required: string[]; optional?: string[] }>> = {
    list_products: { required: [] }, get_product: { required: ['local_product_id'] }, get_product_main_image: { required: ['local_product_id'] },
    list_suppliers: { required: [] }, get_supplier: { required: ['supplier_id'] },
    save_supplier: { required: ['name', 'platform', 'platform_supplier_id', 'product_url', 'contact', 'notes', 'client_mutation_id'], optional: ['supplier_id', 'expected_revision'] },
    save_product_offer: { required: ['local_product_id', 'supplier_id', 'data', 'client_mutation_id'], optional: ['offer_id', 'expected_revision'] },
    select_supplier_offer: { required: ['case_id', 'offer_id', 'expected_case_revision', 'client_mutation_id'] },
    bind_requirement_product: { required: ['case_id', 'local_product_id', 'expected_case_revision', 'client_mutation_id'] },
    set_product_default_offer: { required: ['local_product_id', 'offer_id', 'expected_revision', 'client_mutation_id'] },
    get_procurement_settings: { required: [] },
    save_procurement_settings: { required: ['auto_find', 'auto_inquiry', 'supplier_limit'] },
    set_procurement_enabled: { required: ['enabled'] },
    list_procurement_tasks: { required: [], optional: ['local_product_id'] },
    request_procurement: { required: ['local_product_id', 'client_request_id'] },
    reconcile_procurement: { required: ['task_id'] },
  };
  const productSpec = productFields[method];
  if (productSpec) {
    const fail = () => { throw new Error('商品、供应商或采购操作参数无效。'); };
    const id = (value: unknown) => typeof value === 'string' && !!value.trim() && value.length <= 200;
    const text = (value: unknown) => typeof value === 'string' && value.length <= 50000;
    const revision = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
    const allowed = [...productSpec.required, ...(productSpec.optional ?? [])];
    if (Object.keys(params).some(key => !allowed.includes(key)) || productSpec.required.some(key => !Object.hasOwn(params, key))) fail();
    for (const key of ['local_product_id', 'supplier_id', 'case_id', 'task_id', 'client_mutation_id', 'client_request_id']) {
      if (Object.hasOwn(params, key) && !id(params[key])) fail();
    }
    if (Object.hasOwn(params, 'offer_id') && !(params.offer_id === null && (method === 'select_supplier_offer' || method === 'set_product_default_offer')) && !id(params.offer_id)) fail();
    for (const key of ['expected_revision', 'expected_case_revision']) if (Object.hasOwn(params, key) && !revision(params[key])) fail();
    if (method === 'save_supplier') {
      if (!id(params.name) || !id(params.platform) || !text(params.contact) || !text(params.notes)) fail();
      for (const key of ['platform_supplier_id', 'product_url']) if (params[key] !== null && !text(params[key])) fail();
    }
    if (method === 'save_product_offer') {
      const data = params.data;
      if (!data || typeof data !== 'object' || Array.isArray(data)) fail();
      const fields = data as Record<string, unknown>;
      const strings = ['specifications', 'moq', 'unit', 'unit_price', 'currency', 'lead_time', 'packaging', 'freight', 'source_note', 'customer_wording'];
      const nullableStrings = ['supplier_product_id', 'product_url'];
      const booleans = ['enabled', 'reply_enabled'];
      const offerKeys = [...strings, ...nullableStrings, ...booleans, 'tax_included', 'valid_until', 'source_type'];
      if (Object.keys(fields).some(key => !offerKeys.includes(key))) fail();
      for (const key of strings) if (Object.hasOwn(fields, key) && !text(fields[key])) fail();
      for (const key of nullableStrings) if (Object.hasOwn(fields, key) && fields[key] !== null && !text(fields[key])) fail();
      for (const key of booleans) if (Object.hasOwn(fields, key) && typeof fields[key] !== 'boolean') fail();
      if (Object.hasOwn(fields, 'tax_included') && fields.tax_included !== null && typeof fields.tax_included !== 'boolean') fail();
      if (Object.hasOwn(fields, 'valid_until') && fields.valid_until !== null && !revision(fields.valid_until)) fail();
      if (Object.hasOwn(fields, 'source_type') && !['page_reference', 'supplier_reply', 'manual_verified'].includes(fields.source_type as string)) fail();
      if (typeof fields.unit_price === 'string' && fields.unit_price !== '' && !/^\d+(?:\.\d+)?$/.test(fields.unit_price)) fail();
    }
    for (const key of ['auto_find', 'auto_inquiry', 'enabled']) if (Object.hasOwn(params, key) && typeof params[key] !== 'boolean') fail();
    if (method === 'save_procurement_settings' && (!Number.isSafeInteger(params.supplier_limit) || ((params.supplier_limit as number) < 1 || (params.supplier_limit as number) > 3))) fail();
    return;
  }
  if (['get_requirement_media', 'set_requirement_media_cover', 'set_product_image_source', 'load_requirement_media'].includes(method)) {
    const fields: Record<string, string[]> = {
      get_requirement_media: ['case_id'],
      set_requirement_media_cover: ['case_id', 'media_id', 'expected_revision', 'client_mutation_id'],
      set_product_image_source: ['case_id', 'message_id', 'source_version', 'enabled', 'expected_revision', 'client_mutation_id'],
      load_requirement_media: ['case_id', 'media_id', 'client_request_id'],
    };
    const fail = () => { throw new Error('需求图片操作参数无效。'); };
    const id = (value: unknown) => typeof value === 'string' && !!value.trim() && value.length <= 200;
    if (Object.keys(params).some(key => !fields[method].includes(key)) || fields[method].some(key => !Object.hasOwn(params, key)) || !id(params.case_id)) fail();
    if (method === 'get_requirement_media') return;
    if (method === 'load_requirement_media') {
      if (!id(params.media_id) || !id(params.client_request_id)) fail();
      return;
    }
    if (!id(params.client_mutation_id) || typeof params.expected_revision !== 'number' || !Number.isSafeInteger(params.expected_revision) || params.expected_revision < 0) fail();
    if (method === 'set_requirement_media_cover') {
      if (params.media_id !== null && !id(params.media_id)) fail();
    } else if (!id(params.message_id) || typeof params.source_version !== 'string' || !/^[0-9a-f]{64}$/.test(params.source_version) || typeof params.enabled !== 'boolean') fail();
    return;
  }
  if (['list_builtin_policies', 'adopt_builtin_policy', 'save_policy'].includes(method)) {
    const fail = () => { throw new Error('资料或协助操作参数无效。'); };
    const id = (value: unknown) => typeof value === 'string' && !!value.trim() && value.length <= 256;
    const optionalRevision = params.expected_revision === undefined || typeof params.expected_revision === 'number' && Number.isSafeInteger(params.expected_revision) && params.expected_revision >= 1;
    if (method === 'list_builtin_policies') { if (Object.keys(params).length) fail(); return; }
    const allowed = method === 'adopt_builtin_policy' ? ['template_id', 'content', 'product_scope', 'enabled', 'confirmed', 'expected_revision', 'update'] : ['policy_id', 'kind', 'title', 'content', 'product_scope', 'confirmed', 'public', 'expected_revision', 'valid_until', 'category', 'enabled', 'usage', 'customer_wording', 'internal_notes'];
    if (Object.keys(params).some(key => !allowed.includes(key)) || !optionalRevision) fail();
    if (method === 'adopt_builtin_policy' ? !id(params.template_id) : !['policy', 'product'].includes(String(params.kind)) || !id(params.title) || typeof params.public !== 'boolean' || typeof params.confirmed !== 'boolean') fail();
    if (params.content !== undefined && (typeof params.content !== 'string' || !params.content.trim() || params.content.length > 50000)) fail();
    if (method === 'save_policy' && (params.content === undefined || params.product_scope === undefined || params.policy_id !== undefined && !id(params.policy_id))) fail();
    if (params.product_scope !== undefined && (!Array.isArray(params.product_scope) || params.product_scope.some(scope => !id(scope)))) fail();
    for (const key of ['enabled', 'confirmed', 'update']) if (params[key] !== undefined && typeof params[key] !== 'boolean') fail();
    for (const key of ['customer_wording', 'internal_notes']) if (params[key] !== undefined && (typeof params[key] !== 'string' || (params[key] as string).length > 50000)) fail();
    if (params.usage !== undefined && !['human_only', 'internal_guidance', 'customer_reference'].includes(String(params.usage)) || params.category !== undefined && !['product', 'business', 'reception', 'style'].includes(String(params.category))) fail();
    if (params.valid_until !== undefined && params.valid_until !== null && !(typeof params.valid_until === 'number' && Number.isSafeInteger(params.valid_until) && params.valid_until >= 0)) fail();
    return;
  }
  if (method === 'set_case_category' || method === 'create_procurement_case') {
    const keys: Record<string, string[]> = {
      set_case_category: ['case_id', 'category_label', 'expected_revision', 'client_mutation_id'],
      create_procurement_case: ['conversation_id', 'category_label', 'expected_revision', 'client_mutation_id'],
    };
    const id = (value: unknown) => typeof value === 'string' && !!value.trim() && value.length <= 200;
    const revision = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
    const fail = () => { throw new Error('需求操作参数无效。'); };
    if (Object.keys(params).some(key => !keys[method].includes(key))) fail();
    const required = method === 'set_case_category' ? ['case_id', 'category_label', 'client_mutation_id'] : ['conversation_id', 'client_mutation_id'];
    if (required.some(key => !id(params[key])) || !revision(params.expected_revision)) fail();
    if (params.category_label !== undefined && (typeof params.category_label !== 'string' || !/^[一-鿿A-Za-z]{1,8}$/.test(params.category_label) || !/[一-鿿]/.test(params.category_label))) fail();
    return;
  }
  if (!['begin_manual_edit', 'save_chat_draft', 'send_chat', 'generate_manual_suggestion'].includes(method)) return;
  if (method === 'generate_manual_suggestion') {
    const allowed = ['conversation_id', 'case_id', 'client_request_id', 'input_body', 'input_revision', 'input_hash'];
    if (Object.keys(params).some(key => !allowed.includes(key)) ||
        typeof params.conversation_id !== 'string' || !params.conversation_id.trim() || params.conversation_id.length > 256 ||
        !(params.case_id === null || params.case_id === undefined || typeof params.case_id === 'string' && !!params.case_id.trim() && params.case_id.length <= 256) ||
        typeof params.client_request_id !== 'string' || !params.client_request_id.trim() || params.client_request_id.length > 200) throw new Error('人工建议参数无效。');
    const polishing = params.input_body !== undefined;
    if (polishing ? typeof params.input_body !== 'string' || !params.input_body.trim() || params.input_body.length > 50000 ||
        typeof params.input_revision !== 'number' || !Number.isSafeInteger(params.input_revision) || params.input_revision < 1 ||
        typeof params.input_hash !== 'string' || !/^[0-9a-f]{64}$/.test(params.input_hash) :
        params.input_revision !== undefined || params.input_hash !== undefined) throw new Error('润色正文或版本无效。');
    return;
  }
  const keys = method === 'begin_manual_edit' ? ['conversation_id'] : method === 'save_chat_draft'
    ? ['conversation_id', 'body', 'expected_revision', 'client_mutation_id']
    : ['conversation_id', 'draft_id', 'expected_revision', 'client_request_id'];
  if (Object.keys(params).some(key => !keys.includes(key)) || keys.some(key => !Object.hasOwn(params, key))) throw new Error('聊天操作参数无效。');
  const id = (key: string) => typeof params[key] === 'string' && !!params[key].trim() && params[key].length <= 256;
  if (!id('conversation_id')) throw new Error('客户会话编号无效。');
  if (method === 'begin_manual_edit') return;
  const revision = params.expected_revision;
  const validRevision = typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= (method === 'send_chat' ? 1 : 0);
  if (!(validRevision || (method === 'save_chat_draft' && revision === null))) throw new Error('聊天草稿版本无效。');
  if (method === 'send_chat') {
    if (!id('draft_id') || !id('client_request_id')) throw new Error('发送草稿或原请求编号无效。');
  } else if (typeof params.body !== 'string' || params.body.length > 100000 || !id('client_mutation_id')) throw new Error('聊天正文或保存编号无效。');
}
