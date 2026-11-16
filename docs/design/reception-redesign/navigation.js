(() => {
  'use strict';
  const paths={
    brand:'<path d="M20 11.5a8 8 0 0 1-8.5 8H4l1.3-4.1A8 8 0 1 1 20 11.5Z"/><path d="M8 10h8M8 14h5"/>',
    home:'<path d="m3 10 9-7 9 7v11h-7v-7h-4v7H3V10Z"/>',
    reception:'<path d="M20 11.5a8 8 0 0 1-8.5 8H4l1.3-4.1A8 8 0 1 1 20 11.5Z"/><path d="M8 10h8M8 14h5"/>',
    products:'<path d="m12 3 9 5-9 5-9-5 9-5ZM3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/>',
    suppliers:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/>',
    procurement:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    quotes:'<path d="M5 3h14v18H5V3Zm3 5h8m-8 4h8m-8 4h5"/>',
    customers:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/>',
    library:'<path d="M12 5C9 3 6 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15"/>',
    activity:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    launcher:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    settings:'<path d="m9 3-.6 2.6-2 1.2-2.6-.7-2 3.5L3.7 12l-1.9 2.4 2 3.5 2.6-.7 2 1.2L9 21h4l.6-2.6 2-1.2 2.6.7 2-3.5-1.9-2.4 1.9-2.4-2-3.5-2.6.7-2-1.2L13 3Z"/><circle cx="11" cy="12" r="3"/>'
  };
  const primary=[['home','开始'],['reception','接待'],['products','产品库'],['suppliers','供应商'],['procurement','采购'],['quotes','待我报价'],['customers','客户'],['library','资料']];
  const secondary=[['activity','运行记录'],['launcher','启动台'],['settings','设置']];
  const icon=name=>`<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
  function render(current){const item=([id,title])=>`<button class="nav-item${id===current?' selected':''}" data-design-nav="${id}"${id===current?' aria-current="page"':''}>${icon(id)}<span>${title}</span>${id===current?'<span class="nav-indicator"></span>':''}</button>`;return `<button class="brand-mark" data-design-nav="home" aria-label="客户接待工作台">${icon('brand')}</button><div class="nav-primary">${primary.map(item).join('')}</div><div class="nav-bottom">${secondary.map(item).join('')}</div>`;}
  function renderStatus(enabled){const long=enabled?'自动接待中':'自动接待已关闭',short=enabled?'接待中':'已关闭';return `<span class="status-dot" style="background:${enabled?'#3b9b7c':'#adb8c9'}"></span><span class="reception-status-long">${long}</span><span class="reception-status-short">${short}</span>`;}
  window.DesignNavigation={render,renderStatus};
  const rangeKey='arw-design-auto-range-v1';
  const knownCustomers=new Set(['alex','emma','noah','sofia','liam','mia','oliver','hana']);
  const normalizeRange=value=>({scope:value?.scope==='selected'?'selected':'all',customerIds:Array.isArray(value?.customerIds)?[...new Set(value.customerIds.filter(id=>knownCustomers.has(id)))]:[]});
  let fallbackRange=normalizeRange(null);
  window.DesignReceptionRange=Object.freeze({
    read(){try{return normalizeRange(JSON.parse(sessionStorage.getItem(rangeKey)));}catch{return normalizeRange(fallbackRange);}},
    save(value){fallbackRange=normalizeRange(value);try{sessionStorage.setItem(rangeKey,JSON.stringify(fallbackRange));}catch{}return normalizeRange(fallbackRange);}
  });
  document.addEventListener('click',event=>{const button=event.target.closest('[data-design-nav]');if(!button)return;const route=button.dataset.designNav;const inReception=Boolean(document.getElementById('workspace'));if(route==='reception'){if(!inReception)location.href='index.html';return;}if(inReception)location.href='pages.html#'+route;else{document.getElementById('preview-page').value=route;document.getElementById('preview-page').dispatchEvent(new Event('change',{bubbles:true}));}});
  const receptionNav=document.getElementById('design-navigation');if(receptionNav)receptionNav.innerHTML=render('reception');
})();
