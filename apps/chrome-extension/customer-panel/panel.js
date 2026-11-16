(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let token = '', view = null, customer = null, sequence = 0, searchTimer;
  const pending = new Map();
  const drafts = new Map();
  const draftKey = v => JSON.stringify([v.identity.workspace_id, v.identity.account_id, v.identity.chat_id, v.merchant_id, v.binding_revision]);
  const connectionTimer = setTimeout(() => { if (!token) $('empty').textContent = '侧栏连接较慢，请稍后'; }, 8000);
  function clear() {
    view = null; customer = null; clearTimeout(searchTimer);
    for (const item of pending.values()) item.reject(new Error('客户已变更'));
    pending.clear(); $('content').hidden = true; $('empty').hidden = false;
    $('name').textContent = ''; $('summary').textContent = ''; $('requirements').textContent = '';
    $('merchant').replaceChildren(); $('results').replaceChildren(); $('query').value = ''; $('selected').textContent = ''; $('error').textContent = '';
  }
  function request(action, input) {
    if (!token || !view || view.context_status !== 'ready' || pending.size >= 4) return Promise.reject(new Error('侧栏尚未就绪'));
    const request_id = String(++sequence);
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pending.delete(request_id); reject(new Error('请求超时')); }, 15000);
      pending.set(request_id, { resolve: v => { clearTimeout(timeout); resolve(v); }, reject: e => { clearTimeout(timeout); reject(e); } });
      window.__receptionCustomerPanel(JSON.stringify({ token, request_id, action, identity: view.identity, input }));
    });
  }
  function render(next) {
    clear(); view = next; $('empty').hidden = true; $('content').hidden = false;
    $('name').textContent = next.name;
    const ready = next.context_status === 'ready';
    $('details').hidden = !ready;
    $('basic-merchant').hidden = ready;
    const merchantName = next.merchants.find(m => m.merchant_id === next.merchant_id)?.name;
    $('basic-merchant').textContent = `接待商家：${merchantName || '未选择商家'}`;
    $('context-status').textContent = ready ? '' : next.context_status === 'loading' ? '正在加载客户信息' : '客户信息暂不可用，正在重试';
    for (const id of ['merchant', 'query', 'save', 'takeover']) $(id).disabled = !ready;
    $('summary').textContent = next.summary || '暂无摘要'; $('requirements').textContent = next.requirements || '暂无需求摘要';
    const none = document.createElement('option'); none.value = ''; none.textContent = '未选择商家'; $('merchant').append(none);
    for (const merchant of next.merchants) { const option = document.createElement('option'); option.value = merchant.merchant_id; option.textContent = merchant.name; option.disabled = !merchant.available; $('merchant').append(option); }
    $('merchant').value = next.merchant_id || '';
    const note = next.supplement || { body: '', revision: 0 };
    const draft = drafts.get(draftKey(next));
    $('supplement').value = draft?.body ?? note.body;
    $('supplement').disabled = $('save-supplement').disabled = !ready || !next.merchant_id;
    $('supplement-status').textContent = draft && draft.revision !== note.revision ? '业务补充已变化，请重新打开后保存' : '';
    $('send-status').textContent = next.send_status || '';
    customer = next.conversation_id ? { conversation_id: next.conversation_id } : null;
    $('selected').textContent = customer ? '已关联国际站客户' : '尚未关联客户';
    const paused = next.human_owned || next.reception_paused;
    $('reception').textContent = paused ? '已暂停' : next.reception_active ? '自动接待中' : '未开启'; $('takeover').textContent = paused ? '恢复接待' : '人工接管';
  }
  function fail(error) { $('error').textContent = error.message || '操作未完成'; }
  window.__customerPanelBootstrap = value => {
    if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) return;
    if (token !== value) { clear(); token = value; }
    clearTimeout(connectionTimer);
  };
  window.__customerPanelReceive = raw => {
    let message; try { message = JSON.parse(raw); } catch { return; }
    if (message.kind === 'clear') { clear(); $('empty').textContent = message.status || '正在连接侧栏'; return; }
    if (message.kind === 'view') { render(message.view); return; }
    if (message.kind === 'response') { const item = pending.get(message.request_id); if (!item) return; pending.delete(message.request_id); message.error ? item.reject(new Error(message.error)) : item.resolve(message.result); }
  };
  $('close').addEventListener('click', async () => {
    try { const current = await chrome.windows.getCurrent(); await chrome.sidePanel.close({ windowId: current.id }); }
    catch { $('error').textContent = '无法收起侧栏，请使用浏览器侧栏的关闭按钮'; }
  });
  $('merchant').addEventListener('change', () => { customer = null; $('selected').textContent = '尚未关联客户'; $('results').replaceChildren(); $('supplement').disabled = $('save-supplement').disabled = $('merchant').value !== (view?.merchant_id || '') || !$('merchant').value; });
  $('supplement').addEventListener('input', () => {
    if (!view) return;
    const key = draftKey(view), existing = drafts.get(key);
    drafts.set(key, { body: $('supplement').value, revision: existing?.revision ?? view.supplement?.revision ?? 0 });
    $('supplement-status').textContent = '';
  });
  $('save-supplement').addEventListener('click', async () => {
    if (!view) return;
    const current = view, key = draftKey(current), draft = drafts.get(key) || { body: $('supplement').value, revision: current.supplement?.revision ?? 0 };
    $('save-supplement').disabled = true;
    try {
      const result = await request('supplement', { body: draft.body, expected_revision: draft.revision });
      if (view !== current) return;
      const latest = drafts.get(key);
      if (latest?.body === draft.body) drafts.delete(key);
      else if (latest) drafts.set(key, { ...latest, revision: result.revision });
      current.supplement = result;
      $('supplement-status').textContent = '已保存';
    } catch (error) { if (view === current) $('supplement-status').textContent = '保存失败，请重试'; }
    finally { if (view === current) $('save-supplement').disabled = false; }
  });
  $('query').addEventListener('input', () => {
    clearTimeout(searchTimer); $('results').replaceChildren();
    const merchant = $('merchant').value, query = $('query').value, identity = view?.identity;
    if (!merchant || !query.trim()) return;
    searchTimer = setTimeout(async () => { try {
      const rows = await request('search', { merchant_id: merchant, query });
      if (view?.identity !== identity || $('merchant').value !== merchant || $('query').value !== query) return;
      $('results').replaceChildren();
      for (const row of rows) { const button = document.createElement('button'); button.type = 'button'; button.textContent = [row.name, row.company].filter(Boolean).join(' · '); button.addEventListener('click', () => { customer = row; $('selected').textContent = `已关联：${row.name}`; $('results').replaceChildren(); }); $('results').append(button); }
    } catch (error) { fail(error); } }, 300);
  });
  $('save').addEventListener('click', async () => { try { $('error').textContent = ''; await request('save', { merchant_id: $('merchant').value || null, conversation_id: customer?.conversation_id || null }); } catch (error) { fail(error); } });
  $('takeover').addEventListener('click', async () => { try { $('error').textContent = ''; await request('takeover', { enabled: !(view.human_owned || view.reception_paused) }); } catch (error) { fail(error); } });
  window.addEventListener('unload', clear);
})();
