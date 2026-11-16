(() => {
  'use strict';
  const scenarios = [
    ['suggestion','整段回复建议','建议与润色','就地','生成回复建议','外语正文下方显示中文译文。'],
    ['polish','润色结果','建议与润色','就地','润色已有正文','润色正文与中文译文一起显示。'],
    ['generating','建议生成中','建议与润色','状态','请求回复建议','显示生成状态。'],
    ['stale','建议已失效','建议与润色','就地','生成后收到新消息','重新生成后使用。'],
    ['takeover','直接人工接管','接待状态','状态','点击人工接管','立即切换当前客户，无确认弹窗。'],
    ['resume','直接恢复自动接待','接待状态','状态','点击恢复自动接待','立即恢复，实际接待仍按设置中的范围执行。'],
    ['global','直接开启自动接待','接待状态','状态','点击开启自动接待','按已保存的接待范围开启，不做 AI 预检。'],
    ['exit','退出前有未保存内容','接待状态','确认','退出工作台','保存或继续编辑。'],
    ['rejected','发送明确未提交','发送状态','就地','发送被明确拒绝','可重试确切未提交的原正文。'],
    ['unconfirmed','请求尚未确认','发送状态','状态','等待请求结果','只显示状态，无手动核查入口。'],
    ['unknown','发送结果待确认','发送状态','状态','等待发送结果','保留待定状态，后台沿原操作处理，不重发。'],
    ['offline','连接断开','连接与模型','状态','连接中断','显示连接状态及重新连接动作。'],
    ['model','执行失败后提醒 AI 不可用','连接与模型','失败弹窗','点击回复建议或润色，模拟调用失败','实际执行失败后才弹出提醒。'],
    ['translation','译文暂不可用','内容与保存','状态','译文获取失败','只显示当前状态。'],
    ['supplement','业务补充保存失败','内容与保存','就地','补充保存失败','保留内容，可重试保存。'],
    ['draft','回复正文保存失败','内容与保存','就地','正文保存失败','保留正文，可重试保存。'],
    ['attachment','附件读取失败','内容与保存','就地','附件读取失败','可重新选择附件。'],
    ['image-error','图片加载失败','内容与保存','状态','商品图片读取失败','只显示当前状态。'],
    ['image','商品原图预览','内容与保存','预览','查看商品原图','查看原图。'],
    ['evidence','来源直接定位消息','内容与保存','定位','点击需求字段的来源','直接跳到对应消息，不经过弹窗。'],
    ['saved','保存成功','内容与保存','轻提示','保存完成','短暂提示。'],
    ['history-loading','历史补全中','聊天同步','状态','补全当前客户历史','只显示补全状态。'],
    ['history-failed','历史补全失败','聊天同步','状态','当前客户历史同步失败','从补全按钮重试。']
  ].map(([id,title,group,kind,trigger,description]) => ({id,title,group,kind,trigger,description}));
  window.RECEPTION_FEEDBACK_SCENARIOS = scenarios;
  const $ = s => document.querySelector(s);
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const requested = new URLSearchParams(location.search).get('feedback');
  const sceneId = requested === 'verify' ? 'unknown' : requested;
  if ($('#rf-directory')) {
    let selected = scenarios.find(x => x.id === sceneId) || scenarios[0];
    const list = $('#rf-list'), frame = $('#rf-frame');
    function select(id) {
      selected = scenarios.find(x => x.id === id) || scenarios[0];
      list.querySelectorAll('[data-rf-select]').forEach(b => b.setAttribute('aria-current', String(b.dataset.rfSelect === selected.id)));
      $('#rf-title').textContent = selected.title;
      $('#rf-info').innerHTML = `<div><span>触发</span><strong>${escape(selected.trigger)}</strong></div><div><span>位置</span><strong>${escape(selected.kind)}</strong></div><div><span>动作与说明</span><strong>${escape(selected.description)}</strong></div>`;
      const url = `index.html?feedback=${encodeURIComponent(selected.id)}`;
      frame.src = url; $('#rf-open').href = url;
      history.replaceState(null, '', `?feedback=${encodeURIComponent(selected.id)}`);
      document.body.classList.add('rf-show-preview');
    }
    const groups = [...new Set(scenarios.map(s => s.group))];
    list.innerHTML = groups.map(group => `<section><h2>${escape(group)}</h2>${scenarios.filter(s => s.group === group).map(s => `<button type="button" data-rf-select="${escape(s.id)}"><span>${escape(s.title)}</span><small>${escape(s.kind)}</small></button>`).join('')}</section>`).join('');
    list.addEventListener('click', event => { const b = event.target.closest('[data-rf-select]'); if (b) select(b.dataset.rfSelect); });
    $('#rf-back').addEventListener('click', () => document.body.classList.remove('rf-show-preview'));
    select(selected.id);
    return;
  }
  if (!scenarios.some(s => s.id === sceneId)) return;
  const api = window.ReceptionPreview;
  if (!api) return;
  const {state,session} = api;
  const id = sceneId;
  let owner = state.selected, active = true;
  const btn = (action,label,primary=false) => `<button type="button" class="rf-action${primary?' rf-primary':''}" data-feedback-action="${action}">${label}</button>`;
  const insert = (target,html,where='beforeend') => $(target)?.insertAdjacentHTML(where,html);
  const panel = (title,actions='',tone='warning',attrs='') => `<div class="rf-panel rf-${tone}" data-rf-injected ${attrs} role="status"><strong>${title}</strong>${actions?`<div class="rf-actions">${actions}</div>`:''}</div>`;
  function modal(title,body) {
    api.showModal(title,`<div class="rf-modal-content">${body}</div>`,[{label:'关闭',action:'close-modal'}]);
    const actions = $('#modal-body .rf-modal-actions');
    if(actions){$('#modal-actions').replaceChildren(...actions.childNodes);actions.remove();}
  }
  function suggestion(title,actions) {
    const el=$('#suggestion');el.hidden=false;el.setAttribute('data-rf-injected','');
    el.innerHTML=`<div class="rf-suggestion"><div class="rf-suggestion-heading">${api.icon('spark')}<strong>${title}</strong></div><div class="rf-actions">${actions}</div></div>`;
  }
  function protectSend() {
    $('#send-button').disabled=true;$('#send-button').setAttribute('data-rf-blocked','');$('#composer').classList.add('blocked');
    $('#compose-hint').textContent=id==='offline'?'连接已断开':id==='draft'?'正文未保存':'发送未提交';
  }
  function clearInjected() {
    document.querySelectorAll('[data-rf-injected]').forEach(node=>{
      if(node.id==='suggestion'){node.hidden=true;node.innerHTML='';node.removeAttribute('data-rf-injected');}
      else node.remove();
    });
  }
  function render() {
    if(id==='suggestion'||id==='polish'){
      if(id==='polish'){session().draft='We can arrange the sample. Please send your address.';session().revision++;$('#reply-input').value=session().draft;api.renderActivity();}
      api.actions.suggest();
    } else if(id==='generating') suggestion('正在生成建议',btn('cancel-generation','取消生成'));
    else if(id==='stale') suggestion('建议已失效',btn('discard','弃用')+btn('regenerate','重新生成'));
    else if(id==='takeover'){api.setScenario('auto');owner=state.selected;api.actions.ownership();}
    else if(id==='resume') api.actions.ownership();
    else if(id==='global') api.actions['start-reception']();
    else if(id==='exit') modal('还有内容未保存',`<div class="rf-modal-actions">${btn('cancel-modal','继续编辑')}${btn('save-exit','保存并退出',true)}</div>`);
    else if(['unconfirmed','unknown'].includes(id)){
      session().draft='I will also check the shipping options.';session().pending=true;session().pendingLabel=id==='unconfirmed'?'请求尚未确认':'发送结果待确认';api.renderAll();
    } else if(id==='rejected'){
      session().draft='I will also check the shipping options.';$('#reply-input').value=session().draft;$('#reply-activity').innerHTML=panel('发送未提交',btn('retry-frozen','重试原正文',true));protectSend();
    } else if(id==='offline'){
      state.globalEnabled=false;api.updateHeader();api.renderActivity();session().draft='I’ll check the sample cost for you.';$('#reply-input').value=session().draft;protectSend();
      insert('.conversation-header',panel('连接已断开',btn('reconnect','重新连接'),'notice'),'afterend');
    } else if(id==='translation'){
      const bubble=$('#message-stream .message:last-child .message-bubble');
      bubble?.querySelector('.message-translation')?.remove();
      bubble?.insertAdjacentHTML('beforeend',panel('译文暂不可用','','notice'));
    } else if(id==='supplement'){
      $('#supplement-save').textContent='保存失败';insert('#supplement',panel('业务补充保存失败',btn('retry-supplement','重试保存')));
    } else if(id==='draft'){
      session().draft='I’ll check the sample cost for you.';$('#reply-input').value=session().draft;$('#draft-status').textContent='保存失败';protectSend();
      insert('.composer',panel('回复正文保存失败',btn('retry-draft','重试保存')),'beforebegin');
    } else if(id==='attachment') insert('#supplement-attachments',panel('附件读取失败',btn('retry-attachment','重新选择')));
    else if(id==='image-error'){
      const visual=$('.product-visual');if(visual){visual.hidden=true;visual.insertAdjacentHTML('afterend',panel('图片加载失败','','notice'));}
    } else if(id==='image') api.actions.image();
    else if(id==='evidence') api.actions['locate-source']({dataset:{messageId:'alex-0'}});
    else if(id==='saved') api.toast('已保存');
    else if(id==='history-loading'||id==='history-failed'){
      session().historyLoading=id==='history-loading';api.updateHeader();
      if(id==='history-failed')$('#complete-history-button span').textContent='重试补全';
      insert('.conversation-header',panel(id==='history-loading'?'正在补全历史':'历史补全失败','','notice','data-rf-history'),'afterend');
    }
  }
  function simulateAiFailure() {
    const current=session(),generation=++current.generation;current.aiAttempts=(current.aiAttempts||0)+1;
    suggestion('正在生成建议',btn('cancel-generation','取消生成'));
    setTimeout(()=>{
      if(!active||state.selected!==owner||current.generation!==generation)return;
      api.renderSuggestion();
      modal('AI 不可用',`<p>请求超时</p><div class="rf-modal-actions">${btn('cancel-modal','关闭')}${btn('model-settings','查看模型设置',true)}</div>`);
    },300);
  }
  document.addEventListener('click',event=>{
    const customer=event.target.closest('[data-customer]');
    if(customer&&customer.dataset.customer!==owner){active=false;clearInjected();$('#send-button').removeAttribute('data-rf-blocked');if($('#modal').open)api.closeModal();return;}
    if(!active||state.selected!==owner)return;
    const existing=event.target.closest('[data-action]');
    if(['rejected','draft','offline'].includes(id)&&$('#send-button').hasAttribute('data-rf-blocked')&&existing?.dataset.action==='send'){event.stopImmediatePropagation();return;}
    if(id==='model'&&['suggest','regenerate'].includes(existing?.dataset.action)){event.stopImmediatePropagation();simulateAiFailure();return;}
    if(id==='offline'&&['global','start-reception'].includes(existing?.dataset.action)){event.stopImmediatePropagation();api.toast('连接已断开');return;}
    if(id==='history-failed'&&existing?.dataset.action==='complete-history'){
      event.stopImmediatePropagation();if(session().historyLoading)return;
      const notice=$('[data-rf-history]');if(notice)notice.querySelector('strong').textContent='正在补全历史';
      api.actions['complete-history']();setTimeout(()=>notice?.remove(),900);return;
    }
    const b=event.target.closest('[data-feedback-action]');if(!b)return;
    event.preventDefault();event.stopImmediatePropagation();
    const action=b.dataset.feedbackAction;
    if(action==='cancel-modal')api.closeModal();
    else if(action==='save-exit'){api.closeModal();api.toast('已保存');}
    else if(action==='discard'||action==='cancel-generation'){session().generation++;clearInjected();}
    else if(action==='regenerate'){clearInjected();api.actions.suggest();}
    else if(action==='retry-frozen'){b.closest('.rf-panel').innerHTML='<strong>原正文已受理</strong>';$('#send-button').removeAttribute('data-rf-blocked');api.renderActivity();}
    else if(action==='reconnect'){b.closest('.rf-panel').querySelector('strong').textContent='正在连接';b.disabled=true;}
    else if(action==='model-settings')location.href='pages.html#models';
    else if(action==='retry-supplement'){b.closest('.rf-panel').remove();$('#supplement-save').textContent='已保存';}
    else if(action==='retry-draft'){b.closest('.rf-panel').remove();$('#send-button').removeAttribute('data-rf-blocked');api.renderActivity();}
    else if(action==='retry-attachment')$('#attachment-input').click();
  },true);
  document.addEventListener('keydown',event=>{
    if(active&&state.selected===owner&&$('#send-button').hasAttribute('data-rf-blocked')&&event.target.id==='reply-input'&&event.key==='Enter'&&!event.shiftKey){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  document.addEventListener('input',event=>{
    if(!active||state.selected!==owner)return;
    if(event.target.id==='reply-input'&&$('#send-button').hasAttribute('data-rf-blocked')){
      event.stopImmediatePropagation();session().draft=event.target.value;session().revision++;protectSend();$('#suggest-button span').textContent=session().draft.trim()?'润色正文':'回复建议';
    }
    if(id==='supplement'&&event.target.id==='supplement-input'&&$('[data-rf-injected]')){event.stopImmediatePropagation();session().supplement=event.target.value;$('#supplement-save').textContent='保存失败';}
  },true);
  render();
})();
