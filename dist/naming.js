'use strict';
let datasetNamingOptions=null,datasetNamingReady=null,recordNameForm=null,newNameForm=null,editNameForm=null,cropNameForm=null;
let editNameRoot=null;

class DatasetNameForm {
  constructor(container,prefix,{capture=false,crop=false,record=false,onPath=null}={}){
    this.capture=capture;this.crop=crop;this.record=record;this.onPath=onPath;this.ticket=0;this.result=null;
    this.element=document.createElement('div');this.element.className='dataset-name-form';
    this.element.innerHTML=`<div class="naming-grid"><label data-field="parent">数据集父目录<input data-name="parent" spellcheck="false"></label><label data-field="domain">来源<select data-name="domain"></select></label><label data-field="stage">处理阶段<select data-name="stage"></select></label><label data-field="review">训练审核<select data-name="review"></select></label></div><label data-field="instruction">语言指令<input data-name="instruction" maxlength="300"></label><label data-field="description">场景 / 处理描述<input data-name="description" list="scene-descriptions" maxlength="100" placeholder="例如：娃娃位置变化；裁剪去手"></label><label data-field="note">补充备注（可选）<textarea data-name="note" rows="2" maxlength="200"></textarea></label><p class="naming-fixed-tags field-help"></p><div class="naming-preview"></div><div class="naming-feedback field-help"></div>`;
    container.replaceChildren(this.element);
    for(const [field,options] of [['domain',datasetNamingOptions.domains],['stage',datasetNamingOptions.stages],['review',datasetNamingOptions.reviews]])for(const [key,label] of Object.entries(options))this.get(field).add(new Option(label,key));
    for(const field of capture?['domain','stage','review']:crop?['domain','stage','review']:[])this.element.querySelector('[data-field="'+field+'"]').hidden=true;
    this.element.querySelector('[data-field="parent"]').hidden=!capture||record;
    if(record)for(const field of ['instruction','description','note'])this.element.querySelector('[data-field="'+field+'"]').hidden=true;
    this.element.addEventListener('input',()=>{this.result=null;clearTimeout(this.timer);this.timer=setTimeout(()=>this.update(),200);});
    this.element.addEventListener('change',()=>this.update());this.set(datasetNamingOptions.simple_defaults);
  }
  get(key){return this.element.querySelector('[data-name="'+key+'"]');}
  set(spec){
    this.base={...datasetNamingOptions.simple_defaults,...spec,schema_version:2};
    for(const key of ['domain','stage','review','instruction','description','note'])this.get(key).value=this.base[key];
    this.get('parent').value=state.config.dataset_home;this.result=null;
  }
  spec(){return {...this.base,domain:this.get('domain').value,stage:this.get('stage').value,review:this.get('review').value,
    instruction:this.record?$('record-task').value.trim():this.get('instruction').value.trim(),
    description:this.record?($('record-group').value.trim()||'固定位置'):this.get('description').value.trim(),note:this.get('note').value.trim()};}
  async update(){
    const ticket=++this.ticket,hint=this.element.querySelector('.naming-feedback');
    try{
      const spec=this.spec(),parent=this.record?$('record-parent').value:this.get('parent').value;
      const result=await api(this.capture?'/api/capture-plan':'/api/dataset-name',this.capture?{parent,instruction:spec.instruction,description:spec.description,note:spec.note}:{spec,suggest:this.crop,editing_root:this===editNameForm?editNameRoot:null});
      if(ticket!==this.ticket)return null;this.base=result.spec;this.result=result;
      this.element.querySelector('.naming-preview').textContent=result.name;
      this.element.querySelector('.naming-fixed-tags').textContent=this.capture?'新采集：实机 / 原始 / 待审核 · 时间取系统时钟':this.crop?'处理后副本 / 待审核':('开始时间：'+spec.started_at.replace('T',' '));
      hint.textContent=this.capture?'预览不创建目录。连接采集设备时按系统时间创建子文件夹，同分钟重名自动加序号。':this.crop?'另存新副本，原始轨迹保留。':'只修改显示名称、分类和备注，不移动目录，不改写 episode 的原始语言标签。';
      if(this.onPath)this.onPath(result.root,result);return result;
    }catch(error){if(ticket===this.ticket){this.result=null;hint.textContent=error.message;this.element.querySelector('.naming-preview').textContent='请填写有效路径、语言指令和描述';if(this.onPath)this.onPath('');}return null;}
  }
}
function datasetOptionLabel(item){
  const s=item.naming?.simple_spec;if(!s)return '[未分类] '+item.name+' · '+item.episodes+' 条';
  const review={pending:'待审',train:'可训练',exclude:'排除'}[s.review],stage=s.stage==='raw'?'原始':'处理后';
  return `[${s.domain==='real'?'实机':'仿真'}·${stage}·${review}] ${s.started_at.slice(5,16).replace('T',' ')} · ${s.instruction.slice(0,20)} · ${s.description.slice(0,20)} · ${item.episodes}条`;
}
async function ensureDatasetNaming(){
  if(datasetNamingReady)return datasetNamingReady;
  datasetNamingReady=(async()=>{
    datasetNamingOptions=await api('/api/naming-options');$('record-parent').value=state.config.dataset_home;
    recordNameForm=new DatasetNameForm($('record-name-form'),'record',{capture:true,record:true,onPath:path=>{if($('record-target-mode').value==='new')$('record-root').value=path;}});await recordNameForm.update();
    for(const id of ['record-task','record-group','record-parent'])$(id).addEventListener('input',()=>{clearTimeout(recordNameForm.timer);recordNameForm.timer=setTimeout(()=>recordNameForm.update(),250);});
    $('record-parent-browse').addEventListener('click',()=>handle((async()=>{
      if(!window.pywebview?.api){toast('独立窗口中可选择文件夹，浏览器中可粘贴父目录路径。');return;}
      const path=await window.pywebview.api.choose_folder($('record-parent').value);if(path){$('record-parent').value=path;await recordNameForm.update();}
    })()));
    setInterval(()=>{if(!state?.active&&$('record-target-mode').value==='new')recordNameForm.update();},30000);
    const oldCreate=document.querySelectorAll('.library-create')[0],oldArchive=document.querySelectorAll('.library-create')[1];
    const manager=document.createElement('details');manager.className='panel library-management';
    manager.innerHTML='<summary>数据集管理：新建／名称与备注／恢复归档</summary><div class="library-tabs"><button class="button" data-manager-tab="new">新建采集计划</button><button class="button" data-manager-tab="label">名称、分类与备注</button><button class="button" data-manager-tab="archive">恢复归档</button></div><div id="manager-new"></div><div id="manager-label" class="hidden"><div id="edit-name-form"></div><button id="save-dataset-label" class="button primary">保存名称与分类（不移动文件夹）</button><p class="field-help">可训练是人工审核标签，不自动改变训练条目。</p></div><div id="manager-archive" class="hidden"></div>';
    oldCreate.before(manager);$('manager-new').append(oldCreate);$('manager-archive').append(oldArchive);oldCreate.open=true;oldArchive.open=true;oldCreate.querySelector('summary').hidden=true;oldArchive.querySelector('summary').hidden=true;
    manager.querySelectorAll('[data-manager-tab]').forEach(button=>button.addEventListener('click',()=>{
      for(const name of ['new','label','archive'])$('manager-'+name).classList.toggle('hidden',name!==button.dataset.managerTab);
      manager.querySelectorAll('[data-manager-tab]').forEach(b=>b.classList.toggle('selected',b===button));
    }));
    const newContainer=document.createElement('div');oldCreate.querySelector('.create-row').before(newContainer);$('new-dataset-name').readOnly=true;$('create-dataset').textContent='带入采集工作台';
    newNameForm=new DatasetNameForm(newContainer,'new',{capture:true,onPath:(path,result)=>{$('new-dataset-name').value=result?.name||'';}});await newNameForm.update();
    editNameForm=new DatasetNameForm($('edit-name-form'),'edit');
    $('save-dataset-label').addEventListener('click',()=>handle((async()=>{
      const root=$('dataset-select').value;if(!root||root!==editNameRoot)throw Error('请先选择数据集');
      if(!await editNameForm.update())throw Error('请核对命名信息');const spec=editNameForm.spec();
      if(!await confirmOperation('保存数据集名称与分类','只修改平台显示信息，实际目录、轨迹和语言标签不变。'+(spec.review==='train'?'你确认已人工审核训练条目；成功标签不保证数据质量。':'')))return;
      await api('/api/dataset-management',{action:'label',root,spec,confirmed:true,review_confirmed:spec.review==='train'});
      await refreshDatasets();$('dataset-select').value=root;await openDataset(root);toast('名称与备注已保存，原始目录未移动。');
    })()));
    $('crop-output').readOnly=true;$('crop-browse').textContent='查看裁剪集';
    const item=datasetItems.find(d=>d.root===$('dataset-select').value);if(item)fillDatasetNameEditor(item);
    if(datasetItems.length){for(const option of $('dataset-select').options){const item=datasetItems.find(d=>d.root===option.value);if(item)option.textContent=datasetOptionLabel(item)+(item.protected?' · 保留':'');}refreshRecordTargets();}
    updateNamingControls();return true;
  })();return datasetNamingReady;
}
function updateNamingControls(){if($('save-dataset-label'))$('save-dataset-label').disabled=!!state?.resources_active||!editNameRoot;}
function fillDatasetNameEditor(item){if(!editNameForm)return;editNameRoot=item?.is_feedback?null:item?.root||null;editNameForm.set(item?.is_feedback?datasetNamingOptions.simple_defaults:item?.naming?.simple_spec||datasetNamingOptions.simple_defaults);if(editNameRoot)editNameForm.update();updateNamingControls();}
function refreshRecordTargets(){
  const select=$('record-existing'),previous=select.value;select.replaceChildren(new Option('选择补录的原始数据集',''));
  datasetItems.filter(d=>d.naming?.append_allowed).forEach(d=>select.add(new Option(datasetOptionLabel(d),d.root)));
  if(datasetItems.some(d=>d.root===previous&&d.naming?.append_allowed))select.value=previous;recordTargetChanged();
}
function recordTargetChanged(){
  const existing=$('record-target-mode').value==='existing';$('record-existing').classList.toggle('hidden',!existing);$('record-name-form').classList.toggle('hidden',existing);$('record-parent-label').hidden=existing;
  $('record-browse').hidden=!existing;$('record-root').value=existing?$('record-existing').value:recordNameForm?.result?.root||'';
}
async function captureNaming(){
  await ensureDatasetNaming();
  if($('record-target-mode').value==='existing'){
    const item=datasetItems.find(d=>d.root===$('record-existing').value);if(!item?.naming?.append_allowed)throw Error('请选择原始数据集补录，处理版不混入新原始录像');
    return {root:item.root,spec:item.naming.spec,existing:item.ready,mode:'append',parent:null};
  }
  const planned=await recordNameForm.update();if(!planned)throw Error('请检查父目录、语言指令和场景描述');
  return {root:planned.root,spec:planned.spec,existing:false,mode:'new',parent:planned.parent};
}
async function syncCropNaming(){
  const root=library?.root,episode=selectedEpisode?.index;await ensureDatasetNaming();if(!library||root!==library.root||episode!==selectedEpisode?.index)return;
  cropPlan=null;updateControls();
  const result=await api('/api/crop-plan?root='+encodeURIComponent(root)+'&episode='+episode);
  if(!library||root!==library.root||episode!==selectedEpisode?.index)return;
  cropPlan=result;$('crop-output').value=result.destination||library.root;
  if(result.previous){$('crop-start').value=result.previous.requested_seconds[0];$('crop-end').value=result.previous.requested_seconds[1];}
  $('open-crop-source').classList.toggle('hidden',!library.crop_collection);
  updateCropHint();updateControls();
}
function renderDatasetNaming(item){
  let tags=$('dataset-tags');if(!tags){tags=document.createElement('div');tags.id='dataset-tags';tags.className='dataset-tags';$('dataset-summary').after(tags);}
  let detail=$('dataset-name-detail');if(!detail){detail=document.createElement('div');detail.id='dataset-name-detail';detail.className='dataset-name-detail';tags.after(detail);}
  tags.replaceChildren();const s=item?.naming?.simple_spec;
  if(s)for(const [field,labels] of [['domain',datasetNamingOptions?.domains],['stage',datasetNamingOptions?.stages],['review',datasetNamingOptions?.reviews]]){
    const tag=document.createElement('span');tag.className='dataset-tag tag-'+field+'-'+s[field];tag.textContent=labels?.[s[field]]||s[field];tags.append(tag);
  }
  detail.replaceChildren();for(const text of [s?.description,item?.naming?.note,item?'实际目录：'+item.root:'',item?.naming?.canonical_name?'显示名：'+item.naming.canonical_name:'']){if(!text)continue;const p=document.createElement('p');p.textContent=text;detail.append(p);}fillDatasetNameEditor(item);
}
