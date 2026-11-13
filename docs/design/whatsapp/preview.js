'use strict';

const icons = {
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/>',
  phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 3.1 5.2 2 2 0 0 1 5.1 3h3a2 2 0 0 1 2 1.7l.5 2.8a2 2 0 0 1-.6 1.8L8.7 10.6a16 16 0 0 0 4.7 4.7l1.3-1.3a2 2 0 0 1 1.8-.6l2.8.5a2 2 0 0 1 1.7 2Z"/>',
  home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-6v-7h-4v7H4a1 1 0 0 1-1-1Z"/>',
  box: '<path d="m3 7 9 5 9-5M12 12v10M3 7l9-5 9 5v10l-9 5-9-5Z"/>',
  people: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/><circle cx="9" cy="7" r="4"/>',
  book: '<path d="M12 7v14M3 3h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3Z"/>',
  settings: '<path d="m12 3 2 3 3-.2.8 3 2.4 1.7-1.4 2.7 1 2.8-2.7 1.4-1.4 2.7-2.8-1-2.7 1.4L8.5 18l-3-.8.2-3-3-2 3-2-.2-3 3-.8L10 3Z"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="10.8" cy="10.8" r="7"/><path d="m16 16 5 5"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  link: '<path d="m10 13 4-4m-6 6-1.5 1.5a3.5 3.5 0 0 1-5-5L6 7a3.5 3.5 0 0 1 5 0m2 10a3.5 3.5 0 0 0 5 0l4.5-4.5a3.5 3.5 0 0 0-5-5L16 9"/>',
  arrow: '<path d="M7 17 17 7M7 7h10v10"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  double: '<path d="m2 12 4 4L16 6m-4 10L22 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  spark: '<path d="m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7Z"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Zm0 0L11 13"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="m7 4 14 8-14 8Z"/>',
  refresh: '<path d="M20 8a8 8 0 1 0 .5 7M20 3v5h-5"/>',
  history: '<path d="M3 10a9 9 0 1 1 1.8 7.4M3 4v6h6m3-4v6l4 2"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8M8 17h6"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.chat}</svg>`;
const state = { view: 'workbench', scenario: 'normal', channel: 'wa', bound: true, auto: true, suggestion: true, selected: 'olivia', verified: false, connected: false, draft: '', filter: 'all' };
const app = document.querySelector('#app');
const overlay = document.querySelector('#overlay');
const suggestionBody = "I can check the sample options for the 500 canvas tote bags with your embroidered logo. Could you share the delivery address you'd like us to use? I can then confirm the sample and shipping costs.";
const isUnbound = () => state.scenario === 'unbound' || !state.bound;
const isBlocked = () => ['unknown', 'offline'].includes(state.scenario);
const tag = (text, type = '') => `<span class="tag ${type}">${text}</span>`;
function header() {
  const online = state.view === 'connection' ? state.connected : state.scenario !== 'offline';
  const connectionLabel = online ? '已连接' : state.view === 'connection' ? '等待连接' : '已断开';
  return `<header class="app-header"><div class="brand"><span class="brand-mark">${icon('chat')}</span><div><strong>询盘接待助手</strong><small>客户接待工作台</small></div></div><div class="breadcrumb">工作台 ${icon('chevron')} <span>${state.view === 'connection' ? '渠道连接' : '客户接待'}</span></div><div class="header-right"><span class="merchant-name">演示商家 · Northline</span><span class="connection"><i class="dot ${online ? 'green' : 'amber'}"></i>WhatsApp · ${connectionLabel}</span><button class="btn" data-action="connection">${icon('settings')}渠道设置</button></div></header>`;
}
function rail() {
  return `<nav class="rail" aria-label="工作台导航"><button title="工作台" data-action="home">${icon('home')}</button><button title="客户接待" class="${state.view !== 'connection' ? 'active' : ''}" data-action="home">${icon('chat')}</button><button title="采购需求" data-action="prototype">${icon('file')}</button><button title="产品" data-action="prototype">${icon('box')}</button><button title="客户" data-action="binding">${icon('people')}</button><button title="资料" data-action="prototype">${icon('book')}</button><button title="渠道连接" class="rail-bottom ${state.view === 'connection' ? 'active' : ''}" data-action="connection">${icon('settings')}</button></nav>`;
}
function customers() {
  const rows = [
    ['olivia', 'Olivia Martin', '14:26', 'Could you confirm the sample options…', isUnbound() ? '待绑定' : '已绑定', '3'],
    ['daniel', 'Daniel Brown', '14:18', 'Hi, I saw your products on Alibaba.', '待绑定', '1'],
    ['emma', 'Emma Clarke', '13:47', 'Thank you, I will review the details.', '人工接管', ''],
    ['lucas', 'Lucas Reed', '昨天', 'Can you share the packaging options?', '已绑定', ''],
  ];
  const visibleRows = rows.filter(row => state.filter !== 'unbound' || row[0] === 'daniel');
  return `<aside class="customers"><div class="pane-title"><h2>客户接待<small>4</small></h2><button class="icon" title="更新会话" data-action="refresh">${icon('refresh')}</button></div><div class="channel-tabs"><button data-channel="all" class="${state.channel === 'all' ? 'active' : ''}">全部</button><button data-channel="ali" class="${state.channel === 'ali' ? 'active' : ''}">国际站</button><button data-channel="wa" class="${state.channel === 'wa' ? 'active' : ''}">WhatsApp</button></div><label class="search">${icon('search')}<input aria-label="搜索客户" placeholder="搜索姓名、电话或关联客户"></label><div class="filters"><button data-filter="all" class="${state.filter === 'all' ? 'active' : ''}">全部</button><button data-filter="unbound" class="${state.filter === 'unbound' ? 'active' : ''}">待绑定</button><button data-action="prototype">待回复</button><button data-action="prototype">人工</button></div><div class="customer-list">${visibleRows.map(row => `<button class="customer ${(state.selected === row[0]) ? 'active' : ''}" data-customer="${row[0]}"><div class="customer-head"><strong>${row[1]}</strong><time>${row[2]}</time></div><p>${row[3]}</p><div class="customer-bottom">${tag(state.channel === 'ali' ? '国际站' : 'WhatsApp', state.channel === 'ali' ? 'ali' : 'wa')}${tag(row[4], row[4] === '人工接管' ? 'manual' : 'bound')}${row[5] ? `<span class="unread">${row[5]}</span>` : ''}</div></button>`).join('')}</div><div class="customer-footer"><i class="dot green"></i>当前商家 · 单聊会话</div></aside>`;
}
function chat() {
  const unbound = isUnbound();
  const name = 'Olivia Martin';
  const ali = state.channel === 'ali';
  const notice = state.scenario === 'unknown' ? `<div class="state-notice">${icon('history')}<span>上一条消息的发送结果尚未确认，核查后再继续发送。</span><button class="btn" data-action="reconcile">核查原发送</button></div>` : state.scenario === 'offline' ? `<div class="state-notice">${icon('phone')}<span>WhatsApp 已断开，自动接待已暂停。</span><button class="btn" data-action="connection">查看连接</button></div>` : '';
  const messages = unbound ? `<article class="message"><div class="message-meta">Olivia Martin · 14:18</div><div class="bubble">Hi, I saw your products on Alibaba. Can we discuss the tote bags here?<div class="translation">你好，我在国际站看到了你们的产品，可以在这里聊一下帆布袋吗？</div></div></article>` : `<article class="message"><div class="message-meta">${name} · 14:20</div><div class="bubble">${ali ? 'We need 500 canvas tote bags with our embroidered logo, delivered to Paris.' : 'Hi, this is Olivia. We spoke on Alibaba about the canvas tote bags.'}<div class="translation">${ali ? '我们需要500个帆布袋，带刺绣Logo，送到巴黎。' : '你好，我是 Olivia。我们在国际站聊过帆布袋。'}</div></div></article><article class="message own"><div class="message-meta">Northline · 14:22</div><div class="bubble">Hi Olivia, I have the tote bag details here. We can continue our conversation here.</div><div class="receipt">${icon('double')}已读</div></article><article class="message"><div class="message-meta">${name} · 14:26</div><div class="bubble">Could you confirm the sample options and the address you need for delivery?<div class="translation">可以确认一下样品方案，以及寄送需要我提供什么地址信息吗？</div></div></article>`;
  return `<section class="conversation"><header class="chat-header"><div class="identity"><div class="name-line"><h1>${name}</h1>${tag(ali ? '国际站' : 'WhatsApp', ali ? 'ali' : 'wa')}</div><div class="identity-meta"><span>${'+33 6 00 00 12 34'}</span><button data-action="binding">${icon('link')}${unbound ? '绑定国际站客户' : 'Olivia M. · 国际站'}${icon('chevron')}</button></div></div><div class="chat-tools"><button class="icon" title="查看本轮上下文" data-action="context">${icon('book')}</button><button class="btn" data-action="auto" ${unbound || isBlocked() ? 'disabled' : ''}>${icon(state.auto ? 'pause' : 'play')}${state.auto && !unbound ? '暂停此客户' : '恢复自动接待'}</button><button class="icon context-mobile" title="客户背景" data-action="context">${icon('file')}</button></div></header>${notice}<button class="summary" data-action="context">${icon('book')}<strong>摘要</strong><span>${unbound ? '客户询问能否继续讨论帆布袋。' : '帆布袋500个，刺绣Logo，送至巴黎；正在确认样品和收货信息。'}</span>${icon('down')}</button><div class="messages">${messages}</div><div class="compose-area">${state.suggestion && !unbound && !isBlocked() ? `<div class="suggestion"><div class="suggestion-title">${icon('spark')}回复建议<small>待处理</small></div><p>${suggestionBody}</p><div class="suggestion-actions"><button class="btn primary" data-action="adopt">替换到输入框</button><button class="btn quiet" data-action="generate">重新生成</button><button class="btn quiet" data-action="discard">弃用</button></div></div>` : ''}<div class="composer"><textarea id="draft" aria-label="回复正文" placeholder="输入回复…"></textarea><div class="composer-bottom"><div class="save">${icon('check')}已保存<span class="preview-only">设计预览</span></div><div class="composer-actions"><button class="btn quiet" data-action="generate">${icon('spark')}生成回复建议</button><button class="btn primary send-button" data-action="send" ${isBlocked() ? 'disabled' : ''}>${icon('send')}发送到 ${ali ? '国际站' : 'WhatsApp'}</button></div></div></div></div></section>`;
}
function sidebar() {
  const unbound = isUnbound();
  return `<aside class="context-pane"><div class="context-heading"><h2>客户背景</h2><button class="btn quiet" data-action="${unbound ? 'binding' : 'context'}">${unbound ? '关联客户' : '本轮带入'} ${icon('chevron')}</button></div>${unbound ? `<div class="context-scroll"><div class="empty-context"><div class="empty-symbol">${icon('link')}</div><h3>关联国际站客户</h3><p>绑定后可使用已保存的聊天、<br>采购需求与业务补充。</p><button class="btn primary" data-action="binding">${icon('plus')}绑定客户</button></div></div>` : `<div class="context-scroll"><div class="linked-customer"><div class="line">${tag('国际站', 'ali')}<strong>Olivia M.</strong>${tag('已绑定', 'bound')}</div><p>法国 · Northline 当前商家<br>最近同步 今天14:20</p><button class="link" data-action="ali-history">查看国际站聊天 ${icon('arrow')}</button></div><div class="subheading"><strong>关联采购需求</strong><small>来自国际站</small></div><select class="requirement-select" aria-label="关联采购需求"><option>帆布袋 · 500个 · 本次采购</option><option>棉布袋 · 1000个 · 历史采购</option></select><dl class="facts"><div class="fact"><dt>产品</dt><dd>帆布手提袋</dd></div><div class="fact"><dt>数量与单位</dt><dd>500个</dd></div><div class="fact"><dt>规格</dt><dd>12oz棉帆布 · 原色</dd></div><div class="fact"><dt>定制</dt><dd>客户Logo刺绣</dd></div><div class="fact"><dt>收货地</dt><dd>法国巴黎</dd></div><div class="fact"><dt>样品费用</dt><dd class="blank">尚未确认</dd></div></dl><div class="subheading"><strong>国际站业务补充</strong><button class="source-link" data-action="context">查看来源</button></div><p class="background-note">客户已同意在WhatsApp继续沟通。样品方案、费用及寄送安排尚待确认，不作已安排处理。</p></div>`}<section class="supplement"><div class="supplement-title"><h2>业务补充</h2>${tag('仅 WhatsApp')}</div><div class="supplement-box"><textarea aria-label="WhatsApp业务补充" placeholder="补充本渠道的报价、样品或沟通信息…">${unbound ? '' : '优先确认收货信息；样品费用核实后再答复客户。'}</textarea><footer class="supplement-footer"><button title="添加附件" data-action="attachment">${icon('plus')}</button><button title="附件说明" data-action="help">?</button><small>已保存</small></footer></div></section></aside>`;
}
function binding() {
  return `<div class="scrim" data-dismiss="true"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="binding-title"><header class="modal-header"><div><span class="eyebrow">CUSTOMER CONNECTION</span><h2 id="binding-title">绑定国际站客户</h2><p>核对同一位客户后，带入当前商家的聊天与采购背景。</p></div><button class="icon" data-action="close" aria-label="关闭绑定">${icon('close')}</button></header><div class="modal-body"><div class="binding-left"><span class="field-label">当前 WhatsApp 联系人</span><div class="contact-card">${tag('WhatsApp', 'wa')}<strong>Olivia Martin</strong><p>+33 6 00 00 12 34 · 单聊</p></div><label class="field-label" for="binding-search">选择国际站客户</label><label class="search">${icon('search')}<input id="binding-search" placeholder="搜索客户姓名或买家ID" value="Olivia"></label><div class="candidates"><button class="candidate active" data-candidate="olivia"><span class="selected-check">${icon('check')}</span><strong>Olivia M.</strong><small>法国</small><p>帆布袋 · 500个 · 今天14:20</p></button><button class="candidate" data-candidate="oliver"><strong>Oliver R.</strong><small>英国</small><p>包装袋 · 3000个 · 昨天17:02</p></button><button class="candidate" data-candidate="olive"><strong>Olive Trading</strong><small>德国</small><p>棉布购物袋 · 上周</p></button></div></div><div class="binding-right"><h3>核对客户背景</h3><div class="binding-preview"><small>国际站买家 · 今天14:05</small><p>We need 500 canvas tote bags with our embroidered logo, delivered to Paris.</p></div><div class="binding-preview"><small>国际站买家 · 今天14:17</small><p>My WhatsApp display name is Olivia Martin. We can continue there about the samples.</p></div><div class="binding-preview"><small>业务补充 · 今天14:20</small><p>样品和运费尚未确认；客户提供地址后再核实。</p></div><div class="import-options"><span class="field-label">自动带入的背景</span><label class="check-row"><input type="checkbox" checked>聊天与会话摘要</label><label class="check-row"><input type="checkbox" checked>采购需求与必要历史依据</label><label class="check-row"><input type="checkbox" checked>业务补充与已提取附件内容</label></div></div></div><footer class="modal-footer"><label class="check-row"><input id="verify-customer" type="checkbox" ${state.verified ? 'checked' : ''}>已核对是同一位客户</label><div class="buttons"><button class="btn" data-action="close">取消</button><button class="btn primary" data-action="bind" ${state.verified ? '' : 'disabled'}>确认绑定</button></div></footer></section></div>`;
}
function context() {
  return `<div class="drawer-scrim" data-dismiss="true"><section class="drawer" role="dialog" aria-modal="true" aria-labelledby="context-title"><header class="drawer-header"><div><span class="eyebrow">CONVERSATION CONTEXT</span><h2 id="context-title">本轮带入的客户背景</h2><p>Olivia Martin ${tag('WhatsApp', 'wa')} ${icon('link')} Olivia M. ${tag('国际站', 'ali')}</p></div><button class="icon" data-action="close" aria-label="关闭上下文">${icon('close')}</button></header><div class="drawer-scroll"><div class="context-group"><div class="group-heading"><strong>当前 WhatsApp 对话</strong><small>今天14:26</small></div><div class="source-entry"><div class="source-meta">${tag('WhatsApp', 'wa')}客户最新消息</div><p>Could you confirm the sample options and the address you need for delivery?</p></div></div><div class="context-group"><div class="group-heading"><strong>国际站聊天与摘要</strong><small>已同步至 今天14:20</small></div><div class="source-entry"><div class="source-meta">${tag('国际站', 'ali')}买家 · 今天14:05</div><p>We need 500 canvas tote bags with our embroidered logo, delivered to Paris.</p></div><div class="source-entry"><div class="source-meta">${tag('国际站', 'ali')}会话摘要</div><p>本次采购帆布袋500个，12oz原色棉帆布，Logo刺绣，收货地巴黎；客户询问样品，未确认费用和具体交期。</p></div></div><div class="context-group"><div class="group-heading"><strong>采购需求</strong><small>帆布袋 · 本次采购</small></div><div class="source-entry"><p>500个 · 12oz棉帆布 · 原色 · Logo刺绣 · 法国巴黎</p></div></div><div class="context-group"><div class="group-heading"><strong>相关业务补充</strong><small>2个来源</small></div><div class="source-entry"><div class="source-meta">${tag('国际站补充', 'ali')}商家填写</div><p>样品和运费尚未确认；客户提供地址后再核实。</p></div><div class="source-entry"><div class="source-meta">${tag('WhatsApp补充', 'wa')}商家填写</div><p>优先确认收货信息；样品费用核实后再答复客户。</p></div></div><p class="context-footnote">仅带入当前绑定客户的适用背景。国际站原文与本渠道新消息分别保留，不把商家补充当成客户确认。</p></div><footer class="drawer-footer"><small>国际站背景来自当前商家已同步记录</small><button class="btn" data-action="binding">调整绑定与带入范围</button></footer></section></div>`;
}
function connection() {
  return `<div class="settings-shell">${rail()}<section class="settings-main"><header class="settings-title"><span class="eyebrow">CHANNEL CONNECTION</span><h1>连接 WhatsApp</h1><p>为当前商家连接一个账号，继续接待已关联的国际站客户。</p></header><div class="settings-grid"><article class="settings-card"><h2>${state.connected ? '账号已连接' : '使用手机扫码连接'}</h2><p>当前商家：Northline · 演示</p>${state.connected ? `<div class="account-success"><div class="empty-symbol">${icon('check')}</div><h3>Northline WhatsApp</h3><p>+86 130 0000 1234<br>消息接收已就绪 · 自动接待未开启</p><button class="btn primary" data-action="home">返回客户接待</button></div>` : `<div class="qr-area"><div class="qr-placeholder">${icon('phone')}<strong>扫码区域</strong><small>设计占位 · 无法扫码</small></div></div><p class="qr-caption">实际连接后，二维码随会话更新</p><div class="steps"><div class="step"><span class="step-number">1</span><p>在手机上打开 WhatsApp。</p></div><div class="step"><span class="step-number">2</span><p>进入“已关联设备”，选择“关联设备”。</p></div><div class="step"><span class="step-number">3</span><p>扫描这里的二维码，确认登录账号。</p></div></div><div class="settings-actions"><button class="btn" data-action="refresh-qr">刷新二维码</button><button class="btn primary" data-action="simulate-connected">预览连接成功</button></div>`}</article><div><article class="settings-card"><h2>连接与接待分开管理</h2><div class="setting-row"><div><strong>消息接收</strong><p>登录后接收本账号的单聊消息。</p></div>${tag(state.connected ? '已就绪' : '等待连接', state.connected ? 'wa' : '')}</div><div class="setting-row"><div><strong>自动接待范围</strong><p>仅当前商家已绑定客户的单聊。</p></div>${tag('已绑定客户')}</div><div class="setting-row"><div><strong>AI 生成</strong><p>使用当前商家的 Accio AI 授权。</p></div>${tag('待检查')}</div><div class="setting-row"><div><strong>WhatsApp 自动接待</strong><p>完成连接与绑定后单独开启。</p></div>${tag('暂停', 'manual')}</div></article><article class="settings-card section-rule"><h2>客户绑定后可以使用</h2><div class="steps"><div class="step"><span class="step-number">${icon('chat')}</span><p>国际站已保存聊天与会话摘要</p></div><div class="step"><span class="step-number">${icon('file')}</span><p>关联采购需求与有依据的条件</p></div><div class="step"><span class="step-number">${icon('book')}</span><p>适用资料、业务补充和附件内容</p></div></div></article></div></div></section></div>`;
}
function render() {
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === state.view));
  document.querySelector('#scenario').value = state.scenario;
  app.innerHTML = header() + (state.view === 'connection' ? connection() : `<div class="shell">${rail()}${customers()}${chat()}${sidebar()}</div>`);
  const draft = document.querySelector('#draft');
  if (draft) draft.value = state.draft;
  overlay.innerHTML = state.view === 'binding' ? binding() : state.view === 'context' ? context() : '';
  if (overlay.firstElementChild) requestAnimationFrame(() => overlay.querySelector('button')?.focus({ preventScroll: true }));
}
let toastTimer;
function toast(text) {
  const node = document.querySelector('#toast'); node.textContent = text; node.classList.add('toast-visible');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove('toast-visible'), 2600);
}
function closeOverlay() { state.view = 'workbench'; state.verified = false; render(); }
document.addEventListener('click', event => {
  const view = event.target.closest('[data-view]');
  if (view) { state.view = view.dataset.view; state.verified = false; render(); return; }
  const channel = event.target.closest('[data-channel]');
  if (channel) { state.channel = channel.dataset.channel; render(); return; }
  const filter = event.target.closest('[data-filter]');
  if (filter) { state.filter = filter.dataset.filter; render(); return; }
  const customer = event.target.closest('[data-customer]');
  if (customer) { if (customer.dataset.customer !== 'olivia') { toast('当前设计以 Olivia 为例，其他客户仅展示列表样式'); return; } state.selected = 'olivia'; render(); return; }
  const candidate = event.target.closest('[data-candidate]');
  if (candidate) {
    if (candidate.dataset.candidate !== 'olivia') { toast('示例候选仅供核对，当前设计演示 Olivia 的绑定流程'); return; }
    overlay.querySelectorAll('.candidate').forEach(row => row.classList.toggle('active', row === candidate)); return;
  }
  if (event.target.dataset.dismiss === 'true') { closeOverlay(); return; }
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (!action) return;
  if (['binding', 'context', 'connection'].includes(action)) { state.view = action; state.verified = false; render(); return; }
  if (action === 'close') { closeOverlay(); return; }
  if (action === 'home') { state.view = 'workbench'; render(); return; }
  if (action === 'auto') { state.auto = !state.auto; render(); toast(state.auto ? '设计预览：已恢复此 WhatsApp 客户的自动接待' : '设计预览：已暂停此 WhatsApp 客户'); return; }
  if (action === 'adopt') { state.draft = suggestionBody; state.suggestion = false; state.auto = false; render(); document.querySelector('#draft')?.focus(); return; }
  if (action === 'discard') { state.suggestion = false; render(); return; }
  if (action === 'generate') { state.suggestion = true; render(); toast('设计预览：使用关联背景生成建议，不发送消息'); return; }
  if (action === 'send') { toast('设计预览：不会发送真实 WhatsApp 或国际站消息'); return; }
  if (action === 'bind') { if (!state.verified) return; state.bound = true; state.scenario = 'normal'; state.selected = 'olivia'; state.auto = false; state.suggestion = false; state.view = 'workbench'; state.verified = false; render(); toast('设计预览：绑定已保存，自动接待保持暂停'); return; }
  if (action === 'ali-history') { state.channel = 'ali'; render(); toast('设计预览：切换到关联客户的国际站会话'); return; }
  if (action === 'simulate-connected') { state.connected = true; state.auto = false; render(); return; }
  if (action === 'refresh-qr') { toast('设计占位：不请求真实二维码'); return; }
  if (action === 'reconcile') { toast('设计预览：核查原操作，不重发消息'); return; }
  if (action === 'help') { toast('设计预览：沿用现有图片、PDF、文本补充附件'); return; }
  if (action === 'attachment') { toast('设计预览：附件仅属于当前客户和渠道'); return; }
  if (action === 'refresh') { toast('设计预览：更新当前渠道会话列表'); return; }
  toast('此处沿用现有工作台功能，设计稿聚焦 WhatsApp 接待');
});
document.addEventListener('change', event => {
  if (event.target.id === 'verify-customer') { state.verified = event.target.checked; const confirm = overlay.querySelector('[data-action="bind"]'); if (confirm) confirm.disabled = !state.verified; }
  if (event.target.id === 'scenario') { state.scenario = event.target.value; state.bound = state.scenario !== 'unbound'; state.auto = state.scenario === 'normal'; state.suggestion = state.scenario === 'normal'; state.view = 'workbench'; state.channel = 'wa'; state.selected = 'olivia'; render(); }
});
document.addEventListener('input', event => { if (event.target.id === 'draft') state.draft = event.target.value; });
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && ['binding', 'context'].includes(state.view)) { closeOverlay(); return; }
  if (event.key === 'Tab' && overlay.firstElementChild) {
    const focusable = [...overlay.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)')];
    if (!focusable.length) return;
    if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
    else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
  }
});
render();
