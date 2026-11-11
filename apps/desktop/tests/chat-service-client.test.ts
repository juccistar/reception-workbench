// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { ServiceClient } from '../src/main/service-client';

// A real isolated Node child speaks the framed service protocol. No Python, database or external messages.
const syntheticService = String.raw`
const readline = require('node:readline');
const requests = new Map(); let revision = 0; let saved = null;
const reply = value => process.stdout.write(JSON.stringify(value) + '\n');
readline.createInterface({ input: process.stdin }).on('line', line => {
  const {id, method, params} = JSON.parse(line);
  if (method === 'shutdown') { reply({id,result:{}}); return; }
  if (method === 'begin_manual_edit') { reply({id,result:{conversations:[{conversation_id:params.conversation_id,human_owned:true}]}}); return; }
  if (method === 'save_chat_draft') {
    saved = {...params, purpose:'chat', draft_id:'synthetic-draft',revision:++revision};
    reply({id,result:saved}); return;
  }
  if (method === 'send_chat') {
    const previous = requests.get(params.client_request_id);
    if (previous) {
      if (JSON.stringify(previous.params) !== JSON.stringify(params)) reply({id,error:{code:'request_conflict',message:'原请求参数不一致'}});
      else reply({id,result:previous.ack});
      return;
    }
    if (!saved || saved.revision !== params.expected_revision || saved.conversation_id !== params.conversation_id || saved.draft_id !== params.draft_id) {
      reply({id,error:{code:'revision_conflict',message:'草稿版本不一致'}}); return;
    }
    const ack = {job_id:'synthetic-job-'+(requests.size+1)};
    requests.set(params.client_request_id,{params,ack,body:saved.body});
    reply({event:'job.updated',data:{job_id:ack.job_id,kind:'send_chat',status:'queued',conversation_id:params.conversation_id}});
    reply({id,result:ack}); return;
  }
  reply({id,error:{code:'method_denied',message:'不支持此操作'}});
});
`;

describe('chat service transport with a real child process', () => {
  it('preserves Unicode text, exact revisions, independent RPC ids and stable application request ids', async () => {
    const client = new ServiceClient(process.execPath, ['-e', syntheticService], process.cwd(), { ...process.env, ELECTRON_RUN_AS_NODE: '1' });
    const events: unknown[] = []; client.on('event', value => events.push(value));
    try {
      const ownership = await client.invoke<{ conversations: { human_owned: boolean }[] }>('begin_manual_edit', { conversation_id: 'synthetic-中文' });
      expect(ownership.conversations[0].human_owned).toBe(true);
      const draft = await client.invoke<{ revision: number; body: string }>('save_chat_draft', { conversation_id: 'synthetic-中文', body: '中文\nLiteral $() and `text`', expected_revision: null, client_mutation_id: 'save-1' });
      expect(draft.body).toBe('中文\nLiteral $() and `text`');
      const params = { conversation_id: 'synthetic-中文', draft_id: 'synthetic-draft', expected_revision: draft.revision, client_request_id: 'original-key' };
      const original = await client.invoke('send_chat', params);
      expect(events).toEqual([expect.objectContaining({ event: 'job.updated', data: expect.objectContaining({ status: 'queued' }) })]);
      // A newer saved version must not prevent retrieving the original job for the original request.
      await client.invoke('save_chat_draft', { conversation_id: 'another-synthetic-customer', body: 'New version', expected_revision: 1, client_mutation_id: 'save-2' });
      expect(await client.invoke('send_chat', params)).toEqual(original);
      await expect(client.invoke('send_chat', { ...params, expected_revision: 2 })).rejects.toMatchObject({ code: 'request_conflict', message: '原请求参数不一致' });
      await expect(client.invoke('send_chat', { ...params, client_request_id: 'new-key' })).rejects.toMatchObject({ code: 'revision_conflict' });
      expect(events).toHaveLength(1);
    } finally { await client.shutdown(); }
    expect(client.hasExited()).toBe(true);
  });
});
