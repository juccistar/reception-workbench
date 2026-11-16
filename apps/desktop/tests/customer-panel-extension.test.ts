// @vitest-environment node
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';
const html = readFileSync(new URL('../../chrome-extension/customer-panel/panel.html', import.meta.url), 'utf8');
const script = readFileSync(new URL('../../chrome-extension/customer-panel/panel.js', import.meta.url), 'utf8');
function fixture() {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: `chrome-extension://${'a'.repeat(32)}/panel.html` });
  const window = dom.window as any, packets: any[] = [];
  window.chrome = { windows: { getCurrent: async () => ({ id: 1 }) }, sidePanel: { close: async () => {} } };
  window.__receptionCustomerPanel = (raw: string) => packets.push(JSON.parse(raw));
  window.eval(script);
  const receive = (message: unknown) => window.__customerPanelReceive(JSON.stringify(message));
  const view = (context_status = 'ready') => ({ kind: 'view', view: { context_status, identity: { workspace_id: 'synthetic', runtime_instance_id: 'runtime', account_id: 'account', connection_epoch: 'epoch', chat_id: '29990001@c.us', view_revision: 1 }, name: '合成当前客户', merchants: [], merchant_id: null, conversation_id: null, human_owned: false, reception_paused: false, reception_active: false, summary: '', requirements: '' } });
  const settle = () => { const packet = packets.at(-1); if (packet) receive({ kind: 'response', request_id: packet.request_id, result: true }); };
  return { dom, window, packets, receive, view, settle };
}
describe('native customer panel document', () => {
  it('keeps loading and unavailable customer views non-actionable', () => {
    const f = fixture();
    try {
      f.window.__customerPanelBootstrap('a'.repeat(64));
      for (const status of ['loading', 'unavailable']) {
        f.receive(f.view(status));
        expect(f.window.document.getElementById('name').textContent).toBe('合成当前客户');
        expect(f.window.document.getElementById('content').hidden).toBe(false);
        expect(f.window.document.getElementById('details').hidden).toBe(true);
        for (const id of ['merchant', 'query', 'save', 'takeover']) expect(f.window.document.getElementById(id).disabled).toBe(true);
        f.window.document.getElementById('save').click(); f.window.document.getElementById('takeover').click();
      }
      expect(f.packets).toEqual([]);
      f.receive(f.view());
      expect(f.window.document.getElementById('details').hidden).toBe(false);
      expect(f.window.document.getElementById('save').disabled).toBe(false);
    } finally { f.dom.window.close(); }
  });
  it('closes the association sheet when the current customer changes', () => {
    const f = fixture();
    try {
      f.window.__customerPanelBootstrap('a'.repeat(64)); f.receive(f.view());
      const doc = f.window.document;
      doc.getElementById('change-customer').click();
      expect(doc.getElementById('association-sheet').hidden).toBe(false);
      f.receive({ kind: 'clear', status: '请选择一个单聊' });
      expect(doc.getElementById('association-sheet').hidden).toBe(true);
      expect(f.packets).toEqual([]);
    } finally { f.dom.window.close(); }
  });
  it('rotates the capability after a host reconnect and drops the previous customer view', () => {
    const f = fixture();
    try {
      f.window.__customerPanelBootstrap('a'.repeat(64)); f.receive(f.view());
      f.window.document.getElementById('save').click(); expect(f.packets.at(-1).token).toBe('a'.repeat(64)); f.settle();
      f.window.__customerPanelBootstrap('b'.repeat(64));
      expect(f.window.document.getElementById('content').hidden).toBe(true);
      expect(f.window.document.getElementById('name').textContent).toBe('');
      f.receive(f.view()); f.window.document.getElementById('save').click();
      expect(f.packets.at(-1).token).toBe('b'.repeat(64)); f.settle();
    } finally { f.dom.window.close(); }
  });
  it('distinguishes the initial bridge wait from a verified empty chat selection', () => {
    const f = fixture();
    try {
      expect(f.window.document.getElementById('empty').textContent).toBe('正在连接侧栏');
      f.window.__customerPanelBootstrap('a'.repeat(64));
      f.receive({ kind: 'clear', status: '请选择一个单聊' });
      expect(f.window.document.getElementById('empty').textContent).toBe('请选择一个单聊');
      f.window.__customerPanelBootstrap('invalid'); f.receive(f.view()); f.window.document.getElementById('save').click();
      expect(f.packets.at(-1).token).toBe('a'.repeat(64)); f.settle();
    } finally { f.dom.window.close(); }
  });
});
