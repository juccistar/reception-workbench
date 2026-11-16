(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const productUrl = 'https://www.alibaba.com/product-detail/_1601902417143.html';
  const customers = [
    { id: 'alex', name: 'Alex Morgan', initials: 'AM', avatar: 'portrait', country: '德国', city: 'Hamburg', language: '英语', time: '10:48', snippet: 'Can we see a sample first?', tag: '你正在接待', status: 'human', unread: 0, quote: true, product: true },
    { id: 'emma', name: 'Emma Wilson', initials: 'EW', avatar: 'rose', country: '英国', city: 'London', language: '英语', time: '10:41', snippet: 'Could you send me the price list?', tag: '待回复', status: 'reply', unread: 2, quote: false, product: true },
    { id: 'noah', name: 'Noah Bennett', initials: 'NB', avatar: 'sand', country: '美国', city: 'Austin', language: '英语', time: '10:32', snippet: 'Thanks! Looking forward to it.', tag: '等待客户', status: 'waiting', unread: 0, quote: false, product: false },
    { id: 'sofia', name: 'Sofía Ríos', initials: 'SR', avatar: 'sage', country: '西班牙', city: 'Madrid', language: '西班牙语', time: '10:18', snippet: '¿Podrían cotizar 300 unidades?', tag: '待报价', status: 'quote', unread: 1, quote: true, product: true },
    { id: 'liam', name: 'Liam Parker', initials: 'LP', avatar: 'lilac', country: '澳大利亚', city: 'Sydney', language: '英语', time: '09:56', snippet: 'The ivory color looks great.', tag: '自动接待中', status: 'auto', unread: 0, quote: false, product: true },
    { id: 'mia', name: 'Mia Laurent', initials: 'ML', avatar: 'steel', country: '法国', city: 'Lyon', language: '法语', time: '09:45', snippet: 'Merci pour votre réponse !', tag: '等待客户', status: 'waiting', unread: 0, quote: false, product: false },
    { id: 'oliver', name: 'Oliver Reed', initials: 'OR', avatar: 'sand', country: '加拿大', city: 'Toronto', language: '英语', time: '昨天', snippet: 'Can we customize the packaging?', tag: '待回复', status: 'reply', unread: 1, quote: false, product: true },
    { id: 'hana', name: 'Hana Suzuki', initials: 'HS', avatar: 'rose', country: '日本', city: 'Tokyo', language: '日语', time: '昨天', snippet: 'サンプルの詳細をお願いします。', tag: '人工接待', status: 'human', unread: 0, quote: false, product: false }
  ];
  const summaries = {
    alex: '采购 500 只鼠尾草绿随行杯，定制 Logo；发往德国，希望先确认样品。',
    emma: '关注随行杯的价格与颜色，尚未确认采购数量。',
    noah: '客户初次咨询，等待进一步说明想了解的产品。',
    sofia: '需要 300 只随行杯，希望获得报价；收货地址与定制要求待确认。',
    liam: '偏好象牙白随行杯，正在确认数量及包装。',
    mia: '已回复客户的初步咨询，等待进一步沟通。',
    oliver: '希望定制外包装，需要继续了解包装图稿与采购数量。',
    hana: '客户想先了解样品，产品、数量和目的地尚待确认。'
  };
  const alexMessages = [
    { role: 'buyer', time: '10:42', text: "Hi! We're interested in 500 of these tumblers in sage, with our logo. Could you share a quote?", translation: '你好！我们想采购 500 只这款鼠尾草绿随行杯，并定制 Logo。可以提供报价吗？', product: true },
    { role: 'seller', time: '10:43', parts: [
      { text: "Hi Alex! I've noted 500 pcs in sage with your logo.", translation: '你好 Alex，已记下 500 只鼠尾草绿、定制 Logo。' },
      { text: 'Could you share your logo artwork and when you need them?', translation: '可以发一下 Logo 图稿，以及期望的收货时间吗？' }
    ], auto: true },
    { role: 'buyer', time: '10:48', text: 'Here is our logo. We need delivery to Hamburg by Dec 15. Can we see a sample first?', translation: '这是我们的 Logo。我们需要在 12 月 15 日前送到汉堡。可以先确认样品吗？', file: 'Brand logo.pdf' }
  ];
  const otherMessages = {
    emma: [{ role: 'buyer', time: '10:41', text: 'Hello! Could you send me the price list for your travel tumblers?', translation: '你好，可以发一份随行杯的价格表吗？', product: true }],
    noah: [{ role: 'buyer', time: '10:30', text: 'Hello, I would like to know more about your company.', translation: '你好，我想进一步了解你们的公司。' }, { role: 'seller', time: '10:31', text: 'Hi Noah! What kind of products are you looking for?', translation: '你好 Noah！你正在寻找哪类产品？' }, { role: 'buyer', time: '10:32', text: 'Thanks! Looking forward to it.', translation: '谢谢，期待进一步沟通。' }],
    sofia: [{ role: 'buyer', time: '10:18', text: '¿Podrían cotizar 300 unidades de este vaso térmico?', translation: '可以为这款保温杯的 300 件采购提供报价吗？', product: true }],
    liam: [{ role: 'buyer', time: '09:56', text: 'The ivory color looks great. Could you tell me more about the packaging?', translation: '象牙白很不错。可以介绍一下包装吗？', product: true }],
    mia: [{ role: 'buyer', time: '09:45', text: 'Merci pour votre réponse !', translation: '谢谢你的回复！' }],
    oliver: [{ role: 'buyer', time: '昨天 16:20', text: 'Can we customize the packaging with our own artwork?', translation: '我们可以用自己的图稿定制包装吗？', product: true }],
    hana: [{ role: 'buyer', time: '昨天 15:18', text: 'サンプルの詳細をお願いします。', translation: '请介绍一下样品的详细情况。' }]
  };
  const sessions = new Map(customers.map(c => [c.id, {
    human: c.status === 'human', draft: '', revision: 0, sending: false, pending: false,
    supplement: c.id === 'alex' ? '定制 Logo 需确认图稿与印刷尺寸。\n客户希望先看样品，再确认大货。' : '',
    attachments: c.id === 'alex' ? [{ name: '包装参考.pdf', fixture: true }] : [],
    messages: c.id === 'alex' ? alexMessages.slice() : otherMessages[c.id].slice(), suggestion: null, generation: 0
  }]));
  const state = { selected: 'alex', filter: 'all', query: '', scenario: 'manual', globalEnabled: true, range: window.DesignReceptionRange.read(), translations: true, expanded: false, fullFields: false, viewCase: 'current', panelCollapsed: false };
  let toastTimer, saveTimer, modalReturnFocus, lastCaseButton;
  const current = () => customers.find(c => c.id === state.selected);
  const session = () => sessions.get(state.selected);
  const inAutoScope = id => state.range.scope==='all'||state.range.customerIds.includes(id);
  const isNarrow = () => matchMedia('(max-width:1180px)').matches;
  function toast(text) {
    clearTimeout(toastTimer); $('#toast').textContent = text; $('#toast').hidden = false;
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3000);
  }
  function renderCustomers() {
    const rows = customers.filter(c => {
      const s = sessions.get(c.id);
      const search = `${c.name} ${c.country} ${c.snippet} ${summaries[c.id]}`.toLowerCase().includes(state.query.toLowerCase());
      const filter = state.filter === 'all' || (state.filter === 'human' ? s.human : state.filter === 'quote' ? c.quote : c.status === 'reply');
      return search && filter;
    });
    $('#customer-count').textContent = customers.length;
    $('#customer-list').innerHTML = rows.length ? rows.map(c => {
      const s = sessions.get(c.id);
      const tag = s.pending ? '发送结果待确认' : s.human ? (c.id === state.selected ? '你正在接待' : '人工接待') : !inAutoScope(c.id) ? '未纳入自动接待' : c.tag === '你正在接待' ? '自动接待中' : c.tag;
      const color = s.pending||s.human ? 'amber' : tag==='自动接待中' ? 'green' : tag==='未纳入自动接待' ? '' : c.status==='reply'||c.quote ? 'amber' : '';
      return `<button class="customer-row" data-customer="${c.id}" aria-current="${c.id === state.selected}" aria-label="${esc(c.name)}，${tag}"><div class="customer-row-main"><div class="customer-row-top"><span class="customer-row-name">${esc(c.name)}</span><time>${c.time}</time></div><div class="customer-row-message">${esc(c.snippet)}</div><div class="customer-row-bottom"><span class="row-tag ${color}">${tag}</span>${c.unread ? `<span class="row-unread">${c.unread}</span>` : ''}</div></div></button>`;
    }).join('') : '<p class="empty-list">没有找到客户<br>换个名字或筛选条件试试</p>';
  }
  function updateHeader() {
    const c = current(), s = session();
    $('#current-name').textContent = c.name;
    $('#current-meta').innerHTML = `${c.id === 'alex' ? '<span class="country-flag"></span>' : icon('globe')}${esc(c.country)} · ${esc(c.city)}<span class="meta-separator"></span>${esc(c.language)}`;
    $('#customer-mode').className = `customer-mode${s.human ? '' : state.globalEnabled&&inAutoScope(c.id) ? ' auto' : ' inactive'}`;
    $('#customer-mode').innerHTML = `${icon(s.human ? 'hand' : 'spark')}${s.human ? '你正在接待' : !inAutoScope(c.id) ? '未纳入自动接待' : state.globalEnabled ? '自动接待中' : '自动接待已关闭'}`;
    $('#summary-text').textContent = summaries[c.id] + (state.expanded && c.id === 'alex' ? ' 已提供 Logo 图稿；样品费用、运费与最终包装仍需确认。' : '');
    $('#summary-bar').setAttribute('aria-expanded', String(state.expanded));
    $('#resume-button').innerHTML = s.human ? `恢复自动接待${icon('play')}` : `人工接管${icon('hand')}`;
    $('#resume-button').hidden = s.pending;
    $('#reply-input').placeholder = `输入回复，与 ${c.name.split(' ')[0]} 继续聊聊…`;
    $('#reception-state-label').innerHTML = window.DesignNavigation.renderStatus(state.globalEnabled);
    $('#global-status-text').textContent = state.globalEnabled ? '关闭自动接待' : '开启自动接待';
    $('#global-status').setAttribute('aria-pressed', String(state.globalEnabled));
    $('#global-status').title = state.globalEnabled ? '关闭自动接待' : '开启自动接待';
    $('#global-status').classList.toggle('idle', !state.globalEnabled);
    $('#global-status').style.color = state.globalEnabled ? '' : '#3458db';
    $('#complete-history-button').disabled = Boolean(s.historyLoading);
    $('#complete-history-button').setAttribute('aria-busy', String(Boolean(s.historyLoading)));
    $('#complete-history-button span').textContent = s.historyLoading ? '补全中…' : '补全历史';
  }
  function messageContent(m) {
    return `<p class="message-body">${esc(m.text)}</p>${m.translation ? `<div class="message-translation"><span class="translation-label">译</span>${esc(m.translation)}</div>` : ''}${m.product ? `<button class="message-product" data-action="image" aria-label="查看客户提及的商品图片"><img src="assets/tumbler.svg" alt="鼠尾草绿 Luma 随行杯"><span><strong>Luma 随行保温杯</strong><small>450 ml · 鼠尾草绿</small><span class="text-button">客户提及的商品 ${icon('right')}</span></span></button>` : ''}${m.file ? `<button class="file-bubble" data-action="sample-file">${icon('file')}${esc(m.file)}<small>126 KB</small></button>` : ''}`;
  }
  function renderMessages(scroll = true) {
    const c = current(), s = session();
    $('#message-stream').innerHTML = `<button class="history-link" data-action="earlier">查看更早的聊天</button><div class="message-day">今天 · 11 月 16 日</div>` + s.messages.map((m,index) => `<article class="message ${m.role === 'seller' ? 'seller' : ''}" data-message-id="${c.id}-${index}" tabindex="-1"><div class="message-meta">${m.role === 'seller' ? '澄野生活' : esc(c.name)}${m.auto ? `<span class="auto-mark">${icon('spark')}自动回复</span>` : ''}<time>${esc(m.time)}</time></div>${m.parts ? `<div class="seller-stack">${m.parts.map(part => `<div class="message-bubble">${messageContent(part)}</div>`).join('')}</div>` : `<div class="message-bubble">${messageContent(m)}</div>`}${m.role === 'seller' ? `<div class="message-receipt">${icon('double-check')}${m.preview ? '本地预览消息' : '平台已受理'}</div>` : ''}</article>`).join('');
    if (scroll) $('#message-stream').scrollTop = $('#message-stream').scrollHeight;
  }
  function renderActivity() {
    const s = session();
    const el = $('#reply-activity');
    if (s.pending) {
      el.className = 'reply-activity unknown';
      el.innerHTML = `<div class="activity-copy">${icon('clock')}<strong>${esc(s.pendingLabel||'发送结果待确认')}</strong></div>`;
    } else if (!s.human && state.globalEnabled && inAutoScope(state.selected)) {
      el.className = 'reply-activity active';
      el.innerHTML = `<div class="activity-copy"><span class="activity-blue-dot"></span><strong>正在准备回复</strong><span class="activity-time">将逐条发送</span></div><button class="text-button" data-action="stop-auto">停止${icon('stop')}</button>`;
    } else {
      el.className = 'reply-activity';
      el.innerHTML = `<div class="activity-copy">${icon('check')}<strong>${current().id === 'alex' ? '自动接待已回复 2 条' : '当前没有自动回复任务'}</strong>${current().id === 'alex' ? '<span class="activity-time">10:43</span>' : ''}</div>`;
    }
    $('#composer').classList.toggle('blocked', s.pending);
    $('#compose-hint').textContent = s.pending ? '' : 'Enter 发送，Shift + Enter 换行';
    $('#composer-label').innerHTML = '<span class="mini-dot"></span>人工回复';
    updateComposer();
  }
  function updateComposer() {
    const s = session();
    $('#send-button').disabled = !s.draft.trim() || s.pending || s.sending;
    $('#send-button').innerHTML = `${icon(s.sending ? 'clock' : 'send')}${s.sending ? '发送中' : '发送'}`;
    $('#suggest-button span').textContent = s.draft.trim() ? '润色正文' : '回复建议';
    $('#draft-status').textContent = s.draft ? '草稿已保存' : '草稿已保存';
  }
  function scrollToTail() { const stream = $('#message-stream'); stream.scrollTop = stream.scrollHeight; }
  function renderSuggestion() {
    const s = session(), el = $('#suggestion');
    const stream = $('#message-stream');
    const followTail = stream.scrollHeight - stream.clientHeight - stream.scrollTop < 80;
    el.hidden = !s.suggestion;
    if (!s.suggestion) { if (followTail) requestAnimationFrame(scrollToTail); return; }
    const sg = s.suggestion;
    el.innerHTML = `<div class="suggestion-heading"><span>${icon('spark')}${sg.polish ? '润色结果' : '回复建议'}</span><span>${sg.polish ? '基于当前正文' : '结合聊天与业务补充'}</span></div><p class="suggestion-body">${esc(sg.text)}</p><p class="suggestion-translation"><span class="translation-label">译</span>${esc(sg.translation||'译文暂不可用')}</p><div class="suggestion-actions"><button class="text-button" data-action="discard">弃用</button><button class="text-button" data-action="regenerate">重新生成</button><button class="small-primary" data-action="replace">替换正文</button></div>`;
    if (followTail) requestAnimationFrame(scrollToTail);
  }
  function locateMessage(id) {
    const message=$(`[data-message-id="${CSS.escape(id||'')}"]`,$('#message-stream'));
    if(!message)return;
    closeDrawers();
    $$('.source-located').forEach(node=>node.classList.remove('source-located'));
    message.classList.add('source-located');
    requestAnimationFrame(()=>{message.scrollIntoView({block:'center'});message.focus({preventScroll:true});});
  }
  function renderRequirements() {
    const c = current();
    $('#case-switch').innerHTML = `${state.viewCase === 'current' ? '本次需求' : '历史需求'}${icon('down')}`;
    $('#quote-status').textContent = state.viewCase === 'old' ? '已停止' : c.quote ? '待报价' : '整理中';
    $('#quote-status').hidden = !c.product;
    if (!c.product) {
      $('#requirement-content').innerHTML = `<div class="empty-requirement">${icon('box')}<p>还没有采购需求</p><p>继续聊天，了解客户想要什么。</p></div>`;
      return;
    }
    const isOld = state.viewCase === 'old';
    const qty = isOld ? '200' : c.id === 'alex' ? '500' : c.id === 'sofia' ? '300' : null;
    const fields = [
      ...(qty ? [['数量', `<span class="field-count">${qty}</span>只${!isOld?`<button class="requirement-source" data-action="locate-source" data-message-id="${c.id}-0" aria-label="查看数量来源">来源</button>`:''}`]] : []),
      ['规格', '<span class="field-pill">450 ml</span>' + (c.id === 'alex' ? '<span class="field-pill">鼠尾草绿</span>' : c.id === 'liam' ? '<span class="field-pill">象牙白</span>' : '')],
      ...(c.id === 'alex' ? [
        ['定制', '杯身 Logo<span class="field-muted">，尺寸待确认</span>'],
        ['收货地', '德国 · 汉堡'],
        ['交期', '12 月 15 日前送达']
      ] : [])
    ];
    $('#requirement-content').innerHTML = `<button class="product-visual" data-action="image" aria-label="查看需求商品原图"><img src="assets/tumbler.svg" alt="Luma 鼠尾草绿随行保温杯"></button><div class="product-thumbnails" aria-label="商品参考图">${[1,2,3,4,5].map(n => `<button class="product-thumb" data-thumbnail="${n}" aria-label="查看商品图片 ${n}" aria-pressed="${n === 1}"><img src="assets/tumbler.svg" alt="商品参考视角 ${n}"></button>`).join('')}</div><h3 class="product-title">${isOld ? '春季礼盒采购' : 'Luma 随行保温杯'}</h3><p class="product-subtitle">双层不锈钢杯身 · 旋盖密封</p><div class="product-links"><button class="text-button" data-action="product-link">${icon('external')}商品链接</button><button class="text-button id-copy" data-action="copy-id" aria-label="复制商品编号">1601902417143 ${icon('copy')}</button></div><dl class="requirement-fields">${fields.map(([label, value]) => `<div class="requirement-field"><dt>${label}</dt><dd>${value}</dd></div>`).join('')}${state.fullFields ? '<div class="requirement-field"><dt>其他需求</dt><dd><span class="field-muted">暂无其他已记录需求</span></dd></div>' : ''}</dl><button class="more-fields" data-action="fields" aria-expanded="${state.fullFields}">${state.fullFields ? '收起更多需求' : '更多需求'}${icon('down')}</button><button class="procurement-link" data-action="procurement">${icon('box')}查看商品与供货方案${icon('right')}</button>`;
  }
  function renderAttachments() {
    $('#supplement-attachments').innerHTML = session().attachments.map((a, index) => `<span class="attachment-chip"><button data-attachment="${index}" aria-label="预览 ${esc(a.name)}">${icon('file')}<span>${esc(a.name)}</span></button><button data-remove-attachment="${index}" aria-label="移除 ${esc(a.name)}">${icon('close')}</button></span>`).join('');
  }
  function renderAll() {
    renderCustomers(); updateHeader(); renderMessages(); renderRequirements(); renderActivity(); renderSuggestion();
    $('#reply-input').value = session().draft;
    $('#supplement-input').value = session().supplement;
    $('#supplement-save').innerHTML = `${icon('check')}已保存`;
    renderAttachments();
  }
  function selectCustomer(id) {
    if (!sessions.has(id)) return;
    state.selected = id; state.expanded = false; state.viewCase = 'current'; state.fullFields = false;
    closeDrawers(); closePopover(); renderAll();
  }
  function takeOwnership() {
    session().human = true; updateHeader(); renderCustomers(); renderActivity();
  }
  function syncPanelButton() {
    const visible = isNarrow() ? $('#workspace').classList.contains('drawer-open') : !state.panelCollapsed;
    const button = $('.panel-toggle');
    button.setAttribute('aria-expanded', String(visible));
    button.setAttribute('aria-label', visible ? '收起客户资料' : '展开客户资料');
    button.title = visible ? '收起客户资料' : '展开客户资料';
  }
  function closeDrawers() {
    $('#workspace').classList.remove('drawer-open', 'customers-open');
    $('#drawer-scrim').hidden = true; syncPanelButton();
  }
  function togglePanel() {
    if (isNarrow()) {
      const willOpen = !$('#workspace').classList.contains('drawer-open');
      closeDrawers(); $('#workspace').classList.toggle('drawer-open', willOpen); $('#drawer-scrim').hidden = !willOpen;
    } else {
      state.panelCollapsed = !state.panelCollapsed;
      $('#workspace').classList.toggle('panel-collapsed', state.panelCollapsed);
    }
    syncPanelButton();
  }
  function showModal(title, content, buttons = []) {
    const modal = $('#modal');
    modalReturnFocus = document.activeElement;
    modal.classList.remove('image-modal');
    $('#modal-title').textContent = title; $('#modal-body').innerHTML = content;
    $('#modal-actions').innerHTML = buttons.length ? buttons.map(b => `<button class="button ${b.primary ? 'primary' : ''}" data-action="${b.action}">${esc(b.label)}</button>`).join('') : '<button class="button" data-action="close-modal">关闭</button>';
    closePopover(); if (!modal.open) modal.showModal();
  }
  function closeModal() { $('#modal').close(); if (modalReturnFocus?.isConnected) modalReturnFocus.focus(); }
  function closePopover() { $('#popover').hidden = true; }
  function showPopover(anchor, content) {
    const el = $('#popover');
    el.innerHTML = content; el.hidden = false;
    const rect = anchor.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(rect.right - el.offsetWidth, innerWidth - el.offsetWidth - 8)) + 'px';
    el.style.top = Math.min(rect.bottom + 8, innerHeight - el.offsetHeight - 12) + 'px';
    $('button', el)?.focus();
  }
  function suggestion(polish = Boolean(session().draft.trim())) {
    const s = session();
    const base = s.draft.trim();
    const samplePolish = ['We can arrange the sample. Please send your address.','We can arrange a sample for you. Could you please share your delivery address?'].includes(base);
    const previousTranslation = s.suggestion?.text===base?s.suggestion.translation:s.adoptedTranslation?.text===base?s.adoptedTranslation.translation:null;
    s.suggestion = {
      polish, sourceRevision: s.revision,
      text: polish ? samplePolish?'We can arrange a sample for you. Could you please share your delivery address?':base.replace(/\s+/g, ' ').replace(/([.!?])\1+/g, '$1') : current().id === 'alex' ? "Yes, we can arrange a sample first. Please share your delivery address and postal code in Hamburg, and I'll confirm the sample cost and shipping options for you." : `Hi ${current().name.split(' ')[0]}! Thanks for your message. Could you share a little more about what you need so I can help you with the right details?`,
      translation: polish ? samplePolish?'我们可以为你安排样品。请提供收货地址。':previousTranslation : current().id === 'alex' ? '可以先安排样品。请提供汉堡的详细收货地址和邮编，我会进一步确认样品费用与运输方案。' : '感谢你的消息。可以再说明一下具体需求吗？我会提供相应的信息。'
    };
    renderSuggestion();
  }
  function ownership() {
    const s = session();
    if(!s.human){takeOwnership();return;}
    if(s.pending)return;
    s.human=false;updateHeader();renderActivity();renderCustomers();
  }
  function send() {
    const s = session();
    if (s.pending || !s.draft.trim() || s.sending) return;
    submitPreview();
  }
  function submitPreview() {
    const id = state.selected, s = sessions.get(id);
    if (s.pending || s.sending || !s.draft.trim()) return;
    const frozen = { text: s.draft.trim(), revision: s.revision };
    s.human = true; s.sending = true; renderActivity(); updateHeader(); renderCustomers();
    setTimeout(() => {
      s.messages.push({ role: 'seller', text: frozen.text, time: '刚刚', preview: true });
      s.sending = false;
      if (s.revision === frozen.revision) { s.draft = ''; s.revision++; }
      if (state.selected === id) { $('#reply-input').value = s.draft; renderMessages(); updateComposer(); }
      toast('已加入本地预览聊天，未发送至客户');
    }, 450);
  }
  function setScenario(scenario) {
    if (!['manual', 'auto', 'unknown'].includes(scenario)) return;
    state.scenario = scenario;
    const s = sessions.get('alex');
    s.human = scenario !== 'auto'; s.pending = scenario === 'unknown'; s.pendingLabel=null;
    if (scenario === 'auto') state.globalEnabled = true;
    $$('.scenario-switch button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.scenario === scenario)));
    selectCustomer('alex');
  }
  function saveSupplement() {
    const id = state.selected, s = session();
    s.supplement = $('#supplement-input').value;
    $('#supplement-save').textContent = '保存中…';
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { if (state.selected === id) $('#supplement-save').innerHTML = `${icon('check')}已保存`; }, 450);
  }
  function addFiles(files) {
    const s = session();
    for (const file of files) s.attachments.push({ name: file.name, file });
    renderAttachments();
    if (files.length) toast(`已添加 ${files.length} 个附件，仅用于当前预览`);
  }
  async function copyText(text, success) {
    try { await navigator.clipboard.writeText(text); toast(success); }
    catch { toast('浏览器未允许复制，可在弹窗中选择文字复制'); }
  }
  async function copyImage() {
    try {
      const image = $('.modal-product-image');
      await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
      canvas.getContext('2d').drawImage(image, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('No image');
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]); toast('商品图片已复制');
    } catch { toast('浏览器未允许复制图片，可右键保存原图'); }
  }
  function startReception() {
    state.globalEnabled = true;
    updateHeader(); renderActivity(); renderCustomers(); toast('自动接待已开启（设计预览）');
  }
  function pauseReception() { state.globalEnabled = false; updateHeader(); renderActivity(); }
  const actions = {
    'start-reception': startReception,
    reception: () => { if (matchMedia('(max-width:650px)').matches) actions.customers(); else toast('当前为客户接待页'); },
    panel: togglePanel,
    customers: () => { const willOpen = !$('#workspace').classList.contains('customers-open'); closeDrawers(); $('#workspace').classList.toggle('customers-open', willOpen); $('#drawer-scrim').hidden = !willOpen; },
    'close-customers': closeDrawers,
    translations: () => { state.translations = !state.translations; $('#workspace').classList.toggle('no-translations', !state.translations); $('#translation-toggle').setAttribute('aria-pressed', String(state.translations)); $('#translation-toggle').setAttribute('aria-label', state.translations ? '关闭中文译文' : '显示中文译文'); },
    summary: () => { state.expanded = !state.expanded; updateHeader(); },
    suggest: () => suggestion(),
    regenerate: () => suggestion(session().suggestion?.polish),
    discard: () => { session().suggestion = null; renderSuggestion(); $('#reply-input').focus(); },
    replace: () => { const s = session(); if (!s.suggestion) return; if (s.revision !== s.suggestion.sourceRevision) { s.suggestion=null;renderSuggestion();toast('正文已更新，请重新生成');return; } actions['confirm-replace'](); },
    'confirm-replace': () => { const s = session(); if (!s.suggestion) return; s.adoptedTranslation={text:s.suggestion.text,translation:s.suggestion.translation}; s.draft = s.suggestion.text; s.revision++; s.suggestion = null; $('#reply-input').value = s.draft; renderSuggestion(); takeOwnership(); updateComposer(); if ($('#modal').open) closeModal(); $('#reply-input').focus(); },
    send,
    ownership,
    'stop-auto': () => { takeOwnership(); toast('已停止'); },
    earlier: () => toast('设计预览：此处查看本地更早聊天'),
    'complete-history': () => {
      const id = state.selected, s = session();
      if (s.historyLoading) return;
      s.historyLoading = true; updateHeader();
      setTimeout(() => {
        s.historyLoading = false;
        if (state.selected === id) { updateHeader(); toast('当前客户历史已补全（设计预览）'); }
      }, 850);
    },
    refresh: () => { $('#sync-time').textContent = '刚刚'; toast('预览客户列表已刷新'); },
    image: () => { showModal('客户提供的商品参考图', '<img class="modal-product-image" src="assets/tumbler.svg" alt="Luma 随行保温杯原始示意图">', [{ label: '关闭', action: 'close-modal' }, { label: '复制图片', action: 'copy-image', primary: true }]); $('#modal').classList.add('image-modal'); },
    'copy-image': copyImage,
    'sample-file': () => showModal('Brand logo.pdf', '<div class="modal-person"><span>客户附件</span><strong>PDF · 126 KB</strong></div><p>设计稿使用合成附件。正式界面在此预览客户提供的原文件。</p>'),
    'copy-id': () => copyText('1601902417143', '商品编号已复制'),
    'product-link': () => showModal('商品链接', `<p style="overflow-wrap:anywhere;user-select:text">${productUrl}</p><p>此链接按合成商品编号生成，仅用于设计预览。</p>`, [{ label: '关闭', action: 'close-modal' }, { label: '复制链接', action: 'copy-link', primary: true }]),
    'copy-link': () => copyText(productUrl, '商品链接已复制'),
    'locate-source': button => locateMessage(button.dataset.messageId),
    fields: () => { state.fullFields = !state.fullFields; const scroll = $('.requirement-scroll').scrollTop; renderRequirements(); $('.requirement-scroll').scrollTop = scroll; },
    cases: button => { lastCaseButton = button; showPopover(button, `<button data-action="current-case" class="${state.viewCase === 'current' ? 'selected-choice' : ''}">${icon('box')}<span>随行杯定制采购<small>当前需求 · 500 只</small></span></button><button data-action="old-case" class="${state.viewCase === 'old' ? 'selected-choice' : ''}">${icon('history')}<span>春季礼盒采购<small>历史需求 · 已停止</small></span></button>`); },
    'current-case': () => { state.viewCase = 'current'; closePopover(); renderRequirements(); lastCaseButton = $('#case-switch'); lastCaseButton.focus(); },
    'old-case': () => { state.viewCase = 'old'; closePopover(); renderRequirements(); toast('正在查看历史需求，当前接待事项保持不变'); },
    'customer-more': button => showPopover(button, `<button data-action="ownership">${icon('hand')}<span>${session().human ? '恢复自动接待' : '人工接管'}</span></button><button data-action="complete-history">${icon('refresh')}补全历史</button>`),
    attachment: () => $('#attachment-input').click(),
    global: () => { if (state.globalEnabled) pauseReception(); else startReception(); },
    home: () => { location.href = 'pages.html#home'; },
    settings: () => { location.href = 'pages.html#settings'; },
    products: () => { location.href = 'pages.html#products'; },
    suppliers: () => { location.href = 'pages.html#suppliers'; },
    library: () => { location.href = 'pages.html#library'; },
    launcher: () => { location.href = 'pages.html#launcher'; },
    procurement: () => { location.href = 'pages.html#procurement'; },
    quotes: () => { location.href = 'pages.html#quotes'; },
    profiles: () => { location.href = 'pages.html#customers'; },
    activity: () => { location.href = 'pages.html#activity'; },
    about: () => showModal('接待页设计预览', '<p>全新接待页视觉与交互提案。所有客户、商品、聊天和回执均为合成示例。</p><p>本页面不会连接账号、调用模型或发送真实消息。客户编辑内容刷新后重置；接待范围在本预览标签页内保留。</p>'),
    'close-modal': closeModal
  };
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.disabled) { if (!event.target.closest('#popover')) closePopover(); return; }
    if (button.dataset.customer) return selectCustomer(button.dataset.customer);
    if (button.dataset.filter) {
      state.filter = button.dataset.filter;
      $$('.customer-filters button').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
      return renderCustomers();
    }
    if (button.dataset.scenario) return setScenario(button.dataset.scenario);
    if (button.dataset.thumbnail) {
      $$('.product-thumb').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
      const image = $('.product-visual img'), n = Number(button.dataset.thumbnail);
      image.style.transform = n === 1 ? '' : `scale(${n === 2 ? 1.7 : n === 3 ? 1.5 : n === 4 ? 1.2 : 2})`;
      image.style.objectPosition = n === 2 ? 'center 10%' : n === 3 ? 'center 90%' : 'center';
      return;
    }
    if (button.dataset.removeAttachment !== undefined) {
      session().attachments.splice(Number(button.dataset.removeAttachment), 1); renderAttachments(); return;
    }
    if (button.dataset.attachment !== undefined) {
      const file = session().attachments[Number(button.dataset.attachment)];
      return showModal(file.name, `<p>${file.file ? '附件已保存在当前页面内存，可用于预览关联位置。' : '这是合成参考附件，用于演示业务补充的附件位置。'}</p>`);
    }
    const action = button.dataset.action;
    if (!button.closest('#popover')) closePopover();
    if (action && actions[action]) actions[action](button);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closePopover(); closeDrawers(); }
  });
  $('#drawer-scrim').addEventListener('click', closeDrawers);
  $('#customer-search').addEventListener('input', event => { state.query = event.target.value; renderCustomers(); });
  $('#reply-input').addEventListener('input', event => {
    const s = session(); s.draft = event.target.value; s.revision++;
    if (!s.human) takeOwnership();
    updateComposer();
  });
  $('#reply-input').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); }
  });
  $('#supplement-input').addEventListener('input', saveSupplement);
  $('#attachment-input').addEventListener('change', event => { addFiles([...event.target.files]); event.target.value = ''; });
  const supplementEditor = $('.supplement-editor');
  for (const name of ['dragenter', 'dragover']) supplementEditor.addEventListener(name, event => { event.preventDefault(); supplementEditor.classList.add('dragover'); });
  supplementEditor.addEventListener('dragleave', event => { if (!supplementEditor.contains(event.relatedTarget)) supplementEditor.classList.remove('dragover'); });
  supplementEditor.addEventListener('drop', event => { event.preventDefault(); supplementEditor.classList.remove('dragover'); addFiles([...event.dataTransfer.files]); });
  supplementEditor.addEventListener('paste', event => { const files = [...event.clipboardData.files]; if (files.length) { event.preventDefault(); addFiles(files); } });
  $('#modal').addEventListener('click', event => {
    if (event.target !== $('#modal')) return;
    const rect = $('#modal').getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal();
  });
  const layout = { panelWidth: null, supplementHeight: null };
  let finishResize = null;
  const panelBounds = () => ({ min: 280, max: Math.max(280, Math.min(560, innerWidth - parseFloat(getComputedStyle($('#workspace')).getPropertyValue('--rail')) - parseFloat(getComputedStyle($('#workspace')).getPropertyValue('--list')) - 430)) });
  const supplementBounds = () => ({ min: 180, max: Math.max(180, $('#customer-panel').clientHeight - 212) });
  function changePanelWidth(value) {
    const bounds = panelBounds(); layout.panelWidth = Math.round(Math.min(bounds.max, Math.max(bounds.min, value)));
    $('#workspace').style.setProperty('--panel', layout.panelWidth + 'px');
    $('#details-resize-handle').setAttribute('aria-valuenow', String(layout.panelWidth));
    $('#details-resize-handle').setAttribute('aria-valuemax', String(bounds.max));
  }
  function changeSupplementHeight(value) {
    const bounds = supplementBounds(); layout.supplementHeight = Math.round(Math.min(bounds.max, Math.max(bounds.min, value)));
    $('#workspace').style.setProperty('--supplement-height', layout.supplementHeight + 'px');
    $('#supplement-resize-handle').setAttribute('aria-valuenow', String(layout.supplementHeight));
    $('#supplement-resize-handle').setAttribute('aria-valuemin', String(bounds.min));
    $('#supplement-resize-handle').setAttribute('aria-valuemax', String(bounds.max));
  }
  function syncLayoutHandles() {
    for(const vertical of [false,true]){
      const panel=$(vertical?'#supplement':'#customer-panel');
      if(!panel.getBoundingClientRect().height)continue;
      const bounds=vertical?supplementBounds():panelBounds(),handle=$(vertical?'#supplement-resize-handle':'#details-resize-handle');
      handle.setAttribute('aria-valuemin',String(bounds.min));handle.setAttribute('aria-valuemax',String(bounds.max));
      handle.setAttribute('aria-valuenow',String(Math.round(panel.getBoundingClientRect()[vertical?'height':'width'])));
    }
  }
  function beginLayoutResize(event, vertical) {
    if (event.button !== 0 || (!vertical && isNarrow())) return;
    event.preventDefault(); finishResize?.();
    const start = vertical ? event.clientY : event.clientX;
    const initial = (vertical ? $('#supplement') : $('#customer-panel')).getBoundingClientRect()[vertical ? 'height' : 'width'];
    document.body.classList.add(vertical ? 'resizing-supplement' : 'resizing-details');
    const move = e => { const next = initial + start - (vertical ? e.clientY : e.clientX); if (vertical) changeSupplementHeight(next); else changePanelWidth(next); };
    const done = () => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', done); document.removeEventListener('pointercancel', done); document.body.classList.remove('resizing-supplement', 'resizing-details'); finishResize = null; };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', done, { once: true }); document.addEventListener('pointercancel', done, { once: true }); finishResize = done;
  }
  $('#details-resize-handle').addEventListener('pointerdown', event => beginLayoutResize(event, false));
  $('#supplement-resize-handle').addEventListener('pointerdown', event => beginLayoutResize(event, true));
  $('#details-resize-handle').addEventListener('dblclick', () => { layout.panelWidth = null; $('#workspace').style.removeProperty('--panel'); syncLayoutHandles(); });
  $('#supplement-resize-handle').addEventListener('dblclick', () => { layout.supplementHeight = null; $('#workspace').style.removeProperty('--supplement-height'); syncLayoutHandles(); });
  $('#details-resize-handle').addEventListener('keydown', event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); changePanelWidth($('#customer-panel').getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 16 : -16)); } });
  $('#supplement-resize-handle').addEventListener('keydown', event => { if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); changeSupplementHeight($('#supplement').getBoundingClientRect().height + (event.key === 'ArrowUp' ? 16 : -16)); } });
  window.addEventListener('resize', () => { finishResize?.(); closeDrawers(); closePopover(); syncPanelButton(); if (!isNarrow() && layout.panelWidth !== null) changePanelWidth(layout.panelWidth); if ($('#customer-panel').clientHeight && layout.supplementHeight !== null) changeSupplementHeight(layout.supplementHeight); requestAnimationFrame(scrollToTail); });
  renderAll(); syncPanelButton(); syncLayoutHandles();
  window.ReceptionPreview = Object.freeze({ actions, setScenario, showModal, closeModal, toast, state, session, renderAll, renderMessages, updateHeader, renderActivity, renderSuggestion, icon, esc });
  document.fonts.ready.then(scrollToTail);
})();
