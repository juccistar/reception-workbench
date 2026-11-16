(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const paths = {
    message:'<path d="M20 11.5a8 8 0 0 1-8.5 8H4l1.3-4.1A8 8 0 1 1 20 11.5Z"/><path d="M8 10h8M8 14h5"/>',
    grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    home:'<path d="m3 10 9-7 9 7v11h-7v-7h-4v7H3V10Z"/>',
    box:'<path d="m12 3 9 5-9 5-9-5 9-5ZM3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/>',
    people:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/>',
    book:'<path d="M12 5C9 3 6 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15"/>',
    gear:'<path d="m9 3-.6 2.6-2 1.2-2.6-.7-2 3.5L3.7 12l-1.9 2.4 2 3.5 2.6-.7 2 1.2L9 21h4l.6-2.6 2-1.2 2.6.7 2-3.5-1.9-2.4 1.9-2.4-2-3.5-2.6.7-2-1.2L13 3Z"/><circle cx="11" cy="12" r="3"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    down:'<path d="m6 9 6 6 6-6"/>',right:'<path d="m9 5 7 7-7 7"/>',left:'<path d="m15 5-7 7 7 7"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',
    panel:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    translate:'<path d="M3 5h11M8 3v2m-3 3c2 4 5 6 8 7M12 5c-1 5-5 9-9 11m10 5 4-11 4 11m-6.5-4h5"/>',
    history:'<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6m3-3v6l4 2"/>',
    globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 6.5h14M5 17.5h14"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',check:'<path d="m5 12 4 4L19 6"/>',
    spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"/>',
    send:'<path d="m21 3-7 18-4-8-8-4 19-6ZM10 13 21 3"/>',clip:'<path d="m8 13 7-7a3 3 0 0 1 4 4L9 20a5 5 0 0 1-7-7L13 2"/>',
    image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 16 6-5 4 4 3-3 5 5"/>',
    plus:'<path d="M12 5v14M5 12h14"/>',file:'<path d="M14 3H5v18h14V8l-5-5Zm0 0v6h5M8 13h8m-8 4h6"/>',
    external:'<path d="M14 3h7v7m0-7L11 13M10 5H4v15h15v-6"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',play:'<path d="m8 4 12 8-12 8V4Z"/>',pause:'<path d="M8 5v14M16 5v14"/>',
    shield:'<path d="M12 3 3 7v5c0 5 9 10 9 10s9-5 9-10V7l-9-4Z"/><path d="m8 12 3 3 5-5"/>',info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    refresh:'<path d="M20 9a8 8 0 0 0-14-4L3 8m0-5v5h5M4 15a8 8 0 0 0 14 4l3-3m0 5v-5h-5"/>',
    pin:'<path d="m16 3 5 5-4 1-4 6v3l-7-7h3l6-4 1-4ZM8 16l-5 5"/>',hand:'<path d="M8 12V5a2 2 0 0 1 4 0v6-8a2 2 0 0 1 4 0v8-5a2 2 0 0 1 4 0v10a7 7 0 0 1-13 3l-4-6a2 2 0 0 1 3-2l2 1Z"/>',
    edit:'<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"/>',truck:'<path d="M2 5h12v13H2V5Zm12 5h4l4 5v3h-8"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="19" r="2"/>',quote:'<path d="M5 3h14v18H5V3Zm3 5h8m-8 4h8m-8 4h5"/>',link:'<path d="m10 14 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-1 2-2a4 4 0 1 1 6 6l-5 5a4 4 0 0 1-6 0"/>',
    whatsapp:'<path d="M20.8 11.6a8.8 8.8 0 0 1-13 7.8L3 21l1.6-4.7a8.8 8.8 0 1 1 16.2-4.7Z"/><path d="m8.5 7.2-1 .7c-.7.7.1 3.1 2.2 5.3 2.2 2.1 4.5 2.8 5.2 2.1l.8-1-2.1-1.3-.9.7a6.1 6.1 0 0 1-2.8-2.8l.7-.8-1.3-2.9Z"/>'
  };
  const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
  const names = {home:'开始',products:'产品库',suppliers:'供应商',procurement:'采购中心',quotes:'待我报价',customers:'客户资料',library:'资料与政策',settings:'连接与设置',activity:'运行记录',launcher:'账号启动台',models:'共享模型',whatsapp:'WhatsApp 客户侧栏'};
  const products = [
    {id:'p1',name:'Luma 随行保温杯',spec:'450 ml / 鼠尾草绿',image:'tumbler.svg',category:'保温杯',number:'1601902417143',customers:3,updated:'今天 10:48'},
    {id:'p2',name:'Loop 运动水壶',spec:'750 ml / 雾蓝',image:'bottle.svg',category:'水壶',number:'1601902417206',customers:2,updated:'今天 10:35'},
    {id:'p3',name:'Fold 折叠收纳箱',spec:'35 × 24 × 18 cm',image:'storage-box.svg',category:'收纳',number:'1601902417312',customers:1,updated:'今天 09:52'},
    {id:'p4',name:'Daily 帆布袋',spec:'12 安帆布 / 本色',image:'tote.svg',category:'布袋',number:'定制-20261116',customers:2,updated:'昨天 16:20'},
    {id:'p5',name:'Luma 大容量随行杯',spec:'600 ml / 鼠尾草绿',image:'tumbler.svg',category:'保温杯',number:'1601902417405',customers:1,updated:'昨天 15:08'},
    {id:'p6',name:'Fold 桌面收纳盒',spec:'24 × 16 × 12 cm',image:'storage-box.svg',category:'收纳',number:'1601902417518',customers:1,updated:'11月14日'}
  ];
  const suppliers = [
    {id:'s1',name:'永康青禾日用品有限公司',platform:'1688',city:'浙江 · 永康',contact:'林经理',note:'保温杯与水壶，可按图定制包装',count:3},
    {id:'s2',name:'武义森野工贸有限公司',platform:'1688',city:'浙江 · 武义',contact:'周经理',note:'主营不锈钢杯具',count:2},
    {id:'s3',name:'义乌简物家居有限公司',platform:'1688',city:'浙江 · 义乌',contact:'陈经理',note:'收纳、家居用品',count:2},
    {id:'s4',name:'温州拾光包装有限公司',platform:'1688',city:'浙江 · 温州',contact:'待补充',note:'布袋、礼盒及配套包装',count:1}
  ];
  const offers = [
    {id:'o1',product:'p1',supplier:'s1',price:'28.50',min:'500 只',lead:'图稿确认后约 25 天',shipping:'未含税运',spec:'450 ml，鼠尾草绿，单色 Logo'},
    {id:'o2',product:'p1',supplier:'s2',price:'30.20',min:'300 只',lead:'图稿确认后约 20 天',shipping:'运费另议',spec:'450 ml，杯身丝印'},
    {id:'o3',product:'p3',supplier:'s3',price:'16.80',min:'200 件',lead:'约 15 天',shipping:'未含税运',spec:'35 × 24 × 18 cm，本色'},
    {id:'o4',product:'p5',supplier:'s1',price:'33.00',min:'500 只',lead:'约 25 天',shipping:'未含税运',spec:'600 ml，鼠尾草绿'}
  ];
  const customers = [
    {id:'alex',name:'Alex Morgan',country:'德国',buyer:'buyer-1028',product:'p1',quantity:'500 只',summary:'采购 500 只随行杯，定制 Logo，发往汉堡；希望先确认样品。',status:'待我报价',date:'今天 10:48'},
    {id:'emma',name:'Emma Wilson',country:'英国',buyer:'buyer-1042',product:'p2',quantity:'数量待确认',summary:'希望了解运动水壶的价格、颜色与包装。',status:'待回复',date:'今天 10:41'},
    {id:'sofia',name:'Sofía Ríos',country:'西班牙',buyer:'buyer-1086',product:'p1',quantity:'300 只',summary:'希望获得 300 只保温杯的报价，定制内容仍需确认。',status:'待我报价',date:'今天 10:18'},
    {id:'oliver',name:'Oliver Reed',country:'加拿大',buyer:'buyer-1135',product:'p4',quantity:'800 件',summary:'需要带品牌图案的帆布袋，希望先了解样品。',status:'待我报价',date:'昨天 16:20'},
    {id:'noah',name:'Noah Bennett',country:'美国',buyer:'buyer-1148',product:null,quantity:'',summary:'初次咨询，等待进一步说明想了解的产品。',status:'等待客户',date:'今天 10:32'},
    {id:'mia',name:'Mia Laurent',country:'法国',buyer:'buyer-1162',product:'p3',quantity:'200 件',summary:'关注收纳箱的尺寸与运输方式。',status:'自动接待中',date:'今天 09:45'}
  ];
  const quoteCases=customers.filter(c=>c.status==='待我报价').map(c=>({...c,customerId:c.id,caseLabel:'本次采购'}));
  const quoteMany=new URLSearchParams(location.search).has('quote-many');
  if(quoteMany)for(let index=1;index<=40;index++){const customer=customers.filter(c=>c.status==='待我报价')[(index-1)%3],p=products[(index-1)%products.length];quoteCases.push({...customer,id:'quote-'+index,customerId:customer.id,product:p.id,quantity:(100+index*20)+(p.category==='保温杯'||p.category==='水壶'?' 只':' 件'),caseLabel:'采购事项 '+String(index+3).padStart(2,'0')});}
  const quoteOwner=()=>quoteCases.find(c=>c.id===state.selectedQuote)?.customerId||'alex';
  const tasks = [
    {id:'t1',product:'p1',stage:'已完成',tone:'green',message:'已保存 2 家供货方案',time:'今天 10:36'},
    {id:'t2',product:'p2',stage:'结果待核查',tone:'amber',message:'原任务已提交，等待确切结果',time:'今天 10:35'},
    {id:'t3',product:'p4',stage:'待找货',tone:'muted',message:'等待采购授权',time:'今天 10:12'},
    {id:'t4',product:'p3',stage:'已完成',tone:'green',message:'已保存 1 家供货方案',time:'今天 09:52'}
  ];
  const documents = [
    {id:'d1',title:'定制与打样说明',category:'商品资料',usage:'客户回复',confirmed:true,enabled:true,version:3,builtin:false,text:'适用产品：Luma 随行保温杯\n\n定制范围\n支持杯身 Logo，报价前确认图稿、印刷尺寸和采购数量。\n\n样品安排\n客户可先确认样品。样品费用及运输费用应结合收货地址另行确认。\n\n交付说明\n大货交期按最终图稿、包装方案和排产结果确认，不直接承诺送达日期。'},
    {id:'d2',title:'包装与交付政策',category:'经营政策',usage:'客户回复',confirmed:true,enabled:true,version:2,builtin:false,text:'包装方式\n提供独立包装，定制包装按客户图稿和数量确认。\n\n运输费用\n收货地址、邮编及运输方式确认后，再核算运费。\n\n交期\n先确认样品和生产安排，再与客户约定交期。'},
    {id:'d3',title:'回复前需要核实的内容',category:'接待规则',usage:'AI 内部依据',confirmed:true,enabled:false,version:1,builtin:false,text:'涉及价格、交期或认证时，使用当前有效的经营资料和商家补充。\n\n没有依据的事项先向客户确认，不自行补充承诺。'},
    {id:'d4',title:'简洁、友好地沟通',category:'聊天风格',usage:'AI 内部依据',confirmed:true,enabled:true,version:1,builtin:false,text:'自然回应客户当前的问题。\n\n每次只问与下一步有关的信息，保持简洁、友好。\n\n不使用夸张的宣传语。'},
    {id:'d5',title:'本周报价备忘',category:'人工备注',usage:'仅人工查看',confirmed:false,enabled:false,version:1,builtin:false,text:'本周先确认样品费用与包装方案，再形成对客报价。\n\n本条仅供人工查看，不提供给 AI。'},
    {id:'d6',title:'经营事实填写模板',category:'经营政策',usage:'仅人工查看',confirmed:false,enabled:false,version:1,builtin:true,text:'请填写本商家已核实的信息。\n\n经营品类：\n可提供的定制范围：\n报价包含的内容：\n需要另行确认的内容：'}
  ];
  const events = [
    {id:'e1',time:'10:48',title:'已更新 Alex Morgan 的采购需求',detail:'500 只随行杯，定制 Logo，目的地为德国汉堡。',type:'任务',tone:'green',state:'已完成'},
    {id:'e2',time:'10:36',title:'Luma 随行杯找货完成',detail:'已将 2 家供货方案保存到产品库。',type:'采购',tone:'green',state:'已完成'},
    {id:'e3',time:'10:35',title:'Loop 运动水壶的找货结果待核查',detail:'保留原任务，取得确切结果后继续处理。',type:'采购',tone:'amber',state:'需关注'},
    {id:'e4',time:'10:20',title:'已保存新的包装与交付政策',detail:'经营资料已更新为第 2 版。',type:'资料',tone:'green',state:'已完成'},
    {id:'e5',time:'09:00',title:'国际站连接已恢复',detail:'身份与读取检查已完成。',type:'连接',tone:'green',state:'已完成'}
  ];
  const receptionRange=window.DesignReceptionRange.read();
  const state = {route:'home',section:'reception',globalEnabled:true,search:{},filter:{products:'all',suppliers:'all',procurement:'all',customers:'all',activity:'all'},defaults:{p1:'o1',p3:'o3',p5:'o4'},selectedQuote:'alex',quoteDrafts:{},quoteListScroll:0,quoteDetailScroll:{},doc:'d1',docCategory:'全部资料',directory:'mine',docMode:'edit',docDrafts:{},procurementGranted:false,settingsDirty:false,settings:{scope:receptionRange.scope,translate:true,language:'中文',photos:true,feishu:false},scopeCustomers:receptionRange.customerIds,scopeQuery:'',editingSupplier:null};
  let toastTimer, sheetReturnFocus, modalReturnFocus;
  const byId = (list,id) => list.find(row => row.id === id);
  const product = id => byId(products,id);
  const supplier = id => byId(suppliers,id);
  const badge = (text,tone='muted') => `<span class="s-badge ${tone}">${esc(text)}</span>`;
  const btn = (text,action,primary=false,attrs='') => `<button class="s-btn${primary?' primary':''}" data-action="core:${action}" ${attrs}>${text}</button>`;
  const link = (text,route) => `<button class="s-link" data-nav="${esc(route)}">${text}${icon('right')}</button>`;
  const field = (label,control,hint='') => `<label class="s-field"><span>${label}</span>${control}${hint?`<small>${hint}</small>`:''}</label>`;
  const image = p => `<img src="assets/${p.image}" alt="${esc(p.name)}，合成商品图">`;
  function heading(title,description,actions='') { return `<header class="s-heading"><div><h1>${title}</h1><p>${description}</p></div>${actions?`<div class="s-heading-actions">${actions}</div>`:''}</header>`; }
  function tabs(group,items) { return `<div class="s-tabs" role="group" aria-label="${esc(names[group]||group)}筛选">${items.map(([id,text])=>`<button data-action="core:filter" data-group="${group}" data-value="${id}" aria-pressed="${state.filter[group]===id}">${text}</button>`).join('')}</div>`; }
  function search(group,placeholder) { return `<label class="s-search">${icon('search')}<input id="search-${group}" data-search="${group}" type="search" value="${esc(state.search[group]||'')}" placeholder="${placeholder}" aria-label="${placeholder}"></label>`; }
  function page(content) { return `<div class="s-page">${content}</div>`; }
  function toast(text) { clearTimeout(toastTimer); $('#suite-toast').textContent=text; $('#suite-toast').hidden=false; toastTimer=setTimeout(()=>$('#suite-toast').hidden=true,3200); }
  function navigate(route) { if (route==='reception') { location.href='index.html'; return; } closeSheet(); closeModal(); if(location.hash==='#'+route) render(); else location.hash=route; }
  function openSheet({title,body,footer=''}) { sheetReturnFocus=document.activeElement; $('#suite-sheet-title').textContent=title; $('#suite-sheet-body').innerHTML=body; $('#suite-sheet-footer').innerHTML=footer; $('#suite-sheet-overlay').hidden=false; $('#suite-app').inert=true; $('#suite-sheet').focus(); }
  function closeSheet() { if($('#suite-sheet-overlay').hidden)return; $('#suite-sheet-overlay').hidden=true; $('#suite-app').inert=false; if(sheetReturnFocus?.isConnected)sheetReturnFocus.focus(); }
  function openModal({title,body,buttons=[]}) { modalReturnFocus=document.activeElement; $('#suite-modal-title').textContent=title; $('#suite-modal-body').innerHTML=body; $('#suite-modal-actions').innerHTML=(buttons.length?buttons:[{label:'关闭',action:'core:close-modal'}]).map(b=>`<button class="s-btn${b.primary?' primary':''}" data-action="${b.action}">${esc(b.label)}</button>`).join(''); if(!$('#suite-modal').open)$('#suite-modal').showModal(); }
  function closeModal() { if($('#suite-modal').open)$('#suite-modal').close(); }
  const api = {icon,esc,navigate,toast,refresh:()=>render(),openSheet,closeSheet,openModal,closeModal};
  function renderHome() {
    return page(heading('开始','11 月 16 日，周一。继续处理需要你参与的客户。',btn(`${icon('message')}进入接待`,'open-reception',true))+`<div class="s-home-grid"><section class="s-panel"><header class="s-panel-title"><h2>等你处理 <small>3 位客户</small></h2>${link('查看全部','quotes')}</header>${[
      ['quote','Alex Morgan 需要确认样品与报价','Luma 随行杯，500 只，发往德国汉堡。','处理报价','quotes'],
      ['message','Emma Wilson 正在等待回复','希望了解运动水壶的价格与可选颜色。','继续沟通','reception'],
      ['box','Oliver Reed 询问定制包装','帆布袋，800 件，需要先确认印刷图稿。','查看需求','quotes']
    ].map(([i,title,desc,label,route])=>`<div class="s-task-row"><span class="s-task-icon">${icon(i)}</span><div><h3>${title}</h3><p>${desc}</p></div>${link(label,route)}</div>`).join('')}</section><section class="s-panel"><header class="s-panel-title"><h2>工作区状态</h2>${icon('shield')}</header><div class="s-panel-body"><div class="s-state-row"><span>国际站连接</span>${badge('已连接','green')}</div><div class="s-state-row"><span>接待与翻译模型</span>${link('已配置','models')}</div><div class="s-state-row"><span>自动找货</span><strong>等待本次授权</strong></div><div class="s-state-row"><span>资料与政策</span><strong>3 份已启用</strong></div><div class="s-divider"></div><p class="s-small s-muted">有 1 项找货结果待核查。</p><div style="margin-top:14px">${link('前往采购中心','procurement')}</div></div></section></div><div class="s-home-bottom"><section class="s-panel"><header class="s-panel-title"><h2>最近沟通</h2>${link('客户资料','customers')}</header>${customers.slice(0,3).map(c=>`<div class="s-recent-row"><div><strong>${c.name}</strong><small>${c.country}　${c.date}</small></div>${link('打开聊天','reception')}</div>`).join('')}</section><section class="s-panel"><header class="s-panel-title"><h2>常用资料</h2>${link('管理资料','library')}</header>${documents.slice(0,2).map(d=>`<button class="s-reading-link" style="width:100%;text-align:left" data-action="core:open-doc" data-id="${d.id}">${icon('book')}<span>${d.title}<small>第 ${d.version} 版，已启用</small></span></button>`).join('')}</section></div>`);
  }
  function productRows() {
    const q=(state.search.products||'').toLowerCase();
    const rows=products.filter(p=>(p.name+p.number+p.spec).toLowerCase().includes(q)&&(state.filter.products==='all'||(state.filter.products==='offers'?offers.some(o=>o.product===p.id):!offers.some(o=>o.product===p.id))));
    return rows.length?`<section class="s-panel"><div class="s-table-scroll"><table class="s-table s-products-table"><thead><tr><th>商品</th><th>规格</th><th>供货方案</th><th>关联客户</th><th>最近更新</th><th>操作</th></tr></thead><tbody>${rows.map(p=>{const count=offers.filter(o=>o.product===p.id).length;return `<tr class="s-product-row" data-id="${p.id}"><td><div class="s-table-product">${image(p)}<div><button class="s-link s-primary-cell s-product-open" data-action="core:product" data-id="${p.id}">${p.name}</button><small>${p.category}　${p.number}</small><small class="s-product-mobile-spec">${p.spec}</small></div></div></td><td class="s-product-spec">${p.spec}</td><td class="s-product-offers">${count?badge(count+' 家供货方案','blue'):badge('待找货')}</td><td class="s-product-customers">${p.customers} 位客户</td><td class="s-product-updated">${p.updated}</td><td class="s-product-operation"><button class="s-link" data-action="core:product" data-id="${p.id}">查看详情 ${icon('right')}</button></td></tr>`;}).join('')}</tbody></table></div><footer class="s-table-foot"><span>按最近更新排列</span><span>${rows.length} 件商品</span></footer></section>`:`<div class="s-panel s-empty">${icon('search')}没有找到商品</div>`;
  }
  function renderProducts() { return page(heading('产品库',`从客户需求沉淀产品，集中管理供货方案。` ,btn(`${icon('refresh')}刷新`,'refresh'))+`<div class="s-toolbar">${tabs('products',[['all','全部商品 <small>6</small>'],['offers','已有供货方案'],['pending','待找货']])}${search('products','搜索商品名称或编号')}</div>${productRows()}<p class="s-suite-footer">供货方案中的价格为进价，对客报价由商家确认。</p>`); }
  function showProduct(id) {
    const p=product(id);if(!p)return;
    const po=offers.filter(o=>o.product===id);
    openSheet({title:'商品与供货方案',body:`<div class="s-product-summary">${image(p)}<div>${badge(p.category,'blue')}<h3 style="margin-top:10px">${p.name}</h3><p>${p.spec}<br>${p.number}</p></div></div><section class="s-detail-section"><h3>供货方案 <span class="s-muted">${po.length}</span></h3>${po.length?po.map(o=>`<div class="s-offer ${state.defaults[id]===o.id?'default':''}"><div class="s-offer-top"><div><h4>${esc(supplier(o.supplier).name)}</h4><p>${o.spec}<br>起订量 ${o.min}</p></div><div class="s-offer-cost">¥${o.price}<small>参考进价 / 件</small></div></div><p>交期：${o.lead}　${o.shipping}</p><div class="s-offer-bottom">${state.defaults[id]===o.id?badge('默认方案','blue'):'<span class="s-small s-muted">商家保存的供货信息</span>'}${btn(state.defaults[id]===o.id?'清除默认':'设为默认','default-offer',false,`class="s-btn compact" data-product="${id}" data-id="${o.id}"`)}</div></div>`).join(''):'<div class="s-note">尚无供货方案，可先在采购中心查看找货任务。</div>'}</section><div class="s-note" style="margin-top:18px">默认方案仅用于新关联的采购事项。已有客户的方案保持各自选择，进价不会直接填作对客报价。</div><section class="s-detail-section"><h3>关联客户</h3>${customers.filter(c=>c.product===id).map(c=>`<div class="s-state-row"><span>${c.name}　${c.quantity}</span>${link('查看会话','reception')}</div>`).join('')||'<p class="s-small s-muted">暂无关联客户</p>'}</section>`,footer:btn('关闭','close-sheet')+btn('查看找货任务','go-procurement',true)});
  }
  function renderSuppliers() {
    const q=(state.search.suppliers||'').toLowerCase(),rows=suppliers.filter(s=>(s.name+s.platform+s.city).toLowerCase().includes(q));
    return page(heading('供应商','保留供货来源与联系信息，按商品查看对应方案。',btn(`${icon('plus')}新增供应商`,'new-supplier',true))+`<div class="s-toolbar"><span class="s-small s-muted">已保存 ${suppliers.length} 家供应商</span>${search('suppliers','搜索供应商或平台')}</div><section class="s-panel"><div class="s-table-scroll"><table class="s-table"><thead><tr><th>供应商</th><th>平台 / 地区</th><th>关联商品</th><th>联系人</th><th>操作</th></tr></thead><tbody>${rows.map(s=>`<tr><td><button class="s-link s-primary-cell" data-action="core:supplier" data-id="${s.id}">${esc(s.name)}</button><small>${esc(s.note)}</small></td><td>${esc(s.platform)}<small>${esc(s.city)}</small></td><td>${s.count} 件商品</td><td>${esc(s.contact)}</td><td><button class="s-link" data-action="core:supplier" data-id="${s.id}">查看详情 ${icon('right')}</button></td></tr>`).join('')||'<tr><td colspan="5">没有找到供应商</td></tr>'}</tbody></table></div><footer class="s-table-foot"><span>供应商档案与具体供货方案分别保存</span><span>${rows.length} 家</span></footer></section><div class="s-note" style="margin-top:22px">供货规格、进价和交期在产品库的具体方案中管理。</div>`);
  }
  function showSupplier(id) {
    const s=supplier(id);if(!s)return;state.editingSupplier=id;
    const os=offers.filter(o=>o.supplier===id);
    openSheet({title:'供应商详情',body:`<h3 style="font-size:20px;line-height:1.7">${esc(s.name)}</h3><div class="s-inline" style="margin:13px 0 23px">${badge(s.platform,'blue')}<span class="s-small s-muted">${esc(s.city)}</span></div><dl class="s-detail-facts"><dt>联系人</dt><dd>${esc(s.contact)}</dd><dt>备注</dt><dd>${esc(s.note)}</dd></dl><section class="s-detail-section"><h3>已保存的供货方案</h3>${os.map(o=>`<div class="s-offer"><div class="s-offer-top"><div><h4>${product(o.product).name}</h4><p>${o.spec}</p></div><div class="s-offer-cost">¥${o.price}<small>参考进价 / 件</small></div></div><div style="margin-top:13px"><button class="s-link" data-action="core:product" data-id="${o.product}">管理供货方案 ${icon('right')}</button></div></div>`).join('')||'<div class="s-note">还没有保存供货方案。</div>'}</section>`,footer:btn('关闭','close-sheet')+btn('编辑资料','edit-supplier',true,`data-id="${id}"`)});
  }
  function supplierForm(id) {
    const s=supplier(id)||{name:'',platform:'1688',city:'',contact:'',note:''};state.editingSupplier=id||null;
    openSheet({title:id?'编辑供应商':'新增供应商',body:`<form id="supplier-form">${field('供应商名称',`<input class="s-input" id="supplier-name" required value="${esc(s.name)}" placeholder="填写供应商全称">`)}<div class="s-form-grid">${field('平台',`<input class="s-input" id="supplier-platform" value="${esc(s.platform)}">`)}${field('地区',`<input class="s-input" id="supplier-city" value="${esc(s.city)}">`)}</div>${field('联系人',`<input class="s-input" id="supplier-contact" value="${esc(s.contact)}">`)}${field('备注',`<textarea class="s-input" id="supplier-note">${esc(s.note)}</textarea>`)}</form>`,footer:btn('取消','close-sheet')+btn('保存','save-supplier',true)});
  }
  function renderProcurement() {
    const filter=state.filter.procurement,rows=tasks.filter(t=>filter==='all'||(filter==='unknown'?t.stage==='结果待核查':filter==='done'?t.stage==='已完成':t.stage==='待找货'));
    return page(heading('采购中心','按产品查找供货方案，进度与结果集中保存在这里。',btn(`${icon('gear')}找货设置`,'sourcing-settings'))+`<div class="s-toolbar">${tabs('procurement',[['all','全部任务 <small>4</small>'],['pending','待找货'],['unknown','结果待核查 <small>1</small>'],['done','已完成']])}<span class="s-small s-muted">${state.procurementGranted?'本次找货已授权':'自动找货已暂停'}</span></div><section class="s-panel"><div class="s-table-scroll"><table class="s-table"><thead><tr><th>产品</th><th>任务状态</th><th>当前进度</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${rows.map(t=>{const p=product(t.product);return `<tr><td><div class="s-table-product">${image(p)}<div class="s-primary-cell">${p.name}<small>${p.spec}</small></div></div></td><td>${badge(t.stage,t.tone)}</td><td>${t.message}</td><td>${t.time}</td><td><button class="s-link" data-action="core:task" data-id="${t.id}">${t.stage==='结果待核查'?'核查结果':'查看任务'} ${icon('right')}</button></td></tr>`;}).join('')}</tbody></table></div></section><div class="s-note" style="margin-top:22px">定向询价与报价回收尚未开放。当前可保存、整理并选择供货方案。</div>`);
  }
  function showTask(id) {
    const t=byId(tasks,id),p=product(t.product),unknown=t.stage==='结果待核查';
    openSheet({title:unknown?'核查原找货任务':'找货任务',body:`<div class="s-product-summary">${image(p)}<div>${badge(t.stage,t.tone)}<h3 style="margin-top:10px">${p.name}</h3><p>${t.time}</p></div></div><section class="s-detail-section"><h3>当前进度</h3><div class="s-note ${unknown?'amber':''}">${t.message}${unknown?'。结果确认前，保留原任务，不重复找货。':''}</div></section>${unknown?'<section class="s-detail-section"><h3>原任务</h3><dl class="s-detail-facts"><dt>任务编号</dt><dd>design-sourcing-1026</dd><dt>产品编号</dt><dd>'+p.number+'</dd><dt>结果</dt><dd>等待确切回执</dd></dl></section>':`<section class="s-detail-section"><h3>已保存结果</h3>${offers.filter(o=>o.product===p.id).map(o=>`<div class="s-state-row"><span>${supplier(o.supplier).name}</span><strong>进价 ¥${o.price}</strong></div>`).join('')||'<p class="s-small s-muted">授权后开始查找。</p>'}</section>`}`,footer:btn('关闭','close-sheet')+btn(unknown?'核查结果':t.stage==='待找货'?'设置找货':'查看商品',unknown?'verify-task':t.stage==='待找货'?'sourcing-settings':'task-product',true,`data-id="${id}" data-product="${p.id}"`)});
  }
  function sourcingSettings() { openSheet({title:'自动找货',body:`<p class="s-small s-muted" style="margin-bottom:25px">采购授权独立于自动接待，每次启动后需重新确认。</p><div class="s-setting-line"><div><strong>自动找货</strong><p>为新关联的产品查找供应商</p></div><button class="s-toggle" role="switch" aria-checked="${state.procurementGranted}" data-action="core:toggle-sourcing"></button></div>${field('每件产品最多查找', '<select class="s-select" id="supplier-limit"><option>3 家</option><option>2 家</option><option>1 家</option></select>')}<div class="s-setting-line"><div><strong>定向询价与回复回收</strong><p>尚未开放</p></div>${badge('暂不可用')}</div><div class="s-note amber" style="margin-top:24px">保存设置会暂停找货，需要重新确认本次授权。</div>`,footer:btn('取消','close-sheet')+btn('保存设置','save-sourcing')+btn('本次授权','grant-sourcing',true)}); }
  function renderQuotes() {
    const rows=quoteCases,c=byId(quoteCases,state.selectedQuote)||rows[0],p=product(c.product);
    return page(heading('待我报价',`有 ${rows.length} 项采购需求等待商家报价，先确认产品与交付条件。`)+`<div class="s-quote-layout"><section class="s-panel s-quote-list">${rows.map(row=>`<button class="s-quote-customer ${c.id===row.id?'active':''}" data-action="core:quote" data-id="${row.id}"><strong>${row.name}</strong><small class="s-quote-case-label">${row.caseLabel}</small><p>${product(row.product).name}<br>${row.quantity}　${row.country}</p><div class="s-spread">${badge('待报价','amber')}<time>${row.date}</time></div></button>`).join('')}</section><section class="s-panel s-quote-body" data-quote-id="${c.id}" tabindex="0" aria-label="当前报价事项详情"><header class="s-quote-identity"><div><h2>${c.name}</h2><p>${c.country}　国际站客户</p></div>${link('打开聊天','reception')}</header><div class="s-quote-product">${image(p)}<div><h3>${p.name}</h3><p>${p.spec}</p></div></div><div class="s-quote-facts"><div><span>采购数量</span><strong>${c.quantity}</strong></div><div><span>定制要求</span><strong>${c.customerId==='alex'?'杯身 Logo，尺寸待确认':c.customerId==='oliver'?'按客户图稿印刷':'等待客户进一步说明'}</strong></div><div><span>收货地</span><strong>${c.country}${c.customerId==='alex'?' · 汉堡':''}</strong></div><div><span>交期</span><strong>${c.customerId==='alex'?'12 月 15 日前送达':'尚未提供'}</strong></div></div><div class="s-spread" style="padding:14px 0;border-top:1px solid #e9edf4;border-bottom:1px solid #e9edf4"><span class="s-small s-muted">供货参考</span><button class="s-link" data-action="core:product" data-id="${p.id}">${offers.some(o=>o.product===p.id)?'查看供货进价与条件':'查看产品找货进度'} ${icon('right')}</button></div><section class="s-detail-section"><h3>给接待的业务补充</h3><label><textarea class="s-input" style="min-height:120px" id="quote-supplement" aria-label="报价业务补充" placeholder="填写已核实的对客报价、包装、交期等信息…">${esc(state.quoteDrafts[c.customerId]||'')}</textarea></label><p class="s-small s-muted" style="margin-top:10px">保存到当前客户的业务补充，由接待结合聊天使用。</p></section><div class="s-spread" style="margin-top:22px"><span class="s-small s-muted">交接登记：尚未登记</span>${btn('保存补充','save-quote',true)}</div></section></div>`);
  }
  function renderCustomers() {
    const q=(state.search.customers||'').toLowerCase(),rows=customers.filter(c=>(c.name+c.country+c.buyer).toLowerCase().includes(q));
    return page(heading('客户资料','从已同步的会话查找客户，查看摘要与历次采购需求。')+`<div class="s-toolbar"><span class="s-small s-muted">国际站已同步客户</span>${search('customers','姓名、地区或买家标识')}</div><section class="s-panel"><div class="s-table-scroll"><table class="s-table"><thead><tr><th>客户</th><th>国家 / 地区</th><th>最近采购需求</th><th>当前状态</th><th>最近沟通</th><th>操作</th></tr></thead><tbody>${rows.map(c=>`<tr><td class="s-primary-cell">${c.name}<small>国际站</small></td><td>${c.country}</td><td>${c.product?product(c.product).name:'暂无采购需求'}<small>${c.quantity}</small></td><td>${badge(c.status,c.status==='自动接待中'?'green':c.status==='等待客户'?'muted':'amber')}</td><td>${c.date}</td><td><button class="s-link" data-action="core:customer" data-id="${c.id}">查看资料 ${icon('right')}</button></td></tr>`).join('')||'<tr><td colspan="6">没有找到客户</td></tr>'}</tbody></table></div><footer class="s-table-foot"><span>客户姓名与标识来自平台会话</span><span>${rows.length} 位客户</span></footer></section>`);
  }
  function showCustomer(id) { const c=byId(customers,id);openSheet({title:'客户资料',body:`<h3 style="font-size:22px;margin-bottom:10px">${c.name}</h3><p class="s-small s-muted">${c.country}　国际站</p><section class="s-detail-section"><h3>客户摘要</h3><p style="font-size:13px;line-height:1.9;color:#7b8ca7">${c.summary}</p></section><section class="s-detail-section"><h3>采购需求</h3>${c.product?`<div class="s-product-summary">${image(product(c.product))}<div><h3>${product(c.product).name}</h3><p>${c.quantity}<br>${c.date}</p></div></div>`:'<div class="s-note">当前还没有采购需求，可以直接继续聊天。</div>'}</section><details class="s-advanced"><summary>来源信息 ${icon('down')}</summary><dl class="s-detail-facts"><dt>买家标识</dt><dd>${c.buyer}</dd><dt>平台</dt><dd>国际站</dd><dt>资料来源</dt><dd>已保存的当前商家会话</dd></dl></details>`,footer:btn('关闭','close-sheet')+btn('打开聊天','open-reception',true)}); }
  const docDraft = () => { const d=byId(documents,state.doc)||documents[0];return state.docDrafts[d.id] ||= {...d,dirty:false}; };
  function renderLibrary() {
    const cats=['全部资料','商品资料','经营政策','接待规则','聊天风格','人工备注'];
    const rows=documents.filter(d=>(state.directory==='builtin'?d.builtin:!d.builtin)&&(state.docCategory==='全部资料'||d.category===state.docCategory));
    if(!rows.some(d=>d.id===state.doc)&&rows.length)state.doc=rows[0].id;
    const d=rows.length?docDraft():null;
    return page(heading('资料与政策','经营事实、接待规则与人工备注，各自明确用途。',btn(`${icon('plus')}新建资料`,'new-doc',true))+`<div class="s-library"><nav class="s-library-categories" aria-label="资料分类"><h2>按内容查找</h2>${cats.map((c,i)=>`<button class="s-category ${state.docCategory===c?'active':''}" data-action="core:doc-category" data-value="${c}">${icon(['book','box','file','shield','message','pin'][i])}${c}</button>`).join('')}<div class="s-divider"></div><p class="s-small s-muted" style="padding:0 12px">仅人工备注不会提供给 AI。</p></nav><aside class="s-document-list"><div class="s-document-directory"><button data-action="core:doc-directory" data-value="mine" class="${state.directory==='mine'?'active':''}">我的资料</button><button data-action="core:doc-directory" data-value="builtin" class="${state.directory==='builtin'?'active':''}">内置内容</button></div>${rows.map(row=>`<button class="s-document-row ${row.id===state.doc?'active':''}" data-action="core:doc" data-id="${row.id}"><strong>${esc(state.docDrafts[row.id]?.title ?? row.title)}</strong><small>${row.category}　第 ${row.version} 版</small>${badge(row.enabled?'已启用':'未启用',row.enabled?'green':'muted')}</button>`).join('')||'<div class="s-empty">此分类暂无资料</div>'}</aside><section class="s-document-editor">${d?`<header class="s-document-toolbar"><div class="s-tabs"><button data-action="core:doc-mode" data-value="edit" class="${state.docMode==='edit'?'active':''}">编辑</button><button data-action="core:doc-mode" data-value="preview" class="${state.docMode==='preview'?'active':''}">预览</button></div><div class="s-inline"><span class="s-small s-muted" id="doc-save-state">${d.dirty?'有未保存修改':'已保存'}</span>${btn(d.builtin?'采用到我的资料':'保存资料',d.builtin?'adopt-doc':'save-doc',true)}</div></header><div class="s-document-paper"><input class="s-doc-title" id="doc-title" aria-label="资料标题" value="${esc(d.title)}" ${d.builtin?'readonly':''}><div class="s-doc-meta"><span>${d.category}</span><span>第 ${d.version} 版</span><span>${d.confirmed?'人工已核实':'尚未核实'}</span></div>${state.docMode==='edit'?`<textarea class="s-doc-text" id="doc-text" aria-label="资料正文" ${d.builtin?'readonly':''}>${esc(d.text)}</textarea>`:`<div class="s-doc-preview">${esc(d.text)}</div>`}<div class="s-divider"></div><label class="s-checkbox"><input type="checkbox" id="doc-confirmed" ${d.confirmed?'checked':''} ${d.builtin?'disabled':''}>我已核实本版内容可用于所选用途</label></div><footer class="s-doc-options"><label>用途 <select id="doc-usage" aria-label="资料用途" ${d.builtin?'disabled':''}>${['仅人工查看','AI 内部依据','客户回复'].map(v=>`<option ${d.usage===v?'selected':''}>${v}</option>`).join('')}</select></label><span class="s-inline">接待中使用<button class="s-toggle" role="switch" aria-label="接待中使用当前资料" aria-checked="${d.enabled}" data-action="core:enable-doc" ${d.builtin||d.usage==='仅人工查看'?'disabled':''}></button></span></footer>`:'<div class="s-empty">选择资料，或新建一份</div>'}</section></div>`);
  }
  const settingsSections=[['reception','自动接待','spark'],['translation','聊天翻译','translate'],['media','图片与上下文','image'],['connection','平台连接','link'],['storage','数据与备份','box'],['feishu','飞书登记','file'],['display','界面布局','panel'],['about','版本与更新','info']];
  function settingLine(title,description,control) { return `<div class="s-setting-line"><div><strong>${title}</strong>${description?`<p>${description}</p>`:''}</div>${control}</div>`; }
  function switchControl(key,value,label) { return `<button class="s-toggle" role="switch" aria-checked="${value}" aria-label="${label}" data-action="core:setting-switch" data-key="${key}"></button>`; }
  function renderAutoCustomers() {
    if(state.settings.scope!=='selected')return '';
    const query=state.scopeQuery.trim().toLowerCase(),rows=customers.filter(c=>(c.name+' '+c.country+' '+c.buyer).toLowerCase().includes(query));
    return `<section class="s-auto-customers" aria-label="选择自动接待客户"><header><strong>选择客户</strong><span>已选 ${state.scopeCustomers.length} 位</span></header><label class="s-search">${icon('search')}<input id="auto-customer-search" type="search" value="${esc(state.scopeQuery)}" aria-label="搜索自动接待客户" placeholder="搜索姓名或买家标识"></label><div class="s-auto-customer-list">${rows.map(c=>`<label class="s-auto-customer"><input type="checkbox" data-auto-customer="${c.id}" ${state.scopeCustomers.includes(c.id)?'checked':''}><span><strong>${c.name}</strong><small>${c.country}　${c.buyer}</small></span>${c.id==='alex'?badge('人工接待','amber'):''}</label>`).join('')||'<p class="s-small s-muted">没有找到客户</p>'}</div><p class="s-small s-muted">人工接待中的客户即使被选中，也继续由你处理。</p></section>`;
  }
  function renderSettings() {
    let body='';const section=state.section;
    if(section==='reception') body=`<section class="s-settings-section"><h2>自动接待</h2><p>在顶部开启或关闭自动接待，人工接待中的客户继续由你处理。</p>${settingLine('当前接待状态',state.globalEnabled?'按下面选择的范围自动接待。':'点击顶部按钮即可开启。',badge(state.globalEnabled?'已开启':'已关闭',state.globalEnabled?'green':'muted'))}${settingLine('接待模型','接待与翻译模型在启动台统一配置。',link('共享模型设置','models'))}</section><section class="s-settings-section"><h2>自动接待范围</h2><p>选择自动接待覆盖的客户；当前聊天仍从左侧客户列表打开。</p>${field('自动接待范围',`<select class="s-select" id="setting-auto-scope"><option value="all" ${state.settings.scope==='all'?'selected':''}>全部客户</option><option value="selected" ${state.settings.scope==='selected'?'selected':''}>选中客户</option></select>`)}${renderAutoCustomers()}<div class="s-note">按所选范围接待，人工接待中的客户除外。</div><details class="s-advanced"><summary>高级处理设置 ${icon('down')}</summary><div class="s-form-grid">${field('活动检测间隔','<input class="s-input" type="number" value="2" min="1" aria-label="活动检测间隔">')}${field('消息合并窗口','<input class="s-input" type="number" value="1" min="0" aria-label="消息合并窗口">')}</div><p class="s-small s-muted">检测频率以秒为单位。已有明确配置在正式迁移时保留。</p></details></section>`;
    else if(section==='translation') body=`<section class="s-settings-section"><h2>聊天翻译</h2><p>译文供阅读参考，原始聊天和发送正文保持各自内容。</p>${settingLine('显示中文译文','聊天原文下方显示译文。',switchControl('translate',state.settings.translate,'显示中文译文'))}${settingLine('翻译模型','与接待模型分别选择。',link('共享模型设置','models'))}${field('目标语言','<select class="s-select"><option>中文</option><option>英语</option></select>')}<div class="s-note">翻译仅用于聊天及有效人工建议，不翻译采购事项或自动内部草稿。</div></section>`;
    else if(section==='media') body=`<section class="s-settings-section"><h2>图片与上下文</h2><p>为当前需求准备相关图片，和文字一起提供给接待。</p>${settingLine('相关原图','只使用当前任务适用的受控图片。',switchControl('photos',state.settings.photos,'使用相关原图'))}<div class="s-form-grid">${field('图片准备时间','<input class="s-input" type="number" value="60" min="1">','秒')}${field('整批图片预算','<input class="s-input" type="number" value="64" min="1">','MiB')}${field('图片准备并行数','<select class="s-select"><option>2</option><option>1</option></select>')}${field('近期文字消息','<input class="s-input" type="number" value="50" min="1">','条')}</div><div class="s-note">商品展示图仅用于浏览与找货，不替代客户原图。</div></section>`;
    else if(section==='connection') body=`<section class="s-settings-section"><h2>平台连接</h2><p>当前商家身份与读取状态。</p>${settingLine('澄野生活','国际站已授权会话',badge('已连接','green'))}${settingLine('身份与平台读取','预览结果：已就绪',btn('检查连接','check-connection'))}${settingLine('接待模型检查','单独检查模型可用性。',btn('检查模型','check-model'))}<div class="s-note">检查不会提交客户消息。实际连接应从目标商家的授权会话启动。</div><details class="s-advanced"><summary>连接详情 ${icon('down')}</summary><dl class="s-detail-facts"><dt>当前商家</dt><dd>澄野生活</dd><dt>连接方式</dt><dd>商家授权会话</dd><dt>最近检查</dt><dd>今天 09:00，合成结果</dd></dl></details></section>`;
    else if(section==='storage') body=`<section class="s-settings-section"><h2>数据与备份</h2><p>完整保留聊天、图片、采购、回执与发送保护数据。</p>${field('当前业务文件夹','<input class="s-input" readonly value="D:\\接待工作台数据\\澄野生活">','设计稿中的示例位置')}${settingLine('完整备份','包含账本、回执及有效的未合并写入。',btn('创建备份','backup',true))}${settingLine('恢复备份','选择完整备份，恢复到空文件夹。',btn('恢复备份','restore'))}${settingLine('更换数据文件夹','保留原数据，正常关闭后再切换。',btn('更换位置','move-storage'))}<div class="s-divider"></div><h2>最近备份</h2><div class="s-state-row"><span>2026-11-16 09:00</span>${badge('完整备份','green')}</div><p class="s-small s-muted" style="margin-top:15px">登录环境不包含在业务备份中。</p></section>`;
    else if(section==='feishu') body=`<section class="s-settings-section"><h2>飞书登记</h2><p>可选的异步登记渠道，不影响普通人工聊天。</p>${settingLine('飞书登记','写入需要独立授权。',switchControl('feishu',state.settings.feishu,'启用飞书登记'))}${field('应用 ID','<input class="s-input" placeholder="填写应用 ID" autocomplete="off">')}${field('应用密钥','<input class="s-input" type="password" placeholder="留空保留已有密钥" autocomplete="new-password">')}${field('多维表格链接','<input class="s-input" placeholder="https://example.feishu.cn/base/...">')}<div class="s-inline">${btn('检查表格结构','check-feishu')}${btn('授权本次写入','grant-feishu',true)}</div><details class="s-advanced"><summary>字段映射 ${icon('down')}</summary><div class="s-form-grid">${field('客户字段','<input class="s-input" value="客户">')}${field('采购需求字段','<input class="s-input" value="采购需求">')}</div></details></section>`;
    else if(section==='display') body=`<section class="s-settings-section"><h2>界面布局</h2><p>选择日常接待使用的工作区布局。</p>${field('主导航','<select class="s-select"><option>图标与短名称</option><option>展开名称</option></select>')}${field('客户资料','<select class="s-select"><option>宽窗口默认展开</option><option>默认收起</option></select>')}<div class="s-note">客户列表与聊天顶部使用姓名和状态，不显示头像。</div></section>`;
    else body=`<section class="s-settings-section"><h2>版本与更新</h2><p>当前为独立前端设计预览，不代表本机安装版本。</p>${settingLine('客户接待工作台','0.4.1 设计范围',badge('设计预览','blue'))}${settingLine('检查更新','正式程序从固定发行来源读取更新。',btn('检查更新','check-update'))}<div class="s-note">正式更新会先暂停接待，等待任务结束并完成备份，重启后需重新授权。</div></section>`;
    return page(heading('连接与设置','常用配置集中管理，维护细节按需展开。')+`<div class="s-settings-layout"><nav class="s-settings-nav" aria-label="设置分类">${settingsSections.map(([key,title,i])=>`<a href="#settings/${key}" class="${section===key?'active':''}">${icon(i)}${title}</a>`).join('')}</nav><section class="s-settings-content s-panel">${body}<footer class="s-settings-save"><span id="settings-save-note">${state.settingsDirty?'有未保存修改，保存后暂停相关授权。':'修改仅用于当前设计预览。'}</span>${btn('保存设置','save-settings',true)}</footer></section></div>`);
  }
  function renderActivity() {
    const f=state.filter.activity,rows=events.filter(e=>f==='all'||(f==='attention'?e.state==='需关注':e.type===f));
    return page(heading('运行记录','查看连接、资料与业务任务的进度，遇到问题再展开详情。',btn(`${icon('refresh')}刷新`,'refresh'))+`<div class="s-toolbar">${tabs('activity',[['all','全部'],['attention','需关注 <small>1</small>'],['任务','业务任务'],['采购','采购'],['连接','连接'],['资料','资料']])}<span class="s-small s-muted">今天，11 月 16 日</span></div><section class="s-panel s-activity-list">${rows.map(e=>`<article class="s-event"><time>${e.time}</time><span class="s-event-marker ${e.tone}"></span><div><h2>${e.title}</h2><p>${e.detail}</p></div><button class="s-link" data-action="core:event" data-id="${e.id}">${e.state==='需关注'?'核查任务':'查看详情'} ${icon('right')}</button></article>`).join('')||'<div class="s-empty">此分类暂无运行任务</div>'}</section>`);
  }
  function render() {
    const list=$('.s-quote-list'),detail=$('.s-quote-body');
    if(list)state.quoteListScroll=list.scrollTop;
    if(detail)state.quoteDetailScroll[detail.dataset.quoteId]=detail.scrollTop;
    const [route='home',section='reception']=location.hash.slice(1).split('/');state.route=names[route]?route:'home';state.section=settingsSections.some(s=>s[0]===section)?section:'reception';
    document.title=`${names[state.route]} · 接待工作台设计稿`;
    $('#suite-nav').innerHTML=window.DesignNavigation.render(state.route);
    const global=['launcher','models'].includes(state.route);
    $('#suite-merchant-symbol').innerHTML=icon(global?'grid':'box');
    $('#suite-merchant-name').innerHTML=global?'接待工作台':`澄野生活 <span class="channel-label">国际站</span>`;
    $('#suite-context').textContent=global?'账号与共享模型':names[state.route];
    $('#suite-global').hidden=global||state.route==='whatsapp';
    $('#suite-global-label').textContent=state.globalEnabled?'关闭自动接待':'开启自动接待';
    $('#suite-global').setAttribute('aria-pressed',String(state.globalEnabled));
    $('#suite-global').title=state.globalEnabled?'关闭自动接待':'开启自动接待';
    $('#suite-reception-state').hidden=global||state.route==='whatsapp';
    $('#suite-reception-state').innerHTML=window.DesignNavigation.renderStatus(state.globalEnabled);
    $('#suite-global').classList.toggle('idle',!state.globalEnabled);
    $('#suite-header-link').innerHTML=icon('grid')+'账号启动台';
    $('#preview-page').value=state.route;
    const module=Object.values(window.DesignModules||{}).find(m=>m.routes.includes(state.route));
    const views={home:renderHome,products:renderProducts,suppliers:renderSuppliers,procurement:renderProcurement,quotes:renderQuotes,customers:renderCustomers,library:renderLibrary,settings:renderSettings,activity:renderActivity};
    $('#suite-content').innerHTML=module?module.render(state.route,api):(views[state.route]||renderHome)();
    $('#suite-content').dataset.page=state.route;
    $('#suite-content').dataset.route=state.route==='settings'?'settings/'+state.section:state.route;
    if(state.route==='quotes'){
      $('.s-quote-list').scrollTop=state.quoteListScroll;
      $('.s-quote-body').scrollTop=state.quoteDetailScroll[state.selectedQuote]||0;
    }
  }
  function refreshKeepingFocus() { const el=document.activeElement,id=el?.id,position=el?.selectionStart;render();if(id){const replacement=document.getElementById(id);replacement?.focus();try{if(typeof position==='number')replacement.setSelectionRange(position,position);}catch{}} }
  function val(id) { return document.getElementById(id)?.value.trim()||''; }
  function startSuiteReception() {
    if(state.settings.scope==='selected'&&!state.scopeCustomers.length){toast('请先选择自动接待客户');if(state.route!=='settings'||state.section!=='reception')navigate('settings/reception');else $('#auto-customer-search')?.focus();return;}
    state.globalEnabled=true;render();toast('自动接待已开启（设计预览）');
  }
  function pauseSuiteReception(){state.globalEnabled=false;render();}
  const actions={
    'toggle-reception':()=>state.globalEnabled?pauseSuiteReception():startSuiteReception(),
    'close-sheet':closeSheet,'close-modal':closeModal,'open-reception':()=>navigate('reception'),'reception-settings':()=>navigate('settings/reception'),
    refresh:()=>toast('设计预览已刷新'),filter:el=>{state.filter[el.dataset.group]=el.dataset.value;render();},
    product:el=>showProduct(el.dataset.id),supplier:el=>showSupplier(el.dataset.id),
    'default-offer':el=>{const {product:id,id:offer}=el.dataset;state.defaults[id]=state.defaults[id]===offer?null:offer;showProduct(id);toast('已更新默认供货方案，仅影响新绑定事项');},
    'go-procurement':()=>navigate('procurement'),'new-supplier':()=>supplierForm(), 'edit-supplier':el=>supplierForm(el.dataset.id),
    'save-supplier':()=>{if(!$('#supplier-form').reportValidity())return;const data={name:val('supplier-name'),platform:val('supplier-platform'),city:val('supplier-city'),contact:val('supplier-contact')||'待补充',note:val('supplier-note')};if(state.editingSupplier)Object.assign(supplier(state.editingSupplier),data);else suppliers.push({...data,id:'s'+Date.now(),count:0});closeSheet();render();toast('供应商资料已保存到预览');},
    task:el=>showTask(el.dataset.id),'task-product':el=>showProduct(el.dataset.product),
    'verify-task':()=>{const body=$('#suite-sheet-body');if(!$('#task-verification'))body.insertAdjacentHTML('beforeend','<p id="task-verification" class="s-note amber" style="margin-top:22px" role="status">预览核查：尚未取得确切结果，继续保留原任务。</p>');toast('核查原任务，未重新发起找货');},
    'sourcing-settings':sourcingSettings,'toggle-sourcing':()=>{state.procurementGranted=false;toast('请通过“本次授权”确认找货范围');},
    'save-sourcing':()=>{state.procurementGranted=false;closeSheet();render();toast('找货设置已保存，本次授权已暂停');},
    'grant-sourcing':()=>openModal({title:'授权本次找货',body:'<p>仅为当前商家“澄野生活”的产品执行找货，最多查找 3 家供应商。</p><p>定向询价与报价回收尚未开放。本次预览不会发起真实任务。</p>',buttons:[{label:'取消',action:'core:close-modal'},{label:'确认授权',action:'core:confirm-sourcing',primary:true}]}),
    'confirm-sourcing':()=>{state.procurementGranted=true;closeModal();closeSheet();render();toast('本次找货授权已更新（设计预览）');},
    quote:el=>{state.selectedQuote=el.dataset.id;render();},'save-quote':()=>{state.quoteDrafts[quoteOwner()]=val('quote-supplement');toast('已保存到当前客户的业务补充预览');},customer:el=>showCustomer(el.dataset.id),
    'open-doc':el=>{state.doc=el.dataset.id;state.docCategory='全部资料';state.directory='mine';navigate('library');},
    'doc-category':el=>{state.docCategory=el.dataset.value;render();},'doc-directory':el=>{state.directory=el.dataset.value;state.docCategory='全部资料';render();},doc:el=>{state.doc=el.dataset.id;render();},'doc-mode':el=>{state.docMode=el.dataset.value;render();},
    'new-doc':()=>{const d={id:'d'+Date.now(),title:'未命名资料',category:'商品资料',usage:'仅人工查看',confirmed:false,enabled:false,version:1,builtin:false,text:''};documents.unshift(d);state.doc=d.id;state.docCategory='全部资料';state.directory='mine';state.docMode='edit';render();$('#doc-title').focus();},
    'adopt-doc':()=>{const source=docDraft(),d={...source,id:'d'+Date.now(),title:source.title+'（我的副本）',builtin:false,confirmed:false,enabled:false,usage:'仅人工查看',version:1};delete d.dirty;documents.unshift(d);state.doc=d.id;state.directory='mine';render();toast('已采用为未启用的副本，请填写并核实正文');},
    'enable-doc':()=>{const d=docDraft();if(!d.confirmed)return toast('请先核实本版正文');if(d.usage==='仅人工查看')return;d.enabled=!d.enabled;d.dirty=true;render();},
    'save-doc':()=>{const d=docDraft();if(!d.title.trim())return toast('请填写资料标题');const source=byId(documents,d.id);if(d.dirty){d.version=source.version+1;Object.assign(source,{...d});d.dirty=false;delete source.dirty;state.globalEnabled=false;}render();toast('资料已保存；变更后自动接待需重新确认');},
    'setting-switch':el=>{const key=el.dataset.key;state.settings[key]=!state.settings[key];state.settingsDirty=true;render();},
    'save-settings':()=>{state.settingsDirty=false;state.globalEnabled=false;state.procurementGranted=false;render();toast('设置已保存，相关自动授权已暂停');},
    'pause-reception':pauseSuiteReception,
    'enable-reception':startSuiteReception,
    'check-connection':()=>toast('连接检查预览完成，未访问真实平台'),'check-model':()=>{toast('独立模型检查预览完成，不改变接待启停');},
    backup:()=>openModal({title:'创建完整备份',body:'<p>完整备份包含当前商家的聊天、图片、采购资料、账本及回执。</p><p>此处仅演示备份确认界面，不读取或复制真实文件。</p>',buttons:[{label:'取消',action:'core:close-modal'},{label:'创建备份',action:'core:preview-backup',primary:true}]}),
    'preview-backup':()=>{closeModal();toast('完整备份流程预览完成，未创建真实备份');},
    restore:()=>openModal({title:'恢复完整备份',body:'<p>选择完整业务备份，并恢复到空文件夹。已有业务数据与未决操作不会被覆盖或清除。</p><div class="s-note amber" style="margin-top:16px">恢复前暂停接待、正常关闭后台并核对备份。</div><p>当前为设计预览，不执行文件操作。</p>',buttons:[{label:'关闭',action:'core:close-modal'}]}),
    'move-storage':()=>openModal({title:'更换数据文件夹',body:'<p>先保存编辑、暂停接待并完成完整备份，再切换新的业务文件夹。</p><p>原数据保留，重启后需重新授权。当前仅为流程预览。</p>'}),
    'check-feishu':()=>toast('表格检查预览完成，未访问飞书'),
    'grant-feishu':()=>openModal({title:'授权本次飞书登记',body:'<p>仅允许当前商家的待登记内容写入所配置的表格。登记队列仍需单独处理，重启后写入关闭。</p><p>本次为设计预览，不写入飞书。</p>',buttons:[{label:'取消',action:'core:close-modal'},{label:'确认授权',action:'core:confirm-feishu',primary:true}]}),
    'confirm-feishu':()=>{closeModal();toast('本次飞书授权已更新（设计预览）');},
    'check-update':()=>toast('更新流程预览，未联网检查发行版本'),
    event:el=>{const e=byId(events,el.dataset.id);if(e.state==='需关注')return showTask('t2');openSheet({title:'运行任务详情',body:`${badge(e.state,e.tone)}<h3 style="font-size:17px;line-height:1.8;margin:18px 0 12px">${e.title}</h3><p class="s-small s-muted">${e.detail}</p><div class="s-divider"></div><dl class="s-detail-facts"><dt>时间</dt><dd>今天 ${e.time}</dd><dt>归属</dt><dd>澄野生活</dd><dt>类型</dt><dd>${e.type}</dd></dl><details class="s-advanced"><summary>诊断信息 ${icon('down')}</summary><p class="s-small s-muted">合成任务编号：design-${e.id}<br>此处只展示业务任务诊断。</p></details>`});}
  };
  document.addEventListener('click',event=>{const el=event.target.closest('button,[data-nav]');if(!el||el.disabled)return;if(el.dataset.nav)return navigate(el.dataset.nav);const action=el.dataset.action;if(!action)return;for(const module of Object.values(window.DesignModules||{}))if(module.onAction?.(action,el,api))return;if(action.startsWith('core:'))actions[action.slice(5)]?.(el);});
  document.addEventListener('input',event=>{for(const module of Object.values(window.DesignModules||{}))module.onInput?.(event,api);const el=event.target;if(el.dataset.search){state.search[el.dataset.search]=el.value;refreshKeepingFocus();}if(el.id==='auto-customer-search'){state.scopeQuery=el.value;refreshKeepingFocus();return;}if(el.id==='quote-supplement')state.quoteDrafts[quoteOwner()]=el.value;if(['doc-title','doc-text'].includes(el.id)){const d=docDraft();d[el.id==='doc-title'?'title':'text']=el.value;d.dirty=true;d.confirmed=false;d.enabled=false;$('#doc-save-state').textContent='有未保存修改';$('#doc-confirmed').checked=false;const toggle=$('[data-action="core:enable-doc"]');if(toggle)toggle.setAttribute('aria-checked','false');}if(el.closest('.s-settings-content')&&el.id!=='setting-auto-scope'&&!el.hasAttribute('data-auto-customer')){state.settingsDirty=true;$('#settings-save-note').textContent='有未保存修改，保存后暂停相关授权。';}});
  document.addEventListener('change',event=>{for(const module of Object.values(window.DesignModules||{}))module.onChange?.(event,api);const el=event.target;if(el.id==='preview-page')navigate(el.value);if(el.id==='doc-confirmed'){docDraft().confirmed=el.checked;docDraft().dirty=true;if(!el.checked)docDraft().enabled=false;render();}if(el.id==='doc-usage'){docDraft().usage=el.value;docDraft().dirty=true;if(el.value==='仅人工查看')docDraft().enabled=false;render();}if(el.id==='setting-auto-scope'||el.hasAttribute('data-auto-customer')){if(el.id==='setting-auto-scope')state.settings.scope=el.value;else{const id=el.dataset.autoCustomer;state.scopeCustomers=el.checked?[...new Set([...state.scopeCustomers,id])]:state.scopeCustomers.filter(value=>value!==id);}window.DesignReceptionRange.save({scope:state.settings.scope,customerIds:state.scopeCustomers});state.globalEnabled=false;render();if(el.id==='setting-auto-scope')$('#setting-auto-scope').focus();else $(`[data-auto-customer="${el.dataset.autoCustomer}"]`)?.focus();toast('自动接待范围已更新，可在顶部开启');}});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('#suite-modal').open)closeSheet();if(event.key==='Tab'&&!$('#suite-sheet-overlay').hidden&&!$('#suite-modal').open){const items=$$('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href]', $('#suite-sheet'));const first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}});
  $('#suite-modal').addEventListener('close',()=>{if(modalReturnFocus?.isConnected)modalReturnFocus.focus();});
  $('#suite-modal').addEventListener('click',event=>{if(event.target!==$('#suite-modal'))return;const r=$('#suite-modal').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeModal();});
  window.addEventListener('hashchange',()=>{closeSheet();closeModal();render();$('#suite-content').scrollTop=0;});
  $('.suite-sheet-heading .icon-button').innerHTML=icon('close');$('.suite-modal .icon-button').innerHTML=icon('close');
  window.WorkbenchPreview=Object.freeze({state});
  render();
})();
