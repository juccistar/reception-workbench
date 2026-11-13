'use strict';

// Standalone layout prototypes. All names, messages, images and states are synthetic.
// This file has no reception bridge, network access or business service integration.
const paths = {
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/>',
  home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/><path d="M9 21v-8h6v8"/>',
  box: '<path d="m3 7 9-4 9 4v11l-9 4-9-4Z"/><path d="m3 7 9 4 9-4M12 11v11M7 5l10 4"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
  book: '<path d="M3 3h6a4 4 0 0 1 4 4v14a4 4 0 0 0-4-4H3Zm18 0h-4a4 4 0 0 0-4 4v14a4 4 0 0 1 4-4h4Z"/>',
  settings: '<path d="M12 8v8M8 12h8M4 4l3 1 2-2h6l2 2 3-1 1 5-2 3 2 3-1 5-3-1-2 2H9l-2-2-3 1-1-5 2-3-2-3Z"/>',
  list: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 3h6v3H9ZM9 11h6m-6 4h6"/>',
  cart: '<path d="M3 3h2l3 12h10l3-8H6"/><circle cx="9" cy="20" r="1"/><circle cx="18" cy="20" r="1"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  right: '<path d="m9 5 7 7-7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  left: '<path d="m15 5-7 7 7 7"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 2M4 17l2 2a7 7 0 0 0 12-2"/>',
  send: '<path d="m22 2-7 20-4-9L2 9Zm0 0L11 13"/>',
  sparkle: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4ZM20 2v4m-2-2h4"/>',
  clip: '<path d="m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  history: '<path d="M3 11a9 9 0 1 1 2.4 7M3 4v7h7m2-4v5l3 2"/>',
  translate: '<path d="M3 4h12M9 2v3M5 5c1 6 6 9 8 10M13 5c-1 5-5 9-9 11m11 6 4-10 4 10m-6-4h4"/>',
  person: '<circle cx="12" cy="7" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  checks: '<path d="m2 12 4 4L16 6m-5 9 2 2L23 7"/>',
  pause: '<path d="M8 5v14m8-14v14"/>',
  play: '<path d="m7 4 14 8-14 8Z"/>',
  link: '<path d="m10 13 4-4m-6 6-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m4 2 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  file: '<path d="M14 2H5v20h14V7Zm0 0v6h6M8 13h8m-8 4h5"/>',
  map: '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18m-8-9 4-3v6Z"/>',
  store: '<path d="M3 9 5 3h14l2 6v3a3 3 0 0 1-4 2 3 3 0 0 1-5 0 3 3 0 0 1-5 0 3 3 0 0 1-4-2ZM5 14v7h14v-7M9 21v-5h6v5"/>',
  alert: '<path d="m12 3 10 18H2Z"/><path d="M12 9v4m0 4v.1"/>',
};
const icon = (name, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.dots}</svg>`;
const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function productImage(index) {
  const colors = ['#282f29', '#8c9692', '#8a684a', '#49606a', '#b8a27c'];
  const backgrounds = ['#eeefe6', '#f3f2e9', '#efede4', '#edf0ec', '#f1eee6'];
  const angle = [-7, 4, -2, 8, -5][index];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="560" height="360" viewBox="0 0 560 360"><defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="${backgrounds[index]}"/><stop offset="1" stop-color="#e0e4d9"/></linearGradient><linearGradient id="lens" x2="1" y2="1"><stop stop-color="#fff" stop-opacity=".85"/><stop offset="1" stop-color="#c8d3c5" stop-opacity=".38"/></linearGradient><filter id="shadow"><feGaussianBlur stdDeviation="8"/></filter></defs><rect width="560" height="360" fill="url(#bg)"/><ellipse cx="285" cy="238" rx="184" ry="18" fill="#94a089" opacity=".13" filter="url(#shadow)"/><g transform="translate(280 180) rotate(${angle}) translate(-280 -180)" stroke="${colors[index]}" stroke-width="7" stroke-linejoin="round" fill="url(#lens)"><path d="M91 153c15-17 95-17 130 0 10 9 5 54-11 69-14 13-69 18-91-1-14-13-32-50-28-68Z"/><path d="M335 153c35-17 115-17 130 0 4 18-14 55-28 68-22 19-77 14-91 1-16-15-21-60-11-69Z"/><path d="M225 158c10-16 42-16 54-1h54" fill="none"/><path d="m91 155-24-7 28-56 62-2m308 65 26-8-28-54-61-3" fill="none" stroke-width="6"/><path d="m105 158 18 46m227-46 16 46" stroke="#fff" stroke-opacity=".55" stroke-width="3"/></g></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
const photos = [0, 1, 2, 3, 4].map(productImage);
const candidates = [
  { name: 'Alex Morgan', initials: 'AM', time: '10:45', text: '¿Cuál es la cantidad mínima?', tag: '待回复', type: 'pending', unread: 1 },
  { name: 'Noah Bennett', initials: 'NB', time: '10:31', text: 'Thank you, I will confirm today.', tag: '等待客户', type: '', unread: 0 },
  { name: 'Sofia Ríos', initials: 'SR', time: '09:56', text: '¿Podrían cotizar 300 unidades?', tag: '待报价', type: 'pending', unread: 2 },
  { name: 'Emma Clarke', initials: 'EC', time: '09:28', text: 'Please share the size chart.', tag: '待回复', type: 'pending', unread: 1 },
  { name: 'Liam Parker', initials: 'LP', time: '昨天', text: 'The silver finish looks good.', tag: '需求整理中', type: 'incoming', unread: 0 },
  { name: 'Mia Laurent', initials: 'ML', time: '昨天', text: 'Merci pour votre réponse.', tag: '等待客户', type: '', unread: 0 },
  { name: 'Oliver Reed', initials: 'OR', time: '昨天', text: 'Can we customize the packaging?', tag: '需求整理中', type: 'incoming', unread: 0 },
];
const summary = '客户关注 AERO 02 黑色与银色，询问目录及试单起订量；采购数量、预算尚未明确。';
const suggestion = 'Hola, Alex. Para un pedido de prueba, el mínimo de este modelo es de 100 unidades. ¿Cuántas necesitas en negro y cuántas en plata?';
const fromUrl = new URLSearchParams(location.search).get('layout');
const state = {
  layout: ['a','b','c'].includes(fromUrl) ? fromUrl : 'a', scenario: 'normal', tab: 'requirements', drawer: false,
  translation: true, summaryExpanded: false, suggestionOpen: false, reply: '',
  supplement: 'AERO 02：试单 100 副起；黑色、银色可选。最终价格按数量确认，勿直接引用商品卡价格。',
  files: [{id:'color-reference', name:'颜色参考.png', image:photos[0], size:120000}, {id:'price-reference', name:'报价单.pdf', size:230000}],
  fileError: '', fileSequence: 0, zoom: 0, saveNote: '已保存', supplementNote: '已保存',
};
const app = document.getElementById('app');
const iconButton = (name, action, label, cls = '') => `<button type="button" class="icon-button ${cls}" data-action="${action}" title="${escape(label)}" aria-label="${escape(label)}">${icon(name)}</button>`;
const tag = (text, type = '', glyph = '') => `<span class="tiny-tag ${type}">${glyph ? icon(glyph) : ''}${text}</span>`;
function mainNavigation() {
  const entries = [['home','开始'],['chat','客户接待'],['box','产品库'],['users','供应商'],['cart','采购中心']];
  const bottom = [['list','待我报价'],['person','客户资料'],['book','资料与政策'],['settings','连接与设置']];
  const item = ([name,label]) => `<button type="button" class="nav-item ${name === 'chat' ? 'active' : ''}" data-action="nav-preview" title="${label}" aria-label="${label}">${icon(name)}<span>${label}</span></button>`;
  return `<aside class="main-nav" aria-label="工作空间导航"><p class="nav-label">工作空间</p>${entries.map(item).join('')}<div class="nav-spacer"></div>${bottom.map(item).join('')}<div class="merchant"><div class="merchant-avatar">${icon('store')}</div><div class="merchant-info"><strong>Aero Trading · 示例商家</strong><small><span class="dot green"></span> 本地已连接</small></div></div></aside>`;
}
function customers() {
  return `<aside class="customer-pane" aria-label="客户列表"><div class="pane-heading"><h2>客户接待 <span class="count">24</span></h2>${iconButton('refresh','refresh','同步近期消息')}</div><label class="search">${icon('search')}<input aria-label="搜索示例客户" placeholder="搜索客户或最近消息"></label><div class="filters" aria-label="筛选客户"><button type="button" class="active" data-filter="all">全部</button><button type="button" data-filter="pending">待回复</button><button type="button" data-filter="quote">待报价</button><button type="button" data-filter="manual">人工</button></div><div class="customer-list">${candidates.map((c,index) => `<button type="button" class="customer ${index === 0 ? 'active' : ''}" data-customer="${index}"><span class="avatar">${c.initials}</span><span class="customer-info"><span class="customer-top"><strong>${c.name}</strong><time>${c.time}</time></span><p>${c.text}</p><span class="customer-bottom">${tag(c.tag,c.type)}${index === 0 ? '<span title="人工接管">'+icon('person')+'</span>' : c.unread ? `<span class="unread">${c.unread}</span>` : ''}</span></span></button>`).join('')}</div><div class="customer-footer"><span class="dot green"></span> 已同步的本地客户</div></aside>`;
}
function conversationHeader() {
  return `<header class="conversation-header"><div class="identity"><div class="avatar">AM</div><div><div class="identity-top"><h1>Alex Morgan</h1>${tag('人工接管','manual','person')}</div><small>${icon('map')}西班牙 <span>·</span> 西班牙语</small></div></div><div class="conversation-tools">${iconButton('history','history','历史已补全 · 查看或补全历史')}${iconButton('translate','translation','中文翻译：'+(state.translation?'开启':'关闭'),state.translation?'selected':'')}<div class="tool-divider"></div><div class="status-wrap"><button class="icon-button state-trigger" data-action="status" type="button" aria-expanded="false" aria-label="查看会话状态"><span class="dot ${state.scenario==='offline'?'amber':'green'}"></span>状态 ${icon('down')}</button><div class="status-popover hidden"><h3>会话状态</h3><div class="status-row"><span>全局接待</span><strong>已暂停</strong></div><div class="status-row"><span>当前会话</span><strong>人工接管</strong></div><div class="status-row"><span>AI 能力</span><strong>可用 · 示例</strong></div><div class="status-row"><span>历史</span><strong>本地已补全</strong></div><div class="status-row"><span>服务</span><strong>${state.scenario==='offline'?'已断开':'已连接 · 示例'}</strong></div><button type="button" class="btn" data-action="resume">${icon('play')}恢复此客户自动接待</button></div></div><button type="button" class="btn context-toggle" data-action="open-requirements">${icon('panel')}采购需求</button><button type="button" class="btn text context-toggle" data-action="open-supplement">${icon('clip')}业务补充 <span class="count">${state.files.length}</span></button></div></header>`;
}
function summaryStrip() {
  return `<section class="summary ${state.summaryExpanded?'expanded':''}" aria-label="会话摘要"><button type="button" class="summary-button" data-action="summary" aria-expanded="${state.summaryExpanded}">${icon('book')}<span class="summary-title">摘要</span><span class="summary-text">${summary}${state.summaryExpanded?' 商家已发送颜色参考图，当前尚未发送本轮起订量答复。':''}</span>${icon('down','chevron')}</button></section>`;
}
function criticalNotice() {
  if (state.scenario==='unknown') return `<div class="critical" role="status">${icon('alert')}<p>发送结果未知，先核查原操作。</p><button class="btn" data-action="reconcile" type="button">核查原操作</button></div>`;
  if (state.scenario==='offline') return `<div class="critical error" role="status">${icon('alert')}<p>服务已断开，正文已保留，暂不能发送。</p><button class="btn" data-action="reconnect" type="button">查看连接</button></div>`;
  return '';
}
function messages() {
  return `<div class="conversation-meta"><span>今天 · 11 月 13 日</span><span class="translation-indicator">${icon('translate')}中文翻译</span></div><div class="message-scroll"><div class="message-stack"><div class="date-divider">今天 10:42</div><article class="message"><div class="message-meta"><span>Alex Morgan</span><time>10:42</time></div><div class="message-bubble"><p>Hola, estoy interesado en esta montura. ¿Tienen catálogo y opciones de color?</p><div class="translation">你好，我对这款镜架感兴趣。可以提供目录和可选颜色吗？</div></div></article><article class="message seller"><div class="message-meta"><span>商家</span><time>10:43</time></div><div class="message-bubble"><p>Hola, Alex. Gracias por tu mensaje. Te comparto los colores de este modelo.</p><div class="translation">你好，Alex。感谢咨询，这里是这款镜架的颜色参考。</div><div class="message-images">${[0,1].map(n=>`<button class="message-image" data-photo="${n}" type="button" aria-label="查看聊天图片 ${n+1}"><img src="${photos[n]}" alt="合成镜架颜色参考 ${n+1}"></button>`).join('')}</div></div><span class="read-mark">${icon('checks')}平台已受理</span></article><article class="message"><div class="message-meta"><span>Alex Morgan</span><time>10:45</time></div><div class="message-bubble"><p>Me gustan el negro y el plata. ¿Cuál es la cantidad mínima para un pedido de prueba?</p><div class="translation">我喜欢黑色和银色。试单的最低起订量是多少？</div></div></article></div></div>`;
}
function composer() {
  const issue = state.scenario==='save-error' ? `<div class="critical error" role="status">${icon('alert')}<p>正文保存失败，输入已保留。</p><button class="btn" data-action="retry-save" type="button">重试保存</button></div>` : '';
  return `<div class="compose-area">${issue}<section class="composer" aria-label="回复编辑器">${state.suggestionOpen?`<div class="suggestion-preview"><h3>${icon('sparkle')}回复建议</h3><p>${suggestion}</p><button class="btn" data-action="adopt" type="button">采用到输入框</button></div>`:''}<div class="suggestion-strip"><span>${icon('sparkle')}有一条回复建议</span><button data-action="suggestion" type="button">${state.suggestionOpen?'收起':'查看'} ${icon('right')}</button></div><textarea class="composer-input" aria-label="回复正文" placeholder="输入回复…" spellcheck="false">${escape(state.reply)}</textarea><div class="compose-toolbar"><div class="compose-left"><span class="save-indicator">${icon(state.scenario==='save-error'?'alert':'check')}<span>${state.scenario==='save-error'?'保存失败':state.saveNote}</span></span></div><div class="compose-right"><span class="keyboard-hint">Ctrl + Enter 发送</span><button type="button" class="btn text" data-action="generate">${icon('sparkle')}${state.reply.trim()?'润色':'回复建议'}</button><button type="button" class="btn primary send-button" data-action="send" ${!state.reply.trim()||state.scenario!=='normal'?'disabled':''}>${icon('send')}发送</button></div></div></section></div>`;
}
function requirementContent() {
  return `<div class="context-scroll"><div class="product-intro"><button type="button" class="product-cover" data-photo="0" aria-label="放大商品图片"><img src="${photos[0]}" alt="合成商品 AERO 02"></button><div><h3>AERO 02 光学镜架</h3><p>钛合金 · 方形镜框<br>黑色 / 银色</p></div></div><div class="product-gallery" aria-label="五张商品图片">${photos.map((src,n)=>`<button type="button" class="product-thumb" data-photo="${n}" aria-label="查看商品图片 ${n+1}"><img src="${src}" alt="合成商品图片 ${n+1}"></button>`).join('')}</div><dl class="requirement-list"><div class="requirement-row"><dt>产品 ID</dt><dd><span class="copy-id">1600000000123 <button type="button" data-action="copy-id" aria-label="复制示例产品ID" title="复制产品 ID">${icon('copy')}</button></span></dd></div><div class="requirement-row"><dt>产品链接</dt><dd><button type="button" class="product-link" data-action="product-link">${icon('link')}复制产品链接</button></dd></div><div class="requirement-row"><dt>关键规格</dt><dd>钛合金方形镜架；黑色与银色</dd></div><div class="requirement-row"><dt>数量</dt><dd><span class="blank">—</span></dd></div><div class="requirement-row"><dt>收货地</dt><dd>西班牙</dd></div><div class="requirement-row"><dt>联系方式</dt><dd>Alex Morgan（买家自述）</dd></div></dl><button type="button" class="requirement-action" data-action="product-library">${icon('box')}查看产品与供货方案 ${icon('right')}</button></div>`;
}
function supplementContent() {
  return `<section class="supplement-section" aria-label="业务补充"><div class="supplement-heading"><h2>业务补充</h2><span>${state.files.length} 个附件</span></div><div class="supplement-box"><div class="file-chips">${state.files.map(f=>`<span class="file-chip">${f.image?`<img src="${f.image}" alt="${escape(f.name)} 预览">`:icon('file')}<span title="${escape(f.name)}">${escape(f.name)}</span><button type="button" data-remove="${escape(f.id)}" aria-label="移除 ${escape(f.name)}">${icon('close')}</button></span>`).join('')}</div><textarea class="supplement-input" aria-label="业务补充内容" placeholder="补充报价、交期等信息，可粘贴或拖入文件…" spellcheck="false">${escape(state.supplement)}</textarea><div class="supplement-bottom"><button type="button" class="add-file" data-action="add-file" aria-label="添加业务补充附件" title="添加附件 · 图片、PDF、文本">${icon('plus')}</button><span class="supplement-save">${state.supplementNote}</span></div><input class="file-picker" type="file" multiple accept=".png,.jpg,.jpeg,.webp,.pdf,.txt,.md,.csv" hidden><div class="file-error" ${state.fileError?'':'hidden'} role="status">${escape(state.fileError)}</div></div></section>`;
}
function tabbedContents() {
  const selected = state.tab;
  return `<nav class="context-tabs" aria-label="客户资料页签" role="tablist"><button type="button" class="context-tab ${selected==='requirements'?'active':''}" role="tab" aria-selected="${selected==='requirements'}" data-tab="requirements">采购需求</button><button type="button" class="context-tab ${selected==='supplement'?'active':''}" role="tab" aria-selected="${selected==='supplement'}" data-tab="supplement">业务补充 <small>${state.files.length}</small></button><button type="button" class="context-tab ${selected==='summary'?'active':''}" role="tab" aria-selected="${selected==='summary'}" data-tab="summary">会话摘要</button></nav><div class="tab-content" role="tabpanel" aria-label="${selected==='requirements'?'采购需求':selected==='supplement'?'业务补充':'会话摘要'}">${selected==='requirements'?requirementContent():selected==='supplement'?supplementContent():`<div class="tab-summary"><h3>会话摘要</h3><p>${summary}</p><p>商家已发送颜色参考图，当前尚未发送本轮起订量答复。</p><small>更新于 10:45 · 当前会话</small></div>`}</div>`;
}
function sidePane() {
  if (state.layout==='a') return `<aside class="context-pane" aria-label="采购需求和业务补充"><div class="context-heading"><h2>采购需求</h2>${iconButton('dots','context-menu','需求操作')}</div>${requirementContent()}${supplementContent()}</aside>`;
  if (state.layout==='b'&&!state.drawer) return '';
  return `${state.layout==='b'?'<div class="drawer-scrim" data-action="close-drawer"></div>':''}<aside class="context-pane ${state.layout==='b'?'drawer-open':''}" aria-label="客户资料">${state.layout==='b'?`<div class="drawer-title">客户资料 ${iconButton('close','close-drawer','关闭客户资料')}</div>`:''}${tabbedContents()}</aside>`;
}
function render() {
  app.className = `workbench layout-${state.layout} ${state.translation?'':'no-translations'}`;
  app.innerHTML = `<header class="app-top"><div class="app-brand"><div class="app-logo">${icon('chat')}</div><div><strong>询盘接待助手</strong><small>客户接待工作台</small></div></div><div class="breadcrumb"><span>工作台</span>${icon('right')}<strong>客户接待</strong></div><div class="app-top-right"><span class="global-pause"><span class="dot"></span>自动接待已暂停</span>${iconButton('refresh','refresh','同步消息')}<button type="button" class="btn" data-action="global-settings">${icon('play')}设置自动接待</button></div></header><div class="app-body">${mainNavigation()}${customers()}<main class="conversation" aria-label="Alex Morgan 的客户接待">${conversationHeader()}${summaryStrip()}${criticalNotice()}${messages()}${composer()}</main>${sidePane()}</div>`;
  for (const button of document.querySelectorAll('[data-layout]')) button.classList.toggle('active',button.dataset.layout===state.layout);
  document.getElementById('scenario').value=state.scenario;
  updateSend();
  requestAnimationFrame(() => {
    const messages = app.querySelector('.message-scroll');
    if (messages) messages.scrollTop = messages.scrollHeight;
  });
}
let toastTimer;
function toast(message) {
  const target=document.getElementById('preview-toast');
  target.textContent=message;target.classList.add('visible');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>target.classList.remove('visible'),2800);
}
function updateSend() {
  const send=app.querySelector('.send-button');
  if (send) send.disabled=!state.reply.trim()||state.scenario!=='normal';
  const button=app.querySelector('[data-action="generate"]');
  if (button) button.innerHTML=icon('sparkle')+(state.reply.trim()?'润色':'回复建议');
}
function zoom(index) {
  state.zoom=index;
  document.getElementById('overlay-root').innerHTML=`<div class="image-overlay"><div class="image-modal" role="dialog" aria-modal="true" aria-label="商品图片预览"><div class="image-modal-top"><span>AERO 02 · 商品图片</span>${iconButton('close','close-image','关闭图片')}</div><img src="${photos[index]}" alt="合成商品图片 ${index+1}"><div class="image-modal-bottom">${iconButton('left','previous-image','上一张图片')}<span>${index+1} / 5</span>${iconButton('right','next-image','下一张图片')}</div></div></div>`;
  document.querySelector('[data-action="close-image"]').focus();
}
function receiveFiles(files) {
  const allowed=/\.(png|jpe?g|webp|pdf|txt|md|csv)$/i;
  const failures=[];
  for (const file of files) {
    if (!allowed.test(file.name)) {failures.push(`${file.name}：格式暂不支持`);continue;}
    if (state.files.length>=5) {failures.push('最多添加 5 个附件');break;}
    if (file.size>6*1024*1024) {failures.push(`${file.name}：超过 6 MiB`);continue;}
    if (state.files.reduce((total,row)=>total+row.size,0)+file.size>20*1024*1024) {failures.push('附件合计超过 20 MiB');continue;}
    state.files.push({id:'local-file-'+(++state.fileSequence),name:file.name,size:file.size,image:/^image\//.test(file.type)?URL.createObjectURL(file):null});
  }
  state.fileError=[...new Set(failures)].join('；');state.supplementNote='已加入预览';render();
  if (!failures.length) toast('已加入设计预览；关闭页面后不保留上传文件。');
}
function switchLayout(layout) {
  state.layout=layout;state.drawer=false;state.tab='requirements';state.suggestionOpen=false;
  const url=new URL(location.href);url.searchParams.set('layout',layout);history.replaceState(null,'',url);render();
}
document.addEventListener('click',event=>{
  const layout=event.target.closest('[data-layout]');if(layout){switchLayout(layout.dataset.layout);return;}
  const photo=event.target.closest('[data-photo]');if(photo){zoom(Number(photo.dataset.photo));return;}
  const tab=event.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;render();return;}
  const remove=event.target.closest('[data-remove]');if(remove){const row=state.files.find(f=>f.id===remove.dataset.remove);if(row?.image?.startsWith('blob:'))URL.revokeObjectURL(row.image);state.files=state.files.filter(f=>f.id!==remove.dataset.remove);state.supplementNote='预览已更新';render();return;}
  const customer=event.target.closest('[data-customer]');if(customer&&customer.dataset.customer!=='0'){toast('此稿固定使用 Alex 的合成会话，便于三版对比。');return;}
  const filter=event.target.closest('[data-filter]');if(filter){for(const item of app.querySelectorAll('[data-filter]'))item.classList.toggle('active',item===filter);toast('筛选按钮仅展示选中样式。');return;}
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(!action){if(!event.target.closest('.status-wrap')){app.querySelector('.status-popover')?.classList.add('hidden');app.querySelector('[data-action="status"]')?.setAttribute('aria-expanded','false');}return;}
  if(action==='status'){const popover=app.querySelector('.status-popover');popover.classList.toggle('hidden');app.querySelector('[data-action="status"]').setAttribute('aria-expanded',String(!popover.classList.contains('hidden')));}
  else if(action==='translation'){state.translation=!state.translation;render();}
  else if(action==='summary'){state.summaryExpanded=!state.summaryExpanded;render();}
  else if(action==='suggestion'||action==='generate'){state.suggestionOpen=!state.suggestionOpen;render();}
  else if(action==='adopt'){state.reply=suggestion;state.suggestionOpen=false;state.saveNote='预览已更新';render();app.querySelector('.composer-input').focus();}
  else if(action==='open-requirements'||action==='open-supplement'){state.drawer=true;state.tab=action==='open-supplement'?'supplement':'requirements';render();}
  else if(action==='close-drawer'){state.drawer=false;render();}
  else if(action==='add-file')app.querySelector('.file-picker').click();
  else if(action==='close-image')document.getElementById('overlay-root').innerHTML='';
  else if(action==='previous-image')zoom((state.zoom+4)%5);
  else if(action==='next-image')zoom((state.zoom+1)%5);
  else if(action==='send')toast('这是设计预览，未发送任何消息。');
  else if(action==='retry-save')toast('此处演示保存失败的重试入口，实际修复按开发方案执行。');
  else if(action==='reconcile')toast('此处进入原操作核查；核查期间保持禁发。');
  else if(action==='history')toast('此处查看历史覆盖与补全操作，常态不占聊天高度。');
  else if(action==='product-link'||action==='copy-id')toast('此处复制示例产品信息；预览未访问外部平台。');
  else if(action==='resume')toast('实际恢复仍检查全局启停、授权范围和当前版本。');
  else toast('此按钮展示入口位置；本稿仅演示接待页布局。');
});
document.getElementById('scenario').addEventListener('change',event=>{state.scenario=event.target.value;render();});
document.addEventListener('input',event=>{
  if(event.target.matches('.composer-input')){state.reply=event.target.value;state.saveNote='预览已更新';app.querySelector('.save-indicator span').textContent=state.scenario==='save-error'?'保存失败':state.saveNote;updateSend();}
  if(event.target.matches('.supplement-input')){state.supplement=event.target.value;state.supplementNote='预览已更新';app.querySelector('.supplement-save').textContent=state.supplementNote;}
  if(event.target.matches('.search input')){const value=event.target.value.toLowerCase();for(const row of app.querySelectorAll('[data-customer]'))row.hidden=!row.textContent.toLowerCase().includes(value);}
});
document.addEventListener('change',event=>{if(event.target.matches('.file-picker'))receiveFiles([...event.target.files]);});
document.addEventListener('dragover',event=>{const box=event.target.closest('.supplement-box');if(box&&event.dataTransfer.types.includes('Files')){event.preventDefault();box.classList.add('dragging');}});
document.addEventListener('dragleave',event=>{const box=event.target.closest('.supplement-box');if(box&&!box.contains(event.relatedTarget))box.classList.remove('dragging');});
document.addEventListener('drop',event=>{const box=event.target.closest('.supplement-box');if(event.dataTransfer.files.length){event.preventDefault();if(box)receiveFiles([...event.dataTransfer.files]);else toast('请将文件拖入业务补充输入区。');}});
document.addEventListener('paste',event=>{if(event.target.closest('.supplement-box')){const files=[...event.clipboardData.files];if(files.length){event.preventDefault();receiveFiles(files);}}});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){document.getElementById('overlay-root').innerHTML='';state.drawer=false;render();}
  if(event.ctrlKey&&event.key==='Enter'&&event.target.matches('.composer-input')){event.preventDefault();if(state.reply.trim()&&state.scenario==='normal')toast('这是设计预览，未发送任何消息。');}
});
render();
