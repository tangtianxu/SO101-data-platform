'use strict';
let feedbackItems=[],selectedFeedback=null;
function installFeedbackHistory(){
  const nav=document.createElement('button');nav.className='nav-item';nav.dataset.view='feedback';nav.innerHTML='<span>◷</span>ACT 历史 / 回流';nav.addEventListener('click',()=>setView('feedback'));
  document.querySelector('[data-view="settings"]').before(nav);
  const page=document.createElement('section');page.id='feedback-page';page.className='page hidden';
  page.innerHTML=`<div class="page-heading"><div><div class="eyebrow">POLICY ROLLOUTS</div><h1>ACT 历史与数据回流</h1><p>策略轨迹与专家示范分开保存。成功不自动变成专家数据，失败不自动删除。</p></div><div class="button-row"><button id="feedback-open-folder" class="button">打开回流目录</button><button id="feedback-refresh" class="button">刷新历史</button></div></div><div class="feedback-summary panel"><span id="feedback-summary">尚未读取</span><label>结果筛选<select id="feedback-filter"><option value="all">全部</option><option value="success">成功</option><option value="failure">失败</option><option value="unknown">未判定</option></select></label></div><div class="feedback-layout"><div id="feedback-list" class="feedback-list"></div><section id="feedback-detail" class="panel feedback-detail"><h2>选择一条运行记录</h2><p class="field-help">未来的策略任务会自动记录两路视频、实测状态、实际发送动作、原始策略请求及传感器读数。旧日志未记录的字段不补造。</p></section></div>`;
  document.querySelector('main').append(page);
  $('feedback-refresh').addEventListener('click',()=>handle(refreshFeedback()));
  $('feedback-filter').addEventListener('change',renderFeedback);
  $('feedback-open-folder').addEventListener('click',()=>handle(api('/api/open-folder',{target:'feedback'})));
  const form=document.createElement('div');form.innerHTML='<label>任务语言指令（记录标签）<textarea id="act-task" rows="2" maxlength="300"></textarea></label><label>本次场景描述<input id="act-group" list="scene-descriptions" maxlength="100" placeholder="例如：娃娃位置变化"></label><p class="field-help">ACT 本身不使用语言条件。每次运行自动保存到数据回流目录，结束后标记成功 / 失败；启动复位段只留诊断，不混作任务示范。</p><button id="act-history-link" class="button full">查看历史与回流数据</button>';
  document.querySelector('[data-inspector="act"] h2').after(form);$('act-history-link').addEventListener('click',()=>setView('feedback'));
}
async function refreshFeedback(){
  feedbackItems=await api('/api/feedback');renderFeedback();
  if(selectedFeedback){selectedFeedback=feedbackItems.find(i=>i.root===selectedFeedback.root)||null;if(selectedFeedback)renderFeedbackDetail();}
}
function feedbackResult(item){return item.task_success===true?'成功':item.task_success===false?'失败':'未判定';}
function renderFeedback(){
  const counts=[true,false,null].map(value=>feedbackItems.filter(i=>(i.task_success??null)===value).length);
  $('feedback-summary').textContent=`${feedbackItems.length} 次运行 · 成功 ${counts[0]} · 失败 ${counts[1]} · 未判定 ${counts[2]}`;
  const filter=$('feedback-filter').value,list=$('feedback-list');list.replaceChildren();
  for(const item of feedbackItems){
    if(filter!=='all'&&filter!==(item.task_success===true?'success':item.task_success===false?'failure':'unknown'))continue;
    const button=document.createElement('button');button.className='panel feedback-card'+(selectedFeedback?.root===item.root?' selected':'');
    const header=document.createElement('div'),date=document.createElement('strong'),badge=document.createElement('span');date.textContent=item.created.slice(0,19).replace('T',' ');
    badge.className='dataset-tag '+(item.task_success===true?'tag-review-train':item.task_success===false?'tag-review-exclude':'tag-review-pending');badge.textContent=feedbackResult(item);header.append(date,badge);
    const task=document.createElement('p');task.textContent=item.task;
    const scene=document.createElement('p');scene.className='field-help';scene.textContent=`${item.description} · ${item.frames||0} 帧 · ${item.legacy?'旧日志':item.complete?'保存完整':'未开始 / 不完整'}`;
    button.append(header,task,scene);button.addEventListener('click',()=>{selectedFeedback=item;renderFeedback();renderFeedbackDetail();});list.append(button);
  }
  if(!list.children.length){const empty=document.createElement('p');empty.className='field-help';empty.textContent='暂无符合条件的运行记录；新任务完成后自动出现。';list.append(empty);}
}
function renderFeedbackDetail(){
  const item=selectedFeedback,detail=$('feedback-detail');detail.replaceChildren();if(!item)return;
  const heading=document.createElement('h2');heading.textContent=feedbackResult(item)+' · '+(item.complete?'记录完整':item.legacy?'历史日志':'未开始 / 不完整');detail.append(heading);
  for(const [label,value] of [['任务',item.task],['场景',item.description],['运行状态',item.runtime_outcome||item.state],['模型',item.checkpoint],['目录',item.root],['数据来源','ACT 策略；不是专家动作'],['后续用途','可筛选作 RL 经验 / 专家审核候选；尚无奖励定义、专家纠正或 DAgger 标签']]){
    const line=document.createElement('p'),name=document.createElement('strong');name.textContent=label+'：';line.append(name,document.createTextNode(value||'—'));detail.append(line);
  }
  const view=document.createElement('button');view.className='button primary';view.textContent='查看视频与全部记录曲线';view.disabled=!item.has_dataset;
  view.addEventListener('click',()=>handle(openFeedbackDataset(item)));detail.append(view);
  const noteLabel=document.createElement('label');noteLabel.textContent='结果 / 失败原因备注';const note=document.createElement('textarea');note.rows=3;note.maxLength=1000;note.value=item.note||'';noteLabel.append(note);detail.append(noteLabel);
  const buttons=document.createElement('div');buttons.className='button-row';
  for(const [text,value] of [['成功',true],['失败',false],['未判定',null]]){
    const button=document.createElement('button');button.className='button';button.textContent='标记'+text;button.disabled=!!item.legacy||['preparing','recording','finalizing'].includes(item.state);
    button.addEventListener('click',()=>handle((async()=>{await api('/api/feedback/result',{root:item.root,success:value,note:note.value});await refreshFeedback();toast('人工结果已保存；未移动原始数据，也未自动加入训练。');})()));buttons.append(button);
  }
  detail.append(buttons);
  const hint=document.createElement('p');hint.className='field-help';hint.textContent=item.capture_error||'视频时间轴按采样帧组织；实际发送、相机与反馈时间另有字段。raw 电流/负载不是触觉或实测接触力。';detail.append(hint);
}
async function openFeedbackDataset(item){
  const naming={simple_spec:{domain:'real',stage:'raw',review:'pending',started_at:item.created,instruction:item.task,description:'ACT 回流 · '+item.description,note:''},note:'策略动作，不是专家示范',append_allowed:false};
  const entry={root:item.dataset_root,name:'ACT 回流 · '+item.task,episodes:1,frames:item.frames,ready:true,naming,is_feedback:true,protected:'策略回流独立保存，不通过示范库删除'};
  if(!datasetItems.some(i=>i.root===entry.root))datasetItems.push(entry);
  let option=Array.from($('dataset-select').options).find(o=>o.value===entry.root);if(!option){option=new Option(entry.name,entry.root);$('dataset-select').add(option);}
  $('dataset-select').value=entry.root;setView('datasets');await openDataset(entry.root);
}
