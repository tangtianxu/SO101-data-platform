'use strict';
function installDatasetBrowser(){
  const picker=document.createElement('dialog');picker.id='dataset-browser-dialog';picker.className='dataset-dialog';picker.setAttribute('aria-labelledby','dataset-browser-title');
  picker.innerHTML='<header class="dataset-dialog-header"><div><h2 id="dataset-browser-title">选择数据集</h2><p>按采集时间倒序排列，完整查看任务、场景和文件位置。</p></div><button type="button" class="button" data-close-dataset-dialog>关闭</button></header><div id="dataset-browser-filters"></div><div id="dataset-browser-list" class="dataset-browser-list"></div>';
  const info=document.createElement('dialog');info.id='dataset-info-dialog';info.className='dataset-dialog';info.setAttribute('aria-labelledby','dataset-info-title');
  info.innerHTML='<header class="dataset-dialog-header"><div><h2 id="dataset-info-title">修改名称与资料</h2><p>修改平台显示信息；稳定 ID、实际目录、轨迹和原始任务标签不变。</p></div><button type="button" class="button" data-close-dataset-dialog>关闭</button></header><div id="dataset-info-identity" class="dataset-info-identity"></div><div id="dataset-info-body"></div>';
  document.body.append(picker,info);
  for(const dialog of [picker,info])dialog.querySelector('[data-close-dataset-dialog]').addEventListener('click',()=>dialog.close());
  $('dataset-picker-open').addEventListener('click',()=>{renderDatasetBrowser();picker.showModal();$('dataset-search').focus();});
  $('dataset-edit-info').addEventListener('click',()=>handle(openDatasetInformation('label')));
  $('dataset-open-archive').addEventListener('click',()=>handle(openDatasetInformation('archive')));
}
function renderDatasetBrowser(){
  const list=$('dataset-browser-list');if(!list)return;
  list.replaceChildren();const items=visibleDatasets(),selected=$('dataset-select').value;
  if(!items.length){const empty=document.createElement('p');empty.className='dataset-browser-empty';empty.textContent=$('dataset-search')?.value.trim()?'没有匹配的数据集，请调整搜索或分类。':'此分类暂无数据集。';list.append(empty);return;}
  for(const item of items){
    const spec=item.naming?.simple_spec,button=document.createElement('button');button.type='button';button.className='dataset-choice';button.dataset.datasetRoot=item.root;button.setAttribute('aria-pressed',String(item.root===selected));
    const title=document.createElement('strong');title.className='dataset-choice-title';title.textContent=spec?[spec.instruction,spec.description].filter(Boolean).join(' · '):item.name;
    const meta=document.createElement('span');meta.className='dataset-choice-meta';
    meta.textContent=spec?[(spec.domain==='real'?'实机':'仿真'),spec.stage==='raw'?'原始':'处理后',{pending:'待审核',train:'可训练',exclude:'排除'}[spec.review],spec.started_at.replace('T',' '),item.episodes+' 条',item.protected?'受保护':''].filter(Boolean).join(' · '):'未分类 · '+item.episodes+' 条';
    button.append(title,meta);
    if(item.capture_group){const group=document.createElement('span');group.className='dataset-choice-group';group.textContent='分组采集：'+item.capture_group.collection_name+' / '+item.capture_group.id;button.append(group);}
    if(spec?.note){const note=document.createElement('span');note.className='dataset-choice-note';note.textContent=spec.note;button.append(note);}
    const path=document.createElement('span');path.className='dataset-choice-path';path.textContent=item.root;button.append(path);
    button.addEventListener('click',()=>{$('dataset-browser-dialog').close();handle(openDataset(item.root));});list.append(button);
  }
}
function renderDatasetIdentity(item){
  const spec=item?.naming?.simple_spec;
  $('dataset-current-title').textContent=item?(spec?[spec.instruction,spec.description].filter(Boolean).join(' · '):item.name):'尚未选择数据集';
  $('dataset-current-subtitle').textContent=item?[(spec?.started_at||item.started_at||'未记录采集时间').replace('T',' '),item.episodes+' 条',item.frames!==undefined?item.frames.toLocaleString()+' 帧':''].filter(Boolean).join(' · '):'点击“选择 / 切换”，在大窗口中查看完整名称和资料。';
  $('dataset-edit-info').disabled=!!state?.resources_active||!item||!!item.is_feedback;
  $('dataset-edit-info').title=state?.resources_active?'请先结束设备或导出会话，再修改资料。':item?.is_feedback?'ACT 回流在历史页面标记，不能直接改成专家示范。':'修改显示名称、来源、阶段、审核、场景和备注；不改原始轨迹。';
  renderDatasetBrowser();
}
function datasetInformationIdentity(tab){
  const item=datasetItems.find(d=>d.root===$('dataset-select').value);
  $('dataset-info-identity').textContent=tab==='label'?(item?'稳定 ID（只读）：'+(item.naming?.dataset_id||'保存资料时登记')+'\n实际目录（保留）：'+item.root:'请先选择数据集，再修改资料。'):'归档只移动明确选中的数据集，可恢复；不会永久删除。';
}
async function openDatasetInformation(tab='label'){
  await ensureDatasetNaming();
  if(tab==='label'){
    const item=datasetItems.find(d=>d.root===$('dataset-select').value);
    if(!item||item.is_feedback)throw Error('请先选择可编辑的数据集');
    if(state?.resources_active)throw Error('先结束当前会话，再修改数据集资料');
    fillDatasetNameEditor(item);
  }
  document.querySelector('[data-manager-tab="'+tab+'"]').click();
  $('dataset-info-dialog').showModal();
  if(tab==='label')editNameForm.get('description').focus();
}
