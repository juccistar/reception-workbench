import type { ApiMethod } from './api';

/** Validate the narrow chat protocol in both preload and main. The service binds and checks the exact saved version. */
export function validateChatParameters(method: ApiMethod, params: Record<string, unknown>): void {
  if (!['begin_manual_edit', 'save_chat_draft', 'send_chat'].includes(method)) return;
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
