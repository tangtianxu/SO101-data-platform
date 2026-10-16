'use strict';
let episodeSelection=new Set(),episodeSelectionRoot=null,batchReviewBusy=false;
let motionPreview=null,motionPreviewJob=null,motionPreviewLoading=false,motionAnalysisRequested=false;

function scopedEpisodes(){const mode=$('episode-visibility')?.value||'active';return (library?.episodes||[]).filter(e=>mode==='all'||(mode==='deleted')===!!library.annotations?.[e.index]?.deleted);}
function filteredEpisodes(){const q=$('episode-search').value.toLowerCase();return scopedEpisodes().filter(e=>`${e.index+1} ${e.tasks.join(' ')}`.toLowerCase().includes(q));}
function resetEpisodeSelection(root){if(root!==episodeSelectionRoot){episodeSelection.clear();episodeSelectionRoot=root;if($('episode-visibility'))$('episode-visibility').value='active';}if(motionPreview?.dataset_root!==root){motionPreview=null;motionPreviewJob=null;}}
function invalidateMotionPreview(root){if(motionPreview?.dataset_root===root){motionPreview=null;motionPreviewJob=null;renderMotionPreview();}}
function episodeChoiceRow(button,episode){
  const row=document.createElement('div');row.className='episode-choice-row';
  const box=document.createElement('input');box.type='checkbox';box.checked=episodeSelection.has(episode.index);box.dataset.batchEpisode=episode.index;
  box.setAttribute('aria-label','勾选第 '+(episode.index+1)+' 条');
  box.addEventListener('change',()=>{box.checked?episodeSelection.add(episode.index):episodeSelection.delete(episode.index);updateDatasetBatchControls();});
  row.append(box,button);return row;
}
function updateDatasetBatchControls(){
  if(!$('batch-selection-count'))return;
  const matched=!!library&&episodeSelectionRoot===library.root&&$('dataset-select').value===library.root;
  if(matched){const allowed=new Set(scopedEpisodes().map(e=>e.index));for(const id of episodeSelection)if(!allowed.has(id))episodeSelection.delete(id);}
  const total=matched?scopedEpisodes().length:0,blocked=!matched||batchReviewBusy||!!(state?.active&&state.job?.kind==='crop'),resources=!!state?.resources_active||requestBusy;
  $('batch-selection-count').textContent=`勾选 ${episodeSelection.size} / ${total} 条`;
  const visible=matched?filteredEpisodes():[],visibleSelected=visible.filter(e=>episodeSelection.has(e.index)).length;
  const master=$('episode-select-all');
  master.checked=visible.length>0&&visibleSelected===visible.length;
  master.indeterminate=visibleSelected>0&&visibleSelected<visible.length;
  master.disabled=blocked||!visible.length;
  $('episode-select-all-label').textContent=$('episode-search').value.trim()?'全选筛选结果':'全选';
  $('episode-selected-count').textContent='已选 '+episodeSelection.size+' / '+total+' 条';
  $('episode-clear-selection').disabled=blocked||!episodeSelection.size;
  const scope=$('episode-visibility').value;
  $('batch-select-all').textContent=scope==='deleted'?'全选已删除轨迹':scope==='active'?'全选正常轨迹':'全选本数据集';
  const archiveBlocked=blocked||requestBusy||!!(state?.active&&!(state.job?.kind==='record'&&state.job?.phase==='PAUSED'));
  const selectedRows=matched?[...episodeSelection].map(i=>library.annotations?.[i]||{}):[];
  $('batch-delete-episodes').disabled=archiveBlocked||!selectedRows.some(row=>!row.deleted);
  $('batch-restore-episodes').disabled=archiveBlocked||!selectedRows.some(row=>row.deleted);
  $('episode-trash-count').textContent='已删除 '+(library?.episodes||[]).filter(e=>library.annotations?.[e.index]?.deleted).length+' 条（可恢复）';
  $('batch-select-all').disabled=!total||blocked;$('batch-select-filtered').disabled=!total||blocked;$('batch-clear').disabled=!episodeSelection.size||blocked;
  document.querySelectorAll('[data-batch-result],[data-batch-episode]').forEach(el=>el.disabled=blocked||(!el.hasAttribute('data-batch-episode')&&(!episodeSelection.size||selectedRows.some(row=>row.deleted))));
  $('batch-auto-trim').disabled=!matched||!total||resources||batchReviewBusy||!!library?.crop_collection||!!datasetItems.find(d=>d.root===library?.root)?.is_feedback;
  $('motion-analyze').disabled=!matched||resources||batchReviewBusy||!library||!!library.crop_collection;
  document.querySelectorAll('.motion-settings input,.motion-settings select').forEach(el=>el.disabled=resources||batchReviewBusy);
  document.querySelectorAll('[data-result],#save-note').forEach(el=>el.disabled=batchReviewBusy||!!(state?.active&&state.job?.kind==='crop'));
  $('motion-apply').disabled=resources||batchReviewBusy||!motionPreviewJob||motionPreview?.dataset_root!==library?.root||!document.querySelector('#motion-preview-rows input:checked');
  if($('motion-stop')){
    $('motion-stop').disabled=!(state?.active&&state.job?.kind==='crop')&&!requestBusy;
    $('motion-job-status').textContent=state?.active&&state.job?.kind==='crop'?(state.job.message||'正在处理')+(state.job.phase==='MOTION_ANALYZING'?` · ${state.telemetry?.analysis_done||0}/${state.telemetry?.analysis_total||'—'} 条`:state.job.batch_count?` · ${state.job.batch_index}/${state.job.batch_count} 条`:''):'';
  }
  if($('motion-trim-dialog').open){
    const success=(library?.episodes||[]).filter(e=>library.annotations?.[e.index]?.result==='success').length;
    $('motion-scope-hint').textContent=`本数据集成功 ${success} 条；当前勾选中成功 ${(library?.episodes||[]).filter(e=>episodeSelection.has(e.index)&&library.annotations?.[e.index]?.result==='success').length} 条。其他状态不参与裁剪。`;
  }
}
async function batchArchiveEpisodes(action){
  const root=library?.root,episodes=[...episodeSelection].sort((a,b)=>a-b);
  if(!root||root!==episodeSelectionRoot||root!==$('dataset-select').value||!episodes.length||batchReviewBusy)throw Error('请等待数据集加载完成，再勾选要处理的轨迹');
  batchReviewBusy=true;updateDatasetBatchControls();
  try{
    const response=await api('/api/episodes/archive',{root,episodes,action,crop_revision:library.crop_revision||null});
    if(library?.root===root){
      if(action==='delete'&&episodes.includes(selectedEpisode?.index))pausePlayback();
      library.annotations=response.annotations;invalidateMotionPreview(root);
      if(episodeSelectionRoot===root)episodeSelection.clear();
      renderAnnotation();renderEpisodeList();if(selectedEpisode)await syncCropNaming();
    }
    toast(action==='delete'?'已批量删除 '+response.changed+' 条，可在“已删除”视图恢复；原文件和备注保留。':'已批量恢复 '+response.changed+' 条，并还原删除前的审核状态和备注。');
  }finally{batchReviewBusy=false;updateDatasetBatchControls();}
}
async function batchMark(result){
  const root=library?.root,episodes=[...episodeSelection].sort((a,b)=>a-b);
  if(!root||root!==episodeSelectionRoot||root!==$('dataset-select').value||!episodes.length||batchReviewBusy)throw Error('请等待数据集加载完成，再勾选要标记的轨迹');
  batchReviewBusy=true;updateDatasetBatchControls();
  try{
    const response=await api('/api/annotations/batch',{root,episodes,result,crop_revision:library.crop_revision||null});
    if(library?.root===root){
      library.annotations=response.annotations;invalidateMotionPreview(root);
      renderAnnotation();renderEpisodeList();if(selectedEpisode)await syncCropNaming();
    }
    toast(`已将 ${response.changed} 条标为${{success:'成功',failure:'失败',exclude:'不用于训练',unreviewed:'待审核'}[result]}；保留各条原备注，原始轨迹不变。`);
  }finally{batchReviewBusy=false;updateDatasetBatchControls();}
}
function motionSettings(){
  const result={};
  for(const key of ['arm_delta','gripper_delta','window_s','min_motion_s','pad_before_s','pad_after_s']){
    const text=$('motion-'+key).value.trim();
    if(!text||!Number.isFinite(Number(text)))throw Error('请填写有效的阈值、持续时间和留白，不将空值当作零。');
    result[key]=Number(text);
  }
  result.trim_tail=$('motion-trim-tail').checked;result.signal=$('motion-signal').value;return result;
}
function renderMotionPreview(){
  const list=$('motion-preview-rows');list.replaceChildren();
  const rows=motionPreview?.rows||[];
  $('motion-preview-summary').textContent=motionPreview?`分析完成：可用 ${rows.filter(r=>r.status==='ready').length} 条，跳过 ${rows.filter(r=>r.status!=='ready').length} 条。勾选后批量保存；没有可裁静止段的成功轨迹会整条保留。`:'尚未分析。只读分析不会自动裁剪或改标记。';
  for(const item of rows){
    const row=document.createElement('tr'),cell=document.createElement('td'),box=document.createElement('input');
    box.type='checkbox';box.dataset.motionEpisode=item.episode;box.checked=item.status==='ready';box.disabled=item.status!=='ready';box.setAttribute('aria-label','自动裁剪第 '+(item.episode+1)+' 条');
    box.addEventListener('change',updateDatasetBatchControls);cell.append(box);row.append(cell);
    const labels=[String(item.episode+1),item.duration===undefined?'—':item.duration.toFixed(2)+' s',
      item.status==='ready'?item.start.toFixed(2)+'–'+item.end.toFixed(2)+' s':'跳过',
      item.status==='ready'?item.start.toFixed(2)+' / '+(item.duration-item.end).toFixed(2)+' s':'—',item.reason];
    for(const text of labels){const td=document.createElement('td');td.textContent=text;row.append(td);}
    const action=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='button subtle';button.textContent='查看建议';button.disabled=item.status!=='ready';
    button.addEventListener('click',()=>handle((async()=>{
      if(library?.root!==motionPreview?.dataset_root)throw Error('当前数据集已变化，请重新分析');
      $('motion-trim-dialog').close();await selectEpisode(item.episode);
      $('crop-start').value=item.start.toFixed(6);$('crop-end').value=item.end.toFixed(6);cropSelectionChanged();seek(item.start);
      $('trajectory-chart').scrollIntoView({block:'center',behavior:'smooth'});
    })()));action.append(button);row.append(action);list.append(row);
  }
  updateDatasetBatchControls();
}
async function observeMotionJob(previous,next){
  if(motionAnalysisRequested&&next.job?.finished&&['error','operator_stopped'].includes(next.job.result?.outcome)){motionAnalysisRequested=false;if(next.job.result.outcome==='operator_stopped')toast('运动分析已取消，原始数据和旧裁剪集未修改。');return;}
  if(!motionAnalysisRequested||motionPreviewLoading||!next.job?.finished||next.job.result?.outcome!=='motion_preview')return;
  motionPreviewLoading=true;
  try{
    const id=next.job.id,plan=await api('/api/motion-trim-plan?job_id='+encodeURIComponent(id));
    motionAnalysisRequested=false;
    if(library?.root!==plan.dataset_root){toast('分析已完成；当前数据集已切换，请回原数据集重新分析。');return;}
    motionPreview=plan;motionPreviewJob=id;renderMotionPreview();
    if(!$('motion-trim-dialog').open)$('motion-trim-dialog').showModal();
  }finally{motionPreviewLoading=false;}
}
function installDatasetBatch(){
  const visibility=document.createElement('label');visibility.className='episode-visibility';
  visibility.innerHTML='轨迹视图<select id="episode-visibility" aria-label="轨迹视图"><option value="active">正常轨迹</option><option value="deleted">已删除（可恢复）</option><option value="all">全部轨迹</option></select><span id="episode-trash-count" class="muted">已删除 0 条（可恢复）</span>';
  $('episode-search').before(visibility);
  $('episode-visibility').addEventListener('change',()=>{episodeSelection.clear();renderEpisodeList();});
  const selection=document.createElement('div');selection.id='episode-selection-toolbar';selection.className='episode-selection-toolbar';
  selection.innerHTML='<label><input id="episode-select-all" type="checkbox" aria-label="全选当前轨迹列表"><span id="episode-select-all-label">全选</span></label><span id="episode-selected-count" class="muted" aria-live="polite">已选 0 / 0 条</span><button id="episode-clear-selection" class="button" type="button" disabled>清空</button>';
  $('episode-list').before(selection);
  $('episode-select-all').addEventListener('change',()=>{
    for(const ep of filteredEpisodes())$('episode-select-all').checked?episodeSelection.add(ep.index):episodeSelection.delete(ep.index);
    renderEpisodeList();
  });
  $('episode-clear-selection').addEventListener('click',()=>{episodeSelection.clear();renderEpisodeList();});
  const panel=$('dataset-batch-tools');panel.className='panel dataset-batch-tools';
  panel.innerHTML='<div class="batch-tool-row"><h2>批量处理</h2><span id="batch-selection-count" class="muted">勾选 0 / 0 条</span><button id="batch-select-all" type="button" class="button subtle">勾选全部</button><button id="batch-select-filtered" type="button" class="button subtle">勾选筛选结果</button><button id="batch-clear" type="button" class="button subtle">清空</button></div><div class="batch-tool-row"><span class="field-help">对勾选轨迹标记：</span><button type="button" class="button" data-batch-result="success">成功</button><button type="button" class="button" data-batch-result="failure">失败</button><button type="button" class="button danger-text" data-batch-result="exclude">不用于训练</button><button type="button" class="button subtle" data-batch-result="unreviewed">待审核</button><button id="batch-auto-trim" type="button" class="button primary">自动裁剪成功轨迹</button></div>';
  const archiveRow=document.createElement('div');archiveRow.className='batch-tool-row';
  archiveRow.innerHTML='<button id="batch-delete-episodes" class="button danger-text" type="button" disabled>删除所选轨迹（可恢复）</button><button id="batch-restore-episodes" class="button" type="button" disabled>恢复所选轨迹</button><span class="field-help">整批一次处理，不逐条确认；原始版与裁剪版共享删除状态。原文件保留，不释放磁盘空间；训练导出须筛选未删除轨迹，直接读原目录仍包含全部数据。</span>';
  panel.append(archiveRow);
  $('batch-delete-episodes').addEventListener('click',()=>handle(batchArchiveEpisodes('delete')));
  $('batch-restore-episodes').addEventListener('click',()=>handle(batchArchiveEpisodes('restore')));
  const dialog=document.createElement('dialog');dialog.id='motion-trim-dialog';dialog.className='dataset-dialog';dialog.setAttribute('aria-labelledby','motion-trim-title');
  dialog.innerHTML='<header class="dataset-dialog-header"><div><h2 id="motion-trim-title">按六关节运动自动裁剪</h2><p>仅处理标记成功的原始轨迹。去掉头尾静止段，保留中间停顿；不能判断任务完成，也不能区分误录的复位动作。极慢或短暂动作可能漏检，建议抽查边界。其他来源须确认位置单位为度 / 夹爪百分比。</p></div><button id="motion-close" type="button" class="button">关闭</button></header><div class="motion-settings"><label>范围<select id="motion-scope"><option value="all">本数据集所有成功轨迹</option><option value="selected">当前勾选中的成功轨迹</option></select></label><label>运动依据<select id="motion-signal"><option value="state">从臂实测位置</option><option value="state_or_action">实测或示教目标（更保守）</option></select></label><label>前五关节变化阈值 / °<input id="motion-arm_delta" type="number" min="0.01" max="30" step="0.1" value="0.5"></label><label>夹爪变化阈值 / 百分点<input id="motion-gripper_delta" type="number" min="0.01" max="50" step="0.1" value="1"></label><label>位置比较窗口 / 秒<input id="motion-window_s" type="number" min="0.05" max="2" step="0.05" value="0.2"></label><label>运动至少持续 / 秒<input id="motion-min_motion_s" type="number" min="0.05" max="2" step="0.05" value="0.15"></label><label>起点前留白 / 秒<input id="motion-pad_before_s" type="number" min="0" max="10" step="0.1" value="0.3"></label><label>终点后留白 / 秒<input id="motion-pad_after_s" type="number" min="0" max="10" step="0.1" value="0.5"></label><label class="motion-tail-option"><input id="motion-trim-tail" type="checkbox" checked>同时裁去结尾静止段（取消则只裁开头）</label></div><p id="motion-scope-hint" class="field-help"></p><div class="button-row"><button id="motion-analyze" type="button" class="button">分析建议区间（不写数据）</button><button id="motion-apply" type="button" class="button primary" disabled>批量保存勾选的建议</button></div><p id="motion-preview-summary" class="field-help"></p><div class="motion-preview-table"><table><thead><tr><th>选中</th><th>原始条目</th><th>原时长</th><th>建议保留</th><th>去掉头 / 尾</th><th>说明</th><th></th></tr></thead><tbody id="motion-preview-rows"></tbody></table></div><p class="field-help">每条原始轨迹只保留一个裁剪版；重剪替换已有裁剪，旧版保留可恢复备份。原始数据、六维标签和传感器数值不平滑、不改写。Esc 可取消后台分析/裁剪；未提交的整批不会覆盖旧结果。</p>';
  document.body.append(dialog);
  const stop=document.createElement('button');stop.id='motion-stop';stop.type='button';stop.className='button danger-text';stop.textContent='取消当前处理 · Esc';$('motion-apply').after(stop);
  const status=document.createElement('p');status.id='motion-job-status';status.className='field-help';status.setAttribute('role','status');$('motion-preview-summary').before(status);
  stop.addEventListener('click',()=>handle(command('stop')));
  for(const [id,mode] of [['batch-select-all','all'],['batch-select-filtered','filtered'],['batch-clear','clear']])$(id).addEventListener('click',()=>{
    if(mode==='clear')episodeSelection.clear();else for(const ep of mode==='all'?scopedEpisodes():filteredEpisodes())episodeSelection.add(ep.index);
    renderEpisodeList();
  });
  panel.querySelectorAll('[data-batch-result]').forEach(button=>button.addEventListener('click',()=>handle(batchMark(button.dataset.batchResult))));
  $('batch-auto-trim').addEventListener('click',()=>{renderMotionPreview();dialog.showModal();updateDatasetBatchControls();});
  $('motion-close').addEventListener('click',()=>dialog.close());
  dialog.querySelectorAll('.motion-settings input,.motion-settings select').forEach(el=>el.addEventListener('input',()=>{
    motionPreview=null;motionPreviewJob=null;motionAnalysisRequested=false;renderMotionPreview();
  }));
  $('motion-analyze').addEventListener('click',()=>handle((async()=>{
    if(!library)throw Error('请先选择原始数据集');
    const episodes=$('motion-scope').value==='selected'?[...episodeSelection].sort((a,b)=>a-b):null;
    motionPreview=null;motionPreviewJob=null;motionAnalysisRequested=true;renderMotionPreview();
    try{await startJob('crop',{mode:'motion_preview',dataset_root:library.root,episodes,settings:motionSettings()});toast('只读分析已启动；完成后显示建议，不会自动裁剪。');}
    catch(error){motionAnalysisRequested=false;throw error;}
  })()));
  $('motion-apply').addEventListener('click',()=>handle((async()=>{
    if(!motionPreviewJob||motionPreview?.dataset_root!==library?.root)throw Error('请先重新分析当前数据集');
    const episodes=Array.from($('motion-preview-rows').querySelectorAll('input:checked'),el=>Number(el.dataset.motionEpisode));
    if(!episodes.length)throw Error('请勾选至少一条可用建议');
    pausePlayback();await startJob('crop',{mode:'motion_apply',dataset_root:library.root,preview_job_id:motionPreviewJob,episodes});
    dialog.close();motionPreviewJob=null;toast('批量裁剪已启动；逐条生成，校验通过后统一提交。');
  })()));
  updateDatasetBatchControls();
}
