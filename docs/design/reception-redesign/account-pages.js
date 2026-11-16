(() => {
  'use strict';
  const merchants = ['澄野生活', '山禾制造'];
  const accounts = [
    { id: 'a1', type: '国际站', name: '澄野生活', status: '已打开', merchant: '澄野生活', auto: true },
    { id: 'a2', type: '国际站', name: '山禾制造', status: '需要授权', merchant: '山禾制造', auto: false },
    { id: 'a3', type: 'WhatsApp', name: '海外接待', status: '已连接', merchant: '澄野生活', defaultMerchant: '澄野生活', auto: true },
    { id: 'a4', type: 'WhatsApp', name: '客户服务', status: '已连接', merchant: '山禾制造', defaultMerchant: '山禾制造', auto: false },
    { id: 'a5', type: 'WhatsApp', name: '新账号', status: '连接超时', merchant: '澄野生活', defaultMerchant: '澄野生活', auto: false }
  ];
  const models = [
    { id: 'm1', name: '业务 API', protocol: 'OpenAI 兼容', url: 'https://api.example.com/v1', model: 'reception-model', hasKey: true },
    { id: 'm2', name: '翻译 API', protocol: 'OpenAI 兼容', url: 'https://translate.example.com/v1', model: 'translation-model', hasKey: true }
  ];
  const contacts = [
    { id: 'c1', accountId: 'a3', name: 'Alex Morgan', merchant: '澄野生活', linked: 'Alex Morgan', buyer: 'buyer-1028', auto: true, draft: '', summary: '希望先确认样品，采购 500 只定制 Logo 随行杯，发往德国汉堡。', requirement: 'Luma 随行保温杯 · 500 只' },
    { id: 'c2', accountId: 'a4', name: 'Jamie Chen', merchant: '山禾制造', linked: '', buyer: '', auto: false, draft: '', summary: '询问产品规格及样品。', requirement: '样品 · 规格待确认' }
  ];
  const buyers = [{ name: 'Alex Morgan', id: 'buyer-1028', merchant: '澄野生活' }, { name: 'Taylor Lee', id: 'buyer-1046', merchant: '澄野生活' }, { name: 'Jamie Chen', id: 'buyer-2081', merchant: '山禾制造' }];
  const state = { filter: '全部', contact: 'c1', side: true, reception: 'm1', translation: 'm2', editAccount: null, editModel: null, linkQuery: '' };
  const auth = '请仅在允许启动此工作台的当前目标商家 Accio Work 授权 PowerShell 会话中执行以下命令。\n\n' + String.raw`Start-Process -FilePath '<安装位置>\ReceptionWorkbench.exe' -ArgumentList '"--connect-from-accio"'` + '\n\n该命令启动本机当前版本后立即返回，请勿等待桌面进程退出。';
  const get = (items, id) => items.find(item => item.id === id);
  const button = (action, label, extra = '', primary = false) => `<button type="button" class="s-btn${primary ? ' primary' : ''}" data-action="account:${action}" ${extra}>${label}</button>`;
  const options = (items, selected, esc) => items.map(x => `<option value="${esc(x)}"${x === selected ? ' selected' : ''}>${esc(x)}</option>`).join('');
  const field = (label, control, hint = '') => `<label class="s-field"><span>${label}</span>${control}${hint ? `<small>${hint}</small>` : ''}</label>`;
  const icon = (api, name) => api.icon(name);
  function launcher(api) {
    const e = api.esc;
    const shown = accounts.filter(a => state.filter === '全部' || a.type === state.filter);
    return `<div class="acc-page"><header class="acc-heading"><div><h1>账号</h1><p>连接账号，进入各自的接待窗口。</p></div><div class="acc-heading-actions">${button('models', `${icon(api, 'gear')} 设置`)}${button('add-account', `${icon(api, 'plus')} 添加账号`, '', true)}</div></header>
      <div class="acc-toolbar"><div class="s-tabs acc-tabs" role="tablist">${['全部', '国际站', 'WhatsApp'].map(t => `<button type="button" role="tab" aria-selected="${state.filter === t}" class="${state.filter === t ? 'acc-active' : ''}" data-action="account:filter" data-value="${e(t)}">${e(t)}</button>`).join('')}</div><span>${shown.length} 个账号</span></div>
      <div class="acc-grid">${shown.map(a => `<article class="acc-card"><div class="acc-card-top"><div class="acc-channel ${a.type === 'WhatsApp' ? 'acc-channel-wa' : ''}">${icon(api, a.type === 'WhatsApp' ? 'whatsapp' : 'globe')}</div><div class="acc-card-title"><h2>${e(a.name)}</h2><p>${e(a.type)}</p></div></div><div class="acc-card-bottom"><div><div class="acc-status"><span class="acc-dot ${a.status === '已连接' || a.status === '已打开' ? 'acc-dot-green' : 'acc-dot-amber'}"></span><span>${e(a.status)}</span><button class="acc-edit" type="button" data-action="account:edit-account" data-id="${e(a.id)}" aria-label="编辑${e(a.name)}">${icon(api, 'edit')}</button></div><div class="acc-substatus">${a.auto ? '自动接待中' : '自动接待未开启'}</div></div>${button(a.type === '国际站' && a.status === '需要授权' ? 'authorize' : a.status === '已连接' || a.status === '已打开' || a.status === '等待扫码' ? 'open' : 'connect', a.type === '国际站' && a.status === '需要授权' ? '授权' : a.status === '已连接' || a.status === '已打开' || a.status === '等待扫码' ? '打开窗口' : '连接', `data-id="${e(a.id)}"`)}</div></article>`).join('')}</div>
      <div class="acc-model-strip"><div><strong>共享模型</strong><span>接待和翻译分别使用所选 API</span></div><button class="s-link" data-action="account:models">模型设置 ${icon(api, 'right')}</button></div></div>`;
  }
  function modelPage(api) {
    const e = api.esc;
    const select = (purpose, selected) => `<select class="s-select" data-account-route="${purpose}" aria-label="${purpose === 'reception' ? '接待' : '翻译'}模型">${models.map(m => `<option value="${e(m.id)}"${m.id === selected ? ' selected' : ''}>${e(m.name)} · ${e(m.model)}</option>`).join('')}</select>`;
    return `<div class="model-page"><header class="acc-heading"><div><button class="acc-back" data-action="account:launcher">${icon(api, 'left')} 账号</button><h1>共享模型</h1><p>所有账号共用 API，接待和翻译可分别选择。</p></div>${button('add-model', `${icon(api, 'plus')} 添加 API`, '', true)}</header><section class="model-routing"><div class="model-section-heading"><div><h2>用途设置</h2><p>为不同工作选择模型</p></div></div><div class="model-route-grid"><div><span class="model-route-icon">${icon(api, 'spark')}</span><label>接待模型${select('reception', state.reception)}</label></div><div><span class="model-route-icon">${icon(api, 'translate')}</span><label>翻译模型${select('translation', state.translation)}</label></div></div></section><section class="model-list"><div class="model-section-heading"><div><h2>API 列表</h2><p>管理连接信息</p></div><span>${models.length} 个 API</span></div>${models.map(m => `<article class="model-row"><div class="model-mark">${icon(api, 'box')}</div><div class="model-info"><strong>${e(m.name)}</strong><span>${e(m.model)} <i></i> ${e(m.url)}</span></div><span class="s-badge ${m.hasKey ? 'green' : 'muted'}">${m.hasKey ? '已配置' : '未配置 Key'}</span>${button('edit-model', '编辑', `data-id="${e(m.id)}"`)}</article>`).join('')}</section></div>`;
  }
  function whatsapp(api) {
    const e = api.esc, c = get(contacts, state.contact);
    return `<div class="wa-page"><header class="acc-heading wa-heading"><div><h1>WhatsApp 客户侧栏</h1><p>原始网页布局示意，仅用于预览侧栏位置。</p></div><label class="wa-switch">预览当前联系人<select class="s-select" data-account-contact>${contacts.map(x => `<option value="${e(x.id)}"${x.id === c.id ? ' selected' : ''}>${e(x.name)}</option>`).join('')}</select></label></header><div class="wa-window"><div class="wa-original"><div class="wa-browser"><span></span><span></span><span></span><div>WhatsApp Web</div></div><div class="wa-original-body"><div class="wa-original-rail"><div class="wa-original-label">WhatsApp</div><div class="wa-placeholder-line"></div><div class="wa-placeholder-line short"></div><div class="wa-placeholder-line"></div></div><div class="wa-original-content"><div class="wa-original-title">${e(c.name)}</div><div class="wa-original-empty"><div>${icon(api, 'whatsapp')}</div><strong>WhatsApp 原始网页</strong><p>在原窗口中查看和回复消息</p></div></div></div></div>${state.side ? `<aside class="wa-sidebar"><header><div><span>客户资料</span><h2>${e(c.name)}</h2></div><button type="button" class="acc-edit" data-action="account:toggle-side" aria-label="收起客户侧栏">${icon(api, 'panel')}</button></header><div class="wa-side-content"><div class="wa-reception"><span class="s-badge ${c.auto ? 'green' : 'amber'}">${c.auto ? '自动接待中' : '人工接待'}</span><button class="s-link" data-action="account:takeover">${c.auto ? '人工接管' : '恢复接待'}</button></div><section><div class="wa-section-title"><h3>接待商家</h3><button class="s-link" data-action="account:assign">更改</button></div><p>${e(c.merchant)}</p></section><section><div class="wa-section-title"><h3>国际站客户</h3><button class="s-link" data-action="account:link">${c.linked ? '更改' : '关联客户'}</button></div>${c.linked ? `<p>${e(c.linked)}</p><small>买家标识 ${e(c.buyer)}</small>` : '<p class="wa-muted">尚未关联客户</p>'}</section><section><h3>客户摘要</h3><p>${e(c.summary)}</p></section><section><h3>当前需求</h3><p>${e(c.requirement)}</p></section><section>${field('业务补充', `<textarea class="s-input wa-draft" data-account-draft placeholder="记录与该客户有关的补充信息">${e(c.draft)}</textarea>`, '仅保留在当前预览中')}</section></div></aside>` : `<button class="wa-show-side" data-action="account:toggle-side">${icon(api, 'panel')} 客户资料</button>`}</div></div>`;
  }
  function accountSheet(api, account) {
    const e = api.esc, a = account || { id: '', type: 'WhatsApp', name: '', merchant: merchants[0], defaultMerchant: merchants[0], auto: false };
    state.editAccount = a.id;
    api.openSheet({ title: a.id ? '编辑账号' : '添加账号', body: `<div class="acc-sheet">${!a.id ? field('账号类型', `<select class="s-select" id="acc-type">${options(['WhatsApp', '国际站'], a.type, e)}</select>`) : `<div class="acc-sheet-kind">${icon(api, a.type === 'WhatsApp' ? 'whatsapp' : 'globe')} ${e(a.type)}</div>`}${field('账号名称', `<input class="s-input" id="acc-name" value="${e(a.name)}" placeholder="输入账号名称">`)}<label class="acc-toggle"><span><strong>自动接待</strong><small>为此账号开启自动接待</small></span><input type="checkbox" id="acc-auto" ${a.auto ? 'checked' : ''}></label>${field('当前商家', `<select class="s-select" id="acc-merchant">${options(merchants, a.merchant, e)}</select>`, '更换商家后，该账号联系人的接待预览将暂停，原账本保持不变。')}${field('新联系人默认商家', `<select class="s-select" id="acc-default">${options(merchants, a.defaultMerchant || a.merchant, e)}</select>`, '仅影响新联系人。')}<div class="acc-sheet-link"><span>接待模型</span><button class="s-link" data-action="account:models">共享模型设置 ${icon(api, 'right')}</button></div><details class="acc-details"><summary>更多设置</summary><p>连接详情、备份与恢复可在此维护。</p></details></div>`, footer: button('close-sheet', '取消') + button('save-account', '保存', '', true) });
  }
  function modelSheet(api, model) {
    const e = api.esc, m = model || { id: '', name: '', protocol: 'OpenAI 兼容', url: '', model: '', hasKey: false };
    state.editModel = m.id;
    api.openSheet({ title: m.id ? '编辑 API' : '添加 API', body: `<div class="acc-sheet">${field('名称', `<input class="s-input" id="model-name" value="${e(m.name)}" placeholder="例如：业务 API">`)}${field('协议', `<select class="s-select" id="model-protocol">${options(['OpenAI 兼容', 'Anthropic 兼容'], m.protocol, e)}</select>`)}${field('地址', `<input class="s-input" id="model-url" value="${e(m.url)}" placeholder="https://api.example.com/v1">`)}${field('模型名', `<input class="s-input" id="model-name-value" value="${e(m.model)}" placeholder="reception-model">`)}${field('Key', '<input class="s-input" id="model-key" type="password" autocomplete="new-password" placeholder="留空保留已有 Key">', '预览不会连接网络，Key 不会保存。')}<div class="s-note">连接检查仅为预览演示，不会发起网络请求。</div>${button('check-model', '预览连接检查')}</div>`, footer: button('close-sheet', '取消') + button('save-model', '保存', '', true) });
  }
  function linkSheet(api) {
    const e = api.esc, c = get(contacts, state.contact), q = state.linkQuery.toLowerCase();
    const results = buyers.filter(b => b.merchant === c.merchant && (b.name.toLowerCase().includes(q) || b.id.toLowerCase().includes(q)));
    api.openSheet({ title: '关联国际站客户', body: `<div class="acc-sheet"><p class="acc-sheet-intro">从 ${e(c.merchant)} 已保存的客户中选择。</p>${field('搜索客户', `<input class="s-input" id="wa-link-search" data-account-link-search value="${e(state.linkQuery)}" placeholder="客户姓名或买家标识">`)}<div class="wa-results">${results.length ? results.map(b => `<button type="button" data-action="account:choose-link" data-buyer="${e(b.id)}"><span><strong>${e(b.name)}</strong><small>${e(b.id)}</small></span>${icon(api, 'right')}</button>`).join('') : '<p>没有找到客户</p>'}</div></div>`, footer: button('close-sheet', '取消') });
  }
  function value(id) { return document.getElementById(id)?.value.trim() || ''; }
  function onAction(action, el, api) {
    if (!action.startsWith('account:')) return false;
    const name = action.slice(8), a = get(accounts, el?.dataset.id), c = get(contacts, state.contact);
    if (name === 'filter') { state.filter = el.dataset.value; api.refresh(); }
    else if (name === 'models' || name === 'launcher') { api.closeSheet(); api.navigate(name); }
    else if (name === 'add-account') accountSheet(api);
    else if (name === 'edit-account' && a) accountSheet(api, a);
    else if (name === 'close-sheet') api.closeSheet();
    else if (name === 'save-account') { const n = value('acc-name'); if (!n) { api.toast('请输入账号名称'); return true; } const old = get(accounts, state.editAccount), merchant = value('acc-merchant'); if (old && old.merchant !== merchant) { contacts.forEach(x => { if (x.accountId === old.id) x.auto = false; }); api.toast('商家已更换，相关联系人接待已暂停'); } const data = { name: n, merchant, defaultMerchant: value('acc-default'), auto: !!document.getElementById('acc-auto')?.checked }; if (old) Object.assign(old, data); else accounts.push({ ...data, id: `a${Date.now()}`, type: value('acc-type'), status: value('acc-type') === '国际站' ? '需要授权' : '未连接' }); api.closeSheet(); api.refresh(); }
    else if (name === 'connect' && a) { a.status = '等待扫码'; api.refresh(); api.openModal({ title: '等待扫码', body: '<p>请在新窗口完成扫码。</p><p class="acc-muted">此处为连接状态预览。</p>', buttons: [{ label: '关闭', action: 'account:close-modal' }, { label: '打开窗口', action: `account:open:${a.id}`, primary: true }] }); }
    else if (name === 'open' || name.startsWith('open:')) { api.closeModal(); const target = a || get(accounts, name.slice(5)); if (target?.type === '国际站') api.navigate('reception'); else if (target?.status === '等待扫码') { api.openModal({ title: '等待扫码', body: '<p>请在该账号的新窗口完成扫码。</p><p>此处为合成连接状态，不会打开其他账号的联系人。</p>', buttons: [{ label: '关闭', action: 'account:close-modal' }] }); } else { const contact = contacts.find(x => x.accountId === target?.id); if (contact) state.contact = contact.id; api.navigate('whatsapp'); } }
    else if (name === 'authorize') api.openSheet({ title: '账号授权', body: `<div class="acc-sheet"><p>复制口令，在对应商家的 Accio Work 中运行。</p><label class="s-field"><span>示例口令</span><textarea class="s-input acc-auth" readonly>${api.esc(auth)}</textarea></label><p class="acc-muted">如无法复制，可选择上方全文手动复制。</p></div>`, footer: button('close-sheet', '关闭') + button('copy-auth', '复制口令', '', true) });
    else if (name === 'copy-auth') { if (navigator.clipboard?.writeText) navigator.clipboard.writeText(auth).then(() => api.toast('已复制完整口令')).catch(() => api.toast('无法复制，请选择口令全文手动复制')); else api.toast('无法复制，请选择口令全文手动复制'); }
    else if (name === 'close-modal') api.closeModal();
    else if (name === 'add-model') modelSheet(api);
    else if (name === 'edit-model') modelSheet(api, get(models, el.dataset.id));
    else if (name === 'check-model') api.toast('预览检查完成，未连接网络');
    else if (name === 'save-model') { const n = value('model-name'), url = value('model-url'), mname = value('model-name-value'); if (!n || !url || !mname) { api.toast('请填写名称、地址和模型名'); return true; } const old = get(models, state.editModel), data = { name: n, protocol: value('model-protocol'), url, model: mname, hasKey: !!value('model-key') || !!old?.hasKey }; if (old) Object.assign(old, data); else models.push({ ...data, id: `m${Date.now()}` }); api.closeSheet(); api.refresh(); api.toast('已保存预览设置'); }
    else if (name === 'toggle-side') { state.side = !state.side; api.refresh(); }
    else if (name === 'takeover') { c.auto = !c.auto; api.refresh(); api.toast(c.auto ? '已恢复接待' : '已切换为人工接待'); }
    else if (name === 'assign') api.openSheet({ title: '更改接待商家', body: `<div class="acc-sheet">${field('接待商家', `<select class="s-select" id="wa-merchant">${options(merchants, c.merchant, api.esc)}</select>`, '更换商家后，此客户接待将暂停，原账本保持不变。')}</div>`, footer: button('close-sheet', '取消') + button('save-merchant', '保存', '', true) });
    else if (name === 'save-merchant') { const merchant = value('wa-merchant'); if (c.merchant !== merchant) { c.merchant = merchant; c.auto = false; c.linked = ''; c.buyer = ''; } api.closeSheet(); api.refresh(); api.toast('已保存商家'); }
    else if (name === 'link') { state.linkQuery = ''; linkSheet(api); }
    else if (name === 'choose-link') { const b = buyers.find(x => x.id === el.dataset.buyer && x.merchant === c.merchant); if (b) { c.linked = b.name; c.buyer = b.id; api.closeSheet(); api.refresh(); api.toast('已关联客户'); } }
    return true;
  }
  function onInput(event, api) { const t = event.target; if (t.matches('[data-account-draft]')) get(contacts, state.contact).draft = t.value; if (t.matches('[data-account-link-search]')) { state.linkQuery = t.value; const pos = t.selectionStart; linkSheet(api); const input = document.getElementById('wa-link-search'); input?.focus(); input?.setSelectionRange(pos, pos); } }
  function onChange(event, api) { const t = event.target; if (t.matches('[data-account-contact]')) { state.contact = t.value; api.closeSheet(); api.refresh(); } if (t.matches('[data-account-route]')) { state[t.dataset.accountRoute] = t.value; api.toast('已保存预览设置'); } }
  window.DesignModules ||= {};
  window.DesignModules.accounts = { routes: ['launcher', 'models', 'whatsapp'], render(route, api) { return route === 'launcher' ? launcher(api) : route === 'models' ? modelPage(api) : route === 'whatsapp' ? whatsapp(api) : ''; }, onAction, onInput, onChange };
})();
