'use strict';
let captureProcessRequested=null,captureReview=null,captureReviewBusy=false;
function updateCapturePauseControls(){
  const job=state?.job,record=job?.kind==='record',active=state?.active&&record,paused=active&&job.phase==='PAUSED';
  const locked=state?.offline||runtimeMismatch||state?.identity?.source_changed||state?.restart_required;
  $('capture-pause-actions').hidden=view!=='capture';
  $('pause-capture').disabled=!active||!['RECORDING','RESET'].includes(job.phase)||locked||requestBusy;
  $('resume-capture').disabled=!paused||captureReviewBusy||locked||requestBusy;
  $('process-capture').disabled=!record||captureReviewBusy||!!captureProcessRequested||(active&&!paused&&locked)||(active&&!['RECORDING','RESET','PAUSED'].includes(job.phase));
  $('process-capture').textContent=captureProcessRequested?'等待暂停保存…':'处理已采数据';
  $('capture-review-resume').disabled=!paused||captureReviewBusy||locked;
  const deleted=library?.annotations?.[selectedEpisode?.index]?.deleted;
  $('restore-deleted-episode').hidden=!deleted;
  $('restore-deleted-episode').disabled=!!(state?.active&&!paused);
  if(captureProcessRequested){
    if(job?.id!==captureProcessRequested.job||!active||['ERROR','FINISHED'].includes(job.phase))captureProcessRequested=null;
    else if(paused){captureProcessRequested=null;handle(loadCaptureReview());}
    else if(Date.now()>captureProcessRequested.until){captureProcessRequested=null;toast('暂停尚未确认，请稍后点击“处理已采数据”。',true);}
  }
}
async function loadCaptureReview(){
  if(captureReviewBusy)return;
  const job=state?.job;if(job?.kind!=='record')throw Error('当前没有可处理的采集会话');
  captureReviewBusy=true;
  try{
    captureReview=await api('/api/capture-review?job_id='+encodeURIComponent(job.id));
    if(state.job?.id!==captureReview.job_id)throw Error('采集会话已变化');
    const root=$('capture-review-root');root.replaceChildren();
    for(const data of captureReview.datasets)root.add(new Option(data.name+' · '+data.total+' 条',data.root));
    if(captureReview.datasets.some(d=>d.root===job.group_root))root.value=job.group_root;
    renderCaptureReviewList();if(!$('capture-review-dialog').open)$('capture-review-dialog').showModal();
  }finally{captureReviewBusy=false;if(captureReview)renderCaptureReviewEntry();updateCapturePauseControls();}
}
function captureReviewDataset(){return captureReview?.datasets.find(d=>d.root===$('capture-review-root').value);}
function renderCaptureReviewList(preferred){
  const data=captureReviewDataset(),select=$('capture-review-episode');select.replaceChildren();
  const labels={success:'成功',failure:'失败',exclude:'不用于训练',unreviewed:'待审核'};
  for(let i=(data?.total||0)-1;i>=0;i--){const row=data.annotations[i]||{};select.add(new Option('第 '+(i+1)+' 条 · '+(labels[row.result]||'待审核')+(row.interrupted?' · 中断片段':'')+(row.deleted?' · 已删除，可恢复':''),String(i)));}
  if(preferred!==undefined&&[...select.options].some(o=>o.value===String(preferred)))select.value=String(preferred);
  renderCaptureReviewEntry();
}
function renderCaptureReviewEntry(){
  const data=captureReviewDataset(),value=$('capture-review-episode').value,valid=!!data&&value!=='';
  const row=valid?data.annotations[value]||{}:{};
  $('capture-review-note').value=row.note||'';$('capture-review-result').value=row.result||'unreviewed';
  $('capture-review-entry-hint').textContent=!valid?'尚无已保存轨迹。录制中暂停的有效片段会先保存，再出现在这里。':row.deleted?'已作删除标记，不用于训练；原文件保留，可恢复。':row.interrupted?'暂停前的中断片段，默认不用于训练；请人工检查后再调整标记。':'编辑当前所选轨迹的审核状态和备注。';
  for(const id of ['capture-review-save','capture-review-delete','capture-review-note'])$(id).disabled=!valid||captureReviewBusy;
  $('capture-review-result').disabled=!valid||!!row.deleted||captureReviewBusy;
  $('capture-review-delete').textContent=row.deleted?'恢复此条':'删除此条（可恢复）';
}
async function changeCaptureReview(deleting=false){
  const data=captureReviewDataset(),episode=Number($('capture-review-episode').value);if(!data||!$('capture-review-episode').value)throw Error('请选择已保存轨迹');
  const old=data.annotations[episode]||{},root=data.root,job=captureReview.job_id;
  const payload={job_id:job,root,episode,action:deleting?(old.deleted?'restore':'delete'):'edit',note:$('capture-review-note').value,result:$('capture-review-result').value};
  captureReviewBusy=true;
  for(const id of ['capture-review-save','capture-review-delete','capture-review-note','capture-review-result','capture-review-root','capture-review-episode'])$(id).disabled=true;
  updateCapturePauseControls();
  try{
    captureReview=await api('/api/capture-review',payload);
    if(library?.root===root){library.annotations=captureReview.datasets.find(d=>d.root===root).annotations;renderEpisodeList();renderAnnotation();invalidateMotionPreview(root);}
    toast(deleting?(old.deleted?'已恢复此条':'已作可恢复删除标记，原始文件保留；不用于训练'):'标记与备注已保存');
  }finally{captureReviewBusy=false;$('capture-review-root').disabled=false;$('capture-review-episode').disabled=false;renderCaptureReviewList(episode);updateCapturePauseControls();}
}
async function resumeCapture(){await command('resume_capture',{confirmed:true});if($('capture-review-dialog').open)$('capture-review-dialog').close();await poll();}
function installCapturePause(){
  const restore=document.createElement('button');restore.id='restore-deleted-episode';restore.className='button';restore.hidden=true;restore.textContent='恢复已删除轨迹';
  document.querySelector('#episode-content .annotation-actions').append(restore);
  restore.addEventListener('click',()=>handle((async()=>{
    library.annotations=await api('/api/episode-restore',{root:library.root,episode:selectedEpisode.index,crop_revision:library.crop_revision||null});
    renderEpisodeList();renderAnnotation();toast('轨迹已恢复，原始文件未修改');
  })()));
  const actions=document.createElement('div');actions.id='capture-pause-actions';actions.className='capture-pause-actions';
  actions.innerHTML='<div class="form-grid"><button id="pause-capture" class="button" disabled>暂停采集</button><button id="resume-capture" class="button" disabled>继续采集</button></div><button id="process-capture" class="button full" disabled>处理已采数据</button><p class="field-help">暂停停止跟随并保持力矩，不自动卸力；继续先缓慢追平主臂再进入复位。长时间离开请先可靠支撑并结束／释放力矩。</p>';
  $('shortcut-hint').after(actions);
  const dialog=document.createElement('dialog');dialog.id='capture-review-dialog';dialog.className='dataset-dialog';
  dialog.innerHTML='<h2>采集数据处理</h2><p class="field-help">暂停后可处理本次计划中已经保存的轨迹。中断片段单独保留，不与恢复后的动作拼接。删除只作可恢复标记，不改写录制文件；训练导出须按审核状态过滤。</p><label>数据集／小组<select id="capture-review-root"></select></label><label>轨迹（最近优先）<select id="capture-review-episode"></select></label><p id="capture-review-entry-hint" class="field-help"></p><label>审核状态<select id="capture-review-result"><option value="unreviewed">待审核</option><option value="success">成功</option><option value="failure">失败</option><option value="exclude">不用于训练</option></select></label><label>备注<textarea id="capture-review-note" rows="4" maxlength="1000"></textarea></label><div class="button-row"><button id="capture-review-save" class="button primary">保存标记与备注</button><button id="capture-review-delete" class="button danger-text">删除此条（可恢复）</button></div><div class="dialog-actions"><button id="capture-review-close" class="button">关闭，保持暂停</button><button id="capture-review-resume" class="button primary">继续采集</button></div>';
  document.body.append(dialog);
  $('pause-capture').addEventListener('click',()=>handle(command('pause_capture',{phase:state.job.phase})));
  $('resume-capture').addEventListener('click',()=>handle(resumeCapture()));
  $('process-capture').addEventListener('click',()=>handle((async()=>{
    if(!state.active||state.job.phase==='PAUSED')return loadCaptureReview();
    captureProcessRequested={job:state.job.id,until:Date.now()+120000};updateCapturePauseControls();
    try{await command('pause_capture',{phase:state.job.phase});await poll();}catch(e){captureProcessRequested=null;updateCapturePauseControls();throw e;}
  })()));
  $('capture-review-root').addEventListener('change',()=>renderCaptureReviewList());
  $('capture-review-episode').addEventListener('change',renderCaptureReviewEntry);
  $('capture-review-save').addEventListener('click',()=>handle(changeCaptureReview()));
  $('capture-review-delete').addEventListener('click',()=>handle(changeCaptureReview(true)));
  $('capture-review-close').addEventListener('click',()=>dialog.close());
  $('capture-review-resume').addEventListener('click',()=>handle(resumeCapture()));
}
